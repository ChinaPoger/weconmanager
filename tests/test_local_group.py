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
    """够用的假企业微信: 面板开关 + 搜索结果 + 已选计数。

    空格分隔且每个词都能唯一命中时,写入搜索框就自动勾上 —— 模拟客户端
    「中文全名完全匹配自动选中」。auto_bulk=False 则关掉这个捷径,用来测逐个搜索。
    """

    def __init__(self, directory: dict[str, list[tuple[str, str]]], auto_bulk: bool = True):
        self.directory = directory
        self.auto_bulk = auto_bulk
        self.sheet_open = False
        self.selected = 0
        self.last_search = ""
        self.confirmed = False
        self.scripts: list[str] = []
        self.plus_clicked = False
        self.clicked_names: set[str] = set()

    def _rows_for(self, keyword: str) -> list[tuple[str, str]]:
        parts = [p for p in (keyword or "").split() if p]
        if not parts:
            return []
        if len(parts) > 1 and not self.auto_bulk:
            return []
        if len(parts) == 1:
            return list(self.directory.get(parts[0], []))
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for part in parts:
            for row in self.directory.get(part, []):
                if row[0] in seen:
                    continue
                seen.add(row[0])
                out.append(row)
        return out

    def _auto_selected_names(self) -> set[str]:
        names = set(self.clicked_names)
        if not self.auto_bulk:
            return names
        for part in (self.last_search or "").split():
            if not local_group._looks_like_chinese_fullname(part):
                continue
            rows = self.directory.get(part, [])
            if len(rows) == 1:
                names.add(rows[0][0])
        return names

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

        if "set value of tf to" in script:     # 只写入,结果要另一次脚本来读
            self.last_search = script.split('set value of tf to "')[1].split('"')[0]
            parts = [p for p in self.last_search.split() if p]
            if self.auto_bulk:
                auto = [
                    p for p in parts
                    if local_group._looks_like_chinese_fullname(p)
                    and len(self.directory.get(p, [])) == 1
                ]
                if auto:
                    self.selected = len(auto)
            return True, ""
        if "PICKER_RESULT_ROWS" in script:
            rows = self._rows_for(self.last_search)
            selected = self._auto_selected_names()
            body = "".join(
                f"{n}{SEP_FIELD}{d}{SEP_FIELD}{'1' if n in selected else '0'}"
                f"{SEP_FIELD}10,20,120,28{SEP_ROW}"
                for n, d in rows[: local_group.PICKER_ROW_CAP]
            )
            return True, f"{len(rows)}{SEP_ROW}{body}"
        if "CLICK_PICKER_ROW" in script:
            name = script.split('set clickName to "')[1].split('"')[0]
            self.clicked_names.add(name)
            self.selected += 1
            return True, ""
        if "SEARCH_ROWS" in script or "count of rows of ol" in script:
            rows = self._rows_for(self.last_search)
            body = "".join(f"{n}{SEP_FIELD}{d}{SEP_ROW}" for n, d in rows[:8])
            return True, f"{len(rows)}{SEP_ROW}{body}"
        if "keystroke return" in script:       # 选中第一条
            if self._rows_for(self.last_search):
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
        if "return value of target as text" in script:
            # 确认后窗口已经是新群;单测不模拟切会话延迟
            return True, "陈涛、陈挺"
        if "set value of target to" in script and "AXConfirm" in script:
            return True, "ok"

        return True, ""


