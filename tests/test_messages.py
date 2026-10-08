"""消息渲染 —— 类型修正与引用回复。

上游 export_wxwork 整体打桩:这些用例测的是**我们对上游的修正**,
不该因为上游改了自己的类型表就跟着变绿或变红。
"""
from typing import ClassVar

import pytest

from wecom_svr import localdb, messages


class _FakeExport:
    """模拟上游:14 标成语音、16 标成文件(即上游那套错的映射)。"""

    TYPE: ClassVar[dict] = {0: "文本", 14: "语音", 15: "文件", 16: "文件", 29: "图片"}
    MEDIA: ClassVar[dict] = {14: "[语音]", 15: "[文件]", 16: "[文件]", 29: "[图片]"}

    def type_name(self, ct):
        return self.TYPE.get(ct, f"type{ct}")

    def render(self, ct, content):
        if ct in self.MEDIA:
            return self.MEDIA[ct]
        if isinstance(content, bytes):
            return content.decode("utf-8", "replace")
        return content or ""


@pytest.fixture(autouse=True)
def fake_vendor(monkeypatch):
    class V:
        export = _FakeExport()

    monkeypatch.setattr(messages, "_vendor", lambda: V())


# ─── 类型修正 ──────────────────────────────────────────────────────────

def test_type_14_is_an_image_not_a_voice_message():
    """实测: type=14 抽 4000 条是 3443 png + 271 jpg,上游标「语音」是错的。"""
    assert messages._type_name(14) == "图片"
    assert messages._render(14, b"whatever", "图片") == "[图片]"


def test_type_16_is_a_voice_message_not_a_file():
    """实测: type=16 全库 11 条,只出现 .silk(微信语音编码)。"""
    assert messages._type_name(16) == "语音"
    assert messages._render(16, b"whatever", "语音") == "[语音]"


def test_untouched_types_still_come_from_upstream():
    """只覆盖确证标错的两个码,其余一律跟随上游,别自己发明一套。"""
    assert messages._type_name(15) == "文件"
    assert messages._type_name(29) == "图片"
    assert messages._type_name(0) == "文本"
    assert messages._render(0, "你好", "文本") == "你好"
    assert messages._render(29, b"x", "图片") == "[图片]"


def test_unknown_type_falls_through_to_upstream():
    assert messages._type_name(9999) == "type9999"


def test_type101_wework_image_links_become_an_image_placeholder():
    url = b"https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=v1_one"
    content = b"\x1a" + bytes([len(url)]) + url

    assert messages._render(101, content, "type101") == "[图片]"


# ─── 引用回复 ──────────────────────────────────────────────────────────

def _pb(field: int, payload: bytes) -> bytes:
    """拼一个 length-delimited 的 protobuf 字段。"""
    def varint(v):
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            out.append(b | (0x80 if v else 0))
            if not v:
                return bytes(out)
    return varint(field << 3 | 2) + varint(len(payload)) + payload


def _text_message(payload: bytes) -> bytes:
    """企微普通文本的实测 protobuf 外壳: field 1 -> field 2 -> field 1。"""
    return _pb(1, b"\x08\x00" + _pb(2, _pb(1, payload)))


LINK_PREVIEW = _pb(3045, b"meta") + _pb(1042, b"GitHub Issue #147")


@pytest.mark.parametrize(("payload", "expected"), [
    (b"1", "1"),
    ("好".encode(), "好"),
    ("😂".encode(), "😂"),
])
def test_single_character_text_survives_upstream_placeholder(monkeypatch, payload, expected):
    """上游会把长度小于 2 的 protobuf 文本叶子过滤掉,不能只留下 `[文本]`。"""
    class Export:
        render = staticmethod(lambda _ct, _content: "[文本]")

    monkeypatch.setattr(messages, "_vendor", lambda: type("V", (), {"export": Export()})())

    assert messages._render(2, _text_message(payload), "文本") == expected


def test_short_text_fallback_ignores_unrelated_one_character_fields(monkeypatch):
    class Export:
        render = staticmethod(lambda _ct, _content: "[文本]")

    monkeypatch.setattr(messages, "_vendor", lambda: type("V", (), {"export": Export()})())

    assert messages._render(2, _pb(9, b"1"), "文本") == "[文本]"


def _mention(name: str, rid: int = 1688851653323565) -> bytes:
    return _pb(1, b"\x08\x05" + _pb(2, b"\x08" + _varint(rid) + _pb(2, name.encode())))


def _text_part(text: str) -> bytes:
    return _pb(1, b"\x08\x00" + _pb(2, _pb(1, text.encode())))


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def test_at_mention_keeps_symbol_and_inline_text():
    """真实问题载荷:上游会渲染成 `SMB咨询顾问\n有人吗`。"""
    content = bytes.fromhex(
        "0A1E0805121A08ADB6BEA699808003120F534D42E592A8E8AFA2E9A1BEE997AE"
        "0A100800120C0A0A20E69C89E4BABAE59097"
    )

    assert messages._render(0, content, "文本") == "@SMB咨询顾问 有人吗"


