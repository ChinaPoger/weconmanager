"""把本地群消息同步到 mars 的群消息接口。

下游是 mars 的 `PUT {base}/api/qq/hook/v2/message` —— QQ 机器人原本用的那个
hook,models.MessageHook 的字段名带着 QQ 时代的痕迹(groupnumber/sendernumber),
但 mars 早就认 `clientType: "Wework"`,所以企微消息走同一条路即可,不用改 mars。

几个决定了实现形状的下游事实(读 mars 源码得到,不是猜的):

  - **只有行级去重,不是幂等**: mars 落库前算 checksum = md5(群号+发送人+发送时间+
    正文),表上是唯一索引 + FirstOrCreate,所以重推同一条不会多出一行。
    ⚠️ 但**整个 hook 不幂等**,别据此认为「多推一点没关系」:
      · `Assign(*msg)` 命中也写,而 timeline 是以「该群当前最新消息」为参照重算的 ——
        重推一条老消息会覆盖掉它原来的 timeline(models/qq.go:273、415-417)
      · 告警/Slack/销售工单等副作用在 Upsert 之外无条件触发,重推会再放一遍
        (qq/qq.go:142-175)
      · checksum 不含 LID,同秒同人同正文的两条真实消息会被折叠成一条
    详见 docs/per-group-watermark.md §1.3。
  - **群内顺序有意义**: mars 的 SetTimeline 只在同一群找参照物。每群严格按
    (send_time, LID) 升序;跨群可以独立推进,HTTP 第一阶段仍整体串行。
  - **会顺手建群**: hook 里的 updateGroupInfo 会 upsert qq_groups。第一次推
    某个群就等于在 mars 建了这个群的档案,群名取自本地会话名。
  - **hook 有非幂等副作用**: NightAlert 会跳过 Wework,BigCustomerAlert 只认写死的
    `R:` 群号,销售工单只认显式配置的群号;但 SyncToSlack 会处理所有 Wework 消息,
    robotLastActiveTime 也无条件执行。迁移/回滚不能把重推当成免费操作。

「定期」和「实时」是同一套代码: 后台循环 + mtime 闸门,间隔调到秒级就是准实时。
真正的下限来自企微客户端把消息刷进 SQLite 的延迟,不在这一侧。
"""
import asyncio
import json
import logging
import os
import shutil
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field

import httpx

from . import media, messages
from .bgloop import BackgroundLoop
from .messages import IMAGE_PLACEHOLDER, Cursor, media_payloads
from .settings import Settings, get_settings

log = logging.getLogger(__name__)

HOOK_PATH = "/api/qq/hook/v2/message"


class MarsSyncError(RuntimeError):
    """同步配置或调用失败(可预期,回 400/502)。"""


class MarsNotConfigured(MarsSyncError):
    pass


class MarsStateError(MarsSyncError):
    """水位文件不可读/不可写;继续同步会漏消息或重放副作用。"""


# ─── 水位 ───────────────────────────────────────────────────────────────

MAX_ERROR_LENGTH = 500


def _error_text(value: object) -> str:
    return str(value or "")[:MAX_ERROR_LENGTH]


def _summarize_errors(errors) -> str:
    """多个群的报错收成一句。只有一个群挂时给出原文,那才是能直接排查的信息。

    这个字符串是 `SyncResult.error` 和 `State.last_error` 的共同格式(见
    docs/per-group-watermark.md §6),两处必须一致,所以只写一遍。
    """
    seen = [e for e in errors if e]
    if len(seen) == 1:
        return seen[0]
    return f"{len(seen)} groups failed" if seen else ""


