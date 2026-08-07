"""针对 vendor/wecom-agent 真实内容的契约测试。

上游改过一次目录结构(wecom.py → legacy/self-built-app/wecom.py),
所以每次更新 pkg(scripts/update-agent.sh)都要跑这些用例:
它们锁定 wecom-svr 实际依赖的那部分契约,而不是整个上游仓库。
"""
import importlib.util

import pytest

from wecom_svr.paths import VENDOR_HOME, resolve_agent_home

pytestmark = pytest.mark.skipif(
    not (VENDOR_HOME / ".git").exists(),
    reason="vendor/wecom-agent 未初始化(git submodule update --init)",
)


@pytest.fixture(scope="module")
def home():
    return resolve_agent_home(VENDOR_HOME)


@pytest.fixture(scope="module")
def wecom(home):
    """直接 import 被 vendor 的 wecom.py(只加载模块,不执行 main)。"""
    spec = importlib.util.spec_from_file_location("_vendored_wecom", home.wecom_cli)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendor_layout_is_resolvable(home):
    assert home.wecom_cli.is_file()
    assert home.decrypt_dir.is_dir()


def test_decrypt_entrypoint_still_exists(home):
    """本人身份发消息依赖它解密出 Info.db / Session.db。"""
    assert (home.decrypt_dir / "macos" / "read_wecom.py").is_file()


def test_categories_used_by_this_service_exist(wecom):
    assert {"contact", "message"} <= set(wecom.HANDLERS)


@pytest.mark.parametrize(
    "method,expected_key",
    [("text", "text"), ("markdown", "markdown"), ("news", "news")],
)
def test_msgtype_maps_to_handler_method(wecom, monkeypatch, method, expected_key):
    """wecom-svr 直接把 msgtype 当作 wecom.py 的 method 传,这里锁死该映射。"""
    captured = {}

    def fake_http(http_method, path, params=None, body=None, token=None):
        captured.update(path=path, body=body)
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(wecom, "_http", fake_http)

    args = {"touser": "u", "content": "hi", "articles": [{"title": "t", "url": "u"}]}
    wecom.message(method, args, "token", {"agentid": "1000001"})

    assert captured["path"] == "/cgi-bin/message/send"
    assert captured["body"]["msgtype"] == expected_key
    assert captured["body"]["touser"] == "u"


def test_unknown_method_is_rejected(wecom):
    """未知 msgtype 由 wecom.py 兜底拒绝(_die → SystemExit)。"""
    with pytest.raises(SystemExit):
        wecom.message("video", {"touser": "u"}, "token", {"agentid": "1000001"})