DIRECTORY = {
    "陈涛": [("陈涛(水淼淼)", "增长产品部/业务成功部")],
    "陈挺": [("陈挺", "增长产品部/业务成功部")],
    "王小明": [("王小明", "产品研发部")],
    "SMB售前bot": [("SMB售前bot", "我的客户")],
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
    monkeypatch.setattr(local_group, "_ui_click", lambda x, y: None)
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
    # 中文全名走空格一次勾选,响应里的 name 就是查询词(不再逐个搜备注名)
    assert [m["name"] for m in r["members"]] == ["陈涛", "陈挺"]
    assert [m["query"] for m in r["members"]] == ["陈涛", "陈挺"]
    assert r["member_count"] == 3  # 两个人 + 自己
    enters = [s for s in fake.scripts if "keystroke return" in s]
    assert enters == []  # 一次勾上就不用回车


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
    with pytest.raises(local_group.LocalGroupError, match=r"试了 3 次|已选人数"):
        local_group.create_group(["陈涛", "陈挺"])
    assert fake.confirmed is False


def test_space_separated_chinese_names_are_selected_without_enter(fake):
    r = local_group.create_group(["陈涛", "陈挺", "王小明"])
    assert r["success"] is True
    assert fake.selected == 3
    assert [m["query"] for m in r["members"]] == ["陈涛", "陈挺", "王小明"]
    assert not any("keystroke return" in s for s in fake.scripts)


def test_bulk_search_keeps_caller_order():
    assert local_group._bulk_search_text(["陈涛", "SMB售前bot", "陈挺"]) == (
        "陈涛 SMB售前bot 陈挺"
    )
    assert local_group._bulk_search_text(["chenting01", "chentao", "梁超"]) == (
        "chenting01 chentao 梁超"
    )


def test_pinyin_names_are_clicked_after_chinese_auto_select(fake):
    """空格一次搜到所有人;中文自动勾上,弹框里未勾的英文/拼音点选,不用回车。"""
    r = local_group.create_group(["陈涛", "SMB售前bot", "陈挺"])
    assert r["success"] is True
    assert [m["query"] for m in r["members"]] == ["陈涛", "SMB售前bot", "陈挺"]
    assert fake.selected == 3
    assert fake.last_search == "陈涛 SMB售前bot 陈挺"
    assert not any("keystroke return" in s for s in fake.scripts)
    clicks = [s for s in fake.scripts if "CLICK_PICKER_ROW" in s]
    assert len(clicks) == 1
    assert "SMB售前bot" in clicks[0]


class DropsPickerClick(FakeClient):
    """指定联系人前 N 次真实点击落空,之后才正常选中。"""

    def __init__(self, directory, name: str, drop: int):
        super().__init__(directory)
        self.drop_name = name
        self.to_drop = drop
        self.dropped = 0

    def __call__(self, script, timeout=15):
        if "CLICK_PICKER_ROW" in script:
            name = script.split('set clickName to "')[1].split('"')[0]
            if name == self.drop_name and self.dropped < self.to_drop:
                self.dropped += 1
                return True, ""  # 鼠标发出去了,但客户端没有收到
        return super().__call__(script, timeout)


def test_picker_click_is_retried_on_the_same_contact(monkeypatch):
    client = DropsPickerClick(DIRECTORY, "SMB售前bot", drop=1)
    client.sheet_open = True
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", lambda x, y: None)

    targets = [
        {"name": "郝一君", "query": "haoyijun", "x": 70, "y": 34},
        {"name": "SMB售前bot", "query": "SMB售前bot", "x": 70, "y": 74},
    ]
    clicked = local_group._click_english_candidates(targets, expect_total=2)

    assert [row["name"] for row in clicked] == ["郝一君", "SMB售前bot"]
    assert client.selected == 2
    assert client.dropped == 1


def test_english_candidate_rows_match_query_or_resolved_name(monkeypatch):
    monkeypatch.setattr(
        directory, "resolve_member", lambda q: {"name": "陈挺"} if q == "chenting01" else {"name": q},
    )
    rows = [
        {"name": "陈挺", "dept": "增长产品部"},
        {"name": "陈涛(水淼淼)", "dept": "增长产品部"},
        {"name": "梁超", "dept": ""},
    ]
    hits = local_group._english_candidate_rows(rows, ["chenting01"])
    assert [h["name"] for h in hits] == ["陈挺"]
    assert hits[0]["query"] == "chenting01"


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


def test_closes_a_panel_before_opening_picker(fake):
    fake.sheet_open = True

    result = local_group.create_group(["陈涛", "陈挺"])

    assert result["success"] is True
    assert fake.confirmed is True
    assert fake.sheet_open is False


def test_ui_preflight_recovers_a_stale_picker(fake):
    fake.sheet_open = True

    result = local_group.calibrate_ui()

    assert result["success"] is True
    assert fake.plus_clicked is True
    assert fake.sheet_open is False
    assert any('button "Cancel"' in script for script in fake.scripts)


def test_ui_preflight_recovers_an_unknown_sheet(monkeypatch):
    class UnknownSheet(FakeClient):
        def __call__(self, script, timeout=15):
            if "splitter group 1 of sh)" in script:
                if not self.sheet_open:
                    return True, "0"
                return True, "1" if self.plus_clicked else "2"
            return super().__call__(script, timeout)

    client = UnknownSheet(DIRECTORY)
    client.sheet_open = True
    monkeypatch.setattr(local_group, "_run_applescript", client)

    result = local_group.calibrate_ui()

    assert result["success"] is True
    assert any("key code 53" in script for script in client.scripts)
    assert client.sheet_open is False


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


class AXPlus(FakeClient):
    """AX 树里读得到 "+" 的客户端(企业微信 5.0.10)。

    ``menu=True`` 复刻 5.0.10 的真实行为: 点 "+" 不直接开面板,先弹一个小菜单。
    """

    PLUS_SCREEN = (500, 150)
    PLUS_OFFSET = (300, 31)
    MENU = (440, 168, 177, 79)      # 挂在 "+" 底下

    def __init__(self, directory, *, menu: bool = False, plus_works: bool = True,
                 stray: tuple[int, int, int, int] | None = None):
        super().__init__(directory)
        self.menu = menu
        self.plus_works = plus_works
        self.stray = stray          # 点之前就开着的小窗口(独立会话窗口那种)
        self.menu_open = False
        self.ui_clicks: list[tuple[int, int]] = []

    def __call__(self, script, timeout=15):
        # 前缀里带了小窗口扫描(_MAIN_WINDOW_AND_SMALL)才在头一段后面拼窗口列表
        small = [w for w in (self.stray, self.MENU if self.menu_open else None) if w]
        scans = "set small to small &" in script
        windows = [",".join(str(v) for v in w) for w in small] if scans else []
        if "PLUS_POINT" in script:
            self.scripts.append(script)
            plus = "{},{},{},{}".format(*self.PLUS_SCREEN, *self.PLUS_OFFSET)
            return True, ";".join([plus, *windows])
        if "UI_STATE" in script:       # _PICKER 里也有「count of sheets」,认标记别认它
            self.scripts.append(script)
            code = "1" if self.sheet_open else "0"
            return True, ";".join([code, *windows])
        return super().__call__(script, timeout)

    def click(self, x: int, y: int) -> None:
        """替身鼠标: 真鼠标点的是屏幕坐标,和 click at 的窗口偏移不是一回事。"""
        self.ui_clicks.append((x, y))
        if (x, y) == self.PLUS_SCREEN:
            if not self.plus_works:
                return
            if self.menu:
                self.menu_open = True
            else:
                self.sheet_open = True
        elif self.menu_open:
            self.menu_open = False
            self.sheet_open = True


def test_plus_is_located_through_the_ax_tree(monkeypatch):
    """AX 里读得到 "+" 就直接点它 —— 一次坐标试探都不该发生。"""
    client = AXPlus(DIRECTORY)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", client.click)

    assert local_group.create_group(["陈涛", "陈挺"])["success"] is True
    assert client.ui_clicks == [AXPlus.PLUS_SCREEN]
    assert [s for s in client.scripts if "click at {" in s] == []
    # 换算出来的偏移记回去,坐标兜底那条路才不会停在几个版本前的布局上
    assert ui_offsets.primary("plus") == AXPlus.PLUS_OFFSET


def test_plus_opens_a_menu_before_the_picker(monkeypatch):
    """5.0.10 的 "+" 先弹「发起群聊 / 记录面聊」菜单,得再点一次第一项。"""
    client = AXPlus(DIRECTORY, menu=True)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", client.click)

    assert local_group.create_group(["陈涛", "陈挺"])["success"] is True
    left, top, width, _h = AXPlus.MENU
    assert client.ui_clicks == [
        AXPlus.PLUS_SCREEN,
        (left + width // 2, top + local_group.MENU_FIRST_ITEM_DY),
    ]


def test_a_menu_that_leads_nowhere_stops_instead_of_hunting(monkeypatch):
    """菜单开了却点不出面板 = 客户端改了菜单布局。第二项是「记录面聊」,别乱点。"""

    class DeadMenu(AXPlus):
        def click(self, x, y):
            self.ui_clicks.append((x, y))
            if (x, y) == self.PLUS_SCREEN:
                self.menu_open = True

    client = DeadMenu(DIRECTORY, menu=True)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", client.click)

    with pytest.raises(local_group.LocalGroupError, match="弹出了菜单"):
        local_group.create_group(["陈涛", "陈挺"])
    assert len(client.ui_clicks) == 2      # "+" 一下,第一项一下,到此为止


def test_a_window_that_was_already_open_is_never_taken_for_the_menu(monkeypatch):
    """人家自己弹出来的小会话窗口不是菜单 —— 认错了就是在别人的窗口里盲点一下。"""
    client = AXPlus(DIRECTORY, plus_works=False, stray=(440, 168, 177, 79))
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", client.click)

    with pytest.raises(local_group.LocalGroupError, match="面板没出现"):
        local_group.create_group(["陈涛", "陈挺"])
    # 那个小窗口的几何和真菜单一模一样,唯一的区别就是「点之前就在」。
    # 两下都点在 "+" 上(第二下是重读 AX 之后),一下都没落进那个窗口
    assert client.ui_clicks == [AXPlus.PLUS_SCREEN] * 2


def test_a_menu_that_is_not_anchored_to_the_plus_is_ignored(monkeypatch):
    """新冒出来的小窗口离 "+" 十万八千里,那就不是 "+" 弹的菜单。"""

    class FarAwayPopup(AXPlus):
        MENU = (1400, 900, 177, 79)

    client = FarAwayPopup(DIRECTORY, menu=True)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", client.click)

    with pytest.raises(local_group.LocalGroupError, match="面板没出现"):
        local_group.create_group(["陈涛", "陈挺"])
    assert client.ui_clicks == [AXPlus.PLUS_SCREEN] * 2


def test_a_dead_ax_point_rereads_once_then_stops(monkeypatch):
    """AX 读到的 "+" 点下去没反应: 重读一次再点,还不行就报错 —— 不去会话列表里盲点偏移。"""
    client = AXPlus(DIRECTORY, plus_works=False)
    monkeypatch.setattr(local_group, "_run_applescript", client)
    monkeypatch.setattr(local_group, "_ui_click", client.click)

    with pytest.raises(local_group.LocalGroupError, match="AX 里读到了"):
        local_group.create_group(["陈涛", "陈挺"])
    assert client.ui_clicks == [AXPlus.PLUS_SCREEN, AXPlus.PLUS_SCREEN]
    assert [s for s in client.scripts if "click at {" in s] == []


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
    client.auto_bulk = False
    monkeypatch.setattr(local_group, "_run_applescript", client)

    r = local_group.create_group(["陈涛", "陈挺"])
    assert r["success"] is True
    assert client.confirmed is True
    assert client.enters == 3            # 第一次落空 + 重试 + 第二个人
    assert [m["query"] for m in r["members"]] == ["陈涛", "陈挺"]


def test_gives_up_after_the_retry_budget(monkeypatch):
    client = DropsEnter(DIRECTORY, drop=99)   # 永远落空
    client.auto_bulk = False
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
    assert len(enters) == 0      # 空格一次勾上,等计数涨到 2 即可


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
    monkeypatch.setattr(local_group, "_ui_click", click)

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


def test_rename_succeeds_when_ax_title_is_ahead_of_the_database(rename_env, monkeypatch):
    """刚建完的群 Info.db 经常还是空名,AX 标题已经对上就按成功走。"""
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "临时讨论组")
    monkeypatch.setattr(local_group, "_write_ax_title", lambda name: None)
    monkeypatch.setattr(local_group, "_group_names", lambda: {77: ""})
    monkeypatch.setattr(
        local_group,
        "_open_edit_box_and_read_current",
        lambda: pytest.fail("新版 AX 标题可用时不应走坐标兼容路径"),
    )

    r = local_group.rename_group(77, "临时讨论组")

    assert r["success"] is True
    assert r["name"] == "临时讨论组"
    assert r["old_name"] == "临时讨论组"


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


def test_create_waits_for_the_title_to_switch_before_renaming(fake, monkeypatch):
    """确认后窗口可能还停在旧群上,标题不像新群就不能改名。"""
    names = {1: "旧群"}
    titles = iter(["临时讨论组2-opt4", "陈涛(水淼淼)、SMB售前bot、陈挺"])
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: next(titles, "陈涛(水淼淼)、SMB售前bot、陈挺"))
    renamed = {}
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda rid, name, expect_members=None, already_open=False: (
            renamed.update(title_seen=True) or {"success": True}
        ),
    )

    def confirm():
        names[42] = ""
        fake.confirmed = True
        fake.sheet_open = False

    monkeypatch.setattr(local_group, "_confirm", confirm)

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")
    assert r["success"] is True
    assert r["name"] == "临时讨论组"
    assert renamed.get("title_seen") is True


