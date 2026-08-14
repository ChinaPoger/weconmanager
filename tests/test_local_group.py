"""以本人身份拉群 —— 契约测试,不碰真实客户端。

打桩打在 `_run_applescript` 上,也就是真正的边界(osascript)。这样搜索结果
解析、唯一性校验、计数回读、失败时关面板这些逻辑都是真跑的,只有 AppleScript
本身被替换成一个假客户端。
"""
import re

import pytest
from fastapi.testclient import TestClient

from wecom_svr import directory, local_group, local_send, ui_offsets
from wecom_svr.main import create_app
from wecom_svr.settings import get_agent_home, get_settings

SEP_ROW = local_group._ROW_SEP
SEP_FIELD = local_group._FIELD_SEP


class FakeClient:
    """够用的假企业微信: 面板开关 + 搜索结果 + 已选计数。"""

    def __init__(self, directory: dict[str, list[tuple[str, str]]]):
        self.directory = directory
        self.sheet_open = False
        self.selected = 0
        self.last_search = ""
        self.confirmed = False
        self.scripts: list[str] = []
        self.plus_clicked = False

    def __call__(self, script: str, timeout: int = 15) -> tuple[bool, str]:
        """按脚本特征分派。

        顺序要紧: 面板相关的脚本都带 _PICKER 前缀(里面含 sheet 探测语句),
        所以必须先认动作、最后才认那条独立的 sheet 探测 return。
        """
        self.scripts.append(script)

        if "to activate" in script:
            return True, ""
        if "key code 53" in script:            # Esc 关面板
            self.sheet_open = False
            return True, ""
        if "click at {" in script:             # 点 "+"
            self.plus_clicked = True
            self.sheet_open = True
            return True, ""

        needs_sheet = "splitter group 1 of sheet 1" in script
        if needs_sheet and not self.sheet_open:
            return False, "NO_SHEET"

        if "set value of tf to" in script:     # 搜索: 先总数,再前几行明细
            self.last_search = script.split('set value of tf to "')[1].split('"')[0]
            rows = self.directory.get(self.last_search, [])
            body = "".join(f"{n}{SEP_FIELD}{d}{SEP_ROW}" for n, d in rows[:8])
            return True, f"{len(rows)}{SEP_ROW}{body}"
        if "keystroke return" in script:       # 选中第一条
            if self.directory.get(self.last_search):
                self.selected += 1
            return True, ""
        if 'button "Cancel"' in script:        # 关面板
            self.sheet_open = False
            return True, ""
        if "AXPress" in script:                # 按「完成」
            if self.selected == 0:
                return False, "「完成」按钮是灰的,一个人都没选中"
            self.confirmed = True
            self.sheet_open = False
            return True, ""
        if "group 2 of sg" in script:          # 读已选计数
            return True, f"Initiate a group chat|{self.selected} contact(s) selected|"
        if "count of sheets of mainWin" in script:
            return True, "1" if self.sheet_open else "0"

        return True, ""


DIRECTORY = {
    "陈涛": [("陈涛(水淼淼)", "增长产品部/业务成功部")],
    "陈挺": [("陈挺", "增长产品部/业务成功部")],
    "王小明": [("王小明", "产品研发部")],
    "陈": [("陈涛(水淼淼)", "增长产品部"), ("陈挺", "增长产品部"), ("陈珂", "运营部")],
}


