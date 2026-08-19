import subprocess

import pytest
from fastapi.testclient import TestClient

from wecom_svr import main, startup_preflight
from wecom_svr.localdb import LocalDataError
from wecom_svr.messages import LocalKeyError
from wecom_svr.settings import Settings


def _raise(exc):
    """monkeypatch 的替身里要抛异常时用它 —— 比 `(_ for _ in ()).throw(...)` 好读。"""
    def fail(*args, **kwargs):
        raise exc
    return fail


@pytest.fixture(autouse=True)
def _dont_talk_to_the_real_client(monkeypatch):
    """校准成功后的窗口预热会跑 AppleScript,单测里一律短路。"""
    monkeypatch.setattr(
        startup_preflight.local_send, "_refresh_chat_window_bounds", lambda: (True, "ok")
    )
    monkeypatch.setattr(
        startup_preflight.local_send, "_load_quartz_libraries", lambda: None
    )


def _settings(tmp_path, **updates) -> Settings:
    values = {
        "data_dir": tmp_path,
        "startup_preflight_enabled": True,
        "startup_auto_init": True,
        "startup_ui_check": True,
        **updates,
    }
    return Settings(**values)


def test_valid_key_refreshes_database_and_calibrates_ui(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        startup_preflight.messages,
        "refresh",
        lambda force=False: calls.append(("refresh", force)),
    )
    monkeypatch.setattr(
        startup_preflight.local_group,
        "calibrate_ui",
        lambda: calls.append(("ui", True)),
    )

    result = startup_preflight.run(_settings(tmp_path))

    assert result.database_ready is True
    assert result.key_discovered is False
    assert result.ui_ready is True
    assert calls == [("refresh", True), ("ui", True)]


def test_missing_key_is_discovered_then_revalidated(tmp_path, monkeypatch):
    calls = []

    def refresh(force=False):
        calls.append(("refresh", force))
        if len([c for c in calls if c[0] == "refresh"]) == 1:
            raise LocalKeyError("no key")

    monkeypatch.setattr(startup_preflight.messages, "refresh", refresh)
    monkeypatch.setattr(
        startup_preflight,
        "_discover_key",
        lambda settings: calls.append(("discover", settings.startup_key_timeout)),
    )
    monkeypatch.setattr(startup_preflight.local_group, "calibrate_ui", lambda: None)

    result = startup_preflight.run(_settings(tmp_path))

    assert result.key_discovered is True
    assert calls == [("refresh", True), ("discover", 180), ("refresh", True)]


def test_missing_key_blocks_when_auto_init_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        startup_preflight.messages, "refresh", _raise(LocalKeyError("no key"))
    )

    with pytest.raises(startup_preflight.StartupPreflightError, match="no key"):
        startup_preflight.run(_settings(tmp_path, startup_auto_init=False))


def test_key_discovery_failure_is_actionable_and_redacts_keys(tmp_path, monkeypatch):
    script = tmp_path / "decrypt" / "macos" / "find_key_fast.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    home = type("Home", (), {"decrypt_dir": tmp_path / "decrypt"})()
    monkeypatch.setattr(startup_preflight, "get_agent_home", lambda: home)
    monkeypatch.setattr(
        startup_preflight.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="task_for_pid failed aabbccddeeff00112233445566778899",
        ),
    )

    with pytest.raises(startup_preflight.StartupPreflightError) as caught:
        startup_preflight._discover_key(_settings(tmp_path))

    message = str(caught.value)
    assert "task_for_pid" in message
    assert "aabbccddeeff00112233445566778899" not in message
    assert "<redacted-key>" in message