def test_create_with_name_locates_the_new_group_by_diff(fake, monkeypatch):
    names = {1: "旧群"}
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    captured = {}

    def fake_rename(rid, name, expect_members=None, already_open=False):
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
    # 确认后先改当前窗口,这时 RID 还不知道;落库后再回填
    assert captured["rid"] is None and captured["name"] == "临时讨论组"
    # 改名前要拿成员名核对归属,所以必须把选中的人传下去
    assert captured["expect_members"] == ["陈涛", "陈挺"]


def test_create_with_name_waits_for_delayed_database_visibility(fake, monkeypatch):
    _snapshots(monkeypatch, {1: "旧群"}, {1: "旧群"}, {1: "旧群", 42: ""})
    captured = {}
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda rid, name, expect_members=None, already_open=False: (
            captured.update(rid=rid, name=name) or {"success": True}
        ),
    )
    monkeypatch.setattr(local_group, "GROUP_APPEAR_TIMEOUT", 5.0)

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")

    assert r["success"] is True
    assert r["rid"] == 42
    assert r["name"] == "临时讨论组"
    assert captured == {"rid": None, "name": "临时讨论组"}


def test_a_second_conversation_landing_right_after_still_renames_the_open_group(
    fake, monkeypatch,
):
    """确认后窗口就是新群,即使这期间又落库了别人拉你进的群,也该改当前窗口。
    RID 不唯一就宁可空着,不要猜。"""
    _snapshots(monkeypatch, {1: "旧群"}, {1: "旧群", 42: "", 43: ""})
    monkeypatch.setattr(local_group, "GROUP_APPEAR_TIMEOUT", 0.0)
    renamed = {}
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda rid, name, expect_members=None, already_open=False: (
            renamed.update(rid=rid, name=name, already_open=already_open)
            or {"success": True}
        ),
    )

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")

    assert r["success"] is True
    assert r["name"] == "临时讨论组"
    assert renamed == {"rid": None, "name": "临时讨论组", "already_open": True}
    assert r["rid"] is None
    assert "没能唯一定位" in r["rid_error"]


