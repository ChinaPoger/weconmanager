"""企业微信 BigCDN 图片 TCP 下载协议。

图片正文已经包含下载所需的三个稳定字段:fileid、AES key、原图 md5。客户端还会
在请求中带一份登录会话的 BigCDN authkey;它不进源码和环境示例,而是由一次本机
tcpdump 导入到 ``.data/media_tcp.json``(0600)后按需读取。

协议帧是 ``0xAB + uint32be(整帧长度) + 20 字节命令头 + KV``。响应的 ``filedata``
是 AES-128-ECB + PKCS#7;解密后必须同时通过长度和 md5 校验才交给调用方。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .settings import Settings

log = logging.getLogger(__name__)

MAGIC = 0xAB
CONFIG_VERSION = 1
MAX_FRAME_BYTES = 33 * 1024 * 1024

# filetype 选哪一份文件。1 是 HD 分身,2 是原件,3 是缩略图(同一张图实测只回
# 12KB/37KB,不能当原图用)。多数图两份都在,于是默认先要 HD;但有一批图只有
# 原件,filetype=1 一律被拒,换 filetype=2 才拿得到 md5 对得上的完整原图。
#
# 拒绝的 retcode 不止一个,所以这里不枚举 retcode:HD 那次只要没成,就用
# filetype=2 再要一次。被拒是一个带 retcode 的正常响应、不是超时,这个额外往返
# 很便宜,而换回来的是一张本来要退回去触发企微客户端的图。抓包确认真客户端也是
# 这个顺序:先 filetype=1,不行再 filetype=2。
#
# 不带 downpicformat,因为我们要的是原件。带 downpicformat=4 拿到的是**显示版**
# ——同一张图 292012 字节的原件只回 58400 字节,解密后过不了长度和 md5 校验。
# (早先这里写过「带上服务端就不回包」,那是 _sized_header 那个长度 bug 的假象,
# 不是 downpicformat 的锅;头修好之后带着它一样正常回包。)
#
# `-5103059` 不属于「换个 filetype 能救」的情况:抓包看到真客户端对同一批 fileid
# 也是 filetype=1/2 各试一轮、连试两三遍,拿到的同样是 -5103059,企微界面上那几
# 张图就是打不开。那是文件在服务端真没了,重试只是白跑,认输即可。
FILETYPE_HD = b"1"
FILETYPE_FULL = b"2"


class MediaTCPError(RuntimeError):
    """TCP 配置、协议或图片校验失败。"""


@dataclass(frozen=True)
class ImageTicket:
    fileid: bytes
    aes_key: bytes
    md5: str
    size: int


@dataclass(frozen=True)
class TCPConfig:
    host: str
    port: int
    auth_header: bytes
    download_header: bytes
    common_fields: tuple[tuple[bytes, bytes], ...]


_seq_lock = threading.Lock()
_seq = int(time.time()) % 8000 + 1000


def _next_seq() -> bytes:
    global _seq
    with _seq_lock:
        _seq += 1
        if _seq > 9999:
            _seq = 1000
        return f"{_seq:04d}".encode("ascii")


def read_varint(buf: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while offset < len(buf) and shift <= 63:
        byte = buf[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise MediaTCPError("图片消息 protobuf 截断")


def _protobuf_fields(buf: bytes) -> dict[int, list[bytes | int]]:
    out: dict[int, list[bytes | int]] = {}
    offset = 0
    while offset < len(buf):
        key, offset = read_varint(buf, offset)
        field, wire = key >> 3, key & 7
        if wire == 2:
            size, offset = read_varint(buf, offset)
            end = offset + size
            if end > len(buf):
                raise MediaTCPError("图片消息 protobuf 字段截断")
            out.setdefault(field, []).append(buf[offset:end])
            offset = end
        elif wire == 0:
            value, offset = read_varint(buf, offset)
            out.setdefault(field, []).append(value)
        elif wire == 1:
            offset += 8
        elif wire == 5:
            offset += 4
        else:
            raise MediaTCPError(f"图片消息 protobuf wire type 不支持: {wire}")
        if offset > len(buf):
            raise MediaTCPError("图片消息 protobuf 截断")
    return out


def image_ticket(content) -> ImageTicket:
    """从图片消息正文提取 BigCDN 下载票据和解密参数。"""
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    if not isinstance(content, bytes) or not content:
        raise MediaTCPError("图片消息正文为空")
    fields = _protobuf_fields(content)
    try:
        fileid = fields[1][0]
        key_hex = fields[8][0]
        digest_raw = fields[10][0]
        size = fields[4][0]
    except (KeyError, IndexError) as e:
        raise MediaTCPError("图片消息缺少 fileid/aeskey/md5/size") from e
    if not isinstance(fileid, bytes) or not fileid:
        raise MediaTCPError("图片 fileid 无效")
    if not isinstance(key_hex, bytes) or not isinstance(digest_raw, bytes):
        raise MediaTCPError("图片 aeskey/md5 类型无效")
    if not isinstance(size, int) or size <= 0:
        raise MediaTCPError("图片原始大小无效")
    try:
        aes_key = bytes.fromhex(key_hex.decode("ascii"))
        digest = digest_raw.decode("ascii").lower()
    except (UnicodeDecodeError, ValueError) as e:
        raise MediaTCPError("图片 aeskey/md5 不是十六进制") from e
    if len(aes_key) != 16 or len(digest) != 32:
        raise MediaTCPError("图片 aeskey/md5 长度无效")
    return ImageTicket(fileid=fileid, aes_key=aes_key, md5=digest, size=size)


def _config_path(settings: Settings) -> Path:
    configured = settings.mars_image_tcp_config
    return configured.expanduser() if configured else settings.data_dir / "media_tcp.json"


def _decode(value: str, label: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as e:
        raise MediaTCPError(f"TCP 配置 {label} 不是合法 base64") from e


def load_config(settings: Settings) -> TCPConfig:
    path = _config_path(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MediaTCPError(f"TCP 配置不存在: {path}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise MediaTCPError(f"读取 TCP 配置失败 {path}: {e}") from e
    if raw.get("version") != CONFIG_VERSION:
        raise MediaTCPError(f"TCP 配置版本不支持: {raw.get('version')!r}")
    try:
        host = str(raw["host"])
        port = int(raw["port"])
        auth_header = _decode(raw["auth_header"], "auth_header")
        download_header = _decode(raw["download_header"], "download_header")
        common_fields = tuple(
            (str(key).encode("ascii"), _decode(value, f"common_fields.{key}"))
            for key, value in raw["common_fields"]
        )
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as e:
        raise MediaTCPError(f"TCP 配置结构无效: {e}") from e
    if not host or not 0 < port < 65536:
        raise MediaTCPError("TCP 配置 host/port 无效")
    if len(auth_header) != 20 or len(download_header) != 20:
        raise MediaTCPError("TCP 配置命令头必须是 20 字节")
    keys = {key for key, _value in common_fields}
    required = {
        b"ver", b"weixinnum", b"clientversion", b"clientostype", b"corpid",
        b"authkey", b"nettype", b"acceptdupack",
    }
    if not required <= keys:
        raise MediaTCPError("TCP 配置缺少会话字段")
    return TCPConfig(host, port, auth_header, download_header, common_fields)


def _field(key: bytes, value: bytes) -> bytes:
    return struct.pack(">I", len(key)) + key + struct.pack(">I", len(value)) + value


def _sized_header(header: bytes, payload_size: int) -> bytes:
    """把 20 字节命令头最后两字节改写成本帧字段区的真实长度。

    抓包导入 header 时,连同当时那一次请求的长度一起冻住了。而 fileid 长度**逐图
    不同**(实测 212/214/216/218/300),于是这个冻住的值只对「和抓包时一样长」的
    那些图是对的:短了服务端会一直等剩下的字节 —— 现象就是连上、认证通过、然后
    再也不回下载帧,直到我们自己超时;长了它把字段区读歪,同样不回包。
    """
    return header[:18] + struct.pack(">H", payload_size)


def _frame(header: bytes, fields: list[tuple[bytes, bytes]]) -> bytes:
    payload = b"".join(_field(key, value) for key, value in fields)
    body = _sized_header(header, len(payload)) + payload
    total = len(body) + 5
    return bytes([MAGIC]) + struct.pack(">I", total) + body


def _with_seq(fields: tuple[tuple[bytes, bytes], ...], seq: bytes) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    inserted = False
    for key, value in fields:
        out.append((key, value))
        if key == b"ver":
            out.append((b"seq", seq))
            inserted = True
    if not inserted:
        out.insert(0, (b"seq", seq))
    return out


def build_request(
    config: TCPConfig, ticket: ImageTicket, filetype: bytes = FILETYPE_HD
) -> bytes:
    auth = _frame(config.auth_header, _with_seq(config.common_fields, _next_seq()))
    fields = _with_seq(config.common_fields, _next_seq())
    fields.extend([
        (b"lastretcode", b"0"),
        (b"ipseq", b"0"),
        (b"offset", b"0"),
        (b"fileid", ticket.fileid),
        (b"filetype", filetype),         # 取值见 FILETYPE_HD / FILETYPE_FULL
        (b"wxmsgflag", b""),
        (b"wxchattype", b"1"),
        (b"wxautostart", b"117"),
    ])
    return auth + _frame(config.download_header, fields)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise MediaTCPError("BigCDN 提前关闭连接")
        out.extend(chunk)
    return bytes(out)


def _recv_frame(sock: socket.socket) -> bytes:
    head = _recv_exact(sock, 5)
    if head[0] != MAGIC:
        raise MediaTCPError("BigCDN 响应帧魔数错误")
    size = struct.unpack(">I", head[1:])[0]
    if not 25 <= size <= MAX_FRAME_BYTES:
        raise MediaTCPError(f"BigCDN 响应帧长度异常: {size}")
    return head + _recv_exact(sock, size - 5)


def _parse_frame(frame: bytes) -> dict[bytes, bytes]:
    if len(frame) < 25 or frame[0] != MAGIC:
        raise MediaTCPError("BigCDN 响应帧无效")
    declared = struct.unpack(">I", frame[1:5])[0]
    if declared != len(frame):
        raise MediaTCPError("BigCDN 响应帧截断")
    out: dict[bytes, bytes] = {}
    offset = 25
    while offset < len(frame):
        if offset + 4 > len(frame):
            raise MediaTCPError("BigCDN 响应 key 长度截断")
        key_size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
        key_end = offset + key_size
        if key_size > 128 or key_end + 4 > len(frame):
            raise MediaTCPError("BigCDN 响应 key 无效")
        key = frame[offset:key_end]
        offset = key_end
        value_size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
        value_end = offset + value_size
        if value_end > len(frame):
            raise MediaTCPError("BigCDN 响应 value 截断")
        out[key] = frame[offset:value_end]
        offset = value_end
    return out


def _parse_field_list(frame: bytes) -> list[tuple[bytes, bytes]]:
    """和 ``_parse_frame`` 相同,但保留请求字段顺序供导入配置。"""
    if len(frame) < 25 or frame[0] != MAGIC:
        raise MediaTCPError("BigCDN 请求帧无效")
    if struct.unpack(">I", frame[1:5])[0] != len(frame):
        raise MediaTCPError("BigCDN 请求帧截断")
    out: list[tuple[bytes, bytes]] = []
    offset = 25
    while offset < len(frame):
        if offset + 4 > len(frame):
            raise MediaTCPError("BigCDN 请求 key 截断")
        key_size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
        key = frame[offset:offset + key_size]
        offset += key_size
        if key_size > 128 or offset + 4 > len(frame):
            raise MediaTCPError("BigCDN 请求 key 无效")
        value_size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
        value = frame[offset:offset + value_size]
        offset += value_size
        if offset > len(frame):
            raise MediaTCPError("BigCDN 请求 value 截断")
        out.append((key, value))
    return out


def _split_frames(stream: bytes) -> list[bytes]:
    out: list[bytes] = []
    offset = 0
    while offset < len(stream):
        if offset + 5 > len(stream) or stream[offset] != MAGIC:
            raise MediaTCPError("BigCDN TCP 流不是完整帧")
        size = struct.unpack(">I", stream[offset + 1:offset + 5])[0]
        end = offset + size
        if size < 25 or end > len(stream):
            raise MediaTCPError("BigCDN TCP 流帧截断")
        out.append(stream[offset:end])
        offset = end
    return out


def _pcapng_streams(path: Path) -> list[tuple[str, int, bytes]]:
    """从 tcpdump 的 pcapng 中重组 Ethernet/IPv4 出站 TCP 流。"""
    data = path.read_bytes()
    offset = 0
    linktypes: list[int] = []
    chunks: dict[tuple[str, int, str, int], list[tuple[int, bytes]]] = {}
    while offset + 12 <= len(data):
        block_type, block_size = struct.unpack_from("<II", data, offset)
        if block_size < 12 or offset + block_size > len(data):
            raise MediaTCPError("pcapng block 截断")
        body = data[offset + 8:offset + block_size - 4]
        offset += block_size
        if block_type == 1 and len(body) >= 2:       # Interface Description Block
            linktypes.append(struct.unpack_from("<H", body, 0)[0])
            continue
        if block_type != 6 or len(body) < 20:        # Enhanced Packet Block
            continue
        interface_id, _hi, _lo, captured, _original = struct.unpack_from("<IIIII", body, 0)
        if interface_id >= len(linktypes) or linktypes[interface_id] != 1:
            continue                                  # 只吃 Ethernet;pktap 的副本跳过
        packet = body[20:20 + captured]
        if len(packet) < 14 or packet[12:14] != b"\x08\x00":
            continue
        ip = packet[14:]
        if len(ip) < 20 or ip[9] != 6:
            continue
        ip_header = (ip[0] & 0x0F) * 4
        if len(ip) < ip_header + 20:
            continue
        tcp = ip[ip_header:]
        tcp_header = (tcp[12] >> 4) * 4
        payload = tcp[tcp_header:]
        if not payload:
            continue
        source = socket.inet_ntoa(ip[12:16])
        target = socket.inet_ntoa(ip[16:20])
        source_port, target_port, seq = struct.unpack_from(">HHI", tcp, 0)
        if target_port != 80:
            continue
        key = (source, source_port, target, target_port)
        chunks.setdefault(key, []).append((seq, payload))

    streams: list[tuple[str, int, bytes]] = []
    for (_source, _source_port, target, target_port), parts in chunks.items():
        assembled = bytearray()
        end_seq: int | None = None
        for seq, payload in sorted(parts):
            if end_seq is None:
                assembled.extend(payload)
                end_seq = seq + len(payload)
                continue
            if seq < end_seq:
                payload = payload[end_seq - seq:]
                seq = end_seq
            if not payload:
                continue
            if seq != end_seq:
                assembled.clear()                    # 抓包不完整,这条流不能导入
                break
            assembled.extend(payload)
            end_seq += len(payload)
        if assembled:
            streams.append((target, target_port, bytes(assembled)))
    return streams


def import_pcap(path: str | Path, settings: Settings) -> Path:
    """从一次成功的 tcpdump 导入当前 BigCDN 会话配置,绝不打印 authkey。"""
    source = Path(path).expanduser()
    candidates: list[tuple[str, int, bytes, bytes, list[tuple[bytes, bytes]]]] = []
    for host, port, stream in _pcapng_streams(source):
        try:
            frames = _split_frames(stream)
            if len(frames) != 2:
                continue
            first, second = frames
            auth_fields = _parse_field_list(first)
            download_fields = dict(_parse_field_list(second))
        except MediaTCPError:
            continue
        if download_fields.get(b"filetype") != b"1" or b"fileid" not in download_fields:
            continue
        if b"authkey" not in dict(auth_fields):
            continue
        common = [(key, value) for key, value in auth_fields if key != b"seq"]
        candidates.append((host, port, first[5:25], second[5:25], common))
    if not candidates:
        raise MediaTCPError("抓包里没有完整的 BigCDN 高清图片请求")
    host, port, auth_header, download_header, common = candidates[-1]
    raw = {
        "version": CONFIG_VERSION,
        "host": host,
        "port": port,
        "auth_header": base64.b64encode(auth_header).decode("ascii"),
        "download_header": base64.b64encode(download_header).decode("ascii"),
        "common_fields": [
            [key.decode("ascii"), base64.b64encode(value).decode("ascii")]
            for key, value in common
        ],
    }
    target = _config_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return target


def _retcode(fields: dict[bytes, bytes]) -> str:
    return fields.get(b"retcode", b"").decode("ascii", "replace")


def decrypt_filedata(ciphertext: bytes, ticket: ImageTicket) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise MediaTCPError("BigCDN filedata 不是完整 AES block")
    decryptor = Cipher(algorithms.AES(ticket.aes_key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16 or not padded.endswith(bytes([pad]) * pad):
        raise MediaTCPError("BigCDN filedata PKCS#7 padding 无效")
    data = padded[:-pad]
    if len(data) != ticket.size:
        raise MediaTCPError(f"BigCDN 原图长度不符: {len(data)} != {ticket.size}")
    digest = hashlib.md5(data).hexdigest()
    if digest != ticket.md5:
        raise MediaTCPError(f"BigCDN 原图 md5 不符: {digest} != {ticket.md5}")
    return data


#: `_extension` 可能返回的全部扩展名。加一种魔数就得同步这里 —— media 的缓存探测
#: 按它逐个 lexists(而不是 glob 一个通配),漏一个那类图就会永远「找不到」。
#: `test_every_magic_extension_is_declared` 盯着这条对应关系。
EXTENSIONS = (".png", ".jpg", ".gif", ".webp", ".bmp", ".bin")


def _extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".bin"


def _fetch(
    config: TCPConfig, ticket: ImageTicket, filetype: bytes, timeout: float
) -> tuple[bytes, str]:
    """一次完整的「鉴权 + 下载」往返,返回 ``(filedata 密文, 下载帧 retcode)``。

    鉴权失败直接抛 —— 换 filetype 重试救不了它。下载帧的 retcode 原样交回,
    由 `download` 决定是重试还是认输。
    """
    request = build_request(config, ticket, filetype)
    try:
        with socket.create_connection((config.host, config.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            auth_response = _parse_frame(_recv_frame(sock))
            if _retcode(auth_response) != "0":
                raise MediaTCPError(f"BigCDN 鉴权失败 retcode={_retcode(auth_response)!r}")
            image_response = _parse_frame(_recv_frame(sock))
    except (OSError, TimeoutError) as e:
        raise MediaTCPError(f"连接 BigCDN 失败: {e}") from e
    return image_response.get(b"filedata") or b"", _retcode(image_response)


def download_ticket(
    ticket: ImageTicket, settings: Settings, *, deadline: float | None = None,
) -> tuple[bytes, str]:
    """用已经从消息树中抽出的票据走 BigCDN TCP 拉原图。

    `deadline` 是整条消息共享的取图预算。**每次尝试**都重算剩余时间 —— HD 被拒后
    还有一次 FULL 重试,拿进来时算好的那个数到那时已经过期了。
    """
    if not settings.mars_image_tcp_enabled:
        raise MediaTCPError("TCP 图片下载已关闭")
    config = load_config(settings)
    timeout = float(settings.mars_image_tcp_timeout)

    def remaining() -> float:
        if deadline is None:
            return timeout
        left = deadline - time.monotonic()
        if left <= 0:
            raise MediaTCPError("整条消息的取图预算已耗尽")
        return min(timeout, left)

    ciphertext, retcode = _fetch(config, ticket, FILETYPE_HD, remaining())
    if retcode != "0":
        # 见 FILETYPE_FULL:HD 被拒不代表图没了,更可能是这张只有原件。
        log.debug("BigCDN 拒了 %s 的 HD 分身(retcode=%s),改要原件",
                  ticket.md5, retcode)
        ciphertext, retcode = _fetch(config, ticket, FILETYPE_FULL, remaining())
    if retcode != "0":
        raise MediaTCPError(f"BigCDN 下载失败 retcode={retcode!r}")
    if not ciphertext:
        raise MediaTCPError("BigCDN 响应缺少 filedata")
    data = decrypt_filedata(ciphertext, ticket)
    return data, _extension(data)


def download(content, settings: Settings) -> tuple[bytes, str]:
    """走 BigCDN TCP 拉原图,返回 ``(bytes, ext)``。"""
    return download_ticket(image_ticket(content), settings)


def main() -> None:
    """本地导入工具:``python -m wecom_svr.media_tcp import-pcap capture.pcapng``。"""
    import argparse

    from .settings import get_settings

    parser = argparse.ArgumentParser(description="导入企业微信 BigCDN TCP 抓包配置")
    parser.add_argument("command", choices=["import-pcap"])
    parser.add_argument("pcapng")
    args = parser.parse_args()
    target = import_pcap(args.pcapng, get_settings())
    print(f"已导入 BigCDN TCP 配置: {target}(权限 0600;authkey 未输出)")


if __name__ == "__main__":
    main()