def test_ui_failure_degrades_readiness_without_blocking_startup(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(startup_preflight.messages, "refresh", lambda force=False: None)
    monkeypatch.setattr(
        startup_preflight.local_group, "calibrate_ui", _raise(RuntimeError("no accessibility"))
    )

    result = startup_preflight.run(_settings(tmp_path))

    assert result.database_ready is True
    assert result.ui_ready is False
    assert "UI 预检/校准失败" in caplog.text
    assert "服务继续启动" in caplog.text


def test_learned_plus_offset_skips_opening_the_group_picker(tmp_path, monkeypatch):
    """已经校准过就不要每次启动都弹「发起群聊」。"""
    calls = []
    monkeypatch.setattr(startup_preflight.messages, "refresh", lambda force=False: None)
    monkeypatch.setattr(
        startup_preflight.local_group,
        "calibrate_ui",
        lambda: calls.append("ui"),
    )
    monkeypatch.setattr(
        startup_preflight.ui_offsets, "learned", lambda name, settings=None: (295, 29)
    )

    result = startup_preflight.run(_settings(tmp_path))

    assert result.ui_ready is True
    assert calls == []


def test_collect_only_mode_skips_ui_but_keeps_database_gate(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        startup_preflight.messages,
        "refresh",
        lambda force=False: calls.append("db"),
    )
    monkeypatch.setattr(
        startup_preflight.local_group,
        "calibrate_ui",
        lambda: calls.append("ui"),
    )

    result = startup_preflight.run(_settings(tmp_path, startup_ui_check=False))

    assert result.database_ready is True
    assert result.ui_ready is False
    assert calls == ["db"]


# ─── 通讯录(Session.db)────────────────────────────────────────────────

def test_contacts_are_probed_separately_from_the_message_database(tmp_path, monkeypatch):
    """refresh() 对 Session.db 只记 warning,所以通讯录要单独探一次。"""
    monkeypatch.setattr(startup_preflight.messages, "refresh", lambda force=False: None)
    monkeypatch.setattr(startup_preflight.local_group, "calibrate_ui", lambda: None)
    monkeypatch.setattr(startup_preflight.localdb, "query", lambda *a, **kw: [{"RID": 1}])

    assert startup_preflight.run(_settings(tmp_path)).contacts_ready is True


def test_unusable_contacts_degrade_instead_of_blocking_startup(tmp_path, monkeypatch, caplog):
    """纯发消息/纯采集的部署用不到通讯录,不该因为它起不来 —— 但要报出来。"""
    monkeypatch.setattr(startup_preflight.messages, "refresh", lambda force=False: None)
    monkeypatch.setattr(startup_preflight.local_group, "calibrate_ui", lambda: None)
    monkeypatch.setattr(
        startup_preflight.localdb, "query", _raise(LocalDataError("没有解密后的 Session.db"))
    )

    result = startup_preflight.run(_settings(tmp_path))

    assert result.database_ready is True
    assert result.contacts_ready is False
    assert "通讯录" in caplog.text


# ─── lifespan 接线 ─────────────────────────────────────────────────────

def _preflight(**flags) -> startup_preflight.PreflightResult:
    values = {
        "database_ready": True,
        "key_discovered": False,
        "contacts_ready": True,
        "ui_ready": True,
        **flags,
    }
    return startup_preflight.PreflightResult(**values)


def test_database_preflight_failure_stops_the_server_from_starting(monkeypatch):
    """数据库预检失败仍必须让启动失败。"""
    def boom(settings):
        raise startup_preflight.StartupPreflightError("Info.db 无法解密")

    monkeypatch.setattr(main, "run_startup_preflight", boom)

    with pytest.raises(startup_preflight.StartupPreflightError, match=r"Info\.db"):
        with TestClient(main.create_app()):
            pass


def test_ui_preflight_failure_keeps_health_endpoint_available(monkeypatch):
    monkeypatch.setattr(
        main,
        "run_startup_preflight",
        lambda settings: _preflight(ui_ready=False),
    )

    with TestClient(main.create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["startup_preflight"]["ui_ready"] is False


def test_successful_preflight_is_published_through_health(monkeypatch):
    monkeypatch.setattr(main, "run_startup_preflight", lambda settings: _preflight())

    with TestClient(main.create_app()) as client:
        body = client.get("/health").json()

    assert body["startup_preflight"] == {
        "database_ready": True,
        "key_discovered": False,
        "contacts_ready": True,
        "ui_ready": True,
    }


def test_health_before_lifespan_reports_no_preflight(monkeypatch):
    """没跑 lifespan(比如单测直接构造 TestClient)时不能崩,报 null 就好。"""
    assert TestClient(main.create_app()).get("/health").json()["startup_preflight"] is None
