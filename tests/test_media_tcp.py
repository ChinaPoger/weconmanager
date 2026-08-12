import base64
import contextlib
import hashlib
import json
import struct
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wecom_svr import local_send, media_tcp
from wecom_svr.settings import Settings


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _content(data: bytes, key: bytes = b"0123456789abcdef") -> bytes:
    return b"".join([
        _bytes_field(1, b"signed-file-id"),
        _int_field(4, len(data)),
        _bytes_field(8, key.hex().encode()),
        _bytes_field(10, hashlib.md5(data).hexdigest().encode()),
    ])


def _encrypted(data: bytes, key: bytes = b"0123456789abcdef") -> bytes:
    pad = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data + bytes([pad]) * pad) + encryptor.finalize()


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        mars_image_tcp_config=tmp_path / "media_tcp.json",
    )


def _write_config(settings: Settings) -> None:
    raw = {
        "version": 1,
        "host": "203.0.113.8",
        "port": 80,
        "auth_header": base64.b64encode(b"A" * 20).decode(),
        "download_header": base64.b64encode(b"D" * 20).decode(),
        "common_fields": [
            [key, base64.b64encode(value).decode()]
            for key, value in (
                ("ver", b"1"), ("weixinnum", b"100"),
                ("clientversion", b"200"), ("clientostype", b"mac"),
                ("corpid", b"300"), ("authkey", b"secret-session"),
                ("nettype", b"1"), ("acceptdupack", b"1"),
            )
        ],
    }
    settings.mars_image_tcp_config.write_text(json.dumps(raw), encoding="utf-8")


def test_image_ticket_reads_download_and_decryption_fields():
    data = b"\x89PNG\r\n\x1a\nhello"
    ticket = media_tcp.image_ticket(_content(data))

    assert ticket.fileid == b"signed-file-id"
    assert ticket.aes_key == b"0123456789abcdef"
    assert ticket.size == len(data)
    assert ticket.md5 == hashlib.md5(data).hexdigest()


def test_decrypt_filedata_requires_valid_padding_size_and_md5():
    data = b"\x89PNG\r\n\x1a\nhello"
    ticket = media_tcp.image_ticket(_content(data))

    assert media_tcp.decrypt_filedata(_encrypted(data), ticket) == data
    with pytest.raises(media_tcp.MediaTCPError, match="padding"):
        media_tcp.decrypt_filedata(_encrypted(data)[:-1] + b"x", ticket)


class _Socket:
    def __init__(self, response: bytes):
        self.response = bytearray(response)
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def settimeout(self, _timeout):
        return None

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        chunk = bytes(self.response[:size])
        del self.response[:size]
        return chunk


def test_download_builds_hd_request_and_validates_the_response(tmp_path, monkeypatch):
    data = b"\x89PNG\r\n\x1a\n" + b"image" * 20
    settings = _settings(tmp_path)
    _write_config(settings)
    auth = media_tcp._frame(b"R" * 20, [(b"retcode", b"0")])
    image = media_tcp._frame(
        b"I" * 20,
        [(b"retcode", b"0"), (b"filedata", _encrypted(data))],
    )
    fake = _Socket(auth + image)
    monkeypatch.setattr(media_tcp.socket, "create_connection", lambda *_a, **_kw: fake)

    downloaded, ext = media_tcp.download(_content(data), settings)

    assert downloaded == data
    assert ext == ".png"
    frames = media_tcp._split_frames(fake.sent)
    assert len(frames) == 2
    request = dict(media_tcp._parse_field_list(frames[1]))
    assert request[b"fileid"] == b"signed-file-id"
    assert request[b"filetype"] == b"1"
    assert b"downpicformat" not in request


@pytest.mark.parametrize("fileid_size", [212, 214, 216, 218, 300])
def test_the_command_header_carries_this_frames_payload_length(fileid_size):
    """20 字节命令头的末两字节必须是本帧字段区的真实长度。

    抓包导入的 header 冻着抓包当时那一次的长度,而 fileid 长度逐图不同(线上实测
    212/214/216/218/300 都有)。长度报短了,服务端会一直等剩下的字节 —— 表现为
    连上、认证过、然后再也不回下载帧;报长了它把字段区读歪,同样不回包。
    """
    config = media_tcp.TCPConfig(
        host="203.0.113.8", port=80,
        auth_header=b"A" * 18 + b"\xff\xff",        # 冻住的长度,必须被覆盖
        download_header=b"D" * 18 + b"\xff\xff",
        common_fields=((b"ver", b"1"), (b"authkey", b"secret-session")),
    )
    ticket = media_tcp.ImageTicket(
        fileid=b"F" * fileid_size, aes_key=b"0123456789abcdef",
        md5="0" * 32, size=1,
    )

    frames = media_tcp._split_frames(media_tcp.build_request(config, ticket))

    for frame in frames:
        declared = struct.unpack_from(">H", frame, 5 + 18)[0]
        assert declared == len(frame) - 25, "命令头声明的长度和真实字段区长度不符"


