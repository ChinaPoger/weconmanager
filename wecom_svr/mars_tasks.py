"""从 mars 拉任务并执行 —— mars → 这台 Mac 的反向通道。

`mars_sync` 是本机把群消息推给 mars;这里是反过来:mars 想让这台 Mac 发一条
消息时,把任务落库,本机长轮询认领、执行、回报。为什么不是 WebSocket、
为什么不复用 `qq/hook/mission`,见 docs/mars-task-queue.md。

三条最容易被改错的取舍,在这里再写一遍:

  - **宁可漏发,不重发。** 租约模型是 at-least-once,而发消息不可撤销 ——
    重发是对方屏幕上多一条,漏发只是少一条通知。所以真正动手前先 `/start`
    把任务推进 executing:mars 据此分得清「领了还没做」(可以安全重投)和
    「做了一半失联」(判失败,绝不重投)。少了这一步,租约一到期 mars 就会把
    正在发的那条放回队列,于是真的发两遍 —— 本地怎么记都拦不住。
    这跟 mars_sync 的「宁可重推」正好相反:那边 mars 有 checksum 唯一索引兜底。
  - **令牌是 fencing。** 认领拿到的 lease_token 要带回 start/ack。本机卡住超过
    租约后醒来,回报会被 mars 以 409 拒掉 —— 那条任务已经属于另一次租约了。
  - **本人身份的 action 默认不在白名单。** 白名单默认只有 `app.message.send`;
    不带 `app.` 前缀的那几条是以你本人的身份说话,不是应用发通知。
  - **payload 用和 HTTP 路由同一套 schema 校验。** 校验只有一份,队列这条路
    不会悄悄比 HTTP 那条松。
"""
import asyncio
import contextlib
import json
import logging
import os
import socket
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import NamedTuple

import httpx
from pydantic import BaseModel, ValidationError

from . import local_group, local_send
from .agent import call_wecom
from .bgloop import BackgroundLoop
from .group import GroupError
from .images import materialized_image
from .local_send import LocalDataError, LocalSendError
from .mars_sync import MarsNotConfigured, MarsSyncError, group_number_text, mars_base
from .paths import AgentHomeError
from .schemas import (
    LocalCreateGroupRequest,
    LocalRecallTaskRequest,
    LocalRenameGroupRequest,
    LocalSendRequest,
    SendMessageRequest,
)
from .settings import Settings, get_settings
from .uilock import ClientBusy, ui_reserve

log = logging.getLogger(__name__)

TASKS_PATH = "/api/internal/wecom/tasks"
# 长轮询上限。前面 openresty 的 proxy_read_timeout 默认 60s,别顶着上限
MAX_WAIT = 50.0


class MarsTaskError(MarsSyncError):
    """拉取/回报失败(可预期,回 502)。"""


class MarsTasksNotConfigured(MarsNotConfigured):
    """没配 base_url 或 token(回 400)。"""


class StaleLease(MarsTaskError):
    """令牌过期(mars 回 409):这条任务已经被回收并重新认领,不再属于我们。

    拿到它只有一种正确反应:**丢掉这条任务**。既不重发也不反复重试 ——
    现在拥有它的是另一次租约,我们再插手只会两边都错。
    """


# ─── 任务 ───────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: int
    action: str
    payload: dict
    lease_token: str = ""
    attempts: int = 0
    expire_at: int = 0          # unix 秒,0 = 不过期

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        # payload 在 mars 侧是 json.RawMessage,响应里一定是个对象;不是对象就当空的,
        # 让 precheck 的 schema 校验去报错(那里的报错说得清缺了哪个字段)
        payload = d.get("payload")
        return cls(
            id=int(d.get("id") or 0),
            action=str(d.get("action") or ""),
            payload=payload if isinstance(payload, dict) else {},
            lease_token=str(d.get("lease_token") or ""),
            attempts=int(d.get("attempts") or 0),
            expire_at=int(d.get("expire_at") or 0),
        )


