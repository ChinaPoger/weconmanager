"""Reject ambiguous or stale visual observations before external side effects."""

from types import SimpleNamespace

import pytest

from wecom_svr.windows_group import Member, WindowsGroupError
from wecom_svr.windows_group_ui import GroupPicker, Label, TargetImageChanged


def label(text, x, y):
    return Label(text, x, y, x - 10, y - 8, x + 10, y + 8)


@pytest.fixture
def picker():
    picker = object.__new__(GroupPicker)
    rect = SimpleNamespace(width=lambda: 640, height=lambda: 560)
    picker.main = SimpleNamespace(rectangle=lambda: rect)
    picker.picker = None
    picker._rename_window = None
    picker.selected = []
    return picker


def test_result_search_field_and_selected_panel_are_not_contact_candidates(picker):
    labels = [label("选择联系人", 376, 40), label("陈涛", 100, 40),
              label("陈涛(水淼淼)", 130, 100), label("技术支持部", 140, 122),
              label("陈涛", 400, 100)]
    member = Member("陈涛@技术支持部", "陈涛", "2", "技术支持部")
    assert picker._candidate_labels(labels, member) == [labels[2]]


def test_internal_contact_requires_visible_department_even_with_single_result(picker):
    labels = [label("选择联系人", 376, 40), label("陈涛", 130, 100),
              label("其他部门", 140, 122)]
    assert picker._candidate_labels(labels, Member("陈涛", "陈涛", "2", "技术支持部")) == []


def test_homonyms_without_distinguishing_visual_identity_stay_ambiguous(picker):
    labels = [label("选择联系人", 376, 40), label("陈涛", 130, 100), label("陈涛", 130, 170)]
    assert len(picker._candidate_labels(labels, Member("userid:2", "陈涛", "2"))) == 2


def test_right_panel_does_not_accept_a_match_in_search_field_or_watermark(picker):
    labels = [label("选择联系人", 376, 40), label("伴风望海", 120, 40),
              label("葛波@七牛云0375", 460, 180)]
    assert not picker._right_side_has(labels, Member("伴风望海", "伴风望海", "4"))


@pytest.mark.parametrize("text", ["已选择2个联系人", "已选择:2个联系人", "已选择\uff1a2个联系人"])
def test_selected_count_matches_observed_client_format(text):
    assert GroupPicker._selected_count([label(text, 570, 40)]) == 2


def test_missing_or_ambiguous_selected_count_fails():
    with pytest.raises(WindowsGroupError):
        GroupPicker._selected_count([])
    with pytest.raises(WindowsGroupError):
        GroupPicker._selected_count([label("已选择2个联系人", 570, 40)] * 2)


def test_submit_rejects_extra_selected_member(picker):
    member = Member("陈涛", "陈涛", "2")
    picker.selected = [member]
    picker.observe = lambda: [label("选择联系人", 376, 40), label("陈涛", 400, 100),
                              label("已选择2个联系人", 570, 40)]
    picker.click = lambda _: pytest.fail("must not submit an extra member")
    with pytest.raises(WindowsGroupError, match="人数不一致"):
        picker.submit()


def test_existing_modal_is_not_cancelled_when_open_rejects_it(picker):
    picker.opened_by_us = False
    picker.modal = lambda: SimpleNamespace(close=lambda: pytest.fail("not our modal"))
    picker._restore_clipboard = lambda: None
    with pytest.raises(WindowsGroupError, match="已有"):
        picker.open()
    picker.cancel()


def test_image_match_must_be_unique_and_in_chat_toolbar(picker):
    box = SimpleNamespace(left=580, top=25, width=32, height=29)
    picker.gui = SimpleNamespace(locateAll=lambda *a, **kw: [box, box])
    picker.frame = object()
    with pytest.raises(WindowsGroupError, match="唯一"):
        picker._add_member_icon()
    box.left = 100
    picker.gui.locateAll = lambda *a, **kw: [box]
    with pytest.raises(WindowsGroupError, match="唯一"):
        picker._add_member_icon()


