"""客户端 UI 的独占闸门 —— 所有操控企业微信界面的操作都必须过这里。

发消息、发图、打开会话、拉群、改群名操作的是**同一个客户端界面**: 抢焦点、
抢剪贴板、抢键盘。所以它们必须串行,这一点从一开始就靠一把全局锁保证。

这里要解决的是「串行」之外的两件事:

  1. **默认不排队,直接拒**。裸 `with lock:` 没有超时,调用方 HTTP 超时走人后,
     线程仍然在排队 —— 轮到它时照样会去操控客户端。用户看到的是「请求失败了」,
     然后几分钟后屏幕毫无预兆地弹出选人面板开始拉群。这种幽灵操作比失败糟得多,
     所以默认 `BUSY_TIMEOUT = 0`: 拿不到锁立刻回 409,重试交给调用方。
  2. **正在干什么要能看见**。占用可能长达一分钟(建群要逐个搜成员),调用方
     得能问「现在忙不忙、忙什么、忙多久了」,而不是靠猜。

想改成有界等待就把 BUSY_TIMEOUT 调大 —— 但要清楚代价: 等待期间调用方可能早
已放弃,幽灵操作的窗口就是这么来的。
"""
import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .localdb import LocalSendError

# 0 = 不等,拿不到就拒。见模块文档里为什么这是默认值。
BUSY_TIMEOUT = 0.0

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()

# 「当前这条执行流已经持有 UI 了」—— 存的是那一次占用的 Busy 对象,不是一个 True。
#
# 用 ContextVar 而不是「重入锁」是有意的:重入是按**线程**算的,而这里的调用链是
# 异步的 —— run_once 在事件循环里占锁,真正操作界面的 local_send 跑在
# asyncio.to_thread 的工作线程上,两者不是同一个线程,RLock 认不出来。
# ContextVar 反过来正好:to_thread 会 copy_context,所以标记跟着调用链走,
# 而并发进来的其它 HTTP 请求各有各的 context,看到的仍是「没持有」,照常撞锁回 409。
#
# 存对象而不是 True,是为了让放行**可验证**:context 会被拷贝,一份逃逸出去的
# 拷贝(比如 handler 里起了 create_task)带着 True 就能一路走过 acquire,那时
# 根本没人持锁 —— 正是这个模块开头要防的幽灵操作。存对象就能跟当前占用者对身份,
# 对不上说明这份标记是陈的,老老实实回去抢锁。
_HELD: contextvars.ContextVar["Busy | None"] = contextvars.ContextVar("ui_lock_held", default=None)


class ClientBusy(LocalSendError):
    """客户端正被另一个操作占用(可重试,映射成 409)。"""


@dataclass(frozen=True)
class Busy:
    operation: str
    started: float          # time.monotonic() 的起点

    @property
    def seconds(self) -> float:
        """已经占用了多久。"""
        return round(time.monotonic() - self.started, 1)


_current: Busy | None = None


def current() -> Busy | None:
    """当前占用者(没有则 None)。"""
    with _STATE_LOCK:
        return _current


@contextmanager
def ui_session(operation: str, timeout: float | None = None):
    """独占客户端界面。拿不到就抛 ClientBusy,绝不无限期排队。

    Args:
        operation: 干什么用的,会出现在「忙什么」里和 409 的错误文案里
        timeout: 等多久;None 用 BUSY_TIMEOUT
    """
    global _current

    held = _HELD.get()
    if held is not None and held is current():
        # 本执行流已经通过 ui_reserve 占住界面了,这里再抢就是自己撞自己。
        # 对身份:标记必须指向**当前**这次占用,陈的标记一律当没有(见 _HELD)
        yield
        return

    wait = BUSY_TIMEOUT if timeout is None else timeout
    got = _LOCK.acquire(timeout=wait) if wait > 0 else _LOCK.acquire(blocking=False)
    if not got:
        busy = current()
        detail = f"(正在{busy.operation},已 {busy.seconds}s)" if busy else ""
        raise ClientBusy(
            f"客户端正被另一个操作占用{detail},本次「{operation}」未执行。"
            "这类操作要独占界面,请稍后重试。"
        )

    with _STATE_LOCK:
        _current = Busy(operation, time.monotonic())
    try:
        yield
    finally:
        with _STATE_LOCK:
            _current = None
        _LOCK.release()


@contextmanager
def ui_reserve(operation: str, timeout: float | None = None):
    """先占住界面,再决定要不要动手 —— 拿不到就在**产生任何副作用之前**抛 ClientBusy。

    给 mars 任务队列用的。那条路上「客户端忙」必须在告诉 mars「我要动手了」之前
    暴露出来:那时任务还是 leased,什么都没发生,退回队列是安全的。等进了
    executing 再喊忙,mars 就分不清「没发」和「发了一半」,只能按发过处理。

    占住之后调用链里嵌套的 ui_session 全部放行(见 _HELD),所以 handler 不用改。
    """
    with ui_session(operation, timeout):
        token = _HELD.set(current())
        try:
            yield
        finally:
            _HELD.reset(token)