def test_create_picks_the_new_rid_by_the_name_just_written(fake, monkeypatch):
    """差集里有两个新会话时,改过的那个名字能把它认出来。"""
    _snapshots(
        monkeypatch,
        {1: "旧群"},
        {1: "旧群", 42: "", 43: ""},
        {1: "旧群", 42: "临时讨论组", 43: ""},
    )
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda *a, **kw: {"success": True},
    )

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组")

    assert r["success"] is True
    assert r["name"] == "临时讨论组"
    assert r["rid"] == 42
    assert "rid_error" not in r


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

def test_endpoint_returns_local_error_shape(api, fake, monkeypatch):
    monkeypatch.setattr(local_group, "_group_names", lambda: {})
    r = api.post("/api/v1/group/create", json={"members": ["陈", "陈挺"]})
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert "陈珂" in body["error"]


def test_endpoint_success(api, fake, monkeypatch):
    monkeypatch.setattr(local_group, "_group_names", lambda: {})
    monkeypatch.setattr(local_send, "send_message", lambda **kwargs: {"success": True})
    monkeypatch.setattr(local_group.time, "sleep", lambda _seconds: None)
    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"]})
    assert r.status_code == 200
    assert r.json()["member_count"] == 3


def test_endpoint_sends_initial_message_after_group_creation(api, monkeypatch):
    sent = []
    monkeypatch.setattr(
        local_group,
        "create_group",
        lambda members, name="", nickname="", return_rid=False: {
            "success": True, "rid": 3395103030, "member_count": 3,
        },
    )
    monkeypatch.setattr(
        local_send,
        "send_message",
        lambda **kwargs: sent.append(kwargs) or {
            "success": True, "conv_rid": kwargs["conv_rid"],
        },
    )

    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"]})

    assert r.status_code == 200
    assert sent == [{"conv_rid": 3395103030, "content": "1"}]
    assert r.json()["message_result"]["success"] is True


def test_endpoint_retries_initial_message_until_session_is_ready(api, monkeypatch):
    calls = []
    cursor = local_send.messages.Cursor(100, 1)
    monkeypatch.setattr(
        local_group,
        "create_group",
        lambda members, name="", nickname="", return_rid=False: {
            "success": True, "rid": 3395103030, "member_count": 3,
        },
    )
    def send(**kwargs):
        calls.append(kwargs)
        return {"success": len(calls) >= 2, "error": "未找到 RID=3395103030 的对话"}
    monkeypatch.setattr(local_send, "send_message", send)
    monkeypatch.setattr(local_send.messages, "latest_cursor", lambda _rid: cursor)
    monkeypatch.setattr(local_send.time, "sleep", lambda _seconds: None)

    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"]})

    assert r.status_code == 200
    assert len(calls) == 2
    assert r.json()["message_result"]["success"] is True


def test_endpoint_does_not_resend_when_failed_send_is_already_in_database(api, monkeypatch):
    calls = []
    cursors = iter([
        local_send.messages.Cursor(100, 1),
        local_send.messages.Cursor(100, 1),
        local_send.messages.Cursor(100, 2),
    ])
    monkeypatch.setattr(
        local_group,
        "create_group",
        lambda members, name="", nickname="", return_rid=False: {
            "success": True, "rid": 3395103030, "member_count": 3,
        },
    )
    monkeypatch.setattr(
        local_send,
        "send_message",
        lambda **kwargs: calls.append(kwargs) or {
            "success": False, "error": "Return 已投递但输入框未清空",
        },
    )
    monkeypatch.setattr(local_send.messages, "latest_cursor", lambda _rid: next(cursors))
    monkeypatch.setattr(local_send.time, "sleep", lambda _seconds: None)

    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"]})

    assert r.status_code == 200
    assert len(calls) == 1
    assert r.json()["message_result"]["success"] is True