@pytest.mark.parametrize("refusal", [b"-5103017", b"-5103059"])
def test_a_refused_hd_copy_is_retried_as_a_full_size_original(
    tmp_path, monkeypatch, refusal
):
    """只有原件、没有 HD 分身的图:filetype=1 被拒,filetype=2 才有数据。

    线上这类图占「真的会走到 TCP」那批的一成多,退回客户端触发的代价远高于多一次
    往返。拒绝的 retcode 实测不止一个,所以重试不挑 retcode —— 参数化的这两个都
    是真实抓到的值,任何一个都得能救回来。
    """
    data = b"\x89PNG\r\n\x1a\n" + b"image" * 20
    settings = _settings(tmp_path)
    _write_config(settings)
    auth = media_tcp._frame(b"R" * 20, [(b"retcode", b"0")])
    refused = _Socket(auth + media_tcp._frame(b"I" * 20, [(b"retcode", refusal)]))
    served = _Socket(auth + media_tcp._frame(
        b"I" * 20, [(b"retcode", b"0"), (b"filedata", _encrypted(data))]))
    sockets = iter([refused, served])
    monkeypatch.setattr(
        media_tcp.socket, "create_connection", lambda *_a, **_kw: next(sockets))

    downloaded, ext = media_tcp.download(_content(data), settings)

    assert downloaded == data
    assert ext == ".png"
    first = dict(media_tcp._parse_field_list(media_tcp._split_frames(refused.sent)[1]))
    second = dict(media_tcp._parse_field_list(media_tcp._split_frames(served.sent)[1]))
    assert first[b"filetype"] == media_tcp.FILETYPE_HD
    assert second[b"filetype"] == media_tcp.FILETYPE_FULL
    # 重试要盯着同一个 fileid,并且始终不带 downpicformat —— 带上拿回来的是显示版,
    # 比原件小得多,解密时过不了长度和 md5 校验
    assert second[b"fileid"] == first[b"fileid"] == b"signed-file-id"
    assert b"downpicformat" not in second


def test_a_refused_original_gives_up_instead_of_looping(tmp_path, monkeypatch):
    """原件也被拒就认输 —— 只补一次 filetype,不无限换着要。"""
    settings = _settings(tmp_path)
    _write_config(settings)
    auth = media_tcp._frame(b"R" * 20, [(b"retcode", b"0")])
    refused = media_tcp._frame(b"I" * 20, [(b"retcode", b"-5103017")])
    opened = []

    def _connect(*_args, **_kwargs):
        opened.append(1)
        return _Socket(auth + refused)

    monkeypatch.setattr(media_tcp.socket, "create_connection", _connect)

    with pytest.raises(media_tcp.MediaTCPError, match="下载失败"):
        media_tcp.download(_content(b"image"), settings)
    assert len(opened) == 2


def test_download_rejects_a_nonzero_retcode(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_config(settings)
    fake = _Socket(media_tcp._frame(b"R" * 20, [(b"retcode", b"7")]))
    monkeypatch.setattr(media_tcp.socket, "create_connection", lambda *_a, **_kw: fake)

    with pytest.raises(media_tcp.MediaTCPError, match="鉴权失败"):
        media_tcp.download(_content(b"image"), settings)


def test_missing_config_is_a_normal_tcp_failure(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(media_tcp.MediaTCPError, match="配置不存在"):
        media_tcp.download(_content(b"image"), settings)


def test_client_trigger_uses_background_url_scheme_with_message_id(monkeypatch):
    seen = []
    monkeypatch.setattr(local_send, "ensure_macos", lambda: None)
    monkeypatch.setattr(
        local_send, "get_conversation_by_rid",
        lambda rid: {"rid": rid, "type": 1, "fw_id": 0, "name": "group"},
    )
    monkeypatch.setattr(local_send, "ui_session", lambda *_a: contextlib.nullcontext())
    monkeypatch.setattr(
        local_send.subprocess, "run",
        lambda args, **kwargs: seen.append((args, kwargs)) or SimpleNamespace(
            returncode=0, stdout="", stderr="",
        ),
    )

    ok, url = local_send.trigger_conversation_load(1688, 99)

    assert ok is True
    assert seen[0][0] == ["open", "-g", url]
    assert "roomid=1688" in url and "msgid=99" in url


def test_every_magic_extension_is_declared():
    """`EXTENSIONS` 是 media 缓存探测的白名单,漏一个那类图就永远找不到。"""
    samples = [
        b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
        b"RIFF\x00\x00\x00\x00WEBP", b"BM", b"not an image",
    ]
    produced = {media_tcp._extension(s) for s in samples}
    assert produced == set(media_tcp.EXTENSIONS)
