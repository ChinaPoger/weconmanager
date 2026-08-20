"""拉群接口契约 —— 不联网、不读真实解密库。

通讯录解析被整体打桩:真实数据只在本机存在,CI 里没有,契约测试不该依赖它。
"""
import pytest
from fastapi.testclient import TestClient

from wecom_svr import directory
from wecom_svr.main import create_app
from wecom_svr.settings import get_agent_home, get_settings

ME = {"userid": "renweifeng", "name": "任威风", "vid": 1688851653323273, "alias": ""}
TAO = {"userid": "chentao", "name": "陈涛", "vid": 1688851653323840, "alias": ""}
TING = {"userid": "chenting01", "name": "陈挺", "vid": 1688858207733961, "alias": ""}

_BY_NAME = {"我": ME, "任威风": ME, "陈涛": TAO, "陈挺": TING,
            "renweifeng": ME, "chentao": TAO, "chenting01": TING}


def _clear_self_cache():
    """get_self 可能正被 monkeypatch 换成普通函数,拿不到 cache_clear 是正常的。"""
    clear = getattr(directory.get_self, "cache_clear", None)
    if clear:
        clear()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WECOM_SVR_API_TOKEN", raising=False)
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    _clear_self_cache()
    yield
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    _clear_self_cache()


@pytest.fixture
def fake_directory(monkeypatch):
    """用固定的三个人替换本地库查询。"""
    def resolve_member(q):
        hit = _BY_NAME.get(q.strip())
        if not hit:
            raise directory.MemberNotFound(f"通讯录里找不到「{q}」")
        return hit

    monkeypatch.setattr(directory, "resolve_member", resolve_member)
    monkeypatch.setattr(directory, "get_self", lambda: ME)
    monkeypatch.setattr(directory, "self_userid_or_none", lambda: ME["userid"])
    monkeypatch.setattr(directory, "find_members", lambda kw: [TAO, TING])


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def calls(monkeypatch):
    """记录发往官方 API 的调用,并回一个成功响应。"""
    seen = []

    async def fake_call(category, method, args):
        seen.append({"category": category, "method": method, "args": args})
        return {"errcode": 0, "errmsg": "ok", "chatid": "CHATID001"}

    monkeypatch.setattr("wecom_svr.group.call_wecom", fake_call)
    return seen


# ─── 解析 ──────────────────────────────────────────────────────────────

def test_resolve_puts_self_first_and_dedups(client, fake_directory):
    r = client.post("/api/v1/app/group/members/resolve",
                    json={"members": ["陈涛", "陈挺", "任威风"], "include_self": True})
    assert r.status_code == 200
    assert r.json()["userlist"] == ["renweifeng", "chentao", "chenting01"]


def test_resolve_keeps_the_asked_name(client, fake_directory):
    r = client.post("/api/v1/app/group/members/resolve",
                    json={"members": ["陈涛"], "include_self": False})
    assert r.json()["members"] == [{**TAO, "query": "陈涛"}]


def test_unknown_name_is_400_not_500(client, fake_directory, calls):
    r = client.post("/api/v1/app/group/create",
                    json={"name": "群", "members": ["查无此人"]})
    assert r.status_code == 400
    assert "查无此人" in r.json()["errmsg"]
    assert calls == []  # 没解析出来就绝不该建群


def test_ambiguous_name_is_400_with_candidates(client, monkeypatch, calls):
    def boom(q):
        raise directory.AmbiguousMember("「陈」匹配到多个人: 陈涛(chentao), 陈挺(chenting01)")

    monkeypatch.setattr(directory, "resolve_member", boom)
    monkeypatch.setattr(directory, "get_self", lambda: ME)
    r = client.post("/api/v1/app/group/create",
                    json={"name": "群", "members": ["陈"], "include_self": False})
    assert r.status_code == 400
    assert "chentao" in r.json()["errmsg"]
    assert calls == []


# ─── 建群 ──────────────────────────────────────────────────────────────

def test_create_sends_userids_and_defaults_owner_to_self(client, fake_directory, calls):
    r = client.post("/api/v1/app/group/create",
                    json={"name": "临时讨论组", "members": ["陈涛", "陈挺"]})
    assert r.status_code == 200
    body = calls[0]["args"]["body"]
    assert calls[0] == {"category": "call", "method": "POST",
                        "args": {"path": "/cgi-bin/appchat/create", "body": body}}
    assert body["userlist"] == ["renweifeng", "chentao", "chenting01"]
    assert body["owner"] == "renweifeng"
    assert body["name"] == "临时讨论组"
    assert "chatid" not in body  # 没传就让服务端随机


