"""文档/表格接口测试。

这里不 mock 掉 online/ 包本身 —— 只把最底层的 wecom-cli runner 换成假的。
这样 payload 构造、require_confirmation 闸门、docid/url 校验走的都是上游真实代码,
上游改了语义测试会挂,而不是我们自己的桩在自说自话。
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from wecom_svr import online_bridge
from wecom_svr.main import create_app
from wecom_svr.online_bridge import load_online
from wecom_svr.paths import VENDOR_HOME
from wecom_svr.settings import get_agent_home, get_settings

pytestmark = pytest.mark.skipif(
    not (VENDOR_HOME / "online" / "__init__.py").exists(),
    reason="vendor/wecom-agent 未初始化或版本过旧(无 online/ 包)",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("WECOM_SVR_API_TOKEN", raising=False)
    monkeypatch.delenv("WECOM_AGENT_HOME", raising=False)
    for cache in (get_settings, get_agent_home, load_online):
        cache.cache_clear()
    yield
    for cache in (get_settings, get_agent_home, load_online):
        cache.cache_clear()


@pytest.fixture
def calls(monkeypatch):
    """替换最底层 runner,记录 wecom-cli 本该收到的 (args, payload)。"""
    recorded = []

    def fake_json_runner(_mods):
        def run(args, payload=None):
            recorded.append((list(args), payload))
            return {"ok": True}
        return run

    monkeypatch.setattr(online_bridge, "_json_runner", fake_json_runner)
    return recorded


@pytest.fixture
def client():
    return TestClient(create_app())


# ─── 确认闸门 ──────────────────────────────────────────────────────────

def test_write_without_confirmation_is_403(client, calls):
    r = client.post("/api/v1/doc/create", json={"name": "周报"})
    assert r.status_code == 403
    assert "create_document" in r.json()["errmsg"]
    assert calls == [], "未确认时不应真的调用 wecom-cli"


def test_confirmed_write_reaches_cli_with_upstream_payload(client, calls):
    r = client.post("/api/v1/doc/create", json={"name": "周报", "confirmed": True})
    assert r.status_code == 200
    assert calls == [(["doc", "create_doc"], {"doc_name": "周报", "doc_type": 3})]


def test_readonly_endpoint_needs_no_confirmation(client, calls):
    r = client.post("/api/v1/sheet/info", json={"docid": "abc"})
    assert r.status_code == 200
    assert calls == [(["doc", "sheet_get_info"], {"docid": "abc"})]


# ─── docid / url 二选一 ────────────────────────────────────────────────

def test_both_docid_and_url_rejected(client, calls):
    r = client.post("/api/v1/sheet/info", json={"docid": "a", "url": "https://x"})
    assert r.status_code == 400
    assert calls == []


def test_neither_docid_nor_url_rejected(client, calls):
    r = client.post("/api/v1/sheet/info", json={})
    assert r.status_code == 400
    assert calls == []


def test_url_target_is_accepted(client, calls):
    r = client.post("/api/v1/sheet/info", json={"url": "https://doc.weixin.qq.com/x"})
    assert r.status_code == 200
    assert calls[0][1] == {"url": "https://doc.weixin.qq.com/x"}


# ─── 表格单元格编码(上游 cell_data 逻辑)──────────────────────────────

def test_append_row_encodes_cell_types(client, calls):
    r = client.post("/api/v1/sheet/row/append", json={
        "docid": "d", "sheet_id": "s", "confirmed": True,
        "values": [42, "hello", {"type": "link", "text": "t", "url": "u"}],
    })
    assert r.status_code == 200

    args, payload = calls[0]
    assert args == ["doc", "sheet_append_data"]
    cells = payload["row"]["values"]
    assert cells[0] == {"data_type": "NUMBER", "cell_value": {"number": 42}}
    assert cells[1] == {"data_type": "TEXT", "cell_value": {"text": "hello"}}
    assert cells[2]["data_type"] == "LINK"


def test_update_range_wraps_rows(client, calls):
    r = client.post("/api/v1/sheet/range/update", json={
        "docid": "d", "sheet_id": "s", "confirmed": True,
        "start_row": 2, "start_column": 1, "rows": [["a", 1], ["b", 2]],
    })
    assert r.status_code == 200

    grid = calls[0][1]["grid_data"]
    assert grid["start_row"] == 2 and grid["start_column"] == 1
    assert len(grid["rows"]) == 2
    assert grid["rows"][0]["values"][0]["cell_value"]["text"] == "a"


# ─── 智能表 ────────────────────────────────────────────────────────────

def test_smartsheet_auto_file_switches_command(client, calls):
    body = {"docid": "d", "sheet_id": "s", "records": [], "confirmed": True}
    client.post("/api/v1/smartsheet/records/add", json=body)
    client.post("/api/v1/smartsheet/records/add", json={**body, "auto_file": True})

    assert calls[0][0] == ["doc", "smartsheet_add_records"]
    assert calls[1][0] == ["doc", "+smartsheet_add_records_auto_file"]


def test_smartsheet_get_fields_is_readonly(client, calls):
    r = client.post("/api/v1/smartsheet/fields/get", json={"docid": "d", "sheet_id": "s"})
    assert r.status_code == 200
    assert calls[0][0] == ["doc", "smartsheet_get_fields"]


# ─── 环境/下游失败的区分 ───────────────────────────────────────────────

def test_missing_wecom_cli_is_503(client, monkeypatch):
    def runner(_mods):
        def run(args, payload=None):
            raise FileNotFoundError("wecom-cli")
        return run

    monkeypatch.setattr(online_bridge, "_json_runner", runner)
    r = client.post("/api/v1/doc/create", json={"name": "x", "confirmed": True})
    assert r.status_code == 503
    assert "wecom-cli" in r.json()["errmsg"]


def test_cli_failure_is_502(client, monkeypatch):
    mods = load_online()

    def runner(_mods):
        def run(args, payload=None):
            raise mods.wecom_cli.WecomCliError(["wecom-cli"], 1, "", "not logged in")
        return run

    monkeypatch.setattr(online_bridge, "_json_runner", runner)
    r = client.post("/api/v1/doc/create", json={"name": "x", "confirmed": True})
    assert r.status_code == 502
    assert "not logged in" in r.json()["errmsg"]


def test_cli_timeout_is_502(client, monkeypatch):
    def runner(_mods):
        def run(args, payload=None):
            raise subprocess.TimeoutExpired("wecom-cli", 60)
        return run

    monkeypatch.setattr(online_bridge, "_json_runner", runner)
    r = client.post("/api/v1/doc/create", json={"name": "x", "confirmed": True})
    assert r.status_code == 502
    assert "超时" in r.json()["errmsg"]


# ─── 鉴权覆盖新端点 ────────────────────────────────────────────────────

def test_doc_endpoints_are_behind_auth(monkeypatch):
    monkeypatch.setenv("WECOM_SVR_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    r = TestClient(create_app()).post("/api/v1/doc/create", json={"name": "x", "confirmed": True})
    assert r.status_code == 401
