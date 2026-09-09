import pytest

from wecom_svr import local_send, messages


@pytest.fixture
def script(monkeypatch):
    """截住 osascript 和 Quartz Return,记录生成的脚本。"""
    seen = {
        "scripts": [],
        "timeouts": [],
        "return_calls": 0,
        "key_calls": [],
        "unicode_calls": [],
        "input_calls": [],
        "focus_calls": 0,
    }

    def fake(script, timeout=15):
        seen["scripts"].append(script)
        seen["timeouts"].append(timeout)
        seen["script"] = script
        seen["timeout"] = timeout
        return True, ""

    def fake_return():
        seen["return_calls"] += 1
        return True, "ok"

    def fake_key(keycode, flags=0):
        seen["key_calls"].append((keycode, flags))
        return True, "ok"

    def fake_unicode(text):
        seen["unicode_calls"].append(text)
        return True, "ok"

    def fake_input(text):
        seen["input_calls"].append(text)
        return True, "ok"

    def fake_focus():
        seen["focus_calls"] += 1
        return True, "ok"

    monkeypatch.setattr(local_send, "_run_applescript", fake)
    monkeypatch.setattr(local_send, "_post_return_key", fake_return)
    monkeypatch.setattr(local_send, "_post_key", fake_key)
    monkeypatch.setattr(local_send, "_post_unicode_text", fake_unicode)
    monkeypatch.setattr(local_send, "_set_current_input", fake_input)
    monkeypatch.setattr(local_send, "_focus_chat_input", fake_focus)
    monkeypatch.setattr(local_send, "_focused_chat_input", lambda attempts=4, interval=0.08: (True, 1, "ok"))
    monkeypatch.setattr(local_send, "_release_ax_element", lambda _element: None)
    return seen


def test_paste_and_send_requires_foreground_chat_input(script):
    assert local_send._paste_and_send_current() == (True, "ok")
    input_script = script["scripts"][0]
    assert 'set frontmost to true' in input_script
    assert 'AXFocusedUIElement' in input_script
    assert 'roleBefore is not "AXTextArea"' in input_script
    assert "keystroke return" not in input_script
    assert 'perform action "AXConfirm"' not in input_script
    assert script["timeouts"] == [15]
    assert script["return_calls"] == 1


def test_paste_and_send_text_uses_ax_value(script):
    assert local_send._paste_and_send_current(text='你好 "企业微信"\n') == (True, "ok")
    input_script = script["scripts"][0]
    assert 'set value of inputBefore to "你好 \\"企业微信\\"\\n"' in input_script
    assert 'keystroke "v" using command down' not in input_script
    assert 'set inputPasted to value of attribute "AXFocusedUIElement"' not in input_script


def test_paste_and_send_image_does_not_read_ax_value_after_paste(script):
    assert local_send._paste_and_send_current(paste_delay=0.8) == (True, "ok")
    input_script = script["scripts"][0]
    assert 'keystroke "v" using command down' in input_script
    assert "delay 1.60" in input_script   # 粘贴生效 + 缩略图渲染,各一份 paste_delay
    assert 'set inputPasted to value of attribute "AXFocusedUIElement"' not in input_script
    assert script["return_calls"] == 1


def test_paste_image_splits_clipboard_and_ui_automation(monkeypatch, tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    calls = []
    monkeypatch.setattr(local_send, "_release_ax_element", lambda _element: None)
    focus_results = iter([
        (True, "ok"),                         # 点击聊天输入框
        (True, 1, "ok"),                      # 粘贴前确认
    ])
    monkeypatch.setattr(
        local_send, "_focus_chat_input", lambda: calls.append("focus") or next(focus_results)
    )
    monkeypatch.setattr(
        local_send, "_focused_chat_input", lambda *args, **kwargs: next(focus_results)
    )
    monkeypatch.setattr(
        local_send,
        "_write_image_clipboard",
        lambda path: calls.append(("clipboard", path)) or (True, "ok"),
    )
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda script, timeout=15: calls.append(("paste", script, timeout)) or (True, "ok"),
    )
    monkeypatch.setattr(local_send, "_current_input_value", lambda: (True, "\ufffc", "ok"))
    monkeypatch.setattr(local_send.time, "sleep", lambda delay: calls.append(("sleep", delay)))

    assert local_send._paste_image_current(str(image)) == (True, str(image))
    assert calls[0] == "focus"
    assert calls[1] == ("clipboard", str(image))
    assert calls[2][0] == "paste"
    assert calls[2][2] == 2
    assert len(calls) == 3


