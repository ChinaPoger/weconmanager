"""接口契约测试 —— 不触碰真实企业微信客户端/官方 API。"""
import pytest
from fastapi.testclient import TestClient

from wecom_svr import local_send
from wecom_svr.main import create_app
from wecom_svr.settings import get_agent_home, get_settings


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """默认无鉴权、agent home 指向 submodule。"""
    monkeypatch.delenv("WECOM_SVR_API_TOKEN", raising=False)
    monkeypatch.delenv("WECOM_AGENT_HOME", raising=False)
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_home.cache_clear()


@pytest.fixture
def client():
    return TestClient(create_app())


def test_health_reports_agent_home(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["service"] == "wecom-message-api"


def test_missing_touser_returns_400_official_shape(client):
    r = client.post("/api/v1/app/message/send", json={"content": "hi"})
    assert r.status_code == 400
    assert r.json()["errcode"] == -1


def test_news_without_articles_rejected(client):
    r = client.post("/api/v1/app/message/send", json={"touser": "u", "msgtype": "news"})
    assert r.status_code == 400
    assert "articles" in r.json()["errmsg"]


def test_unknown_msgtype_rejected(client):
    r = client.post("/api/v1/app/message/send", json={"touser": "u", "msgtype": "video", "content": "x"})
    assert r.status_code == 400


def test_message_send_passes_through_agent_response(client, monkeypatch):
    seen = {}

    async def fake_call(category, method, args):
        seen.update(category=category, method=method, args=args)
        return {"errcode": 0, "errmsg": "ok", "msgid": "abc"}

    monkeypatch.setattr("wecom_svr.routers.message.call_wecom", fake_call)
    r = client.post("/api/v1/app/message/send", json={"touser": "zhangsan", "content": "hi"})

    assert r.status_code == 200
    assert r.json() == {"errcode": 0, "errmsg": "ok", "msgid": "abc"}
    assert seen == {
        "category": "message",
        "method": "text",
        "args": {"touser": "zhangsan", "content": "hi"},
    }


def test_contact_search_requires_keyword(client):
    r = client.post("/api/v1/app/contact/search", json={})
    assert r.status_code == 400
    assert r.json()["errcode"] == -1


# ─── local: 本人身份 ────────────────────────────────────────────────────

def test_local_send_requires_target_local_shape(client):
    r = client.post("/api/v1/message/send", json={"content": "hi"})
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert "conv_rid" in body["error"]


def test_local_send_requires_content(client):
    r = client.post("/api/v1/message/send", json={"to": "群"})
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_local_send_failure_is_400_with_body(client, monkeypatch):
    monkeypatch.setattr(
        local_send, "send_message",
        lambda **kw: {"success": False, "error": "未找到: '群'", "suggestions": []},
    )
    r = client.post("/api/v1/message/send", json={"to": "群", "content": "hi"})
    assert r.status_code == 400
    assert r.json()["error"] == "未找到: '群'"


def test_local_send_success(client, monkeypatch):
    seen = {}

    def fake_send(**kw):
        seen.update(kw)
        return {"success": True, "conv_name": "测试群", "conv_rid": kw["conv_rid"],
                "open_method": "url_scheme", "error": None}

    monkeypatch.setattr(
        local_send, "send_message", fake_send,
    )
    r = client.post("/api/v1/message/send", json={
        "conv_rid": 123, "content": "@abz", "mentions": ["abz"],
    })
    assert r.status_code == 200
    assert r.json()["conv_name"] == "测试群"
    assert seen["mentions"] == ["abz"]


def test_local_data_error_maps_to_503(client, monkeypatch):
    def boom(_keyword=""):
        raise local_send.LocalDataError("数据未同步")

    monkeypatch.setattr(local_send, "search_conversations", boom)
    r = client.post("/api/v1/conversations", json={"keyword": "x"})
    assert r.status_code == 503
    assert r.json() == {"success": False, "error": "数据未同步"}


def test_conversations_body_optional(client, monkeypatch):
    monkeypatch.setattr(
        local_send, "search_conversations",
        lambda keyword="": [{"name": "群", "rid": 1, "lid": 2, "type": 1, "fw_id": 0}],
    )
    r = client.post("/api/v1/conversations")
    assert r.status_code == 200
    assert r.json()["conversations"][0]["name"] == "群"


def test_image_requires_exactly_one_source(client):
    r = client.post("/api/v1/message/send", json={"conv_rid": 1, "msgtype": "image"})
    assert r.status_code == 400
    assert "必须且只能" in r.json()["error"]

    r = client.post("/api/v1/message/send", json={
        "conv_rid": 1, "msgtype": "image", "image_path": "/tmp/a.png",
        "image_url": "https://example.com/a.png",
    })
    assert r.status_code == 400


def test_image_base64_is_written_to_a_real_file(client, monkeypatch):
    captured = {}

    def fake_send_image(image_path, conv_rid):
        with open(image_path, "rb") as f:
            captured["bytes"] = f.read()
        return {"success": True, "conv_name": "群", "conv_rid": conv_rid, "error": None}

    monkeypatch.setattr(local_send, "send_image", fake_send_image)
    r = client.post("/api/v1/message/send", json={
        "conv_rid": 1, "msgtype": "image", "image_base64": "aGVsbG8=", "filename": "a.png",
    })
    assert r.status_code == 200
    assert captured["bytes"] == b"hello"


def test_bad_base64_rejected(client):
    r = client.post("/api/v1/message/send", json={
        "conv_rid": 1, "msgtype": "image", "image_base64": "!!!!",
    })
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_old_image_endpoint_is_removed(client):
    r = client.post("/api/v1/image/send", json={"conv_rid": 1, "image_path": "/tmp/a.png"})
    assert r.status_code == 404


# ─── 鉴权 ──────────────────────────────────────────────────────────────

def test_auth_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("WECOM_SVR_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    client = TestClient(create_app())

    r = client.post("/api/v1/app/message/send", json={"touser": "u", "content": "hi"})
    assert r.status_code == 401
    assert r.json() == {"errcode": -1, "errmsg": "unauthorized"}


def test_auth_accepts_bearer_token(monkeypatch):
    monkeypatch.setenv("WECOM_SVR_API_TOKEN", "s3cret")
    get_settings.cache_clear()

    async def fake_call(category, method, args):
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr("wecom_svr.routers.message.call_wecom", fake_call)
    client = TestClient(create_app())

    r = client.post(
        "/api/v1/app/message/send",
        json={"touser": "u", "content": "hi"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 200


def test_health_is_public_even_with_auth(monkeypatch):
    monkeypatch.setenv("WECOM_SVR_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    assert TestClient(create_app()).get("/health").status_code == 200
