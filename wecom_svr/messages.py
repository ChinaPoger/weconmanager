"""本地群消息读取 —— 用已存的 key 增量重解密 Info.db,按会话取新消息。

沿用 wecom-agent `decrypt/macos/monitor.py` 的路子(不重扫内存、不重签,纯读
磁盘 + 已知 key),但做了四处适配:

  - **回放 WAL**(见 wal.py): 上游只读已 checkpoint 的 `Info.db`,实测能滞后
    48 分钟以上,拿它做同步谈不上实时。这里把 `Info.db-wal` 里未落盘的页也
    盖上,最新消息因此只落后几十秒。
  - **原子落位**: 464MB 的 Info.db 写一次要 ~0.8s,直接覆写会让同时在读的
    directory/local_send 拿到半截库。这里先写临时文件再 os.replace。
  - **游标是 (send_time, LID)**: 上游用 `send_time > 水位`,同一秒里的多条消息
    会被吞掉。群聊里同秒多条太常见,必须带 LID 做次级序。
  - **只取群会话**: conversationtype=1,且支持白名单 —— 告警群动辄 20 万条,
    不能无差别往下游推。

消息正文/类型/发送者真名的解析直接复用上游 export_wxwork,不重写一遍:
上游改了协议解析,这里跟着受益。
"""
import functools
import importlib
import logging
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import ModuleType

from . import media, wal
from .localdb import NOT_SYNCED, LocalDataError
from .settings import get_agent_home

log = logging.getLogger(__name__)

# CONVERSATION.conversationtype —— 1 是群聊(0 单聊,3/6/7/10x 是各类应用会话)
GROUP_CONV_TYPE = 1

# 这些类别不是「人说的话」,推给下游只会污染会话统计。
# 「会议」在实测里全是「进入了 XX 的快速会议」这种加入/退出事件 —— 某个团队群
# 7 天里 52 条会议事件对 56 条真实发言,不滤掉就是一半噪音。
SKIP_TYPE_LABELS = frozenset({"系统", "系统通知", "事件", "协作事件", "会议"})

# 上游 export_wxwork.TYPE_LABEL 在这两个码上标反了,实测证据(本机全库抽样):
#   14: 上游标「语音」,但抽 4000 条内容里是 3443 png + 271 jpg + 13 heic,
#       零个语音编码特征 → 其实是**图片**,全库 4932 条都被标错
#   16: 上游标「文件」,但全库仅 11 条且只出现 .silk(微信语音编码) → 其实是**语音**
#       (真正的文件是 15:csv/xlsx/pdf/zip,那个上游没标错)
# 不改 vendor(它是 pin 住的 submodule),在这里覆盖。
TYPE_OVERRIDES = {14: "图片", 16: "语音"}

# 媒体类不抽正文,直接给占位符 —— 覆盖过类型后,占位符也得跟着换。
# IMAGE_PLACEHOLDER 单拎出来是因为下游要拿它做「换成图片 URL」的锚点,
# 两边各写一个字面量就会在改动时无声失配。
IMAGE_PLACEHOLDER = "[图片]"
FILE_PLACEHOLDER = "[文件]"
MEDIA_PLACEHOLDER = {"图片": IMAGE_PLACEHOLDER, "语音": "[语音]", "文件": FILE_PLACEHOLDER}

# 哪些 content_type 要去缓存里找原图。和 TYPE_OVERRIDES 放一起不是随意:
# 14 之所以算图片,正是因为上面那张表把它从「语音」改回了「图片」——
# 这两处必须一起改,分家到 media.py 就等着漏改。
IMAGE_TYPES = frozenset({4, 14, 29})

# type=123 通常是卡片,但企微把「图片 + 随图文字」也装在这个类型里。它的 content
# 顶层 field 1 有多个消息段:一段是图片元数据/文件名,另一段是用户输入的文字。
RICH_IMAGE_TYPES = frozenset({123})

# 图片文件名的扩展名,用来判断被引用或图文消息里的载荷是不是图片
_IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|heic|webp|bmp)\b", re.I)

_KEYS_JSON = "wxwork_keys.json"


class LocalKeyError(LocalDataError):
    """缺少可用的解密 key —— 需要重跑 find_key_fast.py。"""