@pytest.fixture(autouse=True)
def _fast_and_macos(monkeypatch, tmp_path):
    monkeypatch.delenv("WECOM_SVR_API_TOKEN", raising=False)
    # 校准学到的偏移会落盘 —— 指到 tmp_path,别把仓库里的 .data 写脏
    monkeypatch.setenv("WECOM_SVR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_agent_home.cache_clear()
    monkeypatch.setattr(local_group, "ensure_macos", lambda: None)
    monkeypatch.setattr(local_group.time, "sleep", lambda _s: None)
    monkeypatch.setattr(local_group, "SHEET_TIMEOUT", 0.1)
    monkeypatch.setattr(local_group, "PROBE_TIMEOUT", 0.1)
    monkeypatch.setattr(directory, "get_self", lambda: {"userid": "me"})
    monkeypatch.setattr(directory, "resolve_member", lambda q: {"userid": q})
    yield
    get_settings.cache_clear()
    get_agent_home.cache_clear()


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient(DIRECTORY)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    return client


@pytest.fixture
def api():
    return TestClient(create_app())


# ─── 正常路径 ──────────────────────────────────────────────────────────

def test_creates_group_and_reports_who_was_picked(fake):
    r = local_group.create_group(["陈涛", "陈挺"])
    assert r["success"] is True
    assert fake.confirmed is True
    assert [m["name"] for m in r["members"]] == ["陈涛(水淼淼)", "陈挺"]
    assert [m["query"] for m in r["members"]] == ["陈涛", "陈挺"]
    assert r["member_count"] == 3  # 两个人 + 自己


def test_dedups_and_trims_input(fake):
    r = local_group.create_group([" 陈涛 ", "陈挺", "陈涛", ""])
    assert r["success"] is True
    assert len(r["members"]) == 2


def test_ignores_current_user_in_members(fake, monkeypatch):
    monkeypatch.setattr(
        directory,
        "resolve_member",
        lambda q: {"userid": "me" if q == "zixunguwen" else q},
    )
    r = local_group.create_group(["陈涛", "陈挺", "zixunguwen"])
    assert r["success"] is True
    assert [m["query"] for m in r["members"]] == ["陈涛", "陈挺"]
    assert r["member_count"] == 3
    assert "zixunguwen" not in fake.scripts


def test_self_filter_degrades_when_directory_is_unavailable(fake, monkeypatch):
    monkeypatch.setattr(
        directory,
        "get_self",
        lambda: (_ for _ in ()).throw(local_group.LocalSendError("db unavailable")),
    )
    r = local_group.create_group(["陈涛", "陈挺"])
    assert r["success"] is True


def test_opens_picker_before_selecting(fake):
    local_group.create_group(["陈涛", "陈挺"])
    assert fake.plus_clicked is True


# ─── 拒绝路径:宁可不建,也不建错 ──────────────────────────────────────

def test_ambiguous_name_aborts_with_candidates(fake):
    with pytest.raises(local_group.AmbiguousCandidate) as e:
        local_group.create_group(["陈", "陈挺"])
    assert "陈涛(水淼淼)" in str(e.value) and "陈珂" in str(e.value)
    assert fake.confirmed is False
    assert fake.sheet_open is False  # 失败也要把面板收掉


def test_unknown_name_aborts(fake):
    with pytest.raises(local_group.LocalGroupError, match="搜不到"):
        local_group.create_group(["查无此人", "陈挺"])
    assert fake.confirmed is False
    assert fake.sheet_open is False


def test_count_mismatch_aborts_before_confirming(fake, monkeypatch):
    """客户端没真的选上时,计数对不上 —— 必须中止,不能硬按「完成」。"""
    monkeypatch.setattr(local_group, "_selected_count", lambda: 0)
    with pytest.raises(local_group.LocalGroupError, match="已选人数"):
        local_group.create_group(["陈涛", "陈挺"])
    assert fake.confirmed is False


def test_needs_at_least_two_others(fake):
    r = local_group.create_group(["陈涛"])
    assert r["success"] is False
    assert "至少要 2 个人" in r["error"]
    assert fake.plus_clicked is False  # 参数不对就别去动客户端


def test_needs_two_others_after_ignoring_self(fake, monkeypatch):
    monkeypatch.setattr(
        directory,
        "resolve_member",
        lambda q: {"userid": "me" if q == "zixunguwen" else q},
    )
    r = local_group.create_group(["陈涛", "zixunguwen"])
    assert r["success"] is False
    assert "至少要 2 个人" in r["error"]
    assert fake.plus_clicked is False


def test_refuses_when_a_panel_is_already_open(fake):
    fake.sheet_open = True
    with pytest.raises(local_group.LocalGroupError, match="已经有一个弹出面板"):
        local_group.create_group(["陈涛", "陈挺"])


def test_missing_panel_after_plus_is_a_clear_error(monkeypatch):
    class NeverOpens(FakeClient):
        def __call__(self, script, timeout=15):
            if "click at {" in script:
                return True, ""      # 点了,但面板没开
            return super().__call__(script, timeout)

    client = NeverOpens(DIRECTORY)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    with pytest.raises(local_group.LocalGroupError, match="面板没出现"):
        local_group.create_group(["陈涛", "陈挺"])


# ─── 坐标自动校准 ──────────────────────────────────────────────────────
#
# "+" 仍只能按坐标点;会话标题坐标是旧版客户端兼容路径。量出来的常量迟早会被
# 客户端改版打掉,所以点不中要能自己在附近找回来 —— 这几条守的就是那件事。

class OnlyAtOffset(FakeClient):
    """只有点在某个横向偏移上才会开面板,用来验校准真的在挪位置。"""

    def __init__(self, directory, want_dx: int):
        super().__init__(directory)
        self.want_dx = want_dx
        self.clicks: list[int] = []

    def __call__(self, script, timeout=15):
        if "click at {" in script:
            self.clicks.append(int(re.search(r"item 1 of p\) \+ (-?\d+)", script).group(1)))
            if self.clicks[-1] == self.want_dx:
                self.sheet_open = True
                self.plus_clicked = True
            return True, ""
        return super().__call__(script, timeout)


def test_plus_offset_is_calibrated_and_remembered(monkeypatch):
    base_x, base_y = ui_offsets.DEFAULTS["plus"]
    client = OnlyAtOffset(DIRECTORY, want_dx=base_x - 20)
    monkeypatch.setattr(local_group, "_run_applescript", client)

    r = local_group.create_group(["陈涛", "陈挺"])
    assert r["success"] is True
    assert client.clicks == [base_x, base_x - 20]      # 先试原位,再往左挪
    assert ui_offsets.primary("plus") == (base_x - 20, base_y)

    # 记住了就该一发命中 —— 否则每次建群都要白点一轮
    again = OnlyAtOffset(DIRECTORY, want_dx=base_x - 20)
    monkeypatch.setattr(local_group, "_run_applescript", again)
    assert local_group.create_group(["陈涛", "陈挺"])["success"] is True
    assert again.clicks == [base_x - 20]


def test_a_pinned_offset_disables_probing(monkeypatch):
    """人显式配了偏移就只点那一个 —— 配置是指令,试探是猜测,猜测别盖过指令。"""
    monkeypatch.setenv("WECOM_SVR_UI_PLUS_OFFSET", "301,31")
    get_settings.cache_clear()

    client = OnlyAtOffset(DIRECTORY, want_dx=-9999)   # 怎么点都开不了
    monkeypatch.setattr(local_group, "_run_applescript", client)
    with pytest.raises(local_group.LocalGroupError, match="面板没出现"):
        local_group.create_group(["陈涛", "陈挺"])
    assert client.clicks == [301]


def test_an_unfamiliar_sheet_stops_the_probing(monkeypatch):
    """点开的不是选人面板就立刻停 —— 继续试探等于在一个陌生面板上乱点。"""

    class OpensSomethingElse(FakeClient):
        def __call__(self, script, timeout=15):
            if "click at {" in script:
                self.sheet_open = True
                return True, ""
            if "splitter group 1 of sh)" in script:     # 面板状态探测
                return True, "2" if self.sheet_open else "0"
            return super().__call__(script, timeout)

    monkeypatch.setattr(local_group, "_run_applescript", OpensSomethingElse(DIRECTORY))
    with pytest.raises(local_group.LocalGroupError, match="不是「发起群聊」面板"):
        local_group.create_group(["陈涛", "陈挺"])


def test_title_offset_is_calibrated_and_remembered(rename_env, monkeypatch):
    base_x, base_y = ui_offsets.DEFAULTS["title"]
    hit_x = 100 + base_x + 40            # rename_env 里 _window_point 是 (100+dx, 200+dy)
    clicked: list[int] = []

    def click(x, y):
        clicked.append(x)
        if x == hit_x:                    # 只有挪过去 40 点才点得到标题
            local_group._set_clipboard(rename_env["names"][77])

    monkeypatch.setattr(local_group, "_real_click", click)

    r = local_group.rename_group(77, "临时讨论组")
    assert r["success"] is True
    assert clicked == [100 + base_x, hit_x]
    assert ui_offsets.primary("title") == (base_x + 40, base_y)


def test_title_probing_never_types_anything(rename_env, monkeypatch):
    """试探只做「选中 + 复制」。一次没点中的试探如果会打字,那就是往别人的聊天里发消息。"""
    monkeypatch.setattr(local_group, "_real_click", lambda x, y: None)
    with pytest.raises(local_group.LocalGroupError, match="没有输入框拿到焦点"):
        local_group.rename_group(77, "临时讨论组")
    assert rename_env["typed"] is None
    assert rename_env["names"][77] == "陈挺、陈涛、任威风"


# ─── 回车落空时的重试 ──────────────────────────────────────────────────

class DropsEnter(FakeClient):
    """前 N 次回车静默落空 —— 复现实测遇到的「已选人数没涨」。"""

    def __init__(self, directory, drop: int):
        super().__init__(directory)
        self.to_drop = drop
        self.enters = 0

    def __call__(self, script, timeout=15):
        if "keystroke return" in script and "splitter group 1 of sheet 1" in script:
            self.enters += 1
            if self.enters <= self.to_drop:
                return True, ""          # 按了,但客户端没收到
        return super().__call__(script, timeout)


def test_lost_enter_is_retried_and_succeeds(monkeypatch):
    client = DropsEnter(DIRECTORY, drop=1)
    monkeypatch.setattr(local_group, "_run_applescript", client)

    r = local_group.create_group(["陈涛", "陈挺"])
    assert r["success"] is True
    assert client.confirmed is True
    assert client.enters == 3            # 第一次落空 + 重试 + 第二个人
    assert [m["query"] for m in r["members"]] == ["陈涛", "陈挺"]


def test_gives_up_after_the_retry_budget(monkeypatch):
    client = DropsEnter(DIRECTORY, drop=99)   # 永远落空
    monkeypatch.setattr(local_group, "_run_applescript", client)

    with pytest.raises(local_group.LocalGroupError, match="试了 3 次"):
        local_group.create_group(["陈涛", "陈挺"])
    assert client.confirmed is False
    assert client.sheet_open is False          # 放弃时也要收掉面板


def test_a_late_count_does_not_trigger_a_second_enter(fake, monkeypatch):
    """计数只是读早了 —— 绝不能重按回车,那会把已经选上的人取消掉。"""
    reads = iter([0, 1, 1, 2, 2, 2, 2])
    monkeypatch.setattr(local_group, "_selected_count", lambda: next(reads))

    r = local_group.create_group(["陈涛", "陈挺"])
    assert r["success"] is True
    enters = [s for s in fake.scripts
              if "keystroke return" in s and "splitter group 1 of sheet 1" in s]
    assert len(enters) == 2      # 一人一次,没有多按


def test_overshoot_aborts_instead_of_compensating(fake, monkeypatch):
    """多选了人比少选危险 —— 不能再点几下去凑,必须停。"""
    monkeypatch.setattr(local_group, "_selected_count", lambda: 5)
    with pytest.raises(local_group.LocalGroupError, match="超过预期"):
        local_group.create_group(["陈涛", "陈挺"])
    assert fake.confirmed is False


# ─── 改群名 ────────────────────────────────────────────────────────────

@pytest.fixture
def rename_env(monkeypatch, fake):
    """假的「点标题 → 输入框 → 回车 → 落库」链路。"""
    state = {"names": {77: "陈挺、陈涛、任威风"}, "clicked": False, "typed": None, "escaped": False}

    monkeypatch.setattr(local_group.local_send, "get_conversation_by_rid",
                        lambda rid: {"name": "", "rid": rid, "lid": 1, "type": 1, "fw_id": 0})
    monkeypatch.setattr(local_group.local_send, "_open_conversation_by_scheme",
                        lambda conv, **kw: (True, "ok"))
    # 这个 fixture 测旧版坐标兼容路径;新版 AX 路径单独在下方覆盖。
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: None)
    monkeypatch.setattr(local_group, "_window_point", lambda dx, dy: (100 + dx, 200 + dy))
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(state["names"]))

    clip = {"v": ""}
    monkeypatch.setattr(local_group, "_get_clipboard", lambda: clip["v"])
    monkeypatch.setattr(local_group, "_set_clipboard", lambda t: clip.update(v=t))

    def click(x, y):
        state["clicked"] = True
        clip["v"] = state["names"][77]      # 输入框打开,全选复制拿到当前群名

    monkeypatch.setattr(local_group, "_real_click", click)

    orig_osa = local_group._osa

    def osa(body, timeout=20):
        if "key code 53" in body:                      # Esc 退掉改名输入框
            state["escaped"] = True
            return ""
        if "command down" in body and '"v"' in body:   # 粘贴 + 回车
            state["typed"] = clip["v"]
            state["names"][77] = clip["v"]
            return ""
        if "command down" in body:                      # 全选 + 复制
            return ""
        return orig_osa(body, timeout)

    monkeypatch.setattr(local_group, "_osa", osa)
    return state


