"""单实例保护:端口占用时必须立刻退出,不能先去弹企业微信。"""
import json
import logging.handlers
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wecom_svr.singleton import InstanceTaken, claim, describe_listener, listen_socket, try_lock


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_second_claim_on_same_lock_is_treated_as_our_instance(tmp_path):
    """两份服务即使用不同端口,也不能同时去操控同一个客户端。"""
    first = claim("127.0.0.1", _free_port(), tmp_path)
    try:
        with pytest.raises(InstanceTaken, match="锁文件") as caught:
            claim("127.0.0.1", _free_port(), tmp_path)
        assert caught.value.ours is True
    finally:
        first.close()


def test_same_port_with_a_different_lock_is_still_rejected(tmp_path):
    port = _free_port()
    first = claim("127.0.0.1", port, tmp_path / "a")
    try:
        with pytest.raises(InstanceTaken, match="已被占用") as caught:
            claim("127.0.0.1", port, tmp_path / "b")
        assert caught.value.ours is False
    finally:
        first.close()


def test_lock_is_released_so_a_later_start_can_take_over(tmp_path):
    port = _free_port()
    first = claim("127.0.0.1", port, tmp_path)
    first.close()
    second = claim("127.0.0.1", port, tmp_path)
    second.close()


def test_try_lock_is_nonblocking(tmp_path):
    fd = try_lock(tmp_path)
    assert fd is not None and fd >= 0
    try:
        assert try_lock(tmp_path) is None
    finally:
        import os
        os.close(fd)
    assert try_lock(tmp_path) is not None


def test_describe_listener_recognizes_our_health_endpoint():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"service": "wecom-message-api", "status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ours, detail = describe_listener("127.0.0.1", port)
        assert ours is True
        assert "wecom-svr 已在" in detail
    finally:
        server.shutdown()
        server.server_close()


def test_describe_listener_does_not_claim_foreign_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"service":"something-else"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ours, detail = describe_listener("127.0.0.1", port)
        assert ours is False
        assert "其它 HTTP" in detail
    finally:
        server.shutdown()
        server.server_close()


def test_listen_socket_holds_the_port_against_another_bind():
    port = _free_port()
    sock = listen_socket("127.0.0.1", port)
    try:
        with pytest.raises(OSError):
            listen_socket("127.0.0.1", port)
    finally:
        sock.close()


def _settings(tmp_path, **updates):
    values = {
        "host": "127.0.0.1",
        "port": 8090,
        "corpid": "c",
        "secret": "s",
        "auth_enabled": True,
        "data_dir": tmp_path,
        "log_file": None,
        **updates,
    }
    return type("S", (), values)()


def test_main_exits_zero_when_our_instance_already_holds_the_lock(tmp_path, monkeypatch, capsys):
    from wecom_svr import __main__ as entry

    first = claim("127.0.0.1", _free_port(), tmp_path)
    monkeypatch.setattr(entry.sys, "argv", ["wecom-svr", "--port", str(_free_port())])
    monkeypatch.setattr(entry, "get_settings", lambda: _settings(tmp_path))
    def boom(*a, **kw):
        raise AssertionError("不该启动")

    monkeypatch.setattr(entry.Server, "run", boom)
    try:
        with pytest.raises(SystemExit) as caught:
            entry.main()
        assert caught.value.code == 0
        assert "不会再去操控企业微信" in capsys.readouterr().out
    finally:
        first.close()


def test_main_hands_the_claimed_socket_to_uvicorn(tmp_path, monkeypatch):
    from wecom_svr import __main__ as entry

    seen = {}

    def fake_run(self, sockets=None):
        assert sockets
        seen["port"] = sockets[0].getsockname()[1]

    port = _free_port()
    monkeypatch.setattr(entry.sys, "argv", ["wecom-svr", "--port", str(port)])
    monkeypatch.setattr(entry, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(entry.Server, "run", fake_run)

    entry.main()

    assert seen["port"] == port


def test_log_handlers_rotate_to_a_file_when_configured(tmp_path):
    """launchd 重定向的 stderr 没人轮转;配了 log_file 就不能再往 stderr 写一份。"""
    from wecom_svr import __main__ as entry

    plain = entry._log_handlers(_settings(tmp_path))
    assert len(plain) == 1 and not isinstance(plain[0], logging.handlers.RotatingFileHandler)

    rotated = entry._log_handlers(
        _settings(tmp_path, log_file=tmp_path / "svc.log", log_max_bytes=128, log_backups=2)
    )
    assert len(rotated) == 1
    assert isinstance(rotated[0], logging.handlers.RotatingFileHandler)
    assert (rotated[0].maxBytes, rotated[0].backupCount) == (128, 2)


def test_a_blank_log_file_env_var_is_treated_as_unset(monkeypatch, tmp_path):
    """`.env` 里的 `WECOM_SVR_LOG_FILE=` 会被解析成 Path('.'),不能让启动期拿目录当文件。"""
    from wecom_svr.settings import Settings

    monkeypatch.setenv("WECOM_SVR_LOG_FILE", "")
    assert Settings(data_dir=tmp_path).log_file is None