@dataclass(frozen=True, order=True)
class Cursor:
    """增量水位 —— (发送时间, LID)。LID 单调递增,用作同秒内的次级序。"""

    send_time: int = 0
    lid: int = 0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Cursor":
        d = d or {}
        return cls(int(d.get("send_time") or 0), int(d.get("lid") or 0))

    def to_dict(self) -> dict:
        return {"send_time": self.send_time, "lid": self.lid}


@dataclass
class Batch:
    """一次取数的结果。

    `scan_end` 是各群**扫描到的**最后一行(含被过滤掉的系统消息),不是 messages
    的最后一条 —— 某群整批都被过滤时,它的水位得靠这里才能往前走。
    """

    messages: list[dict] = field(default_factory=list)
    scan_end: dict[int, Cursor] = field(default_factory=dict)
    scan_counts: dict[int, int] = field(default_factory=dict)
    selected_rids: list[int] = field(default_factory=list)
    next_offset: int = 0  # 下轮群配额从哪里开始,避免群数大于批大小时饿死尾部群

    @property
    def scanned(self) -> int:
        """SQL 实际返回的行数(过滤前)。就是各群扫描数之和,不另存一份。"""
        return sum(self.scan_counts.values())


# ─── 上游 decrypt/ 模块的按需加载 ────────────────────────────────────────

@dataclass(frozen=True)
class _Vendor:
    export: ModuleType      # export_wxwork —— 正文/类型/发送者解析
    paths: ModuleType       # wecom_paths   —— 客户端库与解密输出目录
    keys_json: str


@functools.lru_cache(maxsize=1)
def _vendor() -> _Vendor:
    """import wecom-agent 的 decrypt/ 模块(纯函数库,import 安全)。

    与 agent.py 不同,这里没走子进程: 这些模块只是解析函数,不会 sys.exit;
    而每条消息都 fork 一次 Python 的开销不可接受。唯一会 SystemExit 的是
    wecom_paths.profile_dir(),调用处单独兜住。
    """
    decrypt_dir = str(get_agent_home().decrypt_dir)
    macos_dir = os.path.join(decrypt_dir, "macos")
    if not os.path.isdir(macos_dir):
        raise LocalDataError(f"未找到 {macos_dir} —— wecom-agent 布局不含 decrypt/macos")

    for p in (macos_dir, decrypt_dir):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        return _Vendor(
            export=importlib.import_module("export_wxwork"),
            paths=importlib.import_module("wecom_paths"),
            keys_json=os.path.join(macos_dir, _KEYS_JSON),
        )
    except ImportError as e:
        raise LocalDataError(f"加载 wecom-agent 解析模块失败: {e}") from e


@functools.lru_cache(maxsize=1)
def _crypto():
    """wxwork_crypto 单独懒加载 —— 它需要 cryptography/pycryptodome。

    没装也只是「不能自己重解密」,读已解密的库照常。分开加载,是为了让缺依赖
    这件事只挡住 refresh(),不至于连群列表都查不了。
    """
    _vendor()  # 保证 sys.path 已就位
    try:
        return importlib.import_module("wxwork_crypto")
    except ImportError as e:
        raise LocalDataError(
            f"加载解密模块失败({e})。装一下 cryptography 即可自动增量解密;"
            "或先手工跑 decrypt/macos/read_wecom.py"
        ) from e


def _source_paths() -> tuple[str, str]:
    """企业微信客户端正在写的原始加密库(Info.db, Session.db)。"""
    v = _vendor()
    try:
        return v.paths.info_db(), v.paths.session_db()
    except SystemExit as e:  # wecom_paths 探测不到 profile 时直接 SystemExit
        raise LocalDataError(str(e) or "未找到企业微信 profile 目录") from e


def _decrypted_dir() -> str:
    return _vendor().paths.decrypted("Messages1")


def _load_key() -> bytes:
    crypto = _crypto()
    src_info, _ = _source_paths()
    if not os.path.exists(src_info):
        raise LocalDataError(f"企业微信本地库不存在: {src_info}")
    with open(src_info, "rb") as f:
        page1 = f.read(crypto.PAGE_SZ)
    keys = crypto.load_valid_keys(_vendor().keys_json, page1)
    if not keys:
        raise LocalKeyError(
            f"{_vendor().keys_json} 里没有能解开 Info.db 的 key。"
            "请在企业微信运行且已登录时重跑 decrypt/macos/find_key_fast.py"
        )
    return keys[0]


