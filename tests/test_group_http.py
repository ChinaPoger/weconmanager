"""Array-based group creation contract, with all UI operations stubbed."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wecom_svr import security
from wecom_svr.main import create_app
from wecom_svr.routers import local
from wecom_svr.uilock import ClientBusy


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(local.local_group, "create_group", lambda *a, **kw: pytest.fail("legacy UI"))
    monkeypatch.setattr(local.local_send, "send_initial_message", lambda *a, **kw: pytest.fail("no message"))
    monkeypatch.setattr(local.windows_group, "create_group", lambda *a, **kw: pytest.fail("unstubbed UI"))
    app = create_app()
    app.dependency_overrides[security.require_token] = lambda: None
    return TestClient(app)


def test_name_array_is_trimmed_and_verification_fields_survive_http(client, monkeypatch):
    seen = {}
    result = {"success": True, "verified": True, "name_verified": True, "reused": True,
              "rid": 3423, "name": "三人组", "member_count": 3,
              "member_userids": ["1", "2", "3"], "error": None}

    def create(members, **kwargs):
        seen.update(members=members, **kwargs)
        return result

    monkeypatch.setattr(local.windows_group, "create_group", create)
    response = client.post("/api/v1/group/create", json={
        "members": [" 伴风望海 ", "陈涛@技术支持部", "葛波"], "name": "三人组",
    })
    assert response.status_code == 200 and response.json() == result
    assert seen == {"members": ["伴风望海", "陈涛@技术支持部", "葛波"],
                    "name": "三人组", "nickname": "", "prepare_only": False}


@pytest.mark.parametrize("body", [
    {}, {"members": "陈涛,伴风望海"}, {"members": []}, {"members": ["陈涛"]},
    {"members": ["陈涛", " "]}, {"members": ["陈涛", 123]},
    {"members": ["陈涛", None]}, {"members": ["陈涛", "伴风望海"], "prepare_only": "false"},
])
def test_invalid_array_rejected_before_ui(client, body):
    response = client.post("/api/v1/group/create", json=body)
    assert response.status_code == 400
    assert response.json()["success"] is False and response.json()["error"]


def test_prepare_only_is_forwarded_without_claiming_creation(client, monkeypatch):
    def prepare(members, *, name, nickname, prepare_only):
        assert prepare_only is True
        return {"success": True, "prepared": True, "submitted": False}

    monkeypatch.setattr(local.windows_group, "create_group", prepare)
    response = client.post("/api/v1/group/create", json={"members": ["甲", "乙"], "prepare_only": True})
    assert response.status_code == 200 and response.json()["submitted"] is False


def test_ambiguous_member_returns_backend_candidates(client, monkeypatch):
    monkeypatch.setattr(local.windows_group, "create_group", lambda *a, **kw: {
        "success": False, "verified": False, "error": "陈涛有同名候选,请用 姓名@部门",
    })
    response = client.post("/api/v1/group/create", json={"members": ["陈涛", "伴风望海"]})
    assert response.status_code == 400 and "姓名@部门" in response.json()["error"]


def test_busy_client_returns_409(client, monkeypatch):
    def busy(*args, **kwargs):
        raise ClientBusy("客户端正被另一个操作占用")

    monkeypatch.setattr(local.windows_group, "create_group", busy)
    response = client.post("/api/v1/group/create", json={"members": ["甲", "乙"]})
    assert response.status_code == 409 and response.json()["success"] is False


def test_prepare_on_other_platform_rejected_before_ui(client, monkeypatch):
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="darwin"))
    response = client.post("/api/v1/group/create", json={"members": ["甲", "乙"], "prepare_only": True})
    assert response.status_code == 400 and "仅支持 Windows" in response.json()["error"]


def test_array_endpoint_keeps_bearer_auth(client, monkeypatch):
    monkeypatch.setattr(security, "get_settings", lambda: SimpleNamespace(api_token="test-token"))
    client.app.dependency_overrides.clear()
    response = client.post("/api/v1/group/create", json={"members": ["甲", "乙"]})
    assert response.status_code == 401
    monkeypatch.setattr(local.windows_group, "create_group", lambda *a, **kw: {"success": True})
    response = client.post("/api/v1/group/create", headers={"Authorization": "Bearer test-token"},
                           json={"members": ["甲", "乙"]})
    assert response.status_code == 200


def test_openapi_exposes_name_array_and_preview(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["LocalCreateGroupRequest"]
    assert schema["properties"]["members"]["type"] == "array"
    assert schema["properties"]["members"]["items"]["minLength"] == 1
    assert schema["properties"]["prepare_only"]["default"] is False
    assert "members[0]" in schema["properties"]["members"]["description"]