def test_paste_image_rejects_silent_clipboard_failure(monkeypatch, tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(local_send, "_release_ax_element", lambda _element: None)
    monkeypatch.setattr(local_send, "_focus_chat_input", lambda: (True, "ok"))
    monkeypatch.setattr(
        local_send, "_focused_chat_input", lambda *args, **kwargs: (True, 1, "ok")
    )
    monkeypatch.setattr(local_send, "_write_image_clipboard", lambda _path: (True, "ok"))
    monkeypatch.setattr(local_send, "_run_applescript", lambda *_a, **_kw: (True, "ok"))
    monkeypatch.setattr(local_send, "_current_input_value", lambda: (True, "", "ok"))
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    assert local_send._paste_image_current(str(image), paste_delay=0.2) == (
        False, "图片粘贴后未出现在输入框中",
    )


def test_paste_and_send_surfaces_submit_failure(monkeypatch):
    monkeypatch.setattr(local_send, "_run_applescript", lambda script, timeout=15: (True, ""))
    monkeypatch.setattr(
        local_send, "_post_return_key", lambda: (False, "提交按键超时")
    )

    ok, error = local_send._paste_and_send_current(text="hello")

    assert ok is False
    assert error == "粘贴/发送失败: 提交按键超时"


def test_paste_and_send_surfaces_accessibility_failure(monkeypatch):
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda script, timeout=15: (False, "当前焦点不是聊天输入框: AXButton"),
    )

    ok, error = local_send._paste_and_send_current()

    assert ok is False
    assert error == "粘贴/发送失败: 当前焦点不是聊天输入框: AXButton"


def test_text_send_does_not_touch_the_clipboard(monkeypatch, script):
    """Quartz 直接输入,不该再借剪贴板中转。"""
    monkeypatch.setattr(local_send, "_open_conversation_by_scheme", lambda conv: (True, "ok"))
    monkeypatch.setattr(
        local_send,
        "_set_clipboard",
        lambda text: pytest.fail("文本发送路径不该写剪贴板"),
    )
    monkeypatch.setattr(
        local_send,
        "_get_clipboard",
        lambda: pytest.fail("文本发送路径不该读剪贴板"),
    )

    assert local_send._send_by_scheme({"rid": 1}, "hi") == (True, "ok")
    assert script["focus_calls"] == 1
    assert script["input_calls"] == ["hi"]
    assert script["key_calls"] == []
    assert script["unicode_calls"] == []


def test_submit_checks_native_ax_focus_then_uses_system_events(monkeypatch):
    seen = {}
    monkeypatch.setattr(local_send, "_focused_chat_input", lambda: (True, 123, "ok"))
    monkeypatch.setattr(
        local_send,
        "_wait_current_input_cleared",
        lambda: seen.update(confirmed=True) or (True, "ok"),
    )

    def fake(script, timeout=15):
        seen["script"] = script
        seen["timeout"] = timeout
        return True, "ok"

    monkeypatch.setattr(local_send, "_run_applescript", fake)

    assert local_send._post_return_key() == (True, "ok")
    assert 'process "企业微信" to key code 36' in seen["script"]
    assert seen["timeout"] == 2
    assert seen["confirmed"] is True


def test_return_selects_mention_without_requiring_empty_input(monkeypatch):
    monkeypatch.setattr(local_send, "_focused_chat_input", lambda: (True, 123, "ok"))
    monkeypatch.setattr(local_send, "_run_applescript", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        local_send,
        "_wait_current_input_cleared",
        lambda: pytest.fail("选择 @ 候选不应校验正文已清空"),
    )

    assert local_send._post_return_key(confirm_send=False) == (True, "ok")