@dataclass
class Outcome:
    """一条任务的执行结果 —— 直接对应 ack 的 body。"""

    ok: bool
    retry: bool = False
    error: str = ""
    result: dict = field(default_factory=dict)

    def to_ack(self, lease_token: str = "") -> dict:
        return {
            "ok": self.ok,
            "retry": self.retry,
            "error": self.error,
            "result": self.result,
            "lease_token": lease_token,
        }


# ─── 动作表 ─────────────────────────────────────────────────────────────

async def _send_app_message(req: SendMessageRequest) -> dict:
    return await call_wecom("message", req.msgtype, req.to_agent_args())


async def _send_local_message(req: LocalSendRequest) -> dict:
    if req.msgtype == "image":
        def send_image() -> dict:
            with materialized_image(req) as image_path:
                return local_send.send_image(image_path=image_path, conv_rid=req.conv_rid)
        return await asyncio.to_thread(send_image)

    if req.mentions is None:
        return await asyncio.to_thread(
            local_send.send_message,
            to=req.to,
            content=req.content,
            conv_rid=req.conv_rid,
        )
    return await asyncio.to_thread(
        local_send.send_message,
        to=req.to,
        content=req.content,
        conv_rid=req.conv_rid,
        mentions=req.mentions,
    )


async def _create_local_group(req: LocalCreateGroupRequest) -> dict:
    result = await asyncio.to_thread(
        local_group.create_group,
        req.members,
        name=req.name,
        nickname=req.nickname,
        return_rid=True,
    )
    # mars 的 ack 会把 result 原样写回任务条目。用 group_id 暴露稳定的业务字段,
    # 同时保留 rid 兼容本机接口调用方。
    if isinstance(result, dict) and result.get("success") and result.get("rid") is not None:
        rid = int(result["rid"])
        result = {**result, "group_id": group_number_text("R:", rid)}
        message = await asyncio.to_thread(local_send.send_initial_message, rid, "1")
        result["message_result"] = message
        if not message.get("success"):
            result["message_error"] = message.get("error") or "发送初始化消息失败"
    elif isinstance(result, dict) and result.get("success"):
        result["message_error"] = "群已建好,但尚未定位到会话 RID,未发送初始化消息"
    return result


async def _rename_local_group(req: LocalRenameGroupRequest) -> dict:
    return await asyncio.to_thread(local_group.rename_group, req.conv_rid, req.name)


async def _recall_local_message(req: LocalRecallTaskRequest) -> dict:
    return await asyncio.to_thread(local_send.recall_message, req.conv_rid, req.lid)


class Action(NamedTuple):
    """一个 action 的全部三件事。schema 和 HTTP 路由用的是同一批。"""

    schema: type[BaseModel]
    run: object
    # needs_ui 要不要独占客户端界面。为真的在 /start **之前**先占锁(见 _run_task)——
    # 界面忙必须在 leased 阶段暴露,那时还能安全退回队列。
    #
    # 放在这里而不是另立一张按 action 名并行的表:并行表没有任何东西保证两边同步,
    # 而写成元组的一项,新增 action 时不回答这个问题**根本编译不过**。
    needs_ui: bool


# 命名和 HTTP 路由同构:`app.*` 是自建应用身份,不带前缀的是本人身份。
ACTIONS: dict[str, Action] = {
    "app.message.send": Action(SendMessageRequest, _send_app_message, False),  # 官方 API,不碰界面
    "message.send": Action(LocalSendRequest, _send_local_message, True),
    "message.recall": Action(LocalRecallTaskRequest, _recall_local_message, True),
    "group.create": Action(LocalCreateGroupRequest, _create_local_group, True),
    "group.rename": Action(LocalRenameGroupRequest, _rename_local_group, True),
}


def allowed_actions(settings: Settings) -> set[str]:
    raw = [a.strip() for a in settings.mars_tasks_actions.split(",") if a.strip()]
    if "*" in raw:
        return set(ACTIONS)
    return {a for a in raw if a in ACTIONS}