def test_create_echoes_resolution_next_to_chatid(client, fake_directory, calls):
    r = client.post("/api/v1/app/group/create",
                    json={"name": "群", "members": ["陈涛", "陈挺"]})
    body = r.json()
    assert body["chatid"] == "CHATID001"
    assert [m["name"] for m in body["members"]] == ["任威风", "陈涛", "陈挺"]


def test_create_without_self_needs_two_others(client, fake_directory, calls):
    r = client.post("/api/v1/app/group/create",
                    json={"name": "群", "members": ["陈涛"], "include_self": False})
    assert r.status_code == 400
    assert "至少 2 人" in r.json()["errmsg"]
    assert calls == []


def test_explicit_owner_wins(client, fake_directory, calls):
    client.post("/api/v1/app/group/create",
                json={"name": "群", "members": ["陈涛", "陈挺"], "owner": "陈涛"})
    assert calls[0]["args"]["body"]["owner"] == "chentao"


def test_bad_chatid_rejected(client, fake_directory, calls):
    r = client.post("/api/v1/app/group/create",
                    json={"name": "群", "members": ["陈涛", "陈挺"], "chatid": "有-中文"})
    assert r.status_code == 400
    assert "0-9a-zA-Z" in r.json()["errmsg"]
    assert calls == []


def test_official_error_passes_through(client, fake_directory, monkeypatch):
    async def fail(category, method, args):
        return {"errcode": 60011, "errmsg": "no privilege to access/modify contact/party/agent"}

    monkeypatch.setattr("wecom_svr.group.call_wecom", fail)
    r = client.post("/api/v1/app/group/create", json={"name": "群", "members": ["陈涛", "陈挺"]})
    assert r.status_code == 200  # 官方业务错误照搬,不翻译成 HTTP 错误
    assert r.json()["errcode"] == 60011


# ─── 改群 / 发群消息 ────────────────────────────────────────────────────

def test_update_resolves_add_and_remove(client, fake_directory, calls):
    r = client.post("/api/v1/app/group/update",
                    json={"chatid": "C1", "add": ["陈挺"], "remove": ["陈涛"]})
    assert r.status_code == 200
    body = calls[0]["args"]["body"]
    assert body == {"chatid": "C1", "add_user_list": ["chenting01"],
                    "del_user_list": ["chentao"]}


def test_update_needs_something_to_change(client, fake_directory):
    r = client.post("/api/v1/app/group/update", json={"chatid": "C1"})
    assert r.status_code == 400


def test_send_uses_appchat_send(client, calls):
    r = client.post("/api/v1/app/group/send", json={"chatid": "C1", "content": "hi"})
    assert r.status_code == 200
    assert calls[0]["args"] == {
        "path": "/cgi-bin/appchat/send",
        "body": {"chatid": "C1", "msgtype": "text", "text": {"content": "hi"}},
    }


def test_send_rejects_unsupported_msgtype(client, calls):
    r = client.post("/api/v1/app/group/send",
                    json={"chatid": "C1", "content": "hi", "msgtype": "image"})
    assert r.status_code == 400
    assert calls == []


def test_get_uses_query_params(client, calls):
    client.post("/api/v1/app/group/get", json={"chatid": "C1"})
    assert calls[0] == {"category": "call", "method": "GET",
                        "args": {"path": "/cgi-bin/appchat/get", "params": {"chatid": "C1"}}}


# ─── 鉴权 ──────────────────────────────────────────────────────────────

def test_group_create_respects_auth(monkeypatch, fake_directory):
    monkeypatch.setenv("WECOM_SVR_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    r = TestClient(create_app()).post("/api/v1/app/group/create",
                                      json={"name": "群", "members": ["陈涛", "陈挺"]})
    assert r.status_code == 401


def test_self_vid_is_stringified(fake_directory):
    assert directory.self_vid_or_none() == str(ME["vid"])


def test_self_vid_returns_none_when_unknown(monkeypatch):
    monkeypatch.setattr(
        directory, "get_self", lambda: (_ for _ in ()).throw(directory.MemberNotFound("x"))
    )
    assert directory.self_vid_or_none() is None


def test_self_vid_returns_none_when_record_has_no_vid(monkeypatch):
    monkeypatch.setattr(directory, "get_self", lambda: {"userid": "me"})
    assert directory.self_vid_or_none() is None


def test_self_vid_returns_none_when_vid_is_blank(monkeypatch):
    monkeypatch.setattr(directory, "get_self", lambda: {"userid": "me", "vid": "  "})
    assert directory.self_vid_or_none() is None