def test_submit_fails_when_return_does_not_clear_input(monkeypatch):
    monkeypatch.setattr(local_send, "_focused_chat_input", lambda: (True, 123, "ok"))
    monkeypatch.setattr(local_send, "_run_applescript", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        local_send,
        "_wait_current_input_cleared",
        lambda: (False, "Return 已投递但输入框未清空,无法确认发送: 'hi'"),
    )

    assert local_send._post_return_key() == (
        False, "Return 已投递但输入框未清空,无法确认发送: 'hi'",
    )


def test_wait_current_input_cleared_polls_until_empty(monkeypatch):
    values = iter([(True, "hi", "ok"), (True, "", "ok")])
    sleeps = []
    monkeypatch.setattr(local_send, "_current_input_value", lambda: next(values))
    monkeypatch.setattr(local_send.time, "sleep", sleeps.append)

    assert local_send._wait_current_input_cleared(attempts=3, interval=0.1) == (True, "ok")
    assert sleeps == [0.1]


def test_wait_current_input_cleared_rejects_stale_text(monkeypatch):
    monkeypatch.setattr(
        local_send, "_current_input_value", lambda: (True, "仍在输入框", "ok")
    )
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    ok, error = local_send._wait_current_input_cleared(attempts=2)

    assert ok is False
    assert "输入框未清空" in error
    assert "仍在输入框" in error


def test_submit_rejects_non_chat_focus_without_sending(monkeypatch):
    monkeypatch.setattr(
        local_send,
        "_focused_chat_input",
        lambda: (False, 0, "当前焦点不是聊天输入框: AXButton"),
    )
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda *_args, **_kwargs: pytest.fail("焦点错误时不应发送 Return"),
    )

    assert local_send._post_return_key() == (
        False, "当前焦点不是聊天输入框: AXButton",
    )


def test_focused_chat_input_recovers_from_cannot_complete(monkeypatch):
    applications = iter([(True, 11, "ok"), (True, 22, "ok")])
    invalidated = []
    sleeps = []
    released = []

    class CoreFoundation:
        @staticmethod
        def CFRelease(ref):
            released.append(ref)

    def attribute(element, name):
        if (element, name) == (11, "AXFocusedUIElement"):
            return False, 0, "读取 AXFocusedUIElement 失败(AXError=-25204)"
        if (element, name) == (22, "AXFocusedUIElement"):
            return True, 101, "ok"
        if (element, name) == (101, "AXRole"):
            return True, 201, "ok"
        raise AssertionError((element, name))

    monkeypatch.setattr(local_send, "_ax_application", lambda: next(applications))
    monkeypatch.setattr(local_send, "_ax_attribute", attribute)
    monkeypatch.setattr(
        local_send, "_invalidate_ax_application", invalidated.append
    )
    monkeypatch.setattr(
        local_send, "_load_accessibility_libraries", lambda: (object(), CoreFoundation())
    )
    monkeypatch.setattr(local_send, "_cf_string_value", lambda _ref: "AXTextArea")
    monkeypatch.setattr(local_send.time, "sleep", sleeps.append)

    assert local_send._focused_chat_input(attempts=2, interval=0.05) == (
        True, 101, "ok",
    )
    assert invalidated == [11]
    assert sleeps == [0.05]
    assert released == [201]


def test_focused_chat_input_keeps_non_chat_focus_failure(monkeypatch):
    released = []

    class CoreFoundation:
        @staticmethod
        def CFRelease(ref):
            released.append(ref)

    monkeypatch.setattr(local_send, "_ax_application", lambda: (True, 11, "ok"))
    monkeypatch.setattr(
        local_send,
        "_ax_attribute",
        lambda element, name: (
            (True, 101, "ok") if name == "AXFocusedUIElement" else (True, 201, "ok")
        ),
    )
    monkeypatch.setattr(
        local_send, "_load_accessibility_libraries", lambda: (object(), CoreFoundation())
    )
    monkeypatch.setattr(local_send, "_cf_string_value", lambda _ref: "AXButton")
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    assert local_send._focused_chat_input(attempts=2) == (
        False, 0, "当前焦点不是聊天输入框: AXButton",
    )
    assert released == [201, 101, 201, 101]