def _decrypt_atomic(src: str, dst: str, key: bytes) -> None:
    """解密到同目录临时文件后 rename —— 读侧永远看到完整的库。"""
    crypto = _crypto()
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst) or ".", prefix=".tmp-", suffix=".db")
    os.close(fd)
    try:
        crypto.decrypt_database(src, tmp, key)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# 干净底稿:只反映已 checkpoint 的主库,WAL 每次盖在它的克隆上而不是它自己
BASE_SUFFIX = ".base"

_SEEN: dict[str, tuple] = {}   # 源库路径 → (主库 mtime, WAL 签名)


def _base_path(live: str) -> str:
    root, ext = os.path.splitext(live)
    return f"{root}{BASE_SUFFIX}{ext}"


def _refresh_db(src: str, live: str, key: bytes, force: bool) -> bool:
    """刷新一个库: 主库变了就重解密底稿,WAL 变了就重新盖一遍。

    两级是因为两者代价差两个数量级 —— 全量解密 464MB 要 ~0.8s(只在 checkpoint
    后才需要),WAL 回放是克隆 + 几十页,~0.1s(每几秒就可能需要)。
    """
    base = _base_path(live)
    wal_path = src + "-wal"

    mtime = os.path.getmtime(src)
    sig = wal.signature(wal_path)
    if not force and _SEEN.get(src) == (mtime, sig) and os.path.exists(live):
        return False

    if force or _SEEN.get(src, (None,))[0] != mtime or not os.path.exists(base):
        _decrypt_atomic(src, base, key)

    crypto = _crypto()
    frames = wal.read(wal_path)
    try:
        wal.overlay(base, live, frames, lambda page, pgno: crypto.decrypt_page(key, page, pgno))
    except (OSError, ValueError) as e:
        # 回放失败就退回纯 checkpoint 视图: 数据会旧一点,但绝不会是坏的
        log.warning("WAL 回放失败,退回已 checkpoint 的视图: %s", e)
        _decrypt_atomic(src, live, key)

    _SEEN[src] = (mtime, sig)
    return True


def refresh(force: bool = False) -> bool:
    """把当前磁盘上的 Info.db/Session.db 重新解密一份(含 WAL 里尚未落盘的消息)。

    主库和 WAL 都没动过就直接跳过 —— 两次 stat 的事。返回是否真的刷新了。
    """
    src_info, src_sess = _source_paths()
    if not os.path.exists(src_info):
        raise LocalDataError(f"企业微信本地库不存在: {src_info}")

    key = _load_key()
    out_dir = _decrypted_dir()
    changed = _refresh_db(src_info, os.path.join(out_dir, "Info.db"), key, force)

    if os.path.exists(src_sess):
        try:
            _refresh_db(src_sess, os.path.join(out_dir, "Session.db"), key, force)
        except Exception as e:  # Session.db 是锦上添花(只影响真名),失败不该挡住消息
            log.warning("刷新 Session.db 失败,发送者只能显示 ID: %s", e)

    return changed


# ─── 读 ─────────────────────────────────────────────────────────────────

def _info_db_path() -> str:
    p = os.path.join(_decrypted_dir(), "Info.db")
    if not os.path.exists(p):
        raise LocalDataError(
            f"未找到解密后的 Info.db({p})。先跑 decrypt/macos/read_wecom.py,"
            "或调用一次同步让本服务自行解密"
        )
    return p


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_info_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    """Info.db 上的只读查询 —— 表不存在时统一翻译成「未同步」(→ 503)。

    和 localdb.query 是同一个契约,但那边连的是 find_db 找到的库、返回 dict 行;
    这里要 sqlite3.Row(取 CAST 出来的 BLOB 列)且连的是自己维护的解密副本。
    """
    conn = _connect()
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise LocalDataError(NOT_SYNCED) from e
        raise
    finally:
        conn.close()


_senders_cache: tuple[tuple[float, int], dict[int, dict]] | None = None