def _verdict(action: str, result: dict) -> Outcome:
    """把两套不同的成功语义收敛成一个结论。

    官方 API 回 {"errcode": 0, ...},本地发送回 {"success": true, ...} ——
    两边失败时 HTTP 都是 200,只看返回码不看 body 会把失败当成功回报给 mars。
    """
    if not isinstance(result, dict):
        return Outcome(False, error=f"{action} 返回了非预期结构: {result!r}"[:500])
    if result.get("errcode") not in (None, 0):
        return Outcome(False, error=str(result.get("errmsg") or result)[:500], result=result)
    if result.get("success") is False:
        return Outcome(False, error=str(result.get("error") or result)[:500], result=result)
    return Outcome(True, result=result)


def precheck(task: Task, settings: Settings) -> Outcome | tuple[BaseModel, Action]:
    """动手之前能判死的都在这里判。没有任何副作用。

    单独一步是因为这些任务根本不该进 executing 状态:它们一条消息都不会发,
    没必要让 mars 把它们记成「结果未知」。
    """
    if task.expire_at and time.time() > task.expire_at:
        return Outcome(False, error="任务已过期,未执行")

    entry = ACTIONS.get(task.action)
    if entry is None:
        return Outcome(False, error=f"不认识的 action: {task.action}")
    if task.action not in allowed_actions(settings):
        return Outcome(
            False,
            error=f"action「{task.action}」不在白名单里,未执行。"
                  f"当前允许: {', '.join(sorted(allowed_actions(settings))) or '(空)'}",
        )

    try:
        req = entry.schema.model_validate(task.payload)
    except ValidationError as e:
        return Outcome(False, error=f"payload 校验失败: {e.errors()[0].get('msg', e)}"[:500])
    return req, entry


async def run_handler(task: Task, req: BaseModel, entry: Action) -> Outcome:
    """真正动手的那一步。到这里为止的失败都还没产生副作用,从这里开始就有了。"""
    try:
        result = await entry.run(req)  # type: ignore[operator]
    except ClientBusy as e:
        # 到这里已经是 executing 了,**不能** retry:界面忙本该在 /start 之前由
        # ui_reserve 拦下(见 _run_task)。走到这条分支说明有别的东西绕过了预占,
        # 而此时无法证明消息没发出去,只能按发过处理。
        log.error("任务 %s 在 executing 阶段才报客户端忙,预占失效了: %s", task.id, e)
        return Outcome(False, error=f"客户端忙且已进入执行阶段,按已发送处理: {e}"[:500])
    except (LocalSendError, LocalDataError, AgentHomeError, GroupError, ValueError, OSError) as e:
        # 「这台机器现在做不了」(没登录、库没解密、参数不对)。重投到同一台机器
        # 上大概率还是同样的错,所以不 retry,把原文带回 mars 让人看见。
        return Outcome(False, error=str(e)[:500])
    except Exception as e:  # 兜底:任何意外都得有结论,不能把循环带崩
        log.exception("任务 %s(%s)执行异常", task.id, task.action)
        return Outcome(False, error=str(e)[:500])

    return _verdict(task.action, result)


# ─── 状态(含 inflight)────────────────────────────────────────────────

@dataclass
class State:
    done: int = 0
    failed: int = 0
    last_run: float = 0.0
    last_error: str = ""
    # 正在执行/等待回报的那一条。见模块文档「宁可漏发」
    inflight: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        inflight = d.get("inflight")
        return cls(
            done=int(d.get("done") or 0),
            failed=int(d.get("failed") or 0),
            last_run=float(d.get("last_run") or 0),
            last_error=str(d.get("last_error") or ""),
            inflight=inflight if isinstance(inflight, dict) else None,
        )


def load_state(settings: Settings | None = None) -> State:
    path = (settings or get_settings()).mars_tasks_state_file
    try:
        return State.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return State()


def save_state(state: State, settings: Settings | None = None) -> None:
    """原子落盘 —— 写临时文件 + fsync + rename。

    直接 write_text 是「先清空再写」:崩在中间就留下一个半截 JSON,而 load_state
    读不动就返回空状态 —— inflight 记录没了,那条任务会被重新执行一遍。
    这个文件存在的唯一目的就是熬过崩溃,所以它必须比普通配置写得更小心。
    """
    path = (settings or get_settings()).mars_tasks_state_file
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mars-tasks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # 目录项本身也要落盘,否则 rename 可能还在页缓存里
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ─── 与 mars 通信 ───────────────────────────────────────────────────────