def test_focused_chat_input_finds_text_area_under_ax_table(monkeypatch):
    released = []
    retained = []

    class CoreFoundation:
        @staticmethod
        def CFRelease(ref):
            released.append(ref)

        @staticmethod
        def CFArrayGetCount(_ref):
            return 1

        @staticmethod
        def CFArrayGetValueAtIndex(_ref, _index):
            return 102

        @staticmethod
        def CFRetain(ref):
            retained.append(ref)
            return ref

    def attribute(element, name):
        values = {
            (11, "AXFocusedUIElement"): (True, 101, "ok"),
            (101, "AXRole"): (True, 201, "ok"),
            (101, "AXChildren"): (True, 301, "ok"),
            (102, "AXRole"): (True, 202, "ok"),
        }
        return values[(element, name)]

    monkeypatch.setattr(local_send, "_ax_application", lambda: (True, 11, "ok"))
    monkeypatch.setattr(local_send, "_ax_attribute", attribute)
    monkeypatch.setattr(
        local_send, "_load_accessibility_libraries", lambda: (object(), CoreFoundation())
    )
    monkeypatch.setattr(
        local_send,
        "_cf_string_value",
        lambda ref: {201: "AXTable", 202: "AXTextArea"}[ref],
    )

    assert local_send._focused_chat_input(attempts=1) == (True, 102, "ok")
    assert retained == [102]
    assert released == [201, 202, 301, 101]


def test_verified_focus_retries_after_ax_table(monkeypatch):
    calls = []
    focus_results = iter([
        (False, 0, "当前焦点不是聊天输入框: AXTable"),
        (True, 1, "ok"),
    ])
    monkeypatch.setattr(local_send, "_chat_window_bounds", (1.0, 2.0, 500.0, 400.0))
    monkeypatch.setattr(
        local_send,
        "_focus_chat_input",
        lambda: calls.append("default") or (True, "ok"),
    )
    monkeypatch.setattr(
        local_send,
        "_focus_chat_input_with_offset",
        lambda offset: calls.append(offset) or (True, "ok"),
    )
    monkeypatch.setattr(
        local_send, "_focused_chat_input", lambda *args, **kwargs: next(focus_results)
    )
    monkeypatch.setattr(local_send, "_release_ax_element", lambda element: calls.append(element))
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    assert local_send._focus_chat_input_verified() == (True, "ok")
    assert calls == ["default", 75, 1]


def test_select_current_input_uses_select_all(monkeypatch):
    calls = []
    monkeypatch.setattr(
        local_send,
        "_post_key",
        lambda keycode, flags=0: calls.append((keycode, flags)) or (True, "ok"),
    )

    assert local_send._select_current_input() == (True, "ok")
    assert calls == [(0, 1 << 20)]


def test_clear_current_input_uses_select_all_and_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(
        local_send,
        "_post_key",
        lambda keycode, flags=0: calls.append((keycode, flags)) or (True, "ok"),
    )

    assert local_send._clear_current_input() == (True, "ok")
    assert calls == [(0, 1 << 20), (51, 0)]


def test_focus_chat_input_uses_window_relative_quartz_click(monkeypatch):
    seen = {}
    monkeypatch.setattr(local_send, "_chat_window_bounds", None)
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda script, timeout=15: (True, "10,20,1000,800"),
    )
    monkeypatch.setattr(
        local_send,
        "_post_mouse_click",
        lambda x, y: seen.update(x=x, y=y) or (True, "ok"),
    )

    assert local_send._focus_chat_input() == (True, "ok")
    assert seen == {"x": 730.0, "y": 775.0}


