"""后台常驻循环的骨架。

`mars_sync`(往 mars 推群消息)和 `mars_tasks`(从 mars 拉任务)是两条方向相反的
通道,但「起停一个 asyncio task」和「持有一个常驻连接池」这两件事两边一模一样。
真正不同的只有一轮做什么、轮次之间怎么等,那两件留给子类的 `_run`。
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import httpx

log = logging.getLogger(__name__)


class BackgroundLoop:
    """一个可起停的常驻循环 + 一个懒建的 HTTP 客户端。

    子类必须实现 `_run`,并把 `name` 设成 asyncio task 名(排障时能看出是谁)。
    """

    name = "background"

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_error: str = ""
        # 常驻连接池,循环跑起来才建 —— 见 http_client
        self.client: httpx.AsyncClient | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name=self.name)

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

    @asynccontextmanager
    async def http_client(self, timeout: float):
        """拿一个客户端用。

        循环跑起来了就复用常驻的那个: 每轮新建等于每轮重握一次 TCP+TLS,
        短间隔轮询下 keep-alive 基本白设。一次性调用(/run、测试)就地建一个。
        """
        if self.client is not None:
            yield self.client
            return
        async with httpx.AsyncClient(timeout=timeout) as client:
            yield client

    async def _run(self) -> None:
        raise NotImplementedError