def _load_senders() -> dict[int, dict]:
    """Session.db USER: RID → {name, userid, corpid, avatar}。

    userid 就是 acctid(官方 API 认的 userid);外部联系人和机器人没有 acctid,
    留空由调用方决定怎么兜。corpid 用来区分同名的跨企业成员。

    结果按 (mtime, size) 缓存: 同步循环每轮都要这张表,而通讯录几乎不变 ——
    默认 3s 间隔下不缓存就是每天两万多次全表扫描。
    """
    global _senders_cache

    path = os.path.join(_decrypted_dir(), "Session.db")
    try:
        st = os.stat(path)
    except OSError:
        return {}

    stamp = (st.st_mtime, st.st_size)
    if _senders_cache and _senders_cache[0] == stamp:
        return _senders_cache[1]

    try:
        conn = sqlite3.connect(path)
        try:
            try:
                rows = conn.execute(
                    "SELECT RID, name, acctid, corpid, avatarurl FROM USER"
                ).fetchall()
                has_corpid = True
            except sqlite3.OperationalError as e:
                # 兼容旧版本地库:没有 corpid 时仍可同步消息,但字段留空。
                if "no such column" not in str(e).lower() or "corpid" not in str(e).lower():
                    raise
                rows = conn.execute(
                    "SELECT RID, name, acctid, avatarurl FROM USER"
                ).fetchall()
                has_corpid = False
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("读取 Session.db USER 失败: %s", e)
        return {}

    senders = {}
    for r in rows:
        senders[r[0]] = {
            "name": r[1] or "",
            "userid": r[2] or "",
            "corpid": (r[3] if has_corpid else "") or "",
            "avatar": (r[4] if has_corpid else r[3]) or "",
        }
    _senders_cache = (stamp, senders)
    return senders


def _type_name(content_type: int) -> str:
    """类型名 —— 先查我们的修正表,再落回上游。"""
    return TYPE_OVERRIDES.get(content_type) or _vendor().export.type_name(content_type)


def _rich_image_text(content_type: int, content) -> str:
    """识别 type=123 里的「图片 + 随图文字」,不是这种结构则返回空串。

    普通 type=123 是应用卡片,仍交给上游按卡片解析。图文消息则在顶层 field 1
    放多个子消息:含图片文件名的那段代表图片,其余段是随图文字。必须把整条原始
    content 留给 media 解析 md5,这里只负责产出 `[图片]` 锚点和文字。
    """
    if content_type not in RICH_IMAGE_TYPES or not content:
        return ""
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")

    parts = _pb_fields(content).get(1, [])
    rendered: list[str] = []
    has_image = False
    for part in parts:
        text = (_vendor().export.render(0, part) or "").strip()
        if _IMAGE_EXT.search(text):
            has_image = True
        elif text and text not in rendered:
            rendered.append(text)
    if not has_image:
        return ""
    return "\n".join([IMAGE_PLACEHOLDER, *rendered])


def _short_text(content) -> str:
    """提取企微文本消息里被上游长度门槛漏掉的单字符正文。

    普通文本的 protobuf 路径实测是 ``1 -> 2 -> 1``。上游 `_seg_text`
    会过滤长度小于 2 的叶子, 因此 `1`, `好`, 单个 emoji 最终只渲染成
    `[文本]`。这里只认这条明确的字段路径, 避免递归捞到一字符元数据。
    """
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    if not isinstance(content, bytes):
        return ""

    for envelope in _pb_fields(content).get(1, []):
        for body in _pb_fields(envelope).get(2, []):
            for payload in _pb_fields(body).get(1, []):
                try:
                    text = payload.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if len(text) == 1 and text.isprintable():
                    return text
    return ""


def _render(content_type: int, content, label: str) -> str:
    """正文 —— 被修正过类型的走我们的占位符,其余交给上游。

    不能直接用上游 render: 它按**自己那套(错的)**类型表决定占位符,
    type=14 会渲染成 `[语音]`,而那其实是图片。
    """
    if content_type in TYPE_OVERRIDES and (ph := MEDIA_PLACEHOLDER.get(label)):
        return ph
    if rich := _rich_image_text(content_type, content):
        return rich
    rendered = _vendor().export.render(content_type, content) or ""
    if label == "文本" and (not rendered or rendered == "[文本]"):
        return _short_text(content) or rendered
    return rendered


#: 一条消息里可能挂着图的原始载荷字段。产出在 `fetch_since`,消费在
#: `mars_sync.message_body`(解析成 URL)和 `_ready_count`(判断还等不等得到)。
#: 集中在这里,是因为「哪些字段可能有图」多一个就得同时改这几处,漏一处都是静默的:
#: 漏了解析那侧图就永远出不来,漏了判断那侧消息就永远不等。
MEDIA_FIELDS = ("raw", "quote_raw")


def media_payloads(msg: dict) -> list:
    """这条消息里所有可能藏着图的原始正文。"""
    return [msg.get(f) for f in MEDIA_FIELDS]