def test_focus_chat_input_reuses_cached_window_bounds(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        local_send, "_chat_window_bounds", (1.0, 2.0, 500.0, 400.0)
    )
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda *_args, **_kwargs: pytest.fail("缓存命中时不应启动 AppleScript"),
    )
    monkeypatch.setattr(
        local_send,
        "_post_mouse_click",
        lambda x, y: seen.update(x=x, y=y) or (True, "ok"),
    )

    assert local_send._focus_chat_input() == (True, "ok")
    assert seen == {"x": 361.0, "y": 357.0}


def test_focus_chat_input_rejects_invalid_window_geometry(monkeypatch):
    monkeypatch.setattr(local_send, "_chat_window_bounds", None)
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda script, timeout=15: (True, "not-window-bounds"),
    )

    ok, error = local_send._focus_chat_input()

    assert ok is False
    assert "无法解析企业微信窗口位置" in error


def test_focus_chat_input_rejects_too_small_window(monkeypatch):
    monkeypatch.setattr(local_send, "_chat_window_bounds", None)
    monkeypatch.setattr(
        local_send,
        "_run_applescript",
        lambda script, timeout=15: (True, "10,20,300,200"),
    )
    monkeypatch.setattr(
        local_send,
        "_post_mouse_click",
        lambda *_args: pytest.fail("窗口尺寸异常时不应点击"),
    )

    ok, error = local_send._focus_chat_input()

    assert ok is False
    assert error == "企业微信窗口尺寸异常: 300x200"


def test_open_conversation_uses_short_settle_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        local_send.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(local_send.time, "sleep", sleeps.append)

    ok, _url = local_send._open_conversation_by_scheme({
        "rid": 1, "type": 1, "fw_id": 0,
    })

    assert ok is True
    assert sleeps == [0.45]


def test_submit_current_input_has_no_default_settle_delay(monkeypatch):
    monkeypatch.setattr(local_send, "_post_return_key", lambda: (True, "ok"))
    monkeypatch.setattr(
        local_send.time,
        "sleep",
        lambda _delay: pytest.fail("默认提交不应额外等待"),
    )

    assert local_send._submit_current_input() == (True, "ok")


def test_initial_message_retries_only_once_when_not_in_database(monkeypatch):
    calls = []
    baseline = messages.Cursor(100, 1)
    monkeypatch.setattr(
        local_send,
        "send_message",
        lambda **kwargs: calls.append(kwargs) or {
            "success": len(calls) == 2,
            "error": "未找到",
        },
    )
    monkeypatch.setattr(local_send.messages, "latest_cursor", lambda _rid: baseline)
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    result = local_send.send_initial_message(77)

    assert result["success"] is True
    assert len(calls) == 2


def test_initial_message_accepts_failed_ui_result_when_cursor_advanced(monkeypatch):
    calls = []
    cursors = iter([
        messages.Cursor(100, 1),
        messages.Cursor(100, 1),
        messages.Cursor(100, 2),
    ])
    monkeypatch.setattr(
        local_send,
        "send_message",
        lambda **kwargs: calls.append(kwargs) or {
            "success": False,
            "error": "Return 已投递但输入框未清空",
        },
    )
    monkeypatch.setattr(local_send.messages, "latest_cursor", lambda _rid: next(cursors))
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    result = local_send.send_initial_message(77)

    assert result["success"] is True
    assert result["conv_rid"] == 77
    assert len(calls) == 1


def test_initial_message_does_not_retry_when_baseline_cursor_is_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(
        local_send,
        "send_message",
        lambda **kwargs: calls.append(kwargs) or {"success": False, "error": "发送失败"},
    )
    monkeypatch.setattr(
        local_send.messages,
        "latest_cursor",
        lambda _rid: (_ for _ in ()).throw(local_send.LocalDataError("db unavailable")),
    )

    result = local_send.send_initial_message(77)

    assert result["success"] is False
    assert len(calls) == 1
    assert "游标不可读" in result["error"]


def test_missing_mentions_keeps_at_text_plain():
    assert local_send._mention_spans("请 @abz 和 @任威风 测试", None) == []


def test_explicit_mentions_support_hyphenated_name():
    content = "请 @七牛-牛来 协助跟进"
    assert local_send._mention_spans(content, ["七牛-牛来"]) == [
        (2, 8, "七牛-牛来"),
    ]


