"""Guard the side-effect boundaries: identity, exact membership and retry recovery."""

import sqlite3
from types import SimpleNamespace

import pytest

from wecom_svr import windows_group as group


@pytest.fixture
def directory(monkeypatch, tmp_path):
    path = tmp_path / "user.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE user_table(id INTEGER,name TEXT,corp_id INTEGER,external_corp_name TEXT DEFAULT '');
        CREATE TABLE department_tableV2(id INTEGER,name TEXT);
        CREATE TABLE user_dept_tableV2(user_id INTEGER,department_id INTEGER);
        INSERT INTO user_table(id,name,corp_id) VALUES
            (1,'葛波',10),(2,'陈涛',10),(3,'陈涛',20),(4,'伴风望海',20),(5,'客户乙',30);
        INSERT INTO user_table VALUES(6,'企业客户',40,'伙伴公司');
        INSERT INTO department_tableV2 VALUES(8,'技术支持部');
        INSERT INTO user_dept_tableV2 VALUES(2,8);
    """)
    conn.close()
    store = SimpleNamespace(self_id=lambda: 1, db=lambda _: sqlite3.connect(path), cache_dir=tmp_path)
    monkeypatch.setattr(group.windows_local, "store", lambda: store)
    return store


def test_homonym_rejected_with_explicit_candidates(directory):
    with pytest.raises(group.WindowsGroupError, match=r"userid:2.*userid:3"):
        group._resolve_members(["陈涛", "伴风望海"])


def test_department_and_userid_select_same_identity_and_self_is_removed(directory):
    members = group._resolve_members(["伴风望海", "葛波", "陈涛@技术支持部", "userid:2", "userid:4"])
    assert [m.userid for m in members] == ["4", "2"]
    assert members[0].external and not members[1].external
    assert members[0].query == "伴风望海"


def test_partial_names_do_not_guess(directory):
    with pytest.raises(group.WindowsGroupError, match="精确匹配"):
        group._resolve_members(["陈", "伴风望海"])


def test_self_and_duplicates_do_not_satisfy_minimum(directory):
    with pytest.raises(group.WindowsGroupError, match="不同成员"):
        group._resolve_members(["伴风望海", "葛波", "userid:4", "伴风望海"])


@pytest.mark.parametrize("queries", [
    [], ["", "伴风望海", "userid:2"], ["葛波", "伴风望海", "userid:2"],
    ["userid:2", "伴风望海"],
])
def test_first_query_must_be_external_without_reordering(directory, queries):
    with pytest.raises(group.WindowsGroupError, match=r"members\[0\]"):
        group._resolve_members(queries)


def test_external_company_is_resolved_for_exact_chat_title(directory):
    members = group._resolve_members(["企业客户", "userid:2"])
    assert members[0].external and members[0].company == "伙伴公司"


def snapshot(cid, ids):
    return {cid: {"rid": 7, "conversation_id": cid, "name": "", "userids": set(ids)}}


def test_only_new_group_with_exact_userids_is_success(monkeypatch):
    observations = iter([
        snapshot("R:old", {"1", "2", "4"}),
        snapshot("R:wrong", {"1", "3", "4"}),
        snapshot("R:new", {"1", "2", "4", "5"}),
        snapshot("R:new", {"1", "2", "4"}),
    ])
    monkeypatch.setattr(group, "_groups", lambda: next(observations))
    monkeypatch.setattr(group.time, "sleep", lambda _: None)
    assert group._verify_created({"1", "2", "4"}, {"R:old"})["conversation_id"] == "R:new"


def test_no_database_confirmation_means_failure(monkeypatch):
    monkeypatch.setattr(group, "_groups", lambda: snapshot("R:wrong", {"1", "3", "4"}))
    with pytest.raises(group.WindowsGroupError, match="禁止自动再次提交"):
        group._verify_created({"1", "2", "4"}, set(), timeout=0)


def test_ambiguous_new_groups_do_not_report_success():
    groups = snapshot("R:a", {"1", "2", "4"}) | snapshot("R:b", {"1", "2", "4"})
    with pytest.raises(group.WindowsGroupError, match="多个"):
        group._matching_group(groups, {"1", "2", "4"}, set())


def test_submit_failure_is_journaled_and_retry_does_not_click_again(directory, monkeypatch):
    from wecom_svr import windows_group_ui

    clicks = []

    class Picker:
        def open(self):
            pass

        def open_from_contact(self, member):
            pass

        def select(self, members):
            pass

        def submit(self):
            clicks.append("submit")
            raise group.WindowsGroupError("outcome unknown")

        def cancel(self):
            pytest.fail("must not cancel/retry after an uncertain submit")

        def _restore_clipboard(self):
            pass

    monkeypatch.setattr(windows_group_ui, "GroupPicker", Picker)
    monkeypatch.setattr(group, "_groups", lambda: {})
    members = group._resolve_members(["伴风望海", "userid:2"])
    with pytest.raises(group.WindowsGroupError, match="unknown"):
        group._create_locked(members)
    with pytest.raises(group.WindowsGroupError, match="未重复建群"):
        group._create_locked(members)
    assert clicks == ["submit"]
    # A late database commit on a subsequent retry recovers without another click.
    monkeypatch.setattr(group, "_groups", lambda: snapshot("R:new", {"1", "2", "4"}))
    result = group._create_locked(members)
    assert result["verified"] and result["reused"] and result["member_count"] == 3
    assert group._create_locked(members)["conversation_id"] == "R:new"
    assert clicks == ["submit"]


def test_unsupported_nickname_and_invalid_names_rejected_before_ui(monkeypatch):
    monkeypatch.setattr(group, "_resolve_members", lambda _: pytest.fail("must reject before any work"))
    monkeypatch.setattr(group.sys, "platform", "win32")
    assert not group.create_group(["a", "b"], nickname="x")["success"]
    assert not group.create_group(["a", "b"], name="x\ny")["success"]
    assert not group.create_group(["a", "b"], name="x" * (group.MAX_NAME_LEN + 1))["success"]


def test_prepare_selects_and_cancels_without_journal_or_submit(directory, monkeypatch):
    from wecom_svr import windows_group_ui

    calls = []
    picker = SimpleNamespace(open=lambda: pytest.fail("must start from first contact's chat"),
                             open_from_contact=lambda member: calls.append(("external", member.userid)),
                             select=lambda members: calls.append(("select", [m.userid for m in members])),
                             cancel=lambda: calls.append("cancel"),
                             submit=lambda: pytest.fail("prepare must not submit"))
    monkeypatch.setattr(windows_group_ui, "GroupPicker", lambda: picker)
    members = group._resolve_members(["伴风望海", "userid:2"])
    assert group._create_locked(members, name="三人组", prepare_only=True)["submitted"] is False
    assert calls == [("external", "4"), ("select", ["2"]), "cancel"]
    assert not (directory.cache_dir / "group-requests").exists()


def test_multiple_external_members_still_start_from_array_first(directory, monkeypatch):
    from wecom_svr import windows_group_ui

    calls = []
    picker = SimpleNamespace(open=lambda: pytest.fail("must not switch to Ctrl+N"),
                             open_from_contact=lambda member: calls.append(member.userid),
                             select=lambda members: calls.append([m.userid for m in members]),
                             cancel=lambda: None)
    monkeypatch.setattr(windows_group_ui, "GroupPicker", lambda: picker)
    members = group._resolve_members(["客户乙", "伴风望海", "葛波", "userid:2", "客户乙"])
    assert group._create_locked(members, prepare_only=True)["prepared"]
    assert calls == ["5", ["4", "2"]]


def test_invalid_anchor_rejected_before_ui_or_existing_group_lookup(directory, monkeypatch):
    monkeypatch.setattr(group, "_groups", lambda: pytest.fail("must validate anchor first"))
    monkeypatch.setattr(group.sys, "platform", "win32")
    result = group.create_group(["葛波", "伴风望海", "userid:2"], name="三人组")
    assert not result["success"] and "members[0]" in result["error"]


def test_name_verification_checks_membership_not_just_same_name(monkeypatch):
    groups = snapshot("R:new", {"1", "3", "4"})
    groups["R:new"]["name"] = "三人组"
    monkeypatch.setattr(group, "_groups", lambda: groups)
    with pytest.raises(group.WindowsGroupError, match="成员"):
        group._verify_name("R:new", {"1", "2", "4"}, "三人组", timeout=0)


def test_ui_rename_without_database_confirmation_is_partial_success(monkeypatch):
    target = snapshot("R:new", {"1", "2", "4"})["R:new"]
    picker = SimpleNamespace(rename=lambda *_args: (True, None))

    def no_confirmation(*_args):
        raise group.WindowsGroupError("数据库尚未确认")

    monkeypatch.setattr(group, "_verify_name", no_confirmation)
    result = group._rename_created(picker, target, [group.Member("陈涛", "陈涛", "2")], "三人组")
    assert result["success"] and not result["name_verified"]
    assert result["name"] == "" and "尚未确认" in result["name_error"]


def test_verified_name_is_returned_after_database_confirmation(monkeypatch):
    target = snapshot("R:new", {"1", "2", "4"})["R:new"]
    picker = SimpleNamespace(rename=lambda *_args: (True, None))
    monkeypatch.setattr(group, "_verify_name", lambda *_args: {**target, "name": "三人组"})
    result = group._rename_created(picker, target, [], "三人组")
    assert result["success"] and result["name_verified"] and result["name"] == "三人组"


def test_named_exact_existing_group_is_reused_without_ui(directory, monkeypatch):
    from wecom_svr import windows_group_ui

    groups = snapshot("R:existing", {"1", "2", "4"})
    groups["R:existing"]["name"] = "三人组"
    monkeypatch.setattr(group, "_groups", lambda: groups)
    monkeypatch.setattr(windows_group_ui, "GroupPicker", lambda: pytest.fail("must not invite again"))
    members = group._resolve_members(["伴风望海", "userid:2"])
    result = group._create_locked(members, name="三人组")
    assert result["reused"] and result["verified"] and result["name_verified"]


def test_recovered_group_does_not_claim_an_unconfirmed_name():
    target = snapshot("R:new", {"1", "2", "4"})["R:new"]
    result = group._recovered(target, [], "三人组")
    assert result["reused"] and not result["name_verified"] and result["name_error"]