def test_endpoint_does_not_guess_rid_for_initial_message(api, monkeypatch):
    sent = []
    monkeypatch.setattr(
        local_group,
        "create_group",
        lambda members, name="", nickname="", return_rid=False: {
            "success": True, "rid": None, "member_count": 3,
        },
    )
    monkeypatch.setattr(local_send, "send_message", lambda **kwargs: sent.append(kwargs))

    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"]})

    assert r.status_code == 200
    assert sent == []
    assert "尚未定位" in r.json()["message_error"]


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


# ─── 我在本群的昵称 ───────────────────────────────────────────────────

def test_create_with_nickname_opens_group_info_not_the_title(fake, monkeypatch):
    names = {1: "旧群"}
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    captured = {}

    def fake_nick(rid, nickname, expect_members=None, expect_title=None, already_open=False):
        captured.update(
            rid=rid, nickname=nickname,
            expect_members=expect_members, expect_title=expect_title,
        )
        return {"success": True}

    monkeypatch.setattr(local_group, "_set_nickname_locked", fake_nick)
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda *a, **kw: pytest.fail("只改昵称时不该走改群名"),
    )

    def confirm():
        names[42] = ""
        fake.confirmed = True
        fake.sheet_open = False

    monkeypatch.setattr(local_group, "_confirm", confirm)

    r = local_group.create_group(["陈涛", "陈挺"], nickname="小王")
    assert r["success"] is True
    assert r["rid"] == 42 and r["nickname"] == "小王"
    assert captured == {
        "rid": None,
        "nickname": "小王",
        "expect_members": ["陈涛", "陈挺"],
        "expect_title": None,
    }


def test_create_with_name_and_nickname_does_both(fake, monkeypatch):
    names = {1: "旧群"}
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    nick = {}
    monkeypatch.setattr(
        local_group,
        "_rename_locked",
        lambda rid, name, expect_members=None, already_open=False: {"success": True},
    )
    monkeypatch.setattr(
        local_group,
        "_set_nickname_locked",
        lambda rid, nickname, expect_members=None, expect_title=None, already_open=False: (
            nick.update(
                rid=rid, nickname=nickname,
                expect_members=expect_members, expect_title=expect_title,
            ) or {"success": True}
        ),
    )
    monkeypatch.setattr(
        local_group,
        "_confirm",
        lambda: names.update({42: ""}) or setattr(fake, "sheet_open", False),
    )

    r = local_group.create_group(["陈涛", "陈挺"], name="临时讨论组", nickname="小王")
    assert r["success"] is True
    assert r["name"] == "临时讨论组"
    assert r["nickname"] == "小王"
    # 群名已经改过,不能再拿成员拼名去对标题,否则会把刚改好名的新群误判成别人的群
    assert nick["expect_title"] == "临时讨论组"
    assert nick["expect_members"] is None


def test_create_still_sets_nickname_when_the_new_group_is_not_unique(fake, monkeypatch):
    """RID 不唯一也不该耽误改当前窗口的昵称;只是响应里没有 rid。"""
    _snapshots(monkeypatch, {1: "旧群"}, {1: "旧群", 42: "", 43: ""})
    nicked = {}
    monkeypatch.setattr(
        local_group,
        "_set_nickname_locked",
        lambda rid, nickname, expect_members=None, expect_title=None, already_open=False: (
            nicked.update(rid=rid, nickname=nickname, already_open=already_open)
            or {"success": True}
        ),
    )

    r = local_group.create_group(["陈涛", "陈挺"], nickname="小王")
    assert r["success"] is True
    assert r["nickname"] == "小王"
    assert nicked == {"rid": None, "nickname": "小王", "already_open": True}
    assert r["rid"] is None
    assert "没能唯一定位" in r["rid_error"]


def test_create_keeps_the_group_when_nickname_fails(fake, monkeypatch):
    names = {1: "旧群"}
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    monkeypatch.setattr(
        local_group,
        "_set_nickname_locked",
        lambda *a, **kw: {"success": False, "error": "打不开群详情"},
    )

    def confirm():
        names[42] = ""
        fake.sheet_open = False

    monkeypatch.setattr(local_group, "_confirm", confirm)

    r = local_group.create_group(["陈涛", "陈挺"], nickname="小王")
    assert r["success"] is True
    assert r["rid"] == 42
    assert "打不开群详情" in r["nickname_error"]


def test_set_nickname_refuses_an_unrelated_conversation(rename_env, monkeypatch):
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: rename_env["names"][77])
    monkeypatch.setattr(
        local_group,
        "_open_group_info_panel",
        lambda: pytest.fail("认不出是新群时不该打开群详情"),
    )
    r = local_group._set_nickname_locked(77, "小王", expect_members=["张三", "李四"])
    assert r["success"] is False
    assert r["unrelated"] is True
    assert "一个字都没改" in r["error"]


