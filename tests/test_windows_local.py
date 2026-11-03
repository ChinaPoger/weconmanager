from unittest.mock import Mock, call

from wecom_svr import windows_local
from wecom_svr.windows_local import WindowsStore


def _store_with_conversations(monkeypatch, conversations):
    store = object.__new__(WindowsStore)
    monkeypatch.setattr(store, "conversations", lambda _name: conversations)
    return store


def test_typed_conversation_lookup_requires_exact_name(monkeypatch):
    store = _store_with_conversations(monkeypatch, [
        {"name": "葛波测试群", "type": 1, "rid": 1},
    ])

    assert store.conversation_by_name("葛波", "group") is None


def test_typed_conversation_lookup_filters_user_and_group(monkeypatch):
    store = _store_with_conversations(monkeypatch, [
        {"name": "同名", "type": 1, "rid": 1},
        {"name": "同名", "type": 0, "rid": 2},
    ])

    assert store.conversation_by_name("同名", "user")["rid"] == 2
    assert store.conversation_by_name("同名", "group")["rid"] == 1


def test_legacy_lookup_keeps_fuzzy_fallback(monkeypatch):
    store = _store_with_conversations(monkeypatch, [
        {"name": "葛波测试群", "type": 1, "rid": 1},
    ])

    assert store.conversation_by_name("葛波")["rid"] == 1


def test_click_waits_for_random_delay(monkeypatch):
    sleep = Mock()
    click = Mock()
    uniform = Mock(return_value=3.25)
    monkeypatch.setattr(windows_local.random, "uniform", uniform)
    monkeypatch.setattr(windows_local.time, "sleep", sleep)

    windows_local._click_with_random_delay(click, coords=(10, 20))

    uniform.assert_called_once_with(0.5, 1.0)
    sleep.assert_called_once_with(3.25)
    click.assert_called_once_with(coords=(10, 20))


def test_type_text_sends_each_character_and_preserves_special_text(monkeypatch):
    send_keys = Mock()
    random_delay = Mock()
    monkeypatch.setattr(windows_local, "_send_keys", send_keys)
    monkeypatch.setattr(windows_local, "_random_delay", random_delay)

    windows_local._type_text("你@({})~\n好")

    assert send_keys.call_args_list == [
        call("{你}"),
        call("{@}"),
        call("{(}"),
        call("{{}"),
        call("{}}"),
        call("{)}"),
        call("{~}"),
        call("+{ENTER}"),
        call("{好}"),
    ]
    assert random_delay.call_args_list == [
        call((0.5, 1.0)),
    ] * 9