def test_rename_verifies_against_the_database(rename_env):
    r = local_group.rename_group(77, "临时讨论组")
    assert r["success"] is True
    assert r["old_name"] == "陈挺、陈涛、任威风"
    assert rename_env["names"][77] == "临时讨论组"


def test_rename_prefers_writable_ax_title(rename_env, monkeypatch):
    """5.0.9 的标题是可写 AXTextField,不应再走剪贴板和坐标探测。"""
    def write_ax_title(name):
        rename_env["names"][77] = name

    monkeypatch.setattr(local_group, "_read_ax_title", lambda: rename_env["names"][77])
    monkeypatch.setattr(local_group, "_write_ax_title", write_ax_title)
    monkeypatch.setattr(
        local_group,
        "_open_edit_box_and_read_current",
        lambda: pytest.fail("新版 AX 标题可用时不应走坐标兼容路径"),
    )

    r = local_group.rename_group(77, "临时讨论组")

    assert r["success"] is True
    assert r["old_name"] == "陈挺、陈涛、任威风"
    assert rename_env["names"][77] == "临时讨论组"
    assert rename_env["clicked"] is False


def test_rename_aborts_when_no_edit_box_took_focus(rename_env, monkeypatch):
    """点了标题但没有输入框拿到焦点 —— 再往下走会把群名发成一条消息。"""
    monkeypatch.setattr(local_group, "_real_click", lambda x, y: None)  # 剪贴板保持哨兵
    with pytest.raises(local_group.LocalGroupError, match="没有输入框拿到焦点"):
        local_group.rename_group(77, "临时讨论组")
    assert rename_env["typed"] is None