def test_set_nickname_accepts_the_title_after_a_successful_rename(rename_env, monkeypatch):
    state = {"wrote": None}
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "临时讨论组")
    monkeypatch.setattr(
        local_group,
        "_scan_nickname_ui",
        lambda: {"kind": "FIELD", "value": f"我在本群的昵称{state['wrote'] or ''}", "x": 1, "y": 1},
    )
    monkeypatch.setattr(local_group, "_open_group_info_panel", lambda **_: None)
    monkeypatch.setattr(local_group, "_write_focused_nickname", lambda name: state.update(wrote=name))
    monkeypatch.setattr(local_group, "_close_group_info", lambda: None)
    monkeypatch.setattr(local_group, "_front_window_mentions", lambda text: text == state["wrote"])
    monkeypatch.setattr(local_group, "_focused_text_field", lambda: None)
    monkeypatch.setattr(
        local_group,
        "_probe_nickname_in_dialog",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )
    monkeypatch.setattr(local_group, "_nickname_dialog_field", lambda: None)

    r = local_group._set_nickname_locked(77, "小王", expect_title="临时讨论组")
    assert r["success"] is True
    assert state["wrote"] == "小王"


def test_set_nickname_writes_the_focused_field_and_verifies(rename_env, monkeypatch):
    state = {"opened": False, "wrote": None, "closed": False, "scans": 0}

    def scan():
        state["scans"] += 1
        if state["wrote"]:
            return {"kind": "FIELD", "value": f"我在本群的昵称{state['wrote']}", "x": 1, "y": 1}
        return {"kind": "ROW", "value": "我在本群的昵称", "x": 10, "y": 20}

    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "")
    monkeypatch.setattr(local_group, "_scan_nickname_ui", scan)
    monkeypatch.setattr(local_group, "_open_group_info_panel", lambda **_: state.update(opened=True))
    monkeypatch.setattr(local_group, "_real_click", lambda x, y: None)
    monkeypatch.setattr(local_group, "_ui_click", lambda x, y: None)
    monkeypatch.setattr(
        local_group,
        "_write_focused_nickname",
        lambda name: state.update(wrote=name),
    )
    monkeypatch.setattr(local_group, "_close_group_info", lambda: state.update(closed=True))
    monkeypatch.setattr(local_group, "_front_window_mentions", lambda text: text == state["wrote"])
    monkeypatch.setattr(local_group, "_focused_text_field", lambda: None)
    monkeypatch.setattr(
        local_group,
        "_probe_nickname_in_dialog",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )
    monkeypatch.setattr(local_group, "_nickname_dialog_field", lambda: None)

    r = local_group._set_nickname_locked(77, "小王", expect_members=["陈挺", "陈涛"])
    assert r["success"] is True
    assert r["nickname"] == "小王"
    assert state["opened"] and state["wrote"] == "小王" and state["closed"]


def test_set_nickname_aborts_when_no_editor_took_focus(rename_env, monkeypatch):
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "")
    monkeypatch.setattr(
        local_group,
        "_scan_nickname_ui",
        lambda: {"kind": "ROW", "value": "我在本群的昵称", "x": 10, "y": 20},
    )
    monkeypatch.setattr(local_group, "_open_group_info_panel", lambda **_: None)
    monkeypatch.setattr(local_group, "_real_click", lambda x, y: None)
    monkeypatch.setattr(local_group, "_ui_click", lambda x, y: None)
    monkeypatch.setattr(
        local_group,
        "_write_focused_nickname",
        lambda name: (_ for _ in ()).throw(local_group.LocalGroupError("没有输入框拿到焦点")),
    )
    monkeypatch.setattr(local_group, "_close_group_info", lambda: None)
    monkeypatch.setattr(local_group, "_nickname_dialog_field", lambda: None)

    r = local_group._set_nickname_locked(77, "小王")
    assert r["success"] is False
    assert "没有输入框拿到焦点" in r["error"]


def test_set_nickname_writes_the_dedicated_dialog_and_confirms(rename_env, monkeypatch):
    state = {"wrote": None, "dialog": {"kind": "FIELD", "value": "", "x": 544, "y": 413}}

    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "临时讨论组")
    monkeypatch.setattr(
        local_group,
        "_scan_nickname_ui",
        lambda: {"kind": "FIELD", "value": "", "x": 544, "y": 413},
    )
    monkeypatch.setattr(local_group, "_open_group_info_panel", lambda **_: None)
    monkeypatch.setattr(local_group, "_nickname_dialog_field", lambda: state["dialog"])
    monkeypatch.setattr(
        local_group,
        "_write_nickname_dialog",
        lambda name: state.update(wrote=name, dialog=None),
    )
    monkeypatch.setattr(local_group, "_close_group_info", lambda: None)
    monkeypatch.setattr(
        local_group,
        "_write_focused_nickname",
        lambda name: pytest.fail("有昵称小窗时不该写聊天输入框"),
    )

    r = local_group._set_nickname_locked(77, "小王", expect_title="临时讨论组")
    assert r["success"] is True
    assert state["wrote"] == "小王"


def test_scan_nickname_ui_uses_labeled_row_instead_of_dumping_the_tree(monkeypatch):
    monkeypatch.setattr(local_group, "_nickname_dialog_field", lambda: None)
    monkeypatch.setattr(
        local_group, "_group_info_labeled_row", lambda _labels: (857, 375, 320, 40)
    )
    monkeypatch.setattr(
        local_group, "_dump_ui_hints", lambda: pytest.fail("不该全树扫描")
    )
    found = local_group._scan_nickname_ui()
    assert found["kind"] == "ROW"
    assert found["x"] == 857 + 320 - 40
    assert found["y"] == 375 + 20


def test_probe_nickname_from_member_menu_uses_current_users_row(monkeypatch):
    clicks = []
    monkeypatch.setattr(
        local_group.directory,
        "get_self",
        lambda: {"name": "SMB咨询顾问"},
    )
    monkeypatch.setattr(
        local_group,
        "_group_member_self_row_point",
        lambda name: (1000, 262) if name == "SMB咨询顾问" else None,
    )
    monkeypatch.setattr(
        local_group,
        "_real_right_click",
        lambda x, y: clicks.append(("right", x, y)),
    )
    monkeypatch.setattr(
        local_group,
        "_member_menu_item_point",
        lambda _labels: (1080, 300),
    )
    monkeypatch.setattr(
        local_group,
        "_ui_click",
        lambda x, y: clicks.append(("left", x, y)),
    )
    monkeypatch.setattr(
        local_group,
        "_await_nickname_dialog",
        lambda timeout=1.2: {
            "kind": "FIELD", "value": "", "x": 900, "y": 300,
        },
    )

    found = local_group._probe_nickname_from_member_menu()

    assert found["kind"] == "FIELD"
    assert clicks == [("right", 1000, 262), ("left", 1080, 300)]