def _media_placeholder(items: list) -> str:
    """认出了媒体但上游没给正文时用的占位符。

    **必须按媒体种类选**: 一律给 `[图片]` 的话,只带附件的消息会渲染成
    「[图片] + [文件] xxx.zip」—— 一个根本不存在的图片占位符挂在附件名前面。
    """
    if any(item.kind == "image" for item in items):
        return IMAGE_PLACEHOLDER
    return FILE_PLACEHOLDER if items else ""


def _pb_fields(buf: bytes) -> dict[int, list[bytes]]:
    """扫 protobuf 顶层字段 → {字段号: [长度分隔型的载荷]}。

    只为回答「有没有某个字段」和「取它的载荷」,所以非 length-delimited 的
    字段只跳过不解。畸形数据一律提前返回已解出的部分,不抛。
    """
    out: dict[int, list[bytes]] = {}
    i, n = 0, len(buf)
    while i < n:
        key = shift = 0
        while i < n:
            b = buf[i]
            key |= (b & 0x7F) << shift
            i += 1
            shift += 7
            if not b & 0x80:
                break
            if shift > 63:
                return out
        field, wire = key >> 3, key & 7
        if wire == 2:                       # length-delimited
            ln = shift = 0
            while i < n:
                b = buf[i]
                ln |= (b & 0x7F) << shift
                i += 1
                shift += 7
                if not b & 0x80:
                    break
            out.setdefault(field, []).append(buf[i:i + ln])
            i += ln
        elif wire == 0:                     # varint
            while i < n and buf[i] & 0x80:
                i += 1
            i += 1
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:                               # 组类型已废弃,遇到就停
            return out
    return out


# 引用回复把被引消息塞在 extracontent 的这个字段里。实测:该群 30 条带
# extracontent 的消息中只有真引用那条含 1002,其余的 3044/3045/3065 是通用
# 元数据、1012/1042 是链接预览 —— 不认字段号而只抽可读文本的话,会把每条
# 带 GitHub 链接的日报都误标成「引用」(踩过,27/76 全是误判)。
QUOTE_FIELD = 1002


def _quote_payload(extracontent) -> bytes | None:
    """取引用字段的原始载荷 —— 被引消息的正文,含图片文件名。"""
    if not extracontent:
        return None
    if isinstance(extracontent, str):
        extracontent = extracontent.encode("utf-8", "surrogateescape")
    payloads = _pb_fields(extracontent).get(QUOTE_FIELD)
    return payloads[0] if payloads else None


def _quote_summary(payload: bytes | None) -> str:
    """从引用载荷里提炼被引用消息的一句话摘要,不是引用则返回空串。

    引用回复的被引内容不在 content 里,只推 content 的话「103会议室」这种
    回复到了下游就没头没尾,看不出在回谁。

    收的是 _quote_payload 的结果而不是 extracontent 本身: 调用方本来就要留着
    载荷给引用图用,重新扫一遍 protobuf 是白扫。
    """
    if not payload:
        return ""

    try:
        raw = _vendor().export.render(0, payload) or ""
    except Exception:  # 上游解析器对畸形数据可能抛任意异常,引用摘要不值得让整条消息失败
        return ""

    for line in raw.splitlines():
        line = line.strip()
        # 形如 CAIQ4cfK0wYY386UtYeAgAMg852rqAY= 的是客户端内部句柄,不是人写的
        if not line or re.fullmatch(r"[A-Za-z0-9+/=]{16,}", line):
            continue
        return IMAGE_PLACEHOLDER if _IMAGE_EXT.search(line) else line[:60]
    return ""


def list_groups(
    search: str = "",
    include_unnamed: bool = False,
    limit: int | None = 200,
) -> list[dict]:
    """列出群会话(挑同步白名单用),按最近活跃排序。

    include_unnamed 才会带上 `name = ''` 的群 —— **刚建出来的群名字正是空的**
    (客户端按成员现拼出显示名,库里不存),拉群时靠前后差集认新群要用到。
    limit=None 表示不截断:改名后回库核验要能查到任意一个老群,不能只看前 200。
    """
    sql = "SELECT RID, name, sort_time FROM CONVERSATION WHERE conversationtype = ?"
    params: tuple = (GROUP_CONV_TYPE,)
    if not include_unnamed:
        sql += " AND name IS NOT NULL AND name != ''"
    if search:
        sql += " AND name LIKE ?"
        params += (f"%{search}%",)
    sql += " ORDER BY sort_time DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params += (int(limit),)
    return [
        {"rid": r["RID"], "name": r["name"] or "", "sort_time": r["sort_time"]}
        for r in _rows(sql, params)
    ]


