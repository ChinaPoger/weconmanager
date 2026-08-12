import subprocess

import pytest

from wecom_svr import startup_preflight
from wecom_svr.messages import LocalKeyError
from wecom_svr.settings import Settings


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
        startup_preflight.messages,
        "refresh",
        lambda force=False: (_ for _ in ()).throw(LocalKeyError("no key")),
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


def test_ui_failure_blocks_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(startup_preflight.messages, "refresh", lambda force=False: None)
    monkeypatch.setattr(
        startup_preflight.local_group,
        "calibrate_ui",
        lambda: (_ for _ in ()).throw(RuntimeError("no accessibility")),
    )

    with pytest.raises(startup_preflight.StartupPreflightError, match="辅助功能"):
        startup_preflight.run(_settings(tmp_path))


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