def test_rename_exception_restores_original_clipboard_and_cancels_own_dialog(picker):
    clipboard = {"text": "原剪贴板"}
    picker.clipboard = SimpleNamespace(paste=lambda: clipboard["text"],
                                      copy=lambda text: clipboard.update(text=text))
    picker._old_clipboard = None
    picker._rename_window = SimpleNamespace(exists=lambda: True, is_visible=lambda: True)
    clicks = []
    picker.observe = lambda: [label("取消", 400, 300)]
    picker.click = lambda item: clicks.append(item.text)
    picker._check_focus = lambda: None

    def fail(*args):
        picker.clipboard.copy("临时群名")
        raise WindowsGroupError("focus lost")

    picker._rename = fail
    with pytest.raises(WindowsGroupError):
        picker.rename("讨论组", ["甲", "乙"])
    assert clipboard["text"] == "原剪贴板" and clicks == ["取消"]
    assert picker.picker is None


def test_nested_clipboard_preservation_keeps_original(picker):
    clipboard = {"text": "原剪贴板"}
    picker.clipboard = SimpleNamespace(paste=lambda: clipboard["text"],
                                      copy=lambda text: clipboard.update(text=text))
    picker._old_clipboard = None
    picker._preserve_clipboard()
    picker.clipboard.copy("搜索词")
    picker._preserve_clipboard()
    picker._restore_clipboard()
    assert clipboard["text"] == "原剪贴板"


@pytest.mark.parametrize("company", ["", "伙伴公司"])
def test_external_anchor_opens_exact_contact_then_starts_group(picker, monkeypatch, company):
    from wecom_svr import windows_group_ui

    monkeypatch.setattr(windows_group_ui.time, "sleep", lambda _: None)
    member = Member("客户", "客户", "4", external=True, company=company)
    display = f"客户@{company or '微信'}"
    result = label(display, 200, 110)
    wrong_company = label("客户@其他公司", 200, 160)
    title = label(display, 420, 40)
    observations = iter([[result, wrong_company], [title]])
    calls = []
    picker.modal = lambda: None
    picker._focus = lambda: None
    picker._require_no_title_editor = lambda: None
    picker._preserve_clipboard = lambda: None
    picker.clipboard = SimpleNamespace(copy=lambda text: calls.append(("search", text)))
    picker.gui = SimpleNamespace(hotkey=lambda *args: None)
    picker.click = lambda item: calls.append(("click", item.text))
    picker._add_member_icon = lambda: label("发起群聊图标", 600, 40)
    picker._wait_picker = lambda: calls.append("picker")
    picker.observe = lambda: [label("发起群聊", 376, 40)]

    def wait(predicate, *args):
        labels = next(observations)
        assert predicate(labels)
        return labels

    picker._wait_labels = wait
    picker.open_from_contact(member)
    assert calls == [("search", "客户"), ("click", display), ("click", "发起群聊图标"), "picker"]


def test_internal_anchor_does_not_open_chat_or_picker(picker):
    picker.modal = lambda: pytest.fail("reject before opening any UI")
    with pytest.raises(WindowsGroupError, match="外部联系人"):
        picker.open_from_contact(Member("同事", "同事", "2"))


def test_changed_image_is_rejected_before_mouse_input(picker, monkeypatch):
    import sys

    from PIL import Image

    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace())
    picker._check_focus = lambda: None
    picker.observed_rect = picker._rect()
    picker.frame = Image.new("RGB", (640, 560), "white")
    picker.screenshot = lambda: Image.new("RGB", (640, 560), "black")
    picker.gui = SimpleNamespace(click=lambda *args: pytest.fail("stale image must not be clicked"))
    with pytest.raises(TargetImageChanged):
        picker.click(label("目标群", 400, 40))


def test_changed_title_is_reobserved_and_clicked_at_its_new_position(picker, monkeypatch):
    from wecom_svr import windows_group_ui

    monkeypatch.setattr(windows_group_ui.time, "sleep", lambda _: None)
    old, fresh = label("目标群", 400, 40), label("目标群", 480, 50)
    observations = iter([[old], [fresh]])
    picker.observe = lambda: next(observations)
    attempts, delivered = [], []

    def click(target):
        attempts.append(target)
        if target is old:
            raise TargetImageChanged("image changed before click")
        delivered.append(target)

    picker.click = click
    result = picker._click_fresh(lambda items: items[0], 1, "missing")
    assert result is fresh and attempts == [old, fresh] and delivered == [fresh]