@dataclass
class GroupState:
    cursor: Cursor = field(default_factory=Cursor)
    active: bool = True
    last_error: str = ""
    fail_streak: int = 0
    last_success: float = 0.0
    last_seen: float = 0.0
    scanned: int = 0
    pushed: int = 0
    held: int = 0
    # 群移出后再加入默认从 latest 开始;旧水位必须另存,否则 status 无法说明缺口,
    # resume_from_tombstone 也无从恢复。
    gap_from: Cursor | None = None
    gap_messages: int = 0

    def resume_from_tombstone(self) -> None:
        """把水位退回移出群之前的位置,并清掉墓碑。

        和 `reconcile_state` 里立墓碑的那几行是一对 —— 放在一起,以后多一个墓碑
        字段时不会出现「立的地方加了、清的地方忘了」这种只在 /status 上看得出来的问题。
        """
        if self.gap_from is None:
            raise MarsSyncError("没有可恢复的 tombstone 水位")
        self.cursor = self.gap_from
        self.gap_from = None
        self.gap_messages = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cursor"] = self.cursor.to_dict()
        d["gap_from"] = self.gap_from.to_dict() if self.gap_from else None
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "GroupState":
        d = d or {}
        gap = d.get("gap_from")
        return cls(
            cursor=Cursor.from_dict(d.get("cursor")),
            active=bool(d.get("active", True)),
            last_error=_error_text(d.get("last_error")),
            fail_streak=int(d.get("fail_streak") or 0),
            last_success=float(d.get("last_success") or 0),
            last_seen=float(d.get("last_seen") or 0),
            scanned=int(d.get("scanned") or 0),
            pushed=int(d.get("pushed") or 0),
            held=int(d.get("held") or 0),
            gap_from=Cursor.from_dict(gap) if gap else None,
            gap_messages=int(d.get("gap_messages") or 0),
        )