def test_rename_aborts_when_clipboard_looks_like_chat_history(rename_env, monkeypatch):
    def click(x, y):
        local_group._set_clipboard("张三: 你好\n李四: 在的\n" * 5)

    monkeypatch.setattr(local_group, "_real_click", click)
    with pytest.raises(local_group.LocalGroupError, match="不像群名"):
        local_group.rename_group(77, "临时讨论组")
    assert rename_env["typed"] is None


def test_clipboard_with_non_utf8_bytes_decodes_instead_of_raising(monkeypatch):
    """剪贴板里可能是图片/富文本。解码异常穿透到 HTTP 层就是 500,而不是「不像群名」。

    直接测 local_send 里的真身: 改名流程用的是 from-import 进来的同一个函数,
    rename_env 会把它整个换掉,从那边测不到解码这一步。
    """
    monkeypatch.setattr(
        local_send.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": b"\xc8\xff"})(),
    )

    assert local_send._get_clipboard() == "��"


def test_a_probe_that_reads_undecodable_bytes_is_not_a_group_name(rename_env, monkeypatch):
    """解码后的替换字符必须被判成「不像群名」,否则会把乱码当群名敲进去。"""
    monkeypatch.setattr(local_group, "_real_click",
                        lambda x, y: local_group._set_clipboard("��"))

    with pytest.raises(local_group.LocalGroupError, match="不像群名"):
        local_group.rename_group(77, "临时讨论组")
    assert rename_env["typed"] is None


