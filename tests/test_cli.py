"""wecom-svr-local 的参数接线。

CLI 掉队过一次(拉群和改名只有 HTTP 端点、CLI 没跟上),所以这里守的是**接线**
而不是业务逻辑:每个开关都要真的走到对应的 local_group / local_send 函数上,
参数原样传到,失败要以非零码退出。业务逻辑本身在 test_local_group.py 里。
"""
import json

import pytest

from wecom_svr import cli


@pytest.fixture
def run(monkeypatch, capsys):
    """跑一次 CLI,返回 (退出码, stdout)。"""

    def _run(*argv: str) -> tuple[int, str]:
        monkeypatch.setattr(cli.sys, "argv", ["wecom-svr-local", *argv])
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                print(e.code)
        return code, capsys.readouterr().out

    return _run


def test_group_passes_members_and_name_through(run, monkeypatch):
    seen = {}

    def create_group(members, name=""):
        seen["members"], seen["name"] = members, name
        return {"success": True, "member_count": 3,
                "members": [{"name": "陈涛(水淼淼)", "dept": "增长产品部", "query": "陈涛"},
                            {"name": "陈挺", "dept": "增长产品部", "query": "陈挺"}]}

    monkeypatch.setattr(cli, "create_group", create_group)
    code, out = run("--group", " 陈涛 , 陈挺 ", "--group-name", "临时讨论组")

    assert code == 0
    assert seen == {"members": ["陈涛", "陈挺"], "name": "临时讨论组"}
    assert "陈涛(水淼淼)" in out and "共 3 人" in out


def test_group_surfaces_a_rename_failure_instead_of_burying_it(run, monkeypatch):
    """群建出来了但名字没改上 —— 这是要人手工补的,不能被一句「已建群」盖过去。"""
    monkeypatch.setattr(cli, "create_group", lambda members, name="": {
        "success": True, "member_count": 3, "members": [],
        "name_error": "群建好了,但改名失败: 没有输入框拿到焦点",
    })
    code, out = run("--group", "陈涛,陈挺", "--group-name", "临时讨论组")
    assert code == 0
    assert "改名失败" in out


def test_group_failure_exits_nonzero(run, monkeypatch):
    monkeypatch.setattr(cli, "create_group", lambda members, name="": {
        "success": False, "error": "除自己外至少要 2 个人才能建群",
    })
    code, out = run("--group", "陈涛")
    assert code == 1
    assert "至少要 2 个人" in out


def test_group_reports_client_errors_as_a_clean_message(run, monkeypatch):
    """搜出多条候选是可预期的失败,应该看到候选名单,而不是一串堆栈。"""

    def boom(members, name=""):
        raise cli.LocalSendError("「陈」在客户端里搜出多条: 陈涛, 陈挺, 陈珂")

    monkeypatch.setattr(cli, "create_group", boom)
    code, out = run("--group", "陈,陈挺")
    assert code == 1
    assert "陈珂" in out


def test_rename_needs_a_rid(run, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "rename_group", lambda *a: called.append(a))
    code, out = run("--rename", "新名字")
    assert code == 1
    assert called == []                      # 不知道改哪个群就别去动客户端
    assert "--rid" in out


def test_rename_passes_rid_and_name_and_reports_the_old_name(run, monkeypatch):
    monkeypatch.setattr(cli, "rename_group", lambda rid, name: {
        "success": True, "rid": rid, "name": name, "old_name": "陈挺、陈涛、任威风",
    })
    code, out = run("--rid", "77", "--rename", "客户群-新名字")
    assert code == 0
    assert "客户群-新名字" in out and "陈挺、陈涛、任威风" in out


def test_rename_verification_failure_exits_nonzero(run, monkeypatch):
    monkeypatch.setattr(cli, "rename_group", lambda rid, name: {
        "success": False, "error": "改名后核验失败: RID=77 的群名仍是 '老名字'",
    })
    code, out = run("--rid", "77", "--rename", "新名字")
    assert code == 1
    assert "核验失败" in out


def test_json_output_is_machine_readable(run, monkeypatch):
    monkeypatch.setattr(cli, "rename_group", lambda rid, name: {
        "success": True, "rid": rid, "name": name, "old_name": "老名字",
    })
    code, out = run("--rid", "77", "--rename", "新名字", "--json")
    assert code == 0
    assert json.loads(out) == {"success": True, "rid": 77, "name": "新名字", "old_name": "老名字"}


def test_sending_a_message_still_works(run, monkeypatch):
    """新加的开关不能挡住原来的主路径。"""
    monkeypatch.setattr(cli, "send_message", lambda to, content, conv_rid: {
        "success": True, "conv_name": "测试群", "conv_rid": conv_rid, "skipped_users": 0,
    })
    code, out = run("--rid", "3395103030", "测试")
    assert code == 0
    assert "测试群" in out
