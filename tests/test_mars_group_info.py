"""群信息同步契约 —— 不读真实解密库、不联网。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from wecom_svr import directory, mars_group_info, mars_sync, messages
from wecom_svr.main import create_app
from wecom_svr.settings import Settings, get_agent_home, get_settings

GROUP = {"rid": 20614687431404, "name": "客户群A", "sort_time": 100}
SELF_VID = "1688856651864877"
MEMBERS = [
    {
        "member_id": "1688851653323840",
        "name": "陈涛",
        "userid": "chentao",
        "corpid": "1970325043038620",
        "avatar": "https://x/a",
    },
    {
        "member_id": "1688858207733961",
        "name": "陈挺",
        "userid": "chenting01",
        "corpid": "1970325043038620",
        "avatar": "https://x/b",
    },
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("WECOM_SVR_API_TOKEN", "WECOM_SVR_MARS_BASE_URL",
              "WECOM_SVR_MARS_SYNC_GROUPS", "WECOM_SVR_MARS_SYNC_ENABLED",
              "WECOM_SVR_MARS_GROUP_INFO_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WECOM_SVR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(directory, "self_vid_or_none", lambda: SELF_VID)
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_home.cache_clear()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        mars_base_url="https://ts.qiniu.io/",
        mars_sync_groups="客户群A",
    )


@pytest.fixture
def local(monkeypatch):
    store = {"members": list(MEMBERS), "name": GROUP["name"]}

    monkeypatch.setattr(messages, "list_groups", lambda search="", **kw: [GROUP])
    monkeypatch.setattr(messages, "resolve_groups", lambda selectors: ([GROUP], []))
    monkeypatch.setattr(
        messages, "group_profile",
        lambda rid: {"rid": rid, "name": store["name"], "sort_time": 0},
    )
    monkeypatch.setattr(messages, "group_members", lambda rid: list(store["members"]))
    return store


@pytest.fixture
def pushed(monkeypatch):
    sent = []
    ctl = {"fail_at": None, "sent": sent}

    async def fake_push(client, payload, settings):
        if ctl["fail_at"] is not None and len(sent) == ctl["fail_at"]:
            raise mars_sync.MarsSyncError("mars 返回 500: boom")
        sent.append(payload)

    monkeypatch.setattr(mars_group_info, "push_one", fake_push)
    return ctl


def test_payload_shape_matches_mars_group_info_report(settings, local):
    payload = mars_group_info.build_payload(GROUP["rid"], settings)
    assert payload == {
        "group_number": "wework-20614687431404",
        "group_name": "客户群A",
        "uid": 0,
        "client_type": "Wework",
        "source": SELF_VID,
        "note": "",
        "members": MEMBERS,
    }


def test_empty_members_and_note_are_not_worth_pushing():
    assert mars_group_info.should_push({"members": [], "note": ""}) is False
    assert mars_group_info.should_push({"members": MEMBERS, "note": ""}) is True
    assert mars_group_info.should_push({"members": [], "note": "UID: 1"}) is True


def test_fingerprint_ignores_source_and_client_type(settings, local):
    a = mars_group_info.build_payload(GROUP["rid"], settings)
    b = {**a, "source": "other", "client_type": "QQ"}
    assert mars_group_info.fingerprint(a) == mars_group_info.fingerprint(b)


def test_fingerprint_changes_when_a_member_is_added(settings, local):
    a = mars_group_info.build_payload(GROUP["rid"], settings)
    local["members"] = [*MEMBERS, {**MEMBERS[0], "member_id": "9"}]
    b = mars_group_info.build_payload(GROUP["rid"], settings)
    assert mars_group_info.fingerprint(a) != mars_group_info.fingerprint(b)


def test_sync_pushes_once_then_skips_unchanged(settings, local, pushed):
    first = run(mars_group_info.sync_once(settings))
    assert first.pushed == 1 and first.unchanged == 0
    assert pushed["sent"][0]["group_number"] == "wework-20614687431404"
    assert [m["userid"] for m in pushed["sent"][0]["members"]] == ["chentao", "chenting01"]

    second = run(mars_group_info.sync_once(settings))
    assert second.pushed == 0 and second.unchanged == 1
    assert len(pushed["sent"]) == 1


def test_member_change_triggers_another_push(settings, local, pushed):
    run(mars_group_info.sync_once(settings))
    local["members"] = [MEMBERS[0]]
    result = run(mars_group_info.sync_once(settings))
    assert result.pushed == 1
    assert len(pushed["sent"][-1]["members"]) == 1


def test_rename_triggers_another_push(settings, local, pushed):
    run(mars_group_info.sync_once(settings))
    local["name"] = "客户群A改名"
    result = run(mars_group_info.sync_once(settings))
    assert result.pushed == 1
    assert pushed["sent"][-1]["group_name"] == "客户群A改名"


def test_empty_member_list_is_skipped_not_pushed(settings, local, pushed):
    local["members"] = []
    result = run(mars_group_info.sync_once(settings))
    assert result.skipped == 1 and result.pushed == 0
    assert pushed["sent"] == []


def test_failed_push_does_not_store_fingerprint(settings, local, pushed):
    pushed["fail_at"] = 0
    result = run(mars_group_info.sync_once(settings))
    assert result.pushed == 0
    assert "boom" in result.error
    pushed["fail_at"] = None
    recovered = run(mars_group_info.sync_once(settings))
    assert recovered.pushed == 1


def test_disabled_group_info_is_a_noop(settings, local, pushed):
    s = settings.model_copy(update={"mars_group_info_enabled": False})
    result = run(mars_group_info.sync_once(s))
    assert result.scanned == 0 and result.pushed == 0
    assert pushed["sent"] == []


def test_dry_run_does_not_push_or_persist(settings, local, pushed):
    result = run(mars_group_info.sync_once(settings, dry_run=True))
    assert result.dry_run is True
    assert result.pending[0]["group_name"] == "客户群A"
    assert pushed["sent"] == []
    assert not settings.mars_group_info_state_file.exists()


@pytest.fixture
def client(monkeypatch, tmp_path, local):
    monkeypatch.setenv("WECOM_SVR_MARS_BASE_URL", "https://ts.qiniu.io")
    monkeypatch.setenv("WECOM_SVR_MARS_SYNC_GROUPS", "客户群A")
    get_settings.cache_clear()
    return TestClient(create_app())


def test_http_preview_and_run(client, local, pushed):
    preview = client.post("/api/v1/mars/sync/group-info/preview")
    assert preview.status_code == 200
    assert preview.json()["pending"][0]["members"][0]["userid"] == "chentao"
    assert pushed["sent"] == []

    run_r = client.post("/api/v1/mars/sync/group-info/run", json={})
    assert run_r.status_code == 200
    assert run_r.json()["pushed"] == 1
    assert len(pushed["sent"]) == 1

    status = client.get("/api/v1/mars/sync/group-info/status")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is True
    assert body["config"]["group_info_url"] == "https://ts.qiniu.io/api/qq/group-infos"
    assert str(GROUP["rid"]) in body["state"]["groups"]


def test_message_sync_also_pushes_group_info(settings, local, pushed, monkeypatch):
    monkeypatch.setattr(messages, "refresh", lambda force=False: False)
    monkeypatch.setattr(
        messages, "fetch_since",
        lambda *a, **kw: messages.Batch(),
    )
    monkeypatch.setattr(messages, "latest_cursor", lambda rid=None: mars_sync.Cursor())
    monkeypatch.setattr(messages, "count_since", lambda cursors: dict.fromkeys(cursors, 0))

    result = run(mars_sync.sync_once(settings))
    assert result.group_info["pushed"] == 1
    assert pushed["sent"][0]["group_number"] == "wework-20614687431404"