@pytest.mark.parametrize(
    ("content", "mentions", "message"),
    [
        ("@abz", ["缺席"], "正文中必须恰好出现一次 @缺席"),
        ("@abz @abz", ["abz"], "正文中必须恰好出现一次 @abz"),
        ("@abz", ["abz", "abz"], "mentions 中姓名重复: abz"),
    ],
)
def test_mention_spans_reject_invalid_declarations(content, mentions, message):
    with pytest.raises(local_send.LocalSendError, match=message):
        local_send._mention_spans(content, mentions)


def test_type_mention_selects_candidate_without_send_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(local_send, "_run_applescript", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(local_send, "_insert_current_input", lambda _text: (True, "ok"))
    monkeypatch.setattr(local_send, "_mention_candidates", lambda: ["abz"])
    monkeypatch.setattr(
        local_send,
        "_post_return_key",
        lambda confirm_send=True: calls.append(confirm_send) or (True, "ok"),
    )
    monkeypatch.setattr(local_send.time, "sleep", lambda _delay: None)

    local_send._type_mention("abz")

    assert calls == [False]


def test_rich_message_selects_mentions_before_sending(monkeypatch):
    calls = []
    monkeypatch.setattr(local_send, "_release_ax_element", lambda _element: None)
    monkeypatch.setattr(local_send, "_focus_chat_input", lambda: calls.append("focus") or (True, "ok"))
    monkeypatch.setattr(
        local_send, "_focused_chat_input", lambda *args, **kwargs: (True, 1, "ok")
    )
    monkeypatch.setattr(local_send, "_clear_current_input", lambda: calls.append("clear") or (True, "ok"))
    monkeypatch.setattr(local_send, "_insert_current_input", lambda text: calls.append(text) or (True, "ok"))
    monkeypatch.setattr(local_send, "_type_mention", lambda name: calls.append(("mention", name)))
    monkeypatch.setattr(local_send.time, "sleep", lambda delay: calls.append(("sleep", delay)))
    monkeypatch.setattr(
        local_send,
        "_submit_current_input",
        lambda: calls.append("submit") or (True, "ok"),
    )
    monkeypatch.setattr(
        local_send,
        "_paste_and_send_current",
        lambda **kwargs: pytest.fail("富文本 @ 消息不应触发剪贴板粘贴"),
    )

    assert local_send._type_rich_message(
        "请 @abz 和 @任威风 测试", ["abz", "任威风"]
    ) == (True, "ok")
    assert calls == [
        "focus", "clear", "请 ", ("mention", "abz"), " 和 ",
        ("mention", "任威风"), " 测试", ("sleep", 0.1), "submit",
    ]


def test_plain_text_uses_verified_ax_input_without_clipboard(monkeypatch):
    calls = []
    monkeypatch.setattr(local_send, "_release_ax_element", lambda _element: None)
    monkeypatch.setattr(local_send, "_focus_chat_input", lambda: calls.append("focus") or (True, "ok"))
    monkeypatch.setattr(
        local_send, "_focused_chat_input", lambda *args, **kwargs: (True, 1, "ok")
    )
    monkeypatch.setattr(
        local_send,
        "_set_current_input",
        lambda text: calls.append(("text", text)) or (True, "ok"),
    )
    monkeypatch.setattr(local_send.time, "sleep", lambda delay: calls.append(("sleep", delay)))
    monkeypatch.setattr(
        local_send,
        "_post_unicode_text",
        lambda _text: pytest.fail("普通文本不应使用无法校验的 Quartz Unicode 事件"),
    )
    monkeypatch.setattr(
        local_send,
        "_submit_current_input",
        lambda: calls.append("submit") or (True, "ok"),
    )
    monkeypatch.setattr(
        local_send,
        "_paste_and_send_current",
        lambda **kwargs: pytest.fail("普通文本不应触发剪贴板粘贴"),
    )

    assert local_send._type_rich_message("test", None) == (True, "ok")
    assert calls == ["focus", ("text", "test"), ("sleep", 0.08), "submit"]
