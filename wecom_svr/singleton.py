"""单实例保护:先占端口和文件锁,再让 Uvicorn 跑 lifespan。

Uvicorn 0.52 的启动顺序是 lifespan → bind。本服务的 lifespan 会打开企业微信
「发起群聊」面板做 UI 预检。端口已被占用时,进程会在弹完框之后才以 exit 3
退出;再配上 `launchctl submit` 默认的 KeepAlive,就会变成「不停弹框」。

入口必须在启动应用之前先把监听套接字拿到手。拿不到就立刻退出,不要去碰客户端。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Windows 没有 fcntl;本服务的 UI 路径本来就只在 macOS 上有意义,
# 单实例锁在非 POSIX 上降级为「只抢端口」。
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class InstanceTaken(RuntimeError):
    """本机已经有一份服务在跑,或端口被别人占了。"""

    def __init__(self, message: str, *, ours: bool) -> None:
        super().__init__(message)
        self.ours = ours


@dataclass
class ClaimedInstance:
    host: str
    port: int
    sock: socket.socket
    lock_fd: int | None
    lock_path: Path | None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass


def lock_path(data_dir: Path) -> Path:
    return data_dir / "wecom-svr.lock"


def try_lock(data_dir: Path) -> int | None:
    """非阻塞独占锁。拿不到返回 None;没有 fcntl 则跳过(返回 -1 表示未启用)。"""
    if fcntl is None:
        return -1
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def listen_socket(host: str, port: int) -> socket.socket:
    """立刻 bind+listen,避免只 bind 时 SO_REUSEADDR 让第二份也 bind 成功。"""
    family = socket.AF_INET6 if host and ":" in host else socket.AF_INET
    sock = socket.socket(family=family)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
    sock.bind((host, port))
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


def _probe_host(host: str) -> str:
    if host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return host


def describe_listener(host: str, port: int, timeout: float = 0.4) -> tuple[bool, str]:
    """探一下占用方是不是自己。返回 (is_ours, detail)。"""
    probe = _probe_host(host)
    url_host = f"[{probe}]" if ":" in probe else probe
    url = f"http://{url_host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return False, f"{host}:{port} 已被占用,且不是可识别的 wecom-svr"
    if isinstance(body, dict) and body.get("service") == "wecom-message-api":
        detail = body.get("detail")
        extra = f", {detail}" if detail else ""
        return True, f"wecom-svr 已在 {host}:{port} 运行{extra}"
    return False, f"{host}:{port} 已被其它 HTTP 服务占用"


def claim(host: str, port: int, data_dir: Path) -> ClaimedInstance:
    """先抢文件锁,再抢端口。失败抛 InstanceTaken,不会碰到企业微信。

    锁在前是因为 macOS 上 SO_REUSEADDR 可能让两份进程都 bind 成功;
    文件锁才是真正的互斥。端口被别人占了则立刻放锁退出。
    """
    path = lock_path(data_dir)
    lock_fd = try_lock(data_dir)
    if lock_fd is None:
        raise InstanceTaken(
            f"另一份 wecom-svr 正在运行(锁文件 {path}),本次启动已放弃,不会再去操控企业微信",
            ours=True,
        )

    try:
        sock = listen_socket(host, port)
    except OSError as e:
        if lock_fd is not None and lock_fd >= 0:
            os.close(lock_fd)
        ours, detail = describe_listener(host, port)
        raise InstanceTaken(f"{detail} ({e})", ours=ours) from e

    return ClaimedInstance(
        host=host,
        port=port,
        sock=sock,
        lock_fd=None if lock_fd == -1 else lock_fd,
        lock_path=path,
    )