def test_title_image_retry_is_bounded(picker, monkeypatch):
    from wecom_svr import windows_group_ui

    monkeypatch.setattr(windows_group_ui.time, "sleep", lambda _: None)
    observations = []
    picker.observe = lambda: observations.append(label("目标群", 400, 40)) or [observations[-1]]
    picker.click = lambda _: (_ for _ in ()).throw(TargetImageChanged("unstable"))
    with pytest.raises(TargetImageChanged):
        picker._click_fresh(lambda items: items[0], 1, "missing")
    assert len(observations) == 3


@pytest.mark.parametrize("message", ["焦点已变化", "目标被其他窗口遮挡", "click outcome unknown"])
def test_title_retry_does_not_retry_other_errors_or_unknown_clicks(picker, message):
    observations = []
    picker.observe = lambda: observations.append(label("目标群", 400, 40)) or [observations[-1]]
    picker.click = lambda _: (_ for _ in ()).throw(WindowsGroupError(message))
    with pytest.raises(WindowsGroupError, match=message):
        picker._click_fresh(lambda items: items[0], 1, "missing")
    assert len(observations) == 1


MEMBERS = ["梁超", "陈涛", "彭龙", "伴风望海", "葛波"]
AUTO_TITLE = "梁超、陈涛、彭龙、伴风望海、葛波"


def test_roster_accepts_icon_noise_on_the_wechat_contact(picker):
    labels = [
        label("群成员·5", 560, 80),
        label("葛波", 560, 140),
        label("陈涛(水淼淼)", 560, 180),
        label("梁超", 560, 220),
        label("彭龙", 560, 260),
        label("1伴风望海", 560, 300),
    ]
    picker._verify_group_members(labels, MEMBERS, 5)


def test_roster_rejects_a_different_member_count_even_if_the_title_matches(picker):
    labels = [label("群成员·4", 560, 80), label(AUTO_TITLE, 400, 40)]
    with pytest.raises(WindowsGroupError, match="人数"):
        picker._verify_group_members(labels, MEMBERS, 5)


def test_covered_member_sidebar_still_accepts_the_open_auto_title(picker):
    picker._verify_group_members([label(AUTO_TITLE, 400, 40)], MEMBERS, 5)


def test_unset_dialog_edits_the_placeholder_not_the_conversation_title(picker):
    header = label(AUTO_TITLE, 400, 40)
    placeholder = label("请输入群名称", 320, 280)
    items = [
        header,
        label("修改群聊名称", 320, 220),
        label("取消", 420, 450),
        label("填写群名称", 220, 300),
        placeholder,
    ]
    assert picker._rename_input(items, AUTO_TITLE) is placeholder
    assert picker._rename_input(items, "") is placeholder


def test_rename_follows_chat_info_instead_of_clicking_the_title(picker):
    """The annotated path: … icon, 群聊名称, placeholder, then 确定."""
    icon = label("聊天信息图标", 600, 40)
    dialog_open = {"value": True}
    dialog = SimpleNamespace(
        exists=lambda: dialog_open["value"],
        is_visible=lambda: dialog_open["value"],
        rectangle=picker.main.rectangle,
    )
    opened = {"dialog": None}
    clicks = []
    field = {"text": ""}
    clip = {"text": "原剪贴板"}
    frames = [
        [label(AUTO_TITLE, 400, 40)],
        [label(AUTO_TITLE, 400, 40)],
        [
            label(AUTO_TITLE, 400, 40),
            label("聊天信息", 500, 80),
            label("群聊名称", 500, 140),
            label("未设置", 600, 140),
            label("填写群名称", 220, 300),
        ],
        [
            label("修改群聊名称", 320, 220),
            label("取消", 420, 450),
            label("请输入群名称", 320, 280),
            label("填写群名称", 220, 300),
        ],
        [
            label("修改群聊名称", 320, 220),
            label("取消", 420, 450),
            label("改名测试", 320, 280),
            label("确定", 300, 450),
        ],
        [label("改名测试", 400, 40)],
    ]

    def click(item):
        clicks.append(item.text)
        if item.text == "群聊名称":
            opened["dialog"] = dialog
        if item.text == "确定":
            dialog_open["value"] = False

    def hotkey(*args):
        if args == ("ctrl", "v"):
            field["text"] = clip["text"]
        elif args == ("ctrl", "c"):
            clip["text"] = field["text"]

    picker._rename_modal = lambda: opened["dialog"]
    picker._chat_info_icon = lambda _items: icon
    picker._focus = lambda: None
    picker._check_focus = lambda: None
    picker.click = click
    picker.observe = lambda: frames.pop(0)
    picker.clipboard = SimpleNamespace(
        copy=lambda text: clip.update(text=text), paste=lambda: clip["text"],
    )
    picker.gui = SimpleNamespace(hotkey=hotkey, press=lambda *_args: None)

    ok, error = picker._rename("改名测试", MEMBERS, AUTO_TITLE)
    assert (ok, error) == (True, None)
    assert clicks == ["聊天信息图标", "群聊名称", "请输入群名称", "确定"]
    assert AUTO_TITLE not in clicks and "填写群名称" not in clicks


