import pytest

from wecom_svr import local_send


@pytest.fixture
def script(monkeypatch):
    """截住 osascript 和 Quartz Return,记录生成的脚本。"""
    seen = {"scripts": [], "timeouts": [], "return_calls": 0}

    def fake(script, timeout=15):
        seen["scripts"].append(script)
        seen["timeouts"].append(timeout)
        seen["script"] = script
        seen["timeout"] = timeout
        return True, ""

    def fake_return():
        seen["return_calls"] += 1
        return True, "ok"

    monkeypatch.setattr(local_send, "_run_applescript", fake)
    monkeypatch.setattr(local_send, "_post_return_key", fake_return)
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
    """AX 直接写入,不该再借剪贴板中转 —— 借了就会把用户剪贴板洗掉。"""
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


def test_mention_spans_are_inferred_and_ordered():
    assert local_send._mention_spans("请 @abz 和 @任威风 测试", None) == [
        (2, 6, "abz"),
        (9, 13, "任威风"),
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


def test_rich_message_selects_mentions_before_sending(monkeypatch):
    calls = []
    monkeypatch.setattr(local_send, "_clear_chat_input", lambda: (True, "ok"))
    monkeypatch.setattr(local_send, "_post_unicode_text", lambda text: calls.append(text) or (True, "ok"))
    monkeypatch.setattr(local_send, "_type_mention", lambda name, selected: calls.append((name, selected)))
    monkeypatch.setattr(local_send, "_mention_input_value", lambda: "\ufffc\ufffc")
    monkeypatch.setattr(
        local_send,
        "_paste_and_send_current",
        lambda **kwargs: calls.append(kwargs) or (True, "ok"),
    )

    assert local_send._type_rich_message("请 @abz 和 @任威风 测试", None) == (True, "ok")
    assert calls == ["请 ", ("abz", 0), " 和 ", ("任威风", 1), " 测试", {}]
