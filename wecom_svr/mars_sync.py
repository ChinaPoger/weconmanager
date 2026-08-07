"""把本地群消息同步到 mars 的群消息接口。

下游是 mars 的 `PUT {base}/api/qq/hook/v2/message` —— QQ 机器人原本用的那个
hook,models.MessageHook 的字段名带着 QQ 时代的痕迹(groupnumber/sendernumber),
但 mars 早就认 `clientType: "Wework"`,所以企微消息走同一条路即可,不用改 mars。

几个决定了实现形状的下游事实(读 mars 源码得到,不是猜的):

  - **天然幂等**: mars 落库前算 checksum = md5(群号+发送人+发送时间+正文),
    表上是唯一索引 + FirstOrCreate。重推同一条只会更新,不会重复。
    所以水位丢了、重启多推一段,都不会脏数据 —— 这让「宁可重推」成为安全默认。
  - **顺序有意义**: mars 的 SetTimeline 按入库顺序切问题分组,乱序会切错。
    因此这里严格串行按 (send_time, LID) 升序推,不做并发。
  - **会顺手建群**: hook 里的 updateGroupInfo 会 upsert qq_groups。第一次推
    某个群就等于在 mars 建了这个群的档案,群名取自本地会话名。
  - **有副作用但可控**: hook 里的告警/转发/销售工单分支都按 robot 号或群配置
    命中,新 robot 号(默认 wecom-svr)不会撞上任何一条。

「定期」和「实时」是同一套代码: 后台循环 + mtime 闸门,间隔调到秒级就是准实时。
真正的下限来自企微客户端把消息刷进 SQLite 的延迟,不在这一侧。
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace

import httpx

from . import media, messages
from .messages import IMAGE_PLACEHOLDER, Cursor
from .settings import Settings, get_settings

log = logging.getLogger(__name__)

HOOK_PATH = "/api/qq/hook/v2/message"


class MarsSyncError(RuntimeError):
    """同步配置或调用失败(可预期,回 400/502)。"""


class MarsNotConfigured(MarsSyncError):
    pass


# ─── 水位 ───────────────────────────────────────────────────────────────

@dataclass
class State:
    cursor: Cursor = field(default_factory=Cursor)
    pushed: int = 0            # 累计推送条数
    last_run: float = 0.0      # 上次跑完的时间戳
    last_push: float = 0.0     # 上次真的推出去东西的时间戳
    last_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cursor"] = self.cursor.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        return cls(
            cursor=Cursor.from_dict(d.get("cursor")),
            pushed=int(d.get("pushed") or 0),
            last_run=float(d.get("last_run") or 0),
            last_push=float(d.get("last_push") or 0),
            last_error=str(d.get("last_error") or ""),
        )


def load_state(settings: Settings | None = None) -> State:
    path = (settings or get_settings()).mars_state_file
    try:
        return State.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return State()


def save_state(state: State, settings: Settings | None = None) -> None:
    """写水位。写坏了顶多重推(下游幂等),所以失败只告警不抛。"""
    path = (settings or get_settings()).mars_state_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("写同步水位失败(下次会重推,不会丢): %s", e)


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


def _resolved(part: str, raw, settings: Settings) -> str:
    """有原图就把这一段里的 `[图片]` 换成公网 URL,换不到就原样留着。

    替换而不是追加: 前端已经能把 URL 渲染成图,再留一个「[图片]」在图上方
    纯属噪音。反过来,URL 没解析出来时占位符必须留着,否则这段就空了。
    """
    if not (settings.kodo_enabled and part and raw):
        return part
    url = media.resolve(raw, settings)
    return _swap(part, url) if url else part


def message_body(msg: dict, settings: Settings) -> str:
    """结构化字段 → 推给 mars 的一段正文。**唯一**把消息拼成字符串的地方。

    正文和引用各有各的图(自己发的那张、被引的那张),分开处理就不必再去拆行
    猜哪一行是引用 —— 之前那版正是这么做的,而它依赖「本地库拼串用的前缀」和
    「这里匹配用的前缀」两个字面量始终一致,改一个就会无声退化成把 URL 全缀到
    行尾。现在两段从头到尾就没合并过。

    引用里的图常常就是上一条消息那张;对象 key 是内容 md5,所以多半直接命中
    已上传的对象,不会重复上传。
    """
    text = _without_push_placeholder(msg.get("text") or "", msg.get("type") or "")
    text = _resolved(text, msg.get("raw"), settings)
    quote = _resolved(msg.get("quote_text") or "", msg.get("quote_raw"), settings)

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

def hook_url(settings: Settings) -> str:
    base = settings.mars_base_url.strip().rstrip("/")
    if not base:
        raise MarsNotConfigured(
            "未配置 mars 地址。设置 WECOM_SVR_MARS_BASE_URL(例: https://ts.qiniu.io)后重试"
        )
    return base + HOOK_PATH


@asynccontextmanager
async def _client(settings: Settings):
    """拿一个 HTTP 客户端推送用。

    后台循环持有一个常驻的: 每轮新建等于每 interval 秒重握一次 TCP+TLS,
    默认 3s 间隔下 keep-alive 基本白设。一次性调用(/run、测试)就地建一个。
    """
    if loop.client is not None:
        yield loop.client
        return
    async with httpx.AsyncClient(timeout=settings.mars_timeout) as client:
        yield client


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

@dataclass
class SyncResult:
    decrypted: bool = False        # 本轮是否真的重解密了(源库有变化)
    scanned: int = 0               # 水位后读到多少条
    pushed: int = 0                # 实际推出去多少条
    groups: list[str] = field(default_factory=list)
    cursor: Cursor = field(default_factory=Cursor)
    dry_run: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cursor"] = self.cursor.to_dict()
        return d


def target_rids(settings: Settings) -> tuple[list[int], list[str], list[str]]:
    """解析同步范围 → (rids, 命中的群名, 没匹配上的选择器)。"""
    if settings.mars_sync_all_groups:
        groups = messages.list_groups()
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


def initial_cursor(settings: Settings) -> Cursor:
    """首跑水位:默认贴着最新一条,只有显式配了 backfill 才往回补。

    倒灌历史是不可撤销的(下游会据此发通知、切问题),默认必须是「不倒灌」。

    公开而不是私有,是因为 /preview 必须用**同一个**首跑水位: 它存在的意义
    就是「先看看第一轮会推什么」,自己另算一套(比如漏掉 backfill)就没意义了。
    """
    back = int(settings.mars_sync_backfill or 0)
    if back <= 0:
        return messages.latest_cursor()
    return cursor_for_backfill(back)


async def sync_once(
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
    before = replace(state)
    if state.cursor == Cursor():
        state.cursor = await asyncio.to_thread(initial_cursor, settings)
        log.info("首次同步,水位定在 %s", state.cursor)

    size = int(limit or settings.mars_sync_batch)
    batch = await asyncio.to_thread(messages.fetch_since, state.cursor, rids, size)
    result.scanned = len(batch)

    if dry_run:
        result.cursor = batch.cursor
        return result

    error = ""
    if batch.messages:
        # to_hook 会去缓存里 glob 原图、算 md5、HEAD + POST 传对象存储 —— 名字
        # 像个纯映射,实则是阻塞 I/O,直接 await 会把整个服务卡住最多两个 kodo_timeout。
        # 整批一次性转换,顺带让同一轮里的多张图共用一个线程切换的开销。
        hooks = await asyncio.to_thread(
            lambda: [to_hook(m, settings) for m in batch]
        )
        async with _client(settings) as client:
            for msg, hook in zip(batch, hooks, strict=True):
                try:
                    await push_one(client, hook, settings)
                except MarsSyncError as e:
                    # 停在这里:剩下的下轮重来,顺序不会乱
                    error = str(e)
                    log.warning("推送中断于 LID=%s: %s", msg["lid"], e)
                    break
                state.cursor = Cursor(msg["send_time"], msg["lid"])
                result.pushed += 1

    # 整批都推成功(或整批都是系统消息)时,水位越过尾部被过滤掉的行。
    # 少了这一步,一批全是系统通知就会把水位永远钉在原地。
    if not error and batch.scanned and batch.cursor > state.cursor:
        state.cursor = batch.cursor

    state.pushed += result.pushed
    state.last_run = time.time()
    if result.pushed:
        state.last_push = state.last_run
    state.last_error = error
    # last_run 每轮都会变,但它只是给人看的 —— 单凭它落盘就是默认间隔下每天
    # 两万多次重写。有实质变化(水位/条数/错误)才写。
    changed = (state.cursor, state.pushed, state.last_error) != (
        before.cursor, before.pushed, before.last_error
    )
    if changed:
        save_state(state, settings)

    result.cursor = state.cursor
    result.error = error
    return result


# ─── 后台循环 ───────────────────────────────────────────────────────────

class SyncLoop:
    """常驻同步任务。间隔小的时候基本等价于实时推送。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_result: SyncResult | None = None
        self.last_error: str = ""
        # 常驻连接池,循环跑起来才建 —— 见 _client
        self.client: httpx.AsyncClient | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="mars-sync")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None

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