def test_probe_nickname_from_member_menu_closes_menu_when_entry_is_missing(monkeypatch):
    monkeypatch.setattr(
        local_group.directory,
        "get_self",
        lambda: {"name": "SMB咨询顾问"},
    )
    monkeypatch.setattr(local_group, "_group_member_self_row_point", lambda _name: (1000, 262))
    monkeypatch.setattr(local_group, "_real_right_click", lambda _x, _y: None)
    monkeypatch.setattr(local_group, "_member_menu_item_point", lambda _labels: None)
    monkeypatch.setattr(local_group, "_osa", lambda _body: "closed")
    monkeypatch.setattr(local_group.time, "sleep", lambda _seconds: None)

    found = local_group._probe_nickname_from_member_menu()

    assert found["kind"] == "NONE"


def test_set_nickname_reports_unsupported_client_entry(rename_env, monkeypatch):
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "")
    monkeypatch.setattr(local_group, "_open_group_info_panel", lambda **_: None)
    monkeypatch.setattr(
        local_group,
        "_scan_nickname_ui",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )
    monkeypatch.setattr(
        local_group,
        "_probe_nickname_in_dialog",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )
    monkeypatch.setattr(
        local_group,
        "_probe_nickname_from_member_menu",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )
    monkeypatch.setattr(local_group, "_close_group_info", lambda: None)

    result = local_group._set_nickname_locked(77, "小王")

    assert result["success"] is False
    assert "没有暴露" in result["error"]


@pytest.mark.parametrize(
    ("corpids", "expected"),
    [
        (["corpA", "corpA"], "内部群没有暴露"),
        (["corpA", "corpB"], "客户端界面很可能改了"),
        (["corpA", ""], "客户端界面很可能改了"),      # USER 表里没有的外部联系人
    ],
)
def test_missing_nickname_entry_error_depends_on_outside_members(
    rename_env, monkeypatch, corpids, expected
):
    """没找到昵称入口: 内部群是版本本来就不给,含外部成员的群找不到才是界面变了。"""
    none = {"kind": "NONE", "value": "", "x": 0, "y": 0}
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: "")
    monkeypatch.setattr(local_group, "_open_group_info_panel", lambda **_: None)
    monkeypatch.setattr(local_group, "_scan_nickname_ui", lambda: none)
    monkeypatch.setattr(local_group, "_probe_nickname_in_dialog", lambda: none)
    monkeypatch.setattr(local_group, "_probe_nickname_from_member_menu", lambda: none)
    monkeypatch.setattr(local_group, "_close_group_info", lambda: None)
    members = [{"member_id": str(i + 1), "corpid": c} for i, c in enumerate(corpids)]
    monkeypatch.setattr(local_group.messages, "group_members", lambda _rid: members)
    monkeypatch.setattr(local_group.directory, "self_vid_or_none", lambda: "1")

    result = local_group._set_nickname_locked(77, "小王")

    assert result["success"] is False
    assert expected in result["error"]


def test_header_more_uses_leftmost_of_three_buttons(monkeypatch):
    monkeypatch.setattr(
        local_group,
        "_header_action_button_points",
        lambda: [(1071, 57), (1111, 57), (1151, 57)],
    )
    assert local_group._header_more_click_point() == (1071, 57)


def test_header_more_stays_leftmost_when_sidebar_is_open(monkeypatch):
    monkeypatch.setattr(
        local_group,
        "_header_action_button_points",
        lambda: [(1111, 57), (1151, 57)],
    )
    assert local_group._header_more_click_point() == (1111, 57)


def test_nickname_verify_accepts_focused_field_when_window_text_is_empty(monkeypatch):
    monkeypatch.setattr(local_group, "_front_window_mentions", lambda text: False)
    monkeypatch.setattr(
        local_group,
        "_focused_text_field",
        lambda: {"value": "小王", "x": 900, "y": 300},
    )
    assert local_group._nickname_write_verified("小王", {"x": 900, "y": 300}) is True
    assert local_group._nickname_write_verified("小王", {"x": 373, "y": 712}) is False


def test_nickname_endpoint_success(api, fake, monkeypatch):
    names = {1: "旧群"}
    monkeypatch.setattr(local_group, "_group_names", lambda: dict(names))
    monkeypatch.setattr(
        local_group,
        "_set_nickname_locked",
        lambda rid, nickname, expect_members=None, expect_title=None, already_open=False: {"success": True},
    )

    def confirm():
        names[42] = ""
        fake.sheet_open = False

    monkeypatch.setattr(local_group, "_confirm", confirm)
    monkeypatch.setattr(local_send, "send_message", lambda **kwargs: {"success": True})
    r = api.post("/api/v1/group/create", json={"members": ["陈涛", "陈挺"], "nickname": "小王"})
    assert r.status_code == 200
    assert r.json()["nickname"] == "小王"
    assert r.json()["rid"] == 42


# ─── 已有群改昵称 ──────────────────────────────────────────────────────