def resolve_groups(selectors: Iterable[str]) -> tuple[list[dict], list[str]]:
    """把「群名 或 RID」的列表解析成群会话,返回 (命中, 没匹配上的原样)。

    白名单写群名更好读,但群名会被改;两种都收,让调用方自己权衡。
    """
    # 同步白名单不能受“挑选界面最多 200 条”的限制。一个长期不活跃的群仍然
    # 必须能按名字/RID 命中。
    groups = list_groups(limit=None)
    by_rid = {str(g["rid"]): g for g in groups}
    by_name = {g["name"]: g for g in groups}

    hit: list[dict] = []
    missing: list[str] = []
    seen: set[int] = set()
    for raw in selectors:
        sel = str(raw).strip()
        if not sel:
            continue
        g = by_rid.get(sel) or by_name.get(sel)
        if not g:
            missing.append(sel)
            continue
        if g["rid"] not in seen:
            seen.add(g["rid"])
            hit.append(g)
    return hit, missing


def latest_cursor(rid: int | None = None) -> Cursor:
    """当前最新一条消息的位置;给了 RID 就只看该群。"""
    sql = "SELECT send_time, LID FROM MESSAGE"
    params: tuple = ()
    if rid is not None:
        sql += " WHERE conv_id = ?"
        params = (int(rid),)
    sql += " ORDER BY send_time DESC, LID DESC LIMIT 1"
    rows = _rows(sql, params)
    if not rows:
        return Cursor()
    return Cursor(int(rows[0]["send_time"] or 0), int(rows[0]["LID"] or 0))


MAX_GROUPS_PER_QUERY = 400  # sqlite 默认 compound SELECT 上限是 500


def _quotas(rids: list[int], limit: int, offset: int) -> tuple[list[tuple[int, int]], int]:
    """给群分本轮额度,并返回下轮轮转起点。

    群数超过 limit 时只能让其中 limit 个群各拿一条;轮转保证其余群在后续轮次
    一定被选中。群数不超过 limit 时每群至少一条,余数也从 offset 开始分。
    """
    if not rids or limit <= 0:
        return [], 0
    n = len(rids)
    start = offset % n
    ordered = rids[start:] + rids[:start]
    group_count = min(n, limit, MAX_GROUPS_PER_QUERY)
    active = ordered[:group_count]
    base, extra = divmod(limit, group_count)
    selected = [(rid, base + (1 if i < extra else 0)) for i, rid in enumerate(active)]
    if group_count < n:
        return selected, (start + group_count) % n
    return selected, (start + extra) % n if extra else start