def test_rename_reports_when_the_name_landed_on_another_group(rename_env, monkeypatch):
    """核验发现名字改到别的群上了 —— 必须说清楚是哪个,而不是笼统报失败。"""
    monkeypatch.setattr(local_group, "_group_names",
                        lambda: {77: "陈挺、陈涛、任威风", 99: "临时讨论组"})
    r = local_group.rename_group(77, "临时讨论组")
    assert r["success"] is False
    assert "RID=99" in r["error"]


def test_rename_rejects_too_long_name(rename_env):
    r = local_group.rename_group(77, "a" * 51)
    assert r["success"] is False
    assert "最多 50" in r["error"]


def _snapshots(monkeypatch, *frames):
    """按顺序吐出几帧「当前群列表」,最后一帧之后一直重复它。"""
    seen = list(frames)

    def next_frame():
        return dict(seen.pop(0) if len(seen) > 1 else seen[0])

    monkeypatch.setattr(local_group, "_group_names", next_frame)


def test_create_with_name_locates_the_new_group_by_diff(fake, monkeypatch):
    names = {1: "旧群"}
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    monkeypatch.setattr(local_group, "GROUP_APPEAR_SETTLE", 0.0)
    captured = {}

    def fake_rename(rid, name, expect_members=None):
        captured.update(rid=rid, name=name, expect_members=expect_members)
        return {"success": True}

    # 打桩打在不持锁的内部函数上 —— create_group 全程只拿一次 ui_session,
    # 中途去调持锁的 rename_group 会自锁死。
    monkeypatch.setattr(local_group, "_rename_locked", fake_rename)

    def confirm():
        names[42] = ""      # 新群落库,名字是空的
        fake.confirmed = True
        fake.sheet_open = False

    monkeypatch.setattr(local_group, "_confirm", confirm)

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")
    assert r["success"] is True
    assert r["rid"] == 42 and r["name"] == "临时讨论组"
    assert captured["rid"] == 42 and captured["name"] == "临时讨论组"
    # 改名前要拿成员名核对归属,所以必须把选中的人传下去
    assert captured["expect_members"] == ["陈涛(水淼淼)", "陈挺"]