@pytest.fixture
def nickname_env(rename_env, monkeypatch):
    """在 rename_env 上叠一层假的「群详情里改昵称」界面:点昵称行 → 写字段 → 回读核验。"""
    state = {"wrote": None, "opened": False, "closed": False, "title": rename_env["names"][77]}

    monkeypatch.setattr(local_group, "_read_ax_title", lambda: state["title"])
    monkeypatch.setattr(local_group, "_open_group_info_panel",
                        lambda **_: state.update(opened=True))
    monkeypatch.setattr(local_group, "_close_group_info", lambda: state.update(closed=True))
    # 点的必须是昵称行,不能落到会话标题上(那是改名)
    monkeypatch.setattr(local_group, "_real_click", lambda x, y: None)
    monkeypatch.setattr(local_group, "_ui_click", lambda x, y: None)
    monkeypatch.setattr(local_group, "_write_focused_nickname",
                        lambda name: state.update(wrote=name))
    monkeypatch.setattr(local_group, "_front_window_mentions",
                        lambda text: text == state["wrote"])
    monkeypatch.setattr(local_group, "_focused_text_field", lambda: None)
    monkeypatch.setattr(
        local_group, "_probe_nickname_in_dialog",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )
    monkeypatch.setattr(local_group, "_nickname_dialog_field", lambda: None)

    def scan():
        if state["wrote"]:
            return {"kind": "FIELD", "value": f"我在本群的昵称{state['wrote']}", "x": 1, "y": 1}
        return {"kind": "ROW", "value": "我在本群的昵称", "x": 10, "y": 20}

    monkeypatch.setattr(local_group, "_scan_nickname_ui", scan)
    return state


def test_set_nickname_opens_the_conversation_by_rid(nickname_env, monkeypatch):
    """已有群走 RID:查本地库、打开会话,界面那一步交给 _set_nickname_locked。"""
    captured = {}

    def fake_set(rid, nickname, **kw):
        captured.update(rid=rid, nickname=nickname, **kw)
        return {"success": True, "rid": rid, "nickname": nickname}

    monkeypatch.setattr(local_group, "_set_nickname_locked", fake_set)

    r = local_group.set_nickname(77, "小王")

    assert r["success"] is True
    assert captured["rid"] == 77 and captured["nickname"] == "小王"
    # 建群那条路是 already_open=True(窗口已经停在新群上);这条必须自己去开
    assert captured.get("already_open", False) is False


def test_set_nickname_refuses_when_the_open_conversation_is_not_the_target(nickname_env, monkeypatch):
    """昵称不进本地库,写错了没有库可以核验 —— 开写之前先拿库里的群名认会话。"""
    monkeypatch.setattr(
        local_group.local_send,
        "get_conversation_by_rid",
        lambda rid: {"name": "陈挺、陈涛、任威风", "rid": rid, "lid": 1, "type": 1, "fw_id": 0},
    )
    nickname_env["title"] = "别人的群"
    monkeypatch.setattr(
        local_group, "_open_group_info_panel", lambda **_: pytest.fail("认错会话时不该打开群详情")
    )

    r = local_group.set_nickname(77, "小王")

    assert r["success"] is False
    assert r["unrelated"] is True
    assert nickname_env["wrote"] is None


def test_set_nickname_rejects_empty_and_overlong_nicknames(nickname_env):
    for nickname in ("", "长" * (local_group.MAX_NAME_LEN + 1)):
        r = local_group.set_nickname(77, nickname)
        assert r["success"] is False, nickname
        assert "群昵称" in r["error"]
    assert nickname_env["wrote"] is None      # 参数不过关就别去动客户端


def test_set_nickname_endpoint_success(api, nickname_env):
    r = api.post("/api/v1/group/nickname", json={"conv_rid": 77, "nickname": "小王"})

    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["nickname"] == "小王"
    assert nickname_env["wrote"] == "小王"
    assert nickname_env["closed"] is True


def test_set_nickname_endpoint_reports_unsupported_client_entry(api, nickname_env, monkeypatch):
    """这个版本客户端没入口 —— 回 400 说清楚,而不是假装成功。"""
    monkeypatch.setattr(
        local_group, "_scan_nickname_ui", lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0}
    )
    monkeypatch.setattr(
        local_group, "_probe_nickname_in_dialog", lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0}
    )
    monkeypatch.setattr(
        local_group, "_probe_nickname_from_member_menu",
        lambda: {"kind": "NONE", "value": "", "x": 0, "y": 0},
    )

    r = api.post("/api/v1/group/nickname", json={"conv_rid": 77, "nickname": "小王"})

    assert r.status_code == 400
    assert r.json()["success"] is False
    assert "没有暴露" in r.json()["error"]


def test_set_nickname_endpoint_rejects_blank_nickname(api, nickname_env):
    r = api.post("/api/v1/group/nickname", json={"conv_rid": 77, "nickname": ""})
    assert r.status_code == 400
    assert r.json()["success"] is False
    assert nickname_env["wrote"] is None


def test_set_nickname_accepts_old_client_title_probe(nickname_env, monkeypatch):
    """旧版没有可读 AX 标题 —— 认会话走「点标题读当前名字」的兼容路径,别把好群拦下。"""
    monkeypatch.setattr(
        local_group.local_send,
        "get_conversation_by_rid",
        lambda rid: {"name": "陈挺、陈涛、任威风", "rid": rid, "lid": 1, "type": 1, "fw_id": 0},
    )
    monkeypatch.setattr(local_group, "_read_ax_title", lambda: None)
    monkeypatch.setattr(
        local_group, "_open_edit_box_and_read_current", lambda: "陈挺、陈涛、任威风"
    )
    cancelled = []
    monkeypatch.setattr(local_group, "_cancel_edit_box", lambda: cancelled.append(True))

    r = local_group.set_nickname(77, "小王")

    assert r["success"] is True
    assert nickname_env["wrote"] == "小王"
    assert cancelled == [True]      # 读标题留下的输入框必须退掉,否则会接管后面的输入