@dataclass
class State:
    version: int = 0
    groups: dict[str, GroupState] = field(default_factory=dict)
    cursor: Cursor = field(default_factory=Cursor)
    pushed: int = 0            # 累计推送条数
    last_run: float = 0.0      # 上次跑完的时间戳
    last_push: float = 0.0     # 上次真的推出去东西的时间戳
    last_error: str = ""
    round_robin: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cursor"] = self.cursor.to_dict()
        d["groups"] = {rid: group.to_dict() for rid, group in self.groups.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        version = int(d.get("version") or 1)
        if version not in (1, 2):
            raise ValueError(f"不支持的状态版本 {version}")
        raw_groups = d.get("groups") or {}
        if not isinstance(raw_groups, dict):
            raise TypeError("groups 不是 JSON object")
        return cls(
            version=version,
            groups={str(rid): GroupState.from_dict(group) for rid, group in raw_groups.items()},
            cursor=Cursor.from_dict(d.get("cursor")),
            pushed=int(d.get("pushed") or 0),
            last_run=float(d.get("last_run") or 0),
            last_push=float(d.get("last_push") or 0),
            last_error=_error_text(d.get("last_error")),
            round_robin=int(d.get("round_robin") or 0),
        )

    def refresh_shadow_cursor(self) -> None:
        active = [g.cursor for g in self.groups.values() if g.active]
        self.cursor = min(active) if active else Cursor()


def load_state(settings: Settings | None = None) -> State:
    path = (settings or get_settings()).mars_state_file
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    except OSError as e:
        raise MarsStateError(f"读取同步水位失败 {path}: {e}") from e
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("顶层不是 JSON object")
        return State.from_dict(parsed)
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as e:
        raise MarsStateError(f"同步水位文件损坏 {path}: {e}") from e


def save_state(state: State, settings: Settings | None = None) -> None:
    """原子写水位。失败必须中止同步,不能把本轮伪装成成功。"""
    path = (settings or get_settings()).mars_state_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise MarsStateError(f"写同步水位失败 {path}: {e}") from e


def _backup_v1_state(settings: Settings) -> None:
    """迁移前留一份只读回滚证据;已有备份不覆盖。"""
    path = settings.mars_state_file
    backup = path.with_name(f"{path.name}.v1.bak")
    if not path.exists() or backup.exists():
        return
    try:
        shutil.copy2(path, backup)
    except OSError as e:
        raise MarsStateError(f"备份旧同步水位失败 {backup}: {e}") from e


# ─── 映射 ───────────────────────────────────────────────────────────────

def group_name(name: str, settings: Settings) -> str:
    """给群名加上 mars 的企微群前缀(默认 `work-`)。

    这不是装饰: mars 靠群名认企微群(`GroupIMCategory`),也靠群名把这类群排除
    在客户未响应告警之外(`groupmsg.LoadMsgs` 的 `left(name,5) != 'work-'`)。
    hook 建群时 group_type 落的是默认值 `customer`,不加前缀的话,一个内部群
    会被 deimos 当成客户群盯着,超时没人回就电话叫 TS 值班 —— 那是打扰别人。
    """
    prefix = settings.mars_group_name_prefix
    if not prefix or name.startswith(prefix):
        return name
    return f"{prefix}{name}"


# 引用部分的前缀。这是**推给 mars 的输出格式**,所以只在这里定义 ——
# 本地库那边只给结构化的 text/quote_text,不掺和正文长什么样。
QUOTE_PREFIX = "> 引用: "

# 这两类的占位符只是上游渲染器加的提示,真实类型会通过 messageType 单独传给
# mars。正文保留它们会出现「[卡片] 708781594 https://...」这样的视觉噪音。
PUSH_PLACEHOLDERS = {"卡片": "[卡片]", "文档链接": "[文档]"}


def _without_push_placeholder(text: str, message_type: str) -> str:
    placeholder = PUSH_PLACEHOLDERS.get(message_type)
    if not placeholder:
        return text
    stripped = text.lstrip()
    if not stripped.startswith(placeholder):
        return text
    return stripped[len(placeholder):].lstrip()


def _swap(part: str, url: str) -> str:
    """把这一段里的 `[图片]` 换成 URL;没有占位符就把 URL 缀在后面。"""
    if IMAGE_PLACEHOLDER in part:
        return part.replace(IMAGE_PLACEHOLDER, url, 1).strip()
    return f"{part} {url}".strip()


def _resolved(
    part: str, raw, settings: Settings, rid="", send_time: int = 0, lid: int = 0,
) -> str:
    """有原图就把这一段里的 `[图片]` 换成公网 URL,换不到就原样留着。

    替换而不是追加: 前端已经能把 URL 渲染成图,再留一个「[图片]」在图上方
    纯属噪音。反过来,URL 没解析出来时占位符必须留着,否则这段就空了。

    `rid`/`send_time` 只往下透给对象 key,不参与「取哪张图」。
    """
    if not (settings.kodo_enabled and part and raw):
        return part
    url = media.resolve(raw, settings, rid, send_time, lid)
    return _swap(part, url) if url else part


def message_body(msg: dict, settings: Settings) -> str:
    """结构化字段 → 推给 mars 的一段正文。**唯一**把消息拼成字符串的地方。

    正文和引用各有各的图(自己发的那张、被引的那张),分开处理就不必再去拆行
    猜哪一行是引用 —— 之前那版正是这么做的,而它依赖「本地库拼串用的前缀」和
    「这里匹配用的前缀」两个字面量始终一致,改一个就会无声退化成把 URL 全缀到
    行尾。现在两段从头到尾就没合并过。

    ⚠️ 引用里那张图常常就是**更早某条**消息的图,但我们手上只有当前这条的时刻,
    对象 key 因此会记成回复的时间,同一张图在桶里存两份。这是按群按时间编址换来的:
    只有内容寻址才能让它们撞成一个对象,而那样就翻不了「某群某天」。多出来的是几十 KB,
    不值得为它把 key 改回去。
    """
    rid, send_time = msg.get("rid", ""), int(msg.get("send_time") or 0)
    lid = int(msg.get("lid") or 0)
    text = _without_push_placeholder(msg.get("text") or "", msg.get("type") or "")
    text = _resolved(text, msg.get("raw"), settings, rid, send_time, lid)
    quote = _resolved(
        msg.get("quote_text") or "", msg.get("quote_raw"), settings, rid, send_time, lid,
    )

    # 引用的内容已经在正文里出现过就不再缀一遍(回复时把原话复述一遍很常见)。
    # 比对放在换完 URL 之后: 两张图都解析成同一个 URL 才是真的同一张图,
    # 在占位符阶段比就会把「图片回复图片」误判成重复,把引用整个丢掉。
    if not quote or quote in text:
        return text
    return f"{text}\n{QUOTE_PREFIX}{quote}" if text else f"{QUOTE_PREFIX}{quote}"


def to_hook(msg: dict, settings: Settings) -> dict:
    """本地消息 → mars models.MessageHook 的 JSON。

    sendernumber 是 mars 判断「这条是不是我方客服回的」的唯一依据,所以优先用
    acctid(企微 userid,跨设备稳定);外部联系人/机器人没有 acctid,退回会话内
    的 vid 并加前缀,免得和 userid 混在一个命名空间里。
    """
    userid = msg.get("sender_userid") or ""
    sender_number = userid or f"{settings.mars_group_prefix}vid-{msg['sender_id']}"
    name = msg.get("sender_name") or sender_number
    return {
        "message": message_body(msg, settings),
        "groupnumber": f"{settings.mars_group_prefix}{msg['rid']}",
        "groupName": group_name(msg.get("group_name") or "", settings),
        "sendernumber": sender_number,
        "displayname": name,
        "nickname": name,
        "timestamp": int(msg.get("send_time") or 0),
        "clientType": settings.mars_client_type,
        "robot": settings.mars_robot,
        "avatar": msg.get("avatar") or "",
        "messageType": msg.get("type") or "",
    }


# ─── 推送 ───────────────────────────────────────────────────────────────

def mars_base(settings: Settings, exc: type = MarsNotConfigured) -> str:
    """mars 的根地址。推消息和拉任务两条通道共用,所以「没配」的说法也只有一份。

    `exc` 让调用方换成自己那一档的异常类型,报错才指得清是哪条通道没配。
    """
    base = settings.mars_base_url.strip().rstrip("/")
    if not base:
        raise exc(
            "未配置 mars 地址。设置 WECOM_SVR_MARS_BASE_URL(例: https://ts.qiniu.io)后重试"
        )
    return base


def hook_url(settings: Settings) -> str:
    return mars_base(settings) + HOOK_PATH


async def push_one(client: httpx.AsyncClient, hook: dict, settings: Settings) -> None:
    """推一条。失败抛 MarsSyncError —— 调用方据此停在当前水位,下轮重来。"""
    try:
        resp = await client.put(hook_url(settings), json=hook)
    except httpx.HTTPError as e:
        raise MarsSyncError(f"调用 mars 失败: {e}") from e
    if resp.status_code != 200:
        body = resp.text.strip()[:200]
        raise MarsSyncError(f"mars 返回 {resp.status_code}: {body}")


# ─── 一轮同步 ───────────────────────────────────────────────────────────

def _ready_count(msgs: list[dict], settings: Settings) -> int:
    """一个群本轮推到第几条 —— 停在第一条仍可能下载到的图片前。

    为什么要停:企微收到图片消息时只写 SQLite 的消息行,图片文件要等你点开
    会话才下载。默认 3 秒的轮询几乎总是跑在下载前面,于是这条消息带着
    `[图片]` 占位符就推走了 —— 而 mars 的 checksum 含正文
    (`md5(群号+发送人+时间+正文)`),事后拿 URL 重推是**另一条**记录,不是更新。
    没有回补的余地,就只能在推之前等。

    等待有上限(`mars_image_wait`): 超时就带着占位符推走,退化成旧行为,
    不会把整条同步链卡死在一张永远不会下载的图上。

    每群独立水位保证这里只会挡住本群;其它群仍可继续。
    """
    wait = float(settings.mars_image_wait or 0)
    if wait <= 0 or not settings.kodo_enabled:
        return len(msgs)

    now = time.time()
    for i, m in enumerate(msgs):
        if now - int(m.get("send_time") or 0) >= wait:
            continue                       # 等够了,带着占位符推走
        payloads = media_payloads(m)
        rid, lid = int(m.get("rid") or 0), int(m.get("lid") or 0)
        for raw in payloads:
            if media.pending(raw, settings):
                media.ensure_local(raw, settings, rid, lid)
        if any(media.pending(raw, settings) for raw in payloads):
            return i
    return len(msgs)


def plan_round(batch, rids: list[int], settings: Settings) -> tuple[list[dict], dict[int, int]]:
    """本轮真正要推的消息,以及每个群被扣下了几条。

    `_sync_once` 和 `/preview` 必须给出同一个答案 —— preview 存在的意义就是
    「下一轮会推什么」,两边各写一遍的话,改了扣图规则就会让 preview 悄悄开始说谎。
    所以这里是唯一一处。

    整轮只占一次线程池:逐群 `to_thread` 在开了 sync_all_groups 时是 276 次空转
    派发(实测 13ms),而每 3 秒就要来一遍。
    """
    by_group: dict[int, list[dict]] = {rid: [] for rid in rids}
    for m in batch.messages:
        by_group[int(m["rid"])].append(m)

    ready_keys: set[tuple[int, int]] = set()
    held: dict[int, int] = {}
    for rid, group_messages in by_group.items():
        if not group_messages:
            continue
        n = _ready_count(group_messages, settings)
        ready_keys.update((rid, int(m["lid"])) for m in group_messages[:n])
        if len(group_messages) > n:
            held[rid] = len(group_messages) - n

    due = [m for m in batch.messages if (int(m["rid"]), int(m["lid"])) in ready_keys]
    return due, held


@dataclass
class SyncResult:
    decrypted: bool = False        # 本轮是否真的重解密了(源库有变化)
    scanned: int = 0               # 水位后读到多少条
    pushed: int = 0                # 实际推出去多少条
    held: int = 0                  # 因为在等图而推迟到下一轮的条数
    groups: list[str] = field(default_factory=list)
    cursor: Cursor = field(default_factory=Cursor)
    group_results: dict[str, dict] = field(default_factory=dict)
    dry_run: bool = False
    partial: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cursor"] = self.cursor.to_dict()
        return d


def target_rids(settings: Settings) -> tuple[list[int], list[str], list[str]]:
    """解析同步范围 → (rids, 命中的群名, 没匹配上的选择器)。"""
    if settings.mars_sync_all_groups:
        groups = messages.list_groups(limit=None)
        return [int(g["rid"]) for g in groups], [g["name"] for g in groups], []

    selectors = settings.mars_group_selectors
    if not selectors:
        raise MarsNotConfigured(
            "未配置要同步的群。设置 WECOM_SVR_MARS_SYNC_GROUPS(群名或 RID,逗号分隔);"
            "或者显式设置 WECOM_SVR_MARS_SYNC_ALL_GROUPS=true;候选见 GET /api/v1/mars/sync/groups"
        )
    hit, missing = messages.resolve_groups(selectors)
    return [g["rid"] for g in hit], [g["name"] for g in hit], missing


def cursor_for_backfill(seconds: int) -> Cursor:
    """「往回补 N 秒」对应的水位。"""
    return Cursor(send_time=max(0, int(time.time()) - int(seconds)), lid=0)


def initial_cursor(settings: Settings, rid: int | None = None) -> Cursor:
    """首跑水位:默认贴着该群最新一条,只有显式配了 backfill 才往回补。

    倒灌历史是不可撤销的(下游会据此发通知、切问题),默认必须是「不倒灌」。

    只由 `reconcile_state` 调用 —— `/preview` 也是经它拿到首跑水位的(`persist=False`),
    这样 preview 和真实首轮用的是同一套起点,不会各算一套(比如漏掉 backfill)。
    """
    back = int(settings.mars_sync_backfill or 0)
    if back <= 0:
        return messages.latest_cursor(rid)
    return cursor_for_backfill(back)


def reconcile_state(
    state: State,
    rids: list[int],
    settings: Settings,
    *,
    persist: bool,
) -> State:
    """迁移 v1 并把 active/tombstone 与当前同步范围对齐。

    `persist=False` 给 preview/dry-run 使用:返回深拷贝,绝不改磁盘状态。
    """
    state = deepcopy(state)
    now = time.time()
    changed = False

    if state.version < 2:
        legacy = state.cursor
        state.groups = {}
        for rid in rids:
            cursor = legacy if legacy != Cursor() else initial_cursor(settings, rid)
            state.groups[str(rid)] = GroupState(cursor=cursor, active=True, last_seen=now)
        state.version = 2
        state.round_robin = 0
        changed = True
        if persist:
            _backup_v1_state(settings)
    else:
        wanted = {str(rid) for rid in rids}
        for key, group in state.groups.items():
            if key not in wanted and group.active:
                group.active = False
                group.last_seen = now
                changed = True

        for rid in rids:
            key = str(rid)
            target_group = state.groups.get(key)
            if target_group is None:
                state.groups[key] = GroupState(
                    cursor=initial_cursor(settings, rid), active=True, last_seen=now
                )
                changed = True
                continue
            if not target_group.active:
                old = target_group.cursor
                counts = messages.count_since({rid: old})
                target_group.active = True
                target_group.gap_from = old
                target_group.gap_messages = counts.get(rid, 0)
                # 重新加入默认不重放副作用;显式 resume 才从 gap_from 追赶。
                target_group.cursor = messages.latest_cursor(rid)
                target_group.last_seen = now
                target_group.last_error = ""
                target_group.fail_streak = 0
                changed = True

    state.refresh_shadow_cursor()
    if changed and persist:
        # 迁移/同步范围变化必须先落盘再发 HTTP,保证过程可重入。
        save_state(state, settings)
    return state


#: 水位的读-改-写要串行 —— `/run` 和后台循环跑的是同一个 `sync_once`,并发进来
#: 两轮会各自读到同一个水位、把同一批消息推两遍。
#: ⚠️ 这是**进程内**的锁,所以当前部署必须保持单进程 uvicorn;改多 worker 之前
#: 要先把它换成跨进程的文件锁。
sync_lock = asyncio.Lock()


async def sync_once(
    settings: Settings | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> SyncResult:
    """互斥跑一轮。当前部署是单进程 uvicorn,所以进程内锁足够。"""
    async with sync_lock:
        return await _sync_once(settings, dry_run, limit)


async def _sync_once(
    settings: Settings | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> SyncResult:
    """跑一轮: 刷新本地库 → 取水位后的群消息 → 顺序推给 mars → 落水位。

    出错时水位只推进到最后一条**成功**的消息 —— 中断点之后下轮重来。
    """
    settings = settings or get_settings()
    result = SyncResult(dry_run=dry_run)

    # target_rids 要查 sqlite,和下面的 refresh/fetch 一样不能占着事件循环
    rids, names, missing = await asyncio.to_thread(target_rids, settings)
    result.groups = names
    if missing:
        log.warning("这些群在本地会话里找不到,已跳过: %s", ", ".join(missing))
    if not rids:
        raise MarsNotConfigured(f"配置的群一个都没匹配上本地会话: {', '.join(missing)}")

    # 解密是 CPU 密集且要 ~0.8s,不能阻塞事件循环
    result.decrypted = await asyncio.to_thread(messages.refresh)

    state = load_state(settings)
    state = await asyncio.to_thread(
        lambda: reconcile_state(state, rids, settings, persist=not dry_run)
    )
    before = state.to_dict()

    size = int(limit or settings.mars_sync_batch)
    cursors = {rid: state.groups[str(rid)].cursor for rid in rids}
    batch = await asyncio.to_thread(messages.fetch_since, cursors, size, False, state.round_robin)
    result.scanned = batch.scanned

    for rid in rids:
        result.group_results[str(rid)] = {
            "scanned": batch.scan_counts.get(rid, 0),
            "pushed": 0,
            "held": 0,
            "error": "",
            "cursor": state.groups[str(rid)].cursor.to_dict(),
        }

    if dry_run:
        result.cursor = state.cursor
        return result

    due, held_by_group = await asyncio.to_thread(plan_round, batch, rids, settings)
    for rid, held in held_by_group.items():
        result.held += held
        result.group_results[str(rid)]["held"] = held

    hooks = await asyncio.to_thread(lambda: [to_hook(m, settings) for m in due])
    # 一个群推挂了就停在那儿,后面的留给下一轮 —— errors 同时就是「已中断的群」
    errors: dict[int, str] = {}
    if due:
        async with loop.http_client(settings.mars_timeout) as client:
            for msg, hook in zip(due, hooks, strict=True):
                rid = int(msg["rid"])
                if rid in errors:
                    continue
                try:
                    await push_one(client, hook, settings)
                except MarsSyncError as e:
                    errors[rid] = _error_text(e)
                    log.warning("群 %s 推送中断于 LID=%s: %s", rid, msg["lid"], e)
                    continue
                state.groups[str(rid)].cursor = Cursor(msg["send_time"], msg["lid"])
                result.pushed += 1
                result.group_results[str(rid)]["pushed"] += 1

    now = time.time()
    for rid in rids:
        group = state.groups[str(rid)]
        if rid not in batch.selected_rids:
            result.group_results[str(rid)]["cursor"] = group.cursor.to_dict()
            continue
        group.scanned = batch.scan_counts.get(rid, 0)
        group.pushed = result.group_results[str(rid)]["pushed"]
        group.held = held_by_group.get(rid, 0)
        error = errors.get(rid, "")
        group.last_error = error
        if error:
            group.fail_streak += 1
            result.group_results[str(rid)]["error"] = error
        else:
            group.fail_streak = 0
            # 只有本群没有 held/failed,才能安全越过过滤掉的批尾行。
            if rid not in held_by_group:
                end = batch.scan_end.get(rid)
                if end and end > group.cursor:
                    group.cursor = end
                if rid in batch.scan_end:
                    group.last_success = now
                    group.last_seen = now
        result.group_results[str(rid)]["cursor"] = group.cursor.to_dict()

    state.pushed += result.pushed
    state.round_robin = batch.next_offset
    if result.pushed:
        state.last_push = now
    round_error = _summarize_errors(errors.values())
    state.last_error = _summarize_errors(
        group.last_error for group in state.groups.values() if group.active and group.last_error
    )
    state.refresh_shadow_cursor()

    # last_run 自身不值得每三秒写盘;有其它实质变化时才一起持久化。
    if state.to_dict() != before:
        state.last_run = now
        save_state(state, settings)

    result.cursor = state.cursor
    result.error = round_error
    result.partial = bool(errors) and len(errors) < len(rids)
    return result


# ─── 后台循环 ───────────────────────────────────────────────────────────

class SyncLoop(BackgroundLoop):
    """常驻同步任务。间隔小的时候基本等价于实时推送。"""

    name = "mars-sync"

    def __init__(self) -> None:
        super().__init__()
        self.last_result: SyncResult | None = None

    async def _run(self) -> None:
        settings = get_settings()
        interval = max(1.0, float(settings.mars_sync_interval))
        log.info("mars 群消息同步已启动,间隔 %.1fs", interval)
        self.client = httpx.AsyncClient(timeout=settings.mars_timeout)
        while True:
            try:
                self.last_result = await sync_once(settings)
                self.last_error = self.last_result.error
            except asyncio.CancelledError:
                raise
            except MarsSyncError as e:
                # 配置类错误:说清楚就行,别刷屏
                if self.last_error != str(e):
                    log.warning("mars 同步暂停: %s", e)
                self.last_error = str(e)
            except Exception as e:  # 本地库没解密、企微没登录…… 循环不能因此死掉
                if self.last_error != str(e):
                    log.exception("mars 同步异常: %s", e)
                self.last_error = str(e)
            await asyncio.sleep(interval)


loop = SyncLoop()