def test_live_dialog_handle_counts_as_open_without_exists(monkeypatch):
    import win32gui

    from wecom_svr import windows_group_ui

    monkeypatch.setattr(win32gui, "IsWindow", lambda handle: handle == 7)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda handle: handle == 7)
    assert windows_group_ui._window_visible(SimpleNamespace(handle=7))
    assert not windows_group_ui._window_visible(SimpleNamespace(handle=8))
    assert windows_group_ui._window_visible(
        SimpleNamespace(exists=lambda: True, is_visible=lambda: True)
    )


def test_clearing_waits_until_the_placeholder_returns(picker):
    dialog_open = {"value": True}
    dialog = SimpleNamespace(
        exists=lambda: dialog_open["value"],
        is_visible=lambda: dialog_open["value"],
        rectangle=picker.main.rectangle,
    )
    opened = {"dialog": None}
    clicks = []
    frames = [
        [label("改名测试", 400, 40), label("聊天信息", 500, 80)],
        [
            label("改名测试", 400, 40),
            label("聊天信息", 500, 80),
            label("群聊名称", 500, 140),
        ],
        [
            label("修改群聊名称", 320, 220),
            label("取消", 420, 450),
            label("改名测试", 320, 280),
        ],
        [
            label("修改群聊名称", 320, 220),
            label("取消", 420, 450),
            label("请输入群名称", 320, 280),
            label("确定", 300, 450),
        ],
        [label(AUTO_TITLE, 400, 40)],
    ]

    def click(item):
        clicks.append(item.text)
        if item.text == "群聊名称":
            opened["dialog"] = dialog
        if item.text == "确定":
            dialog_open["value"] = False

    picker._rename_modal = lambda: opened["dialog"]
    picker._focus = lambda: None
    picker._check_focus = lambda: None
    picker.click = click
    picker.observe = lambda: frames.pop(0)
    picker.gui = SimpleNamespace(hotkey=lambda *_args: None, press=lambda *_args: None)

    ok, error = picker._rename("", MEMBERS, "改名测试")
    assert (ok, error) == (True, None)
    assert clicks == ["群聊名称", "改名测试", "确定"]


def test_title_is_not_clicked_if_it_becomes_ambiguous_after_image_change(picker, monkeypatch):
    from wecom_svr import windows_group_ui

    monkeypatch.setattr(windows_group_ui.time, "sleep", lambda _: None)
    ticks = iter([0, 0, 0, 0, 0, 0, 0, 2])
    monkeypatch.setattr(windows_group_ui.time, "monotonic", lambda: next(ticks, 2))
    target = label("目标群", 400, 40)
    observations = iter([[target], [target, target]])
    picker.observe = lambda: next(observations, [target, target])
    clicks = []

    def click(item):
        clicks.append(item)
        raise TargetImageChanged("changed")

    picker.click = click
    with pytest.raises(WindowsGroupError, match="ambiguous"):
        picker._click_fresh(lambda items: items[0] if len(items) == 1 else None, 1, "ambiguous")
    assert clicks == [target]