def test_create_with_name_waits_for_delayed_database_visibility(fake, monkeypatch):
    _snapshots(monkeypatch, {1: "旧群"}, {1: "旧群"}, {1: "旧群", 42: ""})
    captured = {}
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda rid, name, expect_members=None: (
            captured.update(rid=rid, name=name) or {"success": True}
        ),
    )
    monkeypatch.setattr(local_group, "GROUP_APPEAR_TIMEOUT", 5.0)
    monkeypatch.setattr(local_group, "GROUP_APPEAR_SETTLE", 0.0)

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")

    assert r["success"] is True
    assert r["rid"] == 42
    assert r["name"] == "临时讨论组"
    assert captured == {"rid": 42, "name": "临时讨论组"}


def test_a_second_conversation_landing_right_after_blocks_the_rename(fake, monkeypatch):
    """看到一个新会话就立刻改名,会改到别人刚把你拉进的群上 —— 要多等一拍。"""
    _snapshots(monkeypatch, {1: "旧群"}, {1: "旧群", 42: ""}, {1: "旧群", 42: "", 43: ""})
    monkeypatch.setattr(local_group, "GROUP_APPEAR_SETTLE", 0.0)
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda *a, **kw: pytest.fail("新增会话不唯一时不该动任何一个群"),
    )

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")

    assert r["success"] is True          # 群确实建出来了
    assert r["rid"] is None
    assert "新增 2 个" in r["name_error"]