def fetch_since(
    cursors: dict[int, Cursor],
    limit: int = 500,
    include_skipped: bool = False,
    offset: int = 0,
) -> "Batch":
    """按每群水位取连续前缀,全局仍按 (send_time, LID) 升序返回。

    每个 UNION 分支有自己的 LIMIT,因此深积压群不能吃光整批。LIMIT 仍然在
    系统消息过滤之前生效,所以 `scan_end` 必须按群记录过滤前的扫描终点。
    """
    v = _vendor()
    senders = _load_senders()
    names = {rid: s["name"] for rid, s in senders.items() if s["name"]}

    rid_list = [int(r) for r in cursors]
    selected, next_offset = _quotas(rid_list, int(limit), int(offset))
    if not selected:
        return Batch(next_offset=next_offset)

    # content 必须 cast 成 blob: 列声明是 TEXT,sqlite 会把能过 UTF-8 的行当 str
    # 返回,而正文是 protobuf 混排二进制,再 encode 一次就毁了(图片文件名就丢在
    # 这一步)。extracontent 同理。
    branches: list[str] = []
    params: list = []
    for rid, quota in selected:
        cursor = cursors[rid]
        branches.append(
            "SELECT * FROM ("
            " SELECT LID, conv_id, sender_id, content_type, send_time, content, extracontent"
            " FROM MESSAGE WHERE conv_id = ?"
            " AND (send_time > ? OR (send_time = ? AND LID > ?))"
            " ORDER BY send_time, LID LIMIT ?"
            ")"
        )
        params.extend((rid, cursor.send_time, cursor.send_time, cursor.lid, quota))

    sql = (
        "SELECT m.LID, m.conv_id, m.sender_id, m.content_type, m.send_time,"
        "       CAST(m.content AS BLOB) AS content,"
        "       CAST(m.extracontent AS BLOB) AS extracontent, c.name AS conv_name"
        f" FROM ({' UNION ALL '.join(branches)}) m"
        " JOIN CONVERSATION c ON c.RID = m.conv_id AND c.conversationtype = ?"
        " ORDER BY m.send_time, m.LID"
    )
    params.append(GROUP_CONV_TYPE)

    rows = _rows(sql, params)

    out: list[dict] = []
    scan_end: dict[int, Cursor] = {}
    scan_counts: dict[int, int] = {}
    for r in rows:
        rid = int(r["conv_id"])
        scan_end[rid] = Cursor(int(r["send_time"] or 0), int(r["LID"]))
        scan_counts[rid] = scan_counts.get(rid, 0) + 1
        ctype = r["content_type"]
        rich_image = _rich_image_text(ctype, r["content"])
        label = "图片" if rich_image else _type_name(ctype)
        if label in SKIP_TYPE_LABELS and not include_skipped:
            continue                       # 系统消息占比不小,别为它走一遍媒体树
        text = (rich_image or _render(ctype, r["content"], label)).strip()

        # 引用回复:被引内容不在 content 列里,得单独解出来 —— 不带上的话
        # 「103会议室」这种回复到了下游没头没尾,看不出在回谁
        quote_payload = _quote_payload(r["extracontent"])
        quote_text = _quote_summary(quote_payload)

        # 媒体可能藏在任意嵌套层,不能再由旧类型表/扩展名摘要决定是否把载荷交给下游。
        # 两段仍保持独立,由 media.collect 各自抽取并去重。
        raw_media = media.collect(r["content"])
        quote_media = media.collect(quote_payload)
        text = text or _media_placeholder(raw_media)
        quote_text = quote_text or _media_placeholder(quote_media)

        skipped = label in SKIP_TYPE_LABELS or not (text or quote_text)
        if skipped and not include_skipped:
            continue

        sender = senders.get(r["sender_id"], {})
        out.append({
            "lid": r["LID"],
            "rid": r["conv_id"],
            "group_name": r["conv_name"] or "",
            "send_time": int(r["send_time"] or 0),
            "time": v.export.fmt_time(r["send_time"] or 0),
            "sender_id": r["sender_id"],
            "sender_name": sender.get("name") or v.export.resolve_sender(r["sender_id"], names),
            "sender_userid": sender.get("userid", ""),
            "sender_corpid": sender.get("corpid", ""),
            "avatar": sender.get("avatar", ""),
            "type": label,
            # 正文和引用分开给,**不在这里拼成一段字符串**: 拼了的话下游要把
            # `[图片]` 换成 URL 就得按行拆回来(靠匹配引用前缀猜哪行是引用),
            # 改个前缀两边就无声失配。谁要成段谁自己拼,见 mars_sync.message_body。
            "text": text,
            "quote_text": quote_text,
            "skipped": skipped,
            # 类型表不是媒体存在性的可靠判据:合并转发/引用里的票据会藏在任意深度。
            "raw": r["content"] if ctype in IMAGE_TYPES or rich_image or raw_media else None,
            "quote_raw": quote_payload if quote_media else None,
        })
    return Batch(
        messages=out,
        scan_end=scan_end,
        scan_counts=scan_counts,
        selected_rids=[rid for rid, _quota in selected],
        next_offset=next_offset,
    )


def count_since(cursors: dict[int, Cursor]) -> dict[int, int]:
    """按群估算当前积压条数(过滤前),供状态页使用。"""
    if not cursors:
        return {}
    clauses: list[str] = []
    params: list = []
    for rid, cursor in cursors.items():
        clauses.append("(conv_id = ? AND (send_time > ? OR (send_time = ? AND LID > ?)))")
        params.extend((int(rid), cursor.send_time, cursor.send_time, cursor.lid))
    rows = _rows(
        f"SELECT conv_id, COUNT(*) AS n FROM MESSAGE WHERE {' OR '.join(clauses)} GROUP BY conv_id",
        params,
    )
    return {int(r["conv_id"]): int(r["n"]) for r in rows}
