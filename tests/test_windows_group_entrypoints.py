"""Windows entry points must keep UI writes behind the selected backend."""

import json
import sys
from types import SimpleNamespace

import pytest

from wecom_svr import cli
from wecom_svr.routers import local
from wecom_svr.schemas import LocalCreateGroupRequest, LocalRenameGroupRequest


def test_windows_http_creation_preserves_verification_without_initial_message(monkeypatch):
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    result = {"success": True, "verified": True, "rid": 7, "name_verified": True, "reused": True}
    seen = []
    monkeypatch.setattr(local.windows_group, "create_group",
                        lambda members, **kw: seen.append((members, kw)) or result)
    monkeypatch.setattr(local.local_group, "create_group", lambda *a, **kw: pytest.fail("macOS backend"))
    monkeypatch.setattr(local.local_send, "send_initial_message", lambda *a, **kw: pytest.fail("no message"))
    response = local.create_local_group(LocalCreateGroupRequest(members=["甲", "乙"], name="讨论组"), None)
    assert response.status_code == 200 and json.loads(response.body) == result
    assert seen == [(["甲", "乙"], {"name": "讨论组", "nickname": "", "prepare_only": False})]


def test_windows_http_rename_uses_windows_backend(monkeypatch):
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(local.windows_group, "rename_group",
                        lambda rid, name: {"success": True, "verified": True, "rid": rid, "name": name})
    monkeypatch.setattr(local.local_group, "rename_group", lambda *a: pytest.fail("macOS backend"))
    result = local.rename_local_group(LocalRenameGroupRequest(conv_rid=7, name="讨论组"), None)
    assert result.status_code == 200 and json.loads(result.body)["verified"] is True


def test_windows_cli_preparation_does_not_claim_group_created(monkeypatch, capsys):
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32", argv=[
        "wecom-svr-local", "--group", "甲,乙", "--prepare-only",
    ]))
    monkeypatch.setattr(sys, "argv", cli.sys.argv)
    seen = []
    monkeypatch.setattr(cli.windows_group, "create_group", lambda members, **kw: seen.append(kw) or {
        "success": True, "prepared": True, "submitted": False,
    })
    cli.main()
    assert seen[0]["prepare_only"] is True
    assert "未提交建群" in capsys.readouterr().out


def test_windows_cli_rename_preserves_verified_result(monkeypatch, capsys):
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32", argv=[
        "wecom-svr-local", "--rid", "7", "--rename", "讨论组", "--json",
    ]))
    monkeypatch.setattr(sys, "argv", cli.sys.argv)
    monkeypatch.setattr(cli.windows_group, "rename_group", lambda rid, name: {
        "success": True, "verified": True, "rid": rid, "name": name,
    })
    cli.main()
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_prepare_option_rejected_on_other_platform_before_ui(monkeypatch):
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="darwin", argv=[
        "wecom-svr-local", "--group", "甲,乙", "--prepare-only",
    ]))
    monkeypatch.setattr(sys, "argv", cli.sys.argv)
    monkeypatch.setattr(cli, "create_group", lambda *a, **kw: pytest.fail("must not create"))
    with pytest.raises(SystemExit, match="2"):
        cli.main()