def test_rename_refuses_a_conversation_that_is_not_the_new_group(rename_env):
    """差集选中的会话标题不是这几个人拼出来的 —— 一个键都不能敲。"""
    r = local_group._rename_locked(77, "临时讨论组", expect_members=["张三", "李四"])

    assert r["success"] is False
    assert r["unrelated"] is True
    assert rename_env["typed"] is None
    assert rename_env["names"][77] == "陈挺、陈涛、任威风"
    assert rename_env["escaped"] is True          # 打开的输入框要原样退掉
    assert "请手动改名" in r["error"]


def test_rename_accepts_the_title_composed_from_the_picked_members(rename_env):
    r = local_group._rename_locked(77, "临时讨论组", expect_members=["陈挺", "陈涛", "任威风"])

    assert r["success"] is True
    assert rename_env["names"][77] == "临时讨论组"


@pytest.mark.parametrize(
    "title, members, expected",
    [
        ("", ["陈挺", "陈涛"], True),                       # 新群可以没标题
        ("陈挺、陈涛", ["陈挺", "陈涛"], True),
        ("我、陈挺、陈涛", ["陈挺", "陈涛"], True),          # 自己也被拼进去
        ("陈挺、陈涛…", ["陈挺", "陈涛"], True),            # 末段被截断
        ("陈挺、陈涛(3)", ["陈挺", "陈涛"], True),          # 带人数后缀
        # 选人面板给的是「陈涛(水淼淼)」,标题里只会出现其中一种写法
        ("陈挺、陈涛", ["陈挺", "陈涛(水淼淼)"], True),
        ("陈挺、水淼淼", ["陈挺", "陈涛(水淼淼)"], True),
        ("售后支持群", ["陈挺", "陈涛"], False),            # 远端拉你进的是个有名字的群
        ("陈挺、王五", ["陈挺", "陈涛"], False),            # 成员名单对不上
        ("陈挺", ["陈挺", "陈涛"], False),                  # 只有一半人,认不出来就不改
        ("陈挺、王五…", ["陈挺", "陈涛"], False),           # 截断的标题里冒出陌生人
        ("王五", ["陈挺", "陈涛"], False),
    ],
)
def test_auto_composed_title_recognition(title, members, expected):
    assert local_group._looks_like_a_just_created_group(title, members) is expected


# ─── HTTP 层 ───────────────────────────────────────────────────────────

def test_endpoint_returns_local_error_shape(api, fake):
    r = api.post("/api/v1/group/create", json={"members": ["陈", "陈挺"]})
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert "陈珂" in body["error"]


def test_endpoint_success(api, fake):
    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"]})
    assert r.status_code == 200
    assert r.json()["member_count"] == 3


def test_endpoint_rejects_single_member(api, fake):
    r = api.post("/api/v1/group/create", json={"members": ["陈涛"]})
    assert r.status_code == 400
    assert r.json()["success"] is False
    assert fake.plus_clicked is False


def test_rename_endpoint_success(api, rename_env):
    r = api.post("/api/v1/group/rename", json={"conv_rid": 77, "name": "客户群-新名字"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["name"] == "客户群-新名字"
    assert body["old_name"] == "陈挺、陈涛、任威风"
    assert rename_env["names"][77] == "客户群-新名字"


def test_rename_endpoint_reports_verification_failure(api, rename_env, monkeypatch):
    """回库核验不过要回 400 —— 「脚本没报错」不等于名字改上去了。"""
    monkeypatch.setattr(local_group, "_group_names", lambda: {77: "还是老名字"})
    r = api.post("/api/v1/group/rename", json={"conv_rid": 77, "name": "新名字"})
    assert r.status_code == 400
    assert r.json()["success"] is False
    assert "核验失败" in r.json()["error"]


def test_rename_endpoint_rejects_empty_and_overlong_names(api, rename_env):
    for name in ("", "很长" * 40):
        r = api.post("/api/v1/group/rename", json={"conv_rid": 77, "name": name})
        assert r.status_code == 400, name
        assert r.json()["success"] is False
    assert rename_env["names"][77] == "陈挺、陈涛、任威风"     # 一次都没动过客户端
