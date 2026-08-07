"""UI 独占闸门 —— 并发下必须「拒绝」而不是「排队」。

这里测的是一条真实教训: 裸锁会让 HTTP 早就超时走人的请求继续排队,轮到它时
仍然去操控客户端。用户看到请求失败,几分钟后屏幕自己弹面板 —— 必须拒绝掉。
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from wecom_svr import local_send, uilock
from wecom_svr.main import create_app
from wecom_svr.settings import get_agent_home, get_settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("WECOM_SVR_API_TOKEN", raising=False)
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_home.cache_clear()


def test_second_caller_is_rejected_not_queued():
    with uilock.ui_session("拉群"):
        with pytest.raises(uilock.ClientBusy) as e:
            with uilock.ui_session("发消息"):
                pytest.fail("不该拿到锁")
    assert "拉群" in str(e.value)      # 说清楚是被谁占着
    assert "发消息" in str(e.value)    # 也说清楚是谁被拒了


def test_lock_is_released_even_when_the_body_raises():
    with pytest.raises(RuntimeError), uilock.ui_session("发消息"):
        raise RuntimeError("boom")
    assert uilock.current() is None
    with uilock.ui_session("发消息"):    # 还能再拿到
        pass


def test_current_reports_what_is_running():
    assert uilock.current() is None
    with uilock.ui_session("拉群"):
        busy = uilock.current()
        assert busy is not None
        assert busy.operation == "拉群"
        assert busy.seconds >= 0
    assert uilock.current() is None


def test_waiting_is_opt_in_and_bounded(monkeypatch):
    """把 timeout 调大才会等 —— 而且等不到照样拒,不会无限期挂着。"""
    started = threading.Event()

    def hold():
        with uilock.ui_session("拉群"):
            started.set()
            time.sleep(0.6)

    t = threading.Thread(target=hold)
    t.start()
    assert started.wait(2)
    try:
        t0 = time.monotonic()
        with pytest.raises(uilock.ClientBusy):
            with uilock.ui_session("发消息", timeout=0.15):
                pytest.fail("不该拿到锁")
        assert 0.1 <= time.monotonic() - t0 < 0.5   # 等了,但有界
    finally:
        t.join()


def test_send_while_busy_returns_409_not_400(monkeypatch):
    """409 是可重试信号,和「参数错」的 400 必须区分开。"""
    # 先把本地库查询桩掉: send_message 会先解析会话再抢闸门(参数不对就不该占界面)
    monkeypatch.setattr(local_send, "ensure_macos", lambda: None)
    monkeypatch.setattr(local_send, "resolve_conversation_name",
                        lambda ident, rid=None: {"name": "群", "rid": rid, "lid": 1,
                                                 "type": 1, "fw_id": 0, "skip": 0})
    client = TestClient(create_app())
    with uilock.ui_session("拉群"):
        r = client.post("/api/v1/local/message/send", json={"conv_rid": 1, "content": "hi"})
    assert r.status_code == 409
    body = r.json()
    assert body["success"] is False
    assert "拉群" in body["error"]


def test_busy_endpoint_reflects_state():
    client = TestClient(create_app())
    assert client.get("/api/v1/local/busy").json() == {
        "success": True, "busy": False, "operation": None, "seconds": 0,
    }
    with uilock.ui_session("拉群"):
        body = client.get("/api/v1/local/busy").json()
    assert body["busy"] is True and body["operation"] == "拉群"


def test_send_message_takes_the_gate(monkeypatch):
    """发消息真的走了闸门,而不是绕过去。"""
    seen = {}

    def fake_send(conv, content):
        seen["busy"] = uilock.current()
        return True, "ok"

    monkeypatch.setattr(local_send, "ensure_macos", lambda: None)
    monkeypatch.setattr(local_send, "resolve_conversation_name",
                        lambda ident, rid=None: {"name": "群", "rid": rid, "lid": 1,
                                                 "type": 1, "fw_id": 0, "skip": 0})
    monkeypatch.setattr(local_send, "_send_by_scheme", fake_send)

    local_send.send_message(conv_rid=1, content="hi")
    assert seen["busy"] is not None
    assert seen["busy"].operation == "发消息"