def tasks_url(settings: Settings, suffix: str = "") -> str:
    base = mars_base(settings, MarsTasksNotConfigured)
    if not settings.mars_tasks_token.strip():
        raise MarsTasksNotConfigured(
            "未配置 mars 任务队列的 token。设置 WECOM_SVR_MARS_TASKS_TOKEN 后重试"
            "(要和 mars.json 的 wecom_task_token 一致)"
        )
    return base + TASKS_PATH + suffix


def worker_name(settings: Settings) -> str:
    return settings.mars_tasks_worker.strip() or socket.gethostname()


def _headers(settings: Settings) -> dict:
    return {"Authorization": f"Bearer {settings.mars_tasks_token.strip()}"}


def pull_wait(settings: Settings, wait: float | None = None) -> float:
    return max(0.0, min(MAX_WAIT, float(settings.mars_tasks_wait if wait is None else wait)))


async def _post(client: httpx.AsyncClient, url: str, body: dict, settings: Settings) -> dict:
    try:
        resp = await client.post(url, json=body, headers=_headers(settings))
    except httpx.HTTPError as e:
        raise MarsTaskError(f"调用 mars 失败: {e}") from e
    if resp.status_code == 409:
        raise StaleLease(f"租约已失效,这条任务已不属于本机: {resp.text.strip()[:200]}")
    if resp.status_code != 200:
        raise MarsTaskError(f"mars 返回 {resp.status_code}: {resp.text.strip()[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise MarsTaskError(f"mars 返回的不是 JSON: {resp.text.strip()[:200]}") from e
    return data if isinstance(data, dict) else {}


async def pull(client: httpx.AsyncClient, settings: Settings, wait: float | None = None) -> list[Task]:
    """认领一批任务。队列空时返回空列表(mars 那边是 200,不是 400)。"""
    body = {
        "worker": worker_name(settings),
        "max": int(settings.mars_tasks_max),
        "wait": pull_wait(settings, wait),
        "lease": int(settings.mars_tasks_lease),
    }
    data = await _post(client, tasks_url(settings, "/pull"), body, settings)
    return [Task.from_dict(t) for t in (data.get("tasks") or []) if isinstance(t, dict)]


async def start(client: httpx.AsyncClient, task: Task, settings: Settings) -> None:
    """告诉 mars「我要动手了」。必须在任何不可撤销的操作之前调。

    令牌过期抛 StaleLease —— 这时**绝不能**继续执行:这条任务已经被别人接手。
    """
    await _post(
        client,
        tasks_url(settings, "/start"),
        {"id": task.id, "lease_token": task.lease_token},
        settings,
    )


async def ack(
    client: httpx.AsyncClient, task_id: int, outcome: Outcome, lease_token: str, settings: Settings
) -> None:
    await _post(
        client,
        tasks_url(settings, "/ack"),
        {"id": task_id, **outcome.to_ack(lease_token)},
        settings,
    )


# ─── 跑一轮 ─────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    pulled: int = 0
    done: int = 0
    failed: int = 0
    settled: int = 0        # 补结的 inflight 条数
    dropped: int = 0        # 令牌过期被丢掉的条数
    busy: int = 0           # 因界面被占着而原样退回队列的条数
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


async def _persist(state: State, settings: Settings) -> None:
    """落盘,但别把 fsync 压在事件循环上。

    save_state 一次要 fsync 两回(文件 + 目录),而这个循环跟本机所有 HTTP 接口
    共用一个 loop —— 每条任务落盘四五次,没必要让别人跟着卡。
    """
    await asyncio.to_thread(save_state, state, settings)


def _outcome_from_saved(saved: dict) -> Outcome:
    kept = saved.get("result")
    return Outcome(
        ok=bool(saved.get("ok")),
        retry=bool(saved.get("retry")),
        error=str(saved.get("error") or ""),
        result=kept if isinstance(kept, dict) else {},
    )


async def _settle_inflight(client: httpx.AsyncClient, state: State, settings: Settings) -> None:
    """把上一轮留下的 inflight 结清 —— 只有一种情况:**结果已经有了,ack 没上去**。

    崩在执行途中的那种不落在这里,交给 mars 的 reaper:executing + 租约超时 →
    failed「结果未知」。本机再记一遍是冗余的,所以动手之前不落盘(见 run_once)。
    代价只是失败原因晚一个租约周期出现在 GET /tasks 上,结论完全一样。

    没有 outcome 还能走到这里的,只剩旧版本留下的状态文件。那时无法证明副作用
    没发生,一律认赔 —— 宁可漏发。
    """
    flight = state.inflight
    if not flight:
        return

    saved = flight.get("outcome")
    if isinstance(saved, dict):
        outcome = _outcome_from_saved(saved)
    else:
        # 只可能是旧版本(动手前落盘那版)留下的状态文件。吼一声 —— 否则这条任务
        # 会静默地被结成 failed,排障时分不清是真崩了还是撞上了老格式
        log.warning("inflight 没有 outcome(旧版状态文件),按已发送结案: %s", flight)
        outcome = Outcome(
            False,
            error=f"worker 退出时 {flight.get('action')} 结果未知(无 outcome 记录),"
                  "按已发送处理,不重投",
        )

    try:
        await ack(client, int(flight.get("id") or 0), outcome, str(flight.get("token") or ""), settings)
    except StaleLease as e:
        # 已经被回收并重新认领了,这次回报不算数。丢掉本地记录就行
        log.warning("补报任务 %s 时租约已失效,丢弃: %s", flight.get("id"), e)
        state.inflight = None
        await _persist(state, settings)
        return

    state.inflight = None
    if outcome.ok:
        state.done += 1
    else:
        state.failed += 1
    await _persist(state, settings)


# run_once 的互斥闸。见 run_once 的 docstring:并发跑会重复发送。
_RUN_LOCK = asyncio.Lock()


async def _run_task(
    client: httpx.AsyncClient,
    task: Task,
    checked: tuple[BaseModel, Action],
    settings: Settings,
    result: RunResult,
) -> Outcome | None:
    """占住界面 → `/start` → 执行。返回 None = 租约失效,这条不归本机了。

    **占界面在 `/start` 之前**是这套设计的支点:界面忙必须在 leased 阶段暴露 ——
    那时一条消息都没发过,退回队列是安全的,而这是协议里唯一安全的 retry。
    等进了 executing 再喊忙,mars 就分不清「没发」和「发了一半」,只能按发过处理。
    """
    _, entry = checked
    with contextlib.ExitStack() as ui:
        if entry.needs_ui:
            try:
                ui.enter_context(ui_reserve(f"任务 {task.id} {task.action}"))
            except ClientBusy as e:
                result.busy += 1
                return Outcome(False, retry=True, error=str(e))

        try:
            await start(client, task, settings)
        except StaleLease as e:
            # 这条已经属于另一次租约了,绝不能再执行
            log.warning("任务 %s 的租约已失效,跳过: %s", task.id, e)
            result.dropped += 1
            return None

        # 进了 executing 才开始有副作用。这之后本机不再记「做到哪了」——
        # 崩了由 mars 的 reaper 判「结果未知」,本机再记一遍是冗余的。
        return await run_handler(task, *checked)


async def run_once(settings: Settings | None = None, wait: float | None = None) -> RunResult:
    """结清上一轮 → 拉一批 → 逐条执行并回报。

    任何一步跟 mars 通信失败都就地停下:任务还在 mars 那边(pending 或租约中),
    下一轮重来。

    **全程互斥**:两个 run_once 并发跑会重复发送 —— 后进来的那个 load_state 读到
    前一个刚落盘的 inflight,把它当成崩溃残留补报掉,任务被放回队列又被领一次,
    而前一个还在发。锁是这条路上唯一拦得住它的东西(令牌拦不住:两边用的是同一个)。
    """
    settings = settings or get_settings()
    async with _RUN_LOCK:
        return await _run_once_locked(settings, wait)


async def _run_once_locked(settings: Settings, wait: float | None) -> RunResult:
    result = RunResult()
    state = load_state(settings)
    seconds = pull_wait(settings, wait)

    # 客户端超时必须盖过 wait,否则表现是「每 wait 秒一次超时」
    async with loop.http_client(seconds + settings.mars_timeout) as client:
        if state.inflight:
            await _settle_inflight(client, state, settings)
            result.settled = 1

        tasks = await pull(client, settings, seconds)
        result.pulled = len(tasks)

        for task in tasks:
            # 1. 判死:过期/不认识/不在白名单/payload 不合法。这些一条消息都不会发,
            #    没必要让 mars 把它们记成「结果未知」,所以在 /start 之前判
            checked = precheck(task, settings)

            # 显式声明可空: 判死那条路给的是 Outcome,执行那条路还可能给 None
            # (租约失效)。少了这行,mypy 按第一次赋值把它定成 Outcome,下面那句就报错。
            outcome: Outcome | None
            if isinstance(checked, Outcome):
                outcome = checked        # 判死了,不用动手,也不必进 executing
            else:
                outcome = await _run_task(client, task, checked, settings, result)
                if outcome is None:
                    continue             # 租约失效,这条已经不归本机了

            # 4. 有结论了才落盘。这个文件的唯一用途:结果已经有了,ack 还没上去
            state.inflight = {
                "id": task.id, "token": task.lease_token, "action": task.action,
                "outcome": outcome.to_ack(),
            }
            await _persist(state, settings)

            try:
                await ack(client, task.id, outcome, task.lease_token, settings)
            except StaleLease as e:
                log.warning("任务 %s 回报时租约已失效,丢弃本次结果: %s", task.id, e)
                state.inflight = None
                await _persist(state, settings)
                result.dropped += 1
                continue
            except MarsTaskError as e:
                # inflight 留着,下一轮补 ack —— 结果已经落盘,不会重复执行
                result.error = str(e)
                log.warning("任务 %s 回报失败,留待下轮补报: %s", task.id, e)
                break

            state.inflight = None
            if outcome.ok:
                state.done += 1
                result.done += 1
            else:
                state.failed += 1
                result.failed += 1
                log.warning("任务 %s(%s)失败: %s", task.id, task.action, outcome.error)
            await _persist(state, settings)

    state.last_run = time.time()
    state.last_error = result.error
    await _persist(state, settings)
    return result


# ─── 后台循环 ───────────────────────────────────────────────────────────

class TaskLoop(BackgroundLoop):
    """常驻拉取任务。长轮询本身就是节流,正常轮次之间不 sleep。"""

    name = "mars-tasks"

    def __init__(self) -> None:
        super().__init__()
        self.last_result: RunResult | None = None

    async def _run(self) -> None:
        settings = get_settings()
        seconds = pull_wait(settings)
        idle = max(0.5, float(settings.mars_tasks_idle))
        log.info("mars 任务拉取已启动,worker=%s 长轮询 %.0fs", worker_name(settings), seconds)
        self.client = httpx.AsyncClient(timeout=seconds + settings.mars_timeout)
        while True:
            # 出错、或有任务因界面忙退回 —— 两种都要歇。正常轮次不歇:pull 自己
            # 会阻塞住 wait 秒。只看**本轮**的结论,不读 self.last_result ——
            # 那个字段是给 /status 展示用的,异常轮次里它还是上一轮的对象。
            idle_now = True
            try:
                self.last_result = result = await run_once(settings)
                self.last_error = result.error
                idle_now = bool(result.error or result.busy)
            except asyncio.CancelledError:
                raise
            except MarsSyncError as e:
                # 配置类/下游错误:说清楚就行,别刷屏
                if self.last_error != str(e):
                    log.warning("mars 任务拉取暂停: %s", e)
                self.last_error = str(e)
            except Exception as e:
                if self.last_error != str(e):
                    log.exception("mars 任务拉取异常: %s", e)
                self.last_error = str(e)
            if idle_now:
                await asyncio.sleep(idle)


loop = TaskLoop()
