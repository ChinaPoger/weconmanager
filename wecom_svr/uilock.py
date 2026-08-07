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
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .localdb import LocalSendError

# 0 = 不等,拿不到就拒。见模块文档里为什么这是默认值。
BUSY_TIMEOUT = 0.0

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


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
