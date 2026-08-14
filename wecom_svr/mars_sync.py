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
from pathlib import Path

import httpx

from . import media, messages
from .bgloop import BackgroundLoop
from .messages import FILE_PLACEHOLDER, IMAGE_PLACEHOLDER, Cursor, media_payloads
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
    # 多媒体 Content 规则的部署边界。此点以前禁止自动回放,避免旧消息用新规则重算。
    media_cutover: Cursor | None = None
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

    def rewind(self, cursor: Cursor, *, force: bool = False) -> None:
        """把水位挪到 `cursor`,挡住跨越多媒体切换点的回放。

        和 `media_cutover` 放在一起,而不是留在 HTTP handler 里 —— 立这个字段的
        地方和用它的地方分家,以后多一条改水位的路径就会漏掉这一层检查。
        """
        if not force and self.media_cutover and cursor < self.media_cutover:
            raise MarsSyncError(
                f"新水位早于多媒体切换点 "
                f"{self.media_cutover.send_time}/{self.media_cutover.lid},禁止跨点回放。"
                "确实要重放(接受 mars 里出现重复行)就加 force=true"
            )
        self.cursor = cursor

    def resume_from_tombstone(self, *, force: bool = False) -> None:
        """把水位退回移出群之前的位置,并清掉墓碑。

        和 `reconcile_state` 里立墓碑的那几行是一对 —— 放在一起,以后多一个墓碑
        字段时不会出现「立的地方加了、清的地方忘了」这种只在 /status 上看得出来的问题。
        """
        if self.gap_from is None:
            raise MarsSyncError("没有可恢复的 tombstone 水位")
        self.rewind(self.gap_from, force=force)
        self.gap_from = None
        self.gap_messages = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cursor"] = self.cursor.to_dict()
        d["media_cutover"] = self.media_cutover.to_dict() if self.media_cutover else None
        d["gap_from"] = self.gap_from.to_dict() if self.gap_from else None
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "GroupState":
        d = d or {}
        gap = d.get("gap_from")
        cutover = d.get("media_cutover")
        return cls(
            cursor=Cursor.from_dict(d.get("cursor")),
            media_cutover=Cursor.from_dict(cutover) if cutover else None,
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
        if version not in (1, 2, 3):
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


@dataclass(frozen=True)
class Segment:
    """一条消息里某个媒体载荷分到的配额。

    `mars_image_max_per_message` 是**整条消息**的上限,而正文和引用是分开解析的
    (`MEDIA_FIELDS` 的顺序就是分配顺序)。谁分到哪几张必须只算一次:`_ready_count`
    按它决定等哪些图,`message_body` 按它决定排哪些 URL,两边算出不同的答案就会
    出现「等的不是要排的那张」—— 消息带着占位符推走,而水位那侧还以为等齐了。
    """

    raw: bytes | None
    images: list[media.MediaItem]  # 本段要展示的图,已经扣过整条消息的配额
    files: list[media.MediaItem]   # 附件:没有 aeskey,下不了,只写文件名
    legacy: bool                   # 媒体树里认不出票据,退回旧的单图解析
    omitted: int                   # 超出上限、本条不展示的图片数


def media_plan(msg: dict, settings: Settings) -> tuple[Segment, Segment]:
    """整条消息的媒体分配。**唯一**一处决定「哪几张图属于哪一段」。"""
    remaining = max(0, int(settings.mars_image_max_per_message))
    plan: list[Segment] = []
    for raw in media_payloads(msg):
        items = media.collect(raw) if raw else []
        if not raw:
            plan.append(Segment(raw, [], [], False, 0))
        elif not items:                   # 兼容旧的文件名/CDN 单图载荷:至多一张
            legacy = remaining > 0
            remaining -= int(legacy)
            plan.append(Segment(raw, [], [], legacy, 0 if legacy else 1))
        else:
            images = [item for item in items if item.kind == "image"]
            shown = images[:remaining]
            remaining -= len(shown)
            plan.append(Segment(
                raw, shown, [item for item in items if item.kind == "file"],
                False, len(images) - len(shown),
            ))
    body, quote = plan                    # MEDIA_FIELDS 固定是 (raw, quote_raw)
    return body, quote


def _resolved(
    part: str, segment: Segment, settings: Settings,
    rid="", send_time: int = 0, lid: int = 0, deadline: float | None = None,
) -> list[str]:
    """按分好的配额渲染一段,**按行返回**。正文和引用各调一次,不跨段去重。

    返回行而不是拼好的字符串:调用方还要给引用逐行加前缀、还要汇总省略提示,拼完
    再 `splitlines()` 拆回去,就是把刚握在手里的结构从自己的输出里重新解析一遍。

    每张图独占一行 —— 前端 `.qq-msg-image` 本来就竖排,而换行让 URL 没被渲染成图时
    (URL 挂了、前端旧版本)退化成一行一个链接,仍然可读。
    """
    if not part:
        return []
    if not settings.kodo_enabled:
        rendered = [""] * len(segment.images)
    elif segment.legacy:
        url = media.resolve(segment.raw, settings, rid, send_time, lid)
        return [_swap(part, url) if url else part]
    else:
        rendered = [
            media.resolve_item(item, settings, rid, send_time, lid, deadline)
            for item in segment.images
        ]

    # 取到的第一张换掉正文里的占位符,剩下的各占一行;没取到的留占位符文本。
    head = _swap(part, rendered[0]) if rendered and rendered[0] else part
    lines = [head, *(url or IMAGE_PLACEHOLDER for url in rendered[1:])]
    if segment.files and head.strip() == FILE_PLACEHOLDER:
        lines = lines[1:]                 # 上游合成的裸「[文件]」由文件名行顶掉
    return [*lines, *(f"{FILE_PLACEHOLDER} {item.filename}".rstrip() for item in segment.files)]


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
    # 整条消息共享一个取图预算,而不是每张图各等一个 mars_image_client_wait ——
    # 一条 11 张图的消息卡着的是本群的水位。
    deadline = time.monotonic() + max(0.0, float(settings.mars_image_client_wait))
    body_plan, quote_plan = media_plan(msg, settings)
    lines = _resolved(
        _without_push_placeholder(msg.get("text") or "", msg.get("type") or ""),
        body_plan, settings, rid, send_time, lid, deadline,
    )
    quote = _resolved(
        msg.get("quote_text") or "", quote_plan, settings, rid, send_time, lid, deadline,
    )

    # 引用的内容已经在正文里出现过就不再缀一遍(回复时把原话复述一遍很常见)。
    # 比对放在换完 URL 之后: 两张图都解析成同一个 URL 才是真的同一张图,
    # 在占位符阶段比就会把「图片回复图片」误判成重复,把引用整个丢掉。
    body = "\n".join(lines).strip()
    if (quoted := "\n".join(quote).strip()) and quoted not in body:
        lines += [f"{QUOTE_PREFIX}{line}" for line in quote]
    if omitted := body_plan.omitted + quote_plan.omitted:
        lines.append(f"(另有 {omitted} 张未展示)")
    return "\n".join(lines).strip()


def _outbox_path(msg: dict, settings: Settings) -> Path:
    rid, lid = int(msg.get("rid") or 0), int(msg.get("lid") or 0)
    return settings.mars_outbox_dir / str(rid) / f"{int(msg.get('send_time') or 0)}-{lid}.json"


def peek_outbox(msg: dict, settings: Settings) -> dict | None:
    """这条消息的 Content 已经定过了吗?定过就返回那一份,没有返回 None。

    只读。`/preview` 用它保证「下一轮会推什么」说的是实话 —— 重新算一遍的话,
    对已经落过 outbox 的消息,preview 展示的会是新解析结果,推出去的却是冻结的旧值。
    """
    path = _outbox_path(msg, settings)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise MarsStateError(f"读取 mars outbox 失败 {path}: {e}") from e
    try:
        hook = json.loads(raw)
        if not isinstance(hook, dict) or not isinstance(hook.get("message"), str):
            raise TypeError("hook 格式无效")
        # 早期 outbox 可能把 SQLite 的数字身份原样冻结下来。正文必须保持首次
        # 决定,但身份字段只是 JSON 契约适配,重试时归一化不会改变消息去重键。
        for field in ("userid", "corpid"):
            if field in hook:
                hook[field] = str(hook[field] or "")
        return hook
    except (json.JSONDecodeError, TypeError) as e:
        raise MarsStateError(f"mars outbox 损坏 {path}: {e}") from e


def frozen_hook(msg: dict, settings: Settings) -> dict:
    """这条消息**下一轮会推**的 hook,只读:定过就用定过的那份,没定过才现算。

    `/preview` 和 `outbox_hook` 必须给出同一个答案 —— 各写一遍的话,改了冻结规则
    (比如哪些消息值得落盘)就会让 preview 悄悄开始说谎。
    """
    return peek_outbox(msg, settings) or to_hook(msg, settings)


def outbox_hook(msg: dict, settings: Settings) -> dict:
    """首次生成 hook 后原子落盘;任何投递重试都复用完全相同的 Content。

    为什么必须落盘: mars 的去重 checksum 含正文,所以同一条源消息的 Content 必须在
    它的**所有投递尝试**之间一致 —— 推成功但响应丢了、进程在 push 和落水位之间崩了,
    下一轮都会重新解析媒体,而那时缓存里可能多了一张图,于是 mars 里多一行。

    没有媒体的消息不落盘: 它的 Content 已经是本地库那一行的纯函数,重算必然相同,
    为它付一次 fsync 只是白花钱。
    """
    if not any(media_payloads(msg)):
        return to_hook(msg, settings)
    if (existing := peek_outbox(msg, settings)) is not None:
        return existing

    path = _outbox_path(msg, settings)
    hook = to_hook(msg, settings)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{time.time_ns()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(hook, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        # hard link 是原子的且不会覆盖赢家:并发生成时只有第一个成为首次决定。
        os.link(tmp, path)
        return hook
    except FileExistsError:
        return peek_outbox(msg, settings) or hook
    except OSError as e:
        raise MarsStateError(f"写 mars outbox 失败 {path}: {e}") from e
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def prune_outbox(cursors: dict[str, Cursor], settings: Settings) -> None:
    """丢掉水位已经越过的那些 —— 它们不会再被投递,冻结的 Content 也就没用了。

    这是 outbox 的**唯一**回收路径。不删的话每条推过的消息都会永久留下一个小文件,
    inode 先于字节撑爆。写在落水位之后:先删后落盘的话,崩溃就会让一条正在重试的
    消息丢掉它的首次决定。

    只传**本轮水位动过的群**。文件只会因为水位前进而失效,所以别的群扫了也是白扫 ——
    `state.groups` 会累积每一个见过的群(含墓碑),几百个目录每轮 opendir 一遍,
    其中绝大多数根本不存在。
    """
    for key, cursor in cursors.items():
        for entry in (settings.mars_outbox_dir / key).glob("*-*.json"):
            send_time, _, lid = entry.stem.partition("-")
            try:
                if Cursor(int(send_time), int(lid)) <= cursor:
                    entry.unlink(missing_ok=True)
            except (ValueError, OSError) as e:   # 清不掉只是留下垃圾,不该让本轮失败
                log.warning("清理 mars outbox %s 失败: %s", entry, e)


def to_hook(msg: dict, settings: Settings) -> dict:
    """本地消息 → mars models.MessageHook 的 JSON。

    senderNumber 使用本地通讯录 RID,userid/corpid 单独传成员的企微身份。
    RID 对内部成员、外部联系人和机器人都存在,不会因为跨企业成员没有本企业
    userid 而和其他成员混淆。
    """
    userid = str(msg.get("sender_userid") or "")
    corpid = str(msg.get("sender_corpid") or "")
    sender_number = str(msg.get("sender_id") or "")
    name = msg.get("sender_name") or userid or sender_number
    return {
        "message": message_body(msg, settings),
        # 本地消息没有客户 UID 时保持 0,与 Mars MessageHook 的默认值一致。
        "uid": int(msg.get("uid") or 0),
        "groupNumber": f"{settings.mars_group_prefix}{msg['rid']}",
        "groupName": group_name(msg.get("group_name") or "", settings),
        "senderNumber": sender_number,
        "userid": userid,
        "corpid": corpid,
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
        rid, lid = int(m.get("rid") or 0), int(m.get("lid") or 0)
        # 和 message_body 用同一个预算和同一份配额,否则等的和排的会是不同的图。
        deadline = time.monotonic() + max(0.0, float(settings.mars_image_client_wait))
        # 先把这条能取的都取了再判断:某张取不到不该妨碍同一条里其余几张落地,
        # 否则窗口到期时它们仍是占位符。
        waiting = False
        for segment in media_plan(m, settings):
            if segment.legacy:
                if media.pending(segment.raw, settings):
                    media.ensure_local(segment.raw, settings, rid, lid)
                    waiting = waiting or media.pending(segment.raw, settings)
                continue
            for item in segment.images:
                # 定位一次就把结果一路传下去 —— 每次 glob 都要扫遍缓存目录
                # (`_candidates` 的 docstring 记了实测数字),同一个问题不问三遍。
                loc = media.locate_item(item, settings)
                # type=101 的图片已经带着企微临时下载地址,可以当场拉取,不需要
                # 等本地 Images 缓存或触发客户端切会话。
                if loc.url:
                    continue
                if not loc.hits:
                    got = media.ensure_item_local(item, settings, rid, lid, deadline, loc)
                    waiting = waiting or not got
        if waiting:
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
                # 不设 media_cutover:切换点表示「这之前推过的是旧规则的 Content」,
                # 而全新的群一条都没推过,给它钉一个只会平白挡住正常的 backfill。
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

    # v3 首次启用多媒体规则时把当前水位钉成切换点:切换点之前的消息按旧规则推过,
    # 用新规则重算 Content 会在 mars 里留下第二行,所以不许回放到那之前。
    #
    # 必须遍历**全部**群,不能只遍历 rids —— 升级那一刻不在白名单里(客户端没开、
    # 群名改了)的群否则永远拿不到切换点,以后重新加回来就能无声地跨点回放。
    #
    # ⚠️ 有一段窗口关不上:升级前「已 push 成功但水位没来得及落盘」的消息位于切换点
    # 之后,又还没有 outbox,重推时会用新规则重算。见 docs/multi-media-messages.md §8。
    if state.version < 3:
        for group in state.groups.values():
            group.media_cutover = group.cursor
        state.version = 3
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

    # 先落 outbox 再发 HTTP:即使响应丢失或进程在更新水位前崩溃,下轮 Content 也不漂移。
    hooks = await asyncio.to_thread(lambda: [outbox_hook(m, settings) for m in due])
    # 一个群推挂了就停在那儿,后面的留给下一轮 —— errors 同时就是「已中断的群」
    errors: dict[int, str] = {}
    advanced: set[str] = set()           # 水位真动过的群,回收 outbox 只看它们
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
                advanced.add(str(rid))
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
        # 落盘之后再回收:先删的话,崩在这中间就会让重试中的消息丢掉首次决定。
        outbox_cursors: dict[str, Cursor] = {key: state.groups[key].cursor for key in advanced}
        await asyncio.to_thread(prune_outbox, outbox_cursors, settings)

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