def test_at_only_message_keeps_symbol():
    assert messages._render(0, _mention("SMB咨询顾问"), "文本") == "@SMB咨询顾问"


def test_multiple_at_mentions_preserve_segment_order_and_spaces():
    content = _text_part("请 ") + _mention("张三") + _text_part(" 和 ") + _mention("李四")

    assert messages._render(0, content, "文本") == "请 @张三 和 @李四"


def test_type_123_image_with_caption_keeps_both_parts():
    """企微把图片加随图文字也标成卡片;不能只留下图片文件名。"""
    content = _pb(1, b"WeChatWorkScreenshot_deadbeef.png") + _pb(1, b"aaaa")

    assert messages._rich_image_text(123, content) == "[图片]\naaaa"
    assert messages._render(123, content, "卡片") == "[图片]\naaaa"


@pytest.mark.parametrize("name", ["SMB咨询顾问", "牛小七"])
def test_type_123_image_caption_preserves_mentions(name):
    # 真实图文结构:顶层 field 1 子消息的 field 101 装文本片段。
    caption = _mention(name) + _text_part(" 图中两个模型的使用区别")
    content = _pb(1, b"WeChatWorkScreenshot_deadbeef.png") + _pb(
        1, b"\x08\x00\x28\x02" + _pb(101, caption)
    )
    assert messages._render(123, content, "卡片") == (
        f"[图片]\n@{name} 图中两个模型的使用区别"
    )


def test_regular_type_123_card_is_not_mistaken_for_an_image():
    content = _pb(1, "普通应用卡片".encode())
    assert messages._rich_image_text(123, content) == ""


def _summary(extracontent) -> str:
    """extracontent → 引用摘要。两步是因为载荷要留给引用图用,只扫一次。"""
    return messages._quote_summary(messages._quote_payload(extracontent))


def test_quote_of_an_image_becomes_a_placeholder():
    body = "CAIQ4cfK0wYY386UtYeAgAMg852rqAY=\n企业微信截图_4b36e11f.png".encode()
    assert _summary(_pb(1002, body)) == "[图片]"


def test_quote_of_text_keeps_the_text():
    assert _summary(_pb(1002, "明天下午三点开会".encode())) == "明天下午三点开会"


def test_link_preview_is_not_a_quote():
    """踩过的坑: 不认字段号只抽可读文本,会把每条带链接的日报都误标成引用。

    实测该群 76 条里被误标了 27 条 —— 日报里贴 GitHub issue 链接就中招。
    """
    assert _summary(LINK_PREVIEW) == ""


def test_quote_field_is_read_even_when_metadata_comes_first():
    ec = _pb(3045, b"meta") + _pb(1002, "被引用的话".encode()) + _pb(3065, b"x")
    assert _summary(ec) == "被引用的话"


def test_base64_handles_are_not_mistaken_for_content():
    """`CAIQ...=` 是客户端内部句柄,不是人写的东西,漏掉过滤就会推一堆乱码。"""
    assert _summary(_pb(1002, b"CAIQ4cfK0wYY386UtYeAgAMg852rqAY=")) == ""


def test_quote_is_truncated():
    assert len(_summary(_pb(1002, ("很长" * 200).encode()))) == 60


def test_empty_or_broken_extracontent_is_not_fatal(monkeypatch):
    assert _summary(None) == ""
    assert _summary(b"") == ""
    assert _summary(b"\xff\xff\xff\xff") == ""   # 畸形字节不能抛

    class Boom:
        export = type("E", (), {"render": staticmethod(lambda ct, c: 1 / 0)})()

    monkeypatch.setattr(messages, "_vendor", lambda: Boom())
    # 引用摘要不值得让整条消息失败
    assert _summary(_pb(1002, b"junk")) == ""


def test_int_id_coerces_sqlite_real_and_scientific_strings():
    rid = 10945118031183243
    assert localdb.int_id(rid) == rid
    assert localdb.int_id(float(rid)) == 10945118031183244
    assert localdb.int_id("1.0945118031183244e+16") == 10945118031183244
    assert localdb.int_id(None) == 0
    assert localdb.int_id("") == 0


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _member_frame(vid: int) -> bytes:
    inner = b"\x08" + _varint(vid)   # protobuf field 1, varint
    return b"\x02" + _varint(len(inner)) + inner


def test_parse_member_bin_reads_repeated_frames():
    blob = _member_frame(1688851653323840) + _member_frame(1688858207733961)
    assert messages.parse_member_bin(blob) == [1688851653323840, 1688858207733961]


def test_parse_member_bin_skips_duplicate_and_empty():
    blob = _member_frame(11) + _member_frame(11) + _member_frame(0)
    assert messages.parse_member_bin(blob) == [11]
    assert messages.parse_member_bin(b"") == []
    assert messages.parse_member_bin(None) == []


def test_parse_member_bin_stops_on_truncated_frame():
    good = _member_frame(42)
    assert messages.parse_member_bin(good + b"\x02\x20") == [42]
