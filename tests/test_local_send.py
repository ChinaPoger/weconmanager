import pytest

from wecom_svr import local_send


@pytest.fixture
def script(monkeypatch):
    """截住 osascript,返回一个装着最后一次脚本的 dict。"""
    seen = {}

    def fake(script, timeout=15):
        seen["script"] = script
        seen["timeout"] = timeout
        return True, ""

    monkeypatch.setattr(local_send, "_run_applescript", fake)
    return seen


def test_paste_and_send_requires_foreground_chat_input(script):
    assert local_send._paste_and_send_current() == (True, "ok")
    assert 'set frontmost to true' in script["script"]
    assert 'AXFocusedUIElement' in script["script"]
    assert 'roleBefore is not "AXTextArea"' in script["script"]
    assert 'set inputPasted to value of attribute "AXFocusedUIElement"' not in script["script"]
    assert "key code 36" in script["script"]
    assert 'perform action "AXConfirm"' not in script["script"]


def test_paste_and_send_text_uses_ax_value(script):
    assert local_send._paste_and_send_current(text='你好 "企业微信"\n') == (True, "ok")
    assert 'set value of inputBefore to "你好 \\"企业微信\\"\\n"' in script["script"]
    assert 'keystroke "v" using command down' not in script["script"]
    assert 'AX 写入后输入框内容不匹配' in script["script"]


def test_paste_and_send_image_does_not_read_ax_value_after_paste(script):
    assert local_send._paste_and_send_current(paste_delay=0.8) == (True, "ok")
    assert 'keystroke "v" using command down' in script["script"]
    assert "delay 1.60" in script["script"]   # 粘贴生效 + 缩略图渲染,各一份 paste_delay
    assert 'set inputPasted to value of attribute "AXFocusedUIElement"' not in script["script"]
    assert "key code 36" in script["script"]


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
