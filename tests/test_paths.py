"""agent home 解析 —— 需同时支持上游 main 与旧版目录布局。"""
import pytest

from wecom_svr.paths import AgentHomeError, find_db, resolve_agent_home


def _make_home(tmp_path, cli_rel):
    cli = tmp_path / cli_rel
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("# stub wecom.py\n")
    (tmp_path / "decrypt").mkdir(exist_ok=True)
    return tmp_path


def test_upstream_layout(tmp_path):
    home = resolve_agent_home(_make_home(tmp_path, "legacy/self-built-app/wecom.py"))
    assert home.wecom_cli.name == "wecom.py"
    assert home.wecom_cli.parent.name == "self-built-app"
    assert home.decrypt_dir == tmp_path / "decrypt"


def test_legacy_flat_layout(tmp_path):
    home = resolve_agent_home(_make_home(tmp_path, "wecom.py"))
    assert home.wecom_cli == tmp_path / "wecom.py"


def test_upstream_layout_wins_when_both_present(tmp_path):
    _make_home(tmp_path, "wecom.py")
    _make_home(tmp_path, "legacy/self-built-app/wecom.py")
    assert resolve_agent_home(tmp_path).wecom_cli.parent.name == "self-built-app"


def test_missing_cli_is_reported(tmp_path):
    (tmp_path / "decrypt").mkdir()
    with pytest.raises(AgentHomeError, match="未找到 wecom.py"):
        resolve_agent_home(tmp_path)


def test_missing_home_is_reported(tmp_path):
    with pytest.raises(AgentHomeError, match="不存在"):
        resolve_agent_home(tmp_path / "nope")


def test_config_json_is_optional(tmp_path):
    home = resolve_agent_home(_make_home(tmp_path, "wecom.py"))
    assert home.config_json is None
    (tmp_path / "config.json").write_text("{}")
    assert home.config_json == tmp_path / "config.json"


def test_find_db_walks_decrypt_dir(tmp_path):
    db = tmp_path / "decrypt" / "macos" / "decrypted" / "Messages1" / "Info.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    assert find_db(tmp_path / "decrypt", "Info.db") == str(db)


def test_find_db_missing_is_not_cached(tmp_path):
    """未解密时返回空串;解密后同一路径必须能被再次发现。"""
    decrypt = tmp_path / "decrypt"
    decrypt.mkdir()
    assert find_db(decrypt, "Session.db") == ""

    db = decrypt / "Session.db"
    db.write_bytes(b"")
    assert find_db(decrypt, "Session.db") == str(db)
