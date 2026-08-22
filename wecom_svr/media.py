"""图片落地 —— 从企微本地缓存取原图,传对象存储,换回一个公网 URL。

为什么要这一步:mars 的 `qq_messages.content` 是纯文本列,前端用 `{{ }}` 渲染,
所以只推 `[图片]` 占位符的话,图永远显示不出来。要让人看见图,就得先给图一个
**别人也能打开的地址** —— 图原本躺在这台 Mac 的企微缓存里,别人访问不到,
企微清缓存或换机器就没了。

两段:
  - **定位**: 缓存路径是 `Caches/Images/<年-月>/<md5>[_HD|_THUMB]/<文件名>`,
    **目录名就是发送方原图的 md5**,而那个 md5 在消息正文的 ASN.1 里就有
    (`extract_media_md5`,实测 57/61 与目录名逐字相符)。所以定位是按 md5 直奔
    目录,不靠文件名 —— 文件名会重(实测 `pic.jpg` 一个名字对着 8 张不同的图)。
    没有文件名的那类(表情包、转发进来的图)正文开头直接放着企微 CDN 直链,
    走 `extract_url` 拉回来 —— 但**不能把那个链接直接推给 mars**:它是企微的
    资源,寿命和权限都不归我们管,同样要落到自己的 bucket 里。
  - **上传**: 走七牛直传表单 API,自己签 uploadToken(HMAC-SHA1),不引 SDK。
    对象 key 是 `<群 RID>/<YYYYMMDD>/<HHMMSS>-<md5前8>.<ext>`,按群按天分目录,
    控制台里能直接翻某个群某天的图;末尾那 8 位 md5 不是装饰,`_object_key` 的
    docstring 说明了它挡住的两件事。key 是纯函数,重推同一条消息命中同一个对象,
    上传前先 HEAD 一次就跳过。

同一张图在缓存里最多三个变体,`_HD` 那份才是发送方原始字节:

    <md5>_HD/    原始字节(md5 == 目录名,3875/3876)
    <md5>/       客户端渲染件 —— 可能重编码变大,也可能被放大 2 倍
    <md5>_THUMB/ 缩略图

⚠️ 所以**不能按体积挑**: `_HD` 有 12.1% 比渲染件还小(实测有一张原图 407x138、
渲染件却是插值放大到 800x271 的 —— 字节多一倍,信息一点没多)。旧实现的
「同名取大的那份」因此挑错了 526 张。现在按 md5 认原图,挑不到才退回体积。

⚠️ 目标 bucket 是**公开可读**的(拿到 URL 就能打开,无需鉴权)。群聊图片传上去
等于放到公网上,只是没人知道地址。这是调用方要自己权衡的事,所以默认不开:
不配 kodo 就退回 `[图片]` 占位符。
"""
import base64
import functools
import glob
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx

from . import media_tcp
from .settings import Settings

log = logging.getLogger(__name__)

UPLOAD_HOST = "https://up.qiniup.com"

# 从企微 CDN 拉原图的上限。比这大的多半不是聊天图片,不值得为它撑住内存
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

# 正文里内嵌的原始文件名。前面常有一个 protobuf 长度字节,所以不锚定开头
_MEDIA_NAME = re.compile(rb"([^\x00-\x1f/\\]{1,120}\.(?:png|jpe?g|heic|gif|webp|bmp))")

# RID 理论上是纯数字,但它进对象 key,不能指望这一点 —— 别的字符一律换成下划线
_KEY_UNSAFE = re.compile(r"[^0-9A-Za-z_-]")
_HEX32 = re.compile(rb"^[0-9a-fA-F]{32}$")
# 这是 Content 生成协议的一部分;改变它会改变历史消息抽出的媒体集合。
MEDIA_TREE_MAX_DEPTH = 6
MEDIA_TREE_MAX_NODES = 4096
MEDIA_TREE_MAX_BYTES = 16 * 1024 * 1024
_MEDIA_CONTAINER_FIELDS = frozenset({1, 17, 103, 107, 1002})
_MARS_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_WEWORK_IMAGE_URL = re.compile(
    rb"https?://imunion\.weixin\.qq\.com/cgi-bin/mmae-bin/tpdownloadmedia\?param="
    rb"[^\x00-\x20\x7f-\xff\"<>]+"
)


class MediaError(RuntimeError):
    """取图或上传失败(可预期,调用方退回占位符即可)。"""


@dataclass(frozen=True)
class MediaItem:
    """消息树里一个可识别媒体项。顺序由 :func:`collect` 的规范遍历定义。"""

    md5: str
    size: int
    filename: str
    kind: Literal["image", "file"]
    ticket: media_tcp.ImageTicket | None
    source_url: str = ""       # type101 首选图片 URL,仅用于身份和缓存目录推导
    md5_verified: bool = True  # False 表示 md5 只是 URL 去重键,不能拿来校验图片字节
    cache_keys: tuple[str, ...] = ()  # type101 客户端按下载 URL 的 md5 命名缓存目录


def _scan_image_urls(content: bytes) -> list[str]:
    """全文找企微图片地址,只做 URL 层面的去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _WEWORK_IMAGE_URL.finditer(content):
        try:
            url = match.group().decode("ascii")
        except UnicodeDecodeError:
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def image_urls(content) -> list[str]:
    """取正文里企微图片地址,按**图片身份**去重。

    ``content_type=101`` 的一张图会同时带原图、缩略图和嵌套引用,这些地址
    的签名不同,不能按 URL 去重。``_image_links`` 会优先用协议里的图片
    MD5 识别同一张图,并选字段 3 的原图地址。

    这里只认企微图片下载接口,不全文接管普通 ``http(s)`` 链接,避免把
    链接卡片、聊天记录里的 URL 和头像误判成图片。
    """
    if not content:
        return []
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    if not isinstance(content, bytes):
        return []
    return [url for url, _digest, _cache_keys in _image_links(content)]


def _pb_nodes(buf: bytes) -> list[tuple[int, int, bytes | int]] | None:
    """完整解析一层 protobuf;截断、非法字段或不支持的 wire type 返回 None。"""
    out: list[tuple[int, int, bytes | int]] = []
    offset = 0
    try:
        while offset < len(buf):
            key, offset = media_tcp.read_varint(buf, offset)
            field, wire = key >> 3, key & 7
            if field <= 0 or field > 16383:
                return None
            if wire == 2:
                size, offset = media_tcp.read_varint(buf, offset)
                end = offset + size
                if end > len(buf):
                    return None
                out.append((field, wire, buf[offset:end]))
                offset = end
            elif wire == 0:
                value, offset = media_tcp.read_varint(buf, offset)
                out.append((field, wire, value))
            elif wire == 1:
                if offset + 8 > len(buf):
                    return None
                out.append((field, wire, buf[offset:offset + 8]))
                offset += 8
            elif wire == 5:
                if offset + 4 > len(buf):
                    return None
                out.append((field, wire, buf[offset:offset + 4]))
                offset += 4
            else:
                return None
    except media_tcp.MediaTCPError:
        return None
    return out


def _image_links(content: bytes) -> list[tuple[str, str, tuple[str, ...]]]:
    """返回 ``(临时地址, 图片 MD5, 客户端缓存键)``;无协议身份时按 URL 去重。

    实测 type=101 的结构是:

    - field 3: 原图 URL, field 4/5/6: 原图大小和尺寸;
    - field 8: 原图 MD5;
    - field 26: 同一张图的缩略 URL, field 27/28/29: 缩略图大小和尺寸;
    - field 31/32: 重复的 MD5/嵌套引用。

    因此 field 3、26、32 里的 URL 不是三张图片。协议未带可识别 MD5
    (例如单元测试里的简化载荷)时,才保留所有不同 URL。
    """
    urls = _scan_image_urls(content)
    if not urls:
        return []
    root = _pb_nodes(content)
    if root is not None:
        digest = next(
            (
                value.decode("ascii").lower()
                for field, wire, value in root
                if field == 8
                and wire == 2
                and isinstance(value, bytes)
                and _HEX32.fullmatch(value)
            ),
            "",
        )
        if digest:
            # field 3 是原图 URL,比 field 26 的缩略图更适合落地。
            primary = [
                url
                for field, wire, value in root
                if field == 3 and wire == 2 and isinstance(value, bytes)
                for url in _scan_image_urls(value)
            ]
            # 个人微信发进企微群的 type101 图片不按图片 MD5 落本地缓存。抓包和
            # 写盘对齐确认,目录名是 field 26 下载 URL 的 md5;不同客户端版本还
            # 可能选 field 3/32,因此保留这一张图所有下载 URL 的摘要作为别名。
            cache_keys = tuple(hashlib.md5(url.encode("ascii")).hexdigest() for url in urls)
            return [(primary[0] if primary else urls[0], digest, cache_keys)]
    return [
        (url, "", (hashlib.md5(url.encode("ascii")).hexdigest(),))
        for url in urls
    ]


def _item_at(nodes: list[tuple[int, int, bytes | int]]) -> MediaItem | None:
    fields: dict[int, bytes | int] = {}
    for field, _wire, value in nodes:
        fields.setdefault(field, value)      # 重复字段取首次出现,和 collect 的规范序一致
    try:
        fileid, size, key_hex, digest_raw = fields[1], fields[4], fields[8], fields[10]
    except KeyError:
        return None
    if not isinstance(fileid, bytes) or not fileid or not isinstance(size, int) or size <= 0:
        return None
    if (
        not isinstance(key_hex, bytes)
        or not isinstance(digest_raw, bytes)
        or not _HEX32.fullmatch(digest_raw)
    ):
        return None
    digest = digest_raw.decode("ascii").lower()
    filename = ""
    name_raw = fields.get(2, b"")
    if isinstance(name_raw, bytes):
        try:
            filename = name_raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    if _HEX32.fullmatch(key_hex):
        ticket = media_tcp.ImageTicket(fileid, bytes.fromhex(key_hex.decode("ascii")), digest, size)
        return MediaItem(digest, size, filename, "image", ticket)
    if not key_hex:
        return MediaItem(digest, size, filename, "file", None)
    return None                         # 未知协议,不要武断地当附件或图片


def collect(content) -> list[MediaItem]:
    """正文 → 全部媒体,规范序(字段号、wire occurrence、深度优先),按 md5 去重。

    正文与引用由调用方分别调用,这里不会跨段去重。递归深度和其它边界是输出协议,
    不能当普通性能参数随意调整。
    """
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    if not isinstance(content, bytes) or not content or len(content) > MEDIA_TREE_MAX_BYTES:
        return []
    root = _pb_nodes(content)
    if root is None:
        return []
    found: list[MediaItem] = []
    node_count = 0
    byte_count = 0

    def walk(nodes: list[tuple[int, int, bytes | int]], size: int, depth: int) -> None:
        """已解析好的一层 → 收下它的媒体项,再按规范序下沉。

        节点由调用方解析后传进来,不在这里再解一遍:决定要不要递归本来就得先解析,
        重解一次既费时,又给「判断时」和「下沉后」留了看法不一致的余地。
        """
        nonlocal node_count, byte_count
        if node_count >= MEDIA_TREE_MAX_NODES:
            return
        byte_count += size
        if byte_count > MEDIA_TREE_MAX_BYTES:
            return
        node_count += 1
        if item := _item_at(nodes):
            found.append(item)
        if depth >= MEDIA_TREE_MAX_DEPTH:
            return
        # sorted 会丢掉跨字段顺序;这里明确按字段号排序,稳定排序保留同字段 wire order。
        for field, wire, value in sorted(nodes, key=lambda n: n[0]):
            if wire != 2 or field not in _MEDIA_CONTAINER_FIELDS or not isinstance(value, bytes):
                continue
            child = _pb_nodes(value)
            # opaque 载荷(fileid、md5、字符串)偶尔也能被 protobuf 扫完,所以下沉
            # 的条件不是「解得动」,而是「解出来确实像媒体节点或媒体容器」。
            if child and (_item_at(child) or {n[0] for n in child} & _MEDIA_CONTAINER_FIELDS):
                walk(child, len(value), depth + 1)

    walk(root, len(content), 0)
    # type=101 不走标准 ImageTicket 字段布局。字段 4 仍是原图声明大小;
    # 同时按协议里的图片 MD5 去重,避免一张图的原图/缩略图被排成两张。
    type101_size = next(
        (
            value
            for field, wire, value in root
            if field == 4 and wire == 0 and isinstance(value, int) and value > 0
        ),
        0,
    )
    for index, (url, media_digest, cache_keys) in enumerate(_image_links(content), 1):
        # 没有协议 MD5 的简化/兼容载荷只能用 URL 摘要做本条消息内的去重键;
        # 有协议 MD5 时则保留它,这样还能命中已经落地的本地缓存。
        digest = media_digest or hashlib.md5(url.encode("ascii")).hexdigest()
        found.append(MediaItem(
            digest,
            type101_size if media_digest else 0,
            f"image-{index}.jpg",
            "image",
            None,
            url,
            bool(media_digest),
            cache_keys,
        ))

    # 同 md5 后遇到 image 时把 file 就地升级,保留首次位置和首次文件名。
    # dict 保序,且替换已有 key 不会改变它的位置 —— 正好是要的语义。
    out: dict[str, MediaItem] = {}
    for item in found:
        first = out.get(item.md5)
        if first is None:
            out[item.md5] = item
        elif first.kind == "file" and item.kind == "image":
            out[item.md5] = MediaItem(
                item.md5, item.size, first.filename or item.filename, "image", item.ticket,
                first.source_url or item.source_url, first.md5_verified or item.md5_verified,
                tuple(dict.fromkeys((*first.cache_keys, *item.cache_keys))),
            )
        elif first.kind == "image" and item.kind == "image":
            out[item.md5] = MediaItem(
                item.md5,
                first.size or item.size,
                first.filename or item.filename,
                "image",
                first.ticket or item.ticket,
                first.source_url or item.source_url,
                first.md5_verified or item.md5_verified,
                tuple(dict.fromkeys((*first.cache_keys, *item.cache_keys))),
            )
    return list(out.values())


def extract_filename(content) -> str:
    """从消息正文里抠出原始图片文件名,抠不到返回空串。

    正文是 protobuf/二进制混排,不能按 UTF-8 解 —— 调用方务必用
    `cast(content as blob)` 取原始字节,否则 sqlite 会按 TEXT 返回 str,
    再 encode 一次就把二进制毁了(踩过)。

    起点靠 **protobuf 的长度前缀自校验**定:正则只能圈出个大概范围,前面那个
    长度字节会被一起圈进来(实测多出一个 `;`,即 0x3B —— 正好是文件名长度),
    拿它当文件名去 glob 就永远找不到文件。所以在范围内逐位试,取「前一个字节
    的值 == 剩余长度」的那个切点。
    """
    if not content:
        return ""
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    m = _MEDIA_NAME.search(content)
    if not m:
        return ""

    start, end = m.start(1), m.end(1)
    for i in range(start, end):
        # 单字节 varint 只能表示 <128;更长的名字前缀是多字节,这里放过让下面兜底
        if i > 0 and content[i - 1] == end - i:
            start = i
            break

    try:
        return content[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return ""


# 正文里那段 ASN.1 是**十六进制 ASCII 文本**,不是二进制 —— 所以 `04 10` 在字节流里
# 长成字面的 "0410" 四个字符。别用正则去抓它:正则匹配不允许重叠,前面一个 "0410"
# 会把真正那个连同后面 32 位一起吞掉(踩过,抓出来的是错位 6 个字符的一串)。
_HEX_RUN = re.compile(rb"[0-9a-fA-F]{64,}")


def _der_octets16(blob: bytes, out: list[str], depth: int = 0) -> None:
    """递归走 DER,收集所有 16 字节的 OCTET STRING。

    要递归是因为 md5 埋在第二层: 顶层 `30`(SEQUENCE)里是 `04 62`(OCTET STRING),
    它的**值本身**又是一个 `30`(SEQUENCE),md5 的 `04 10` 在那里面。只走顶层会
    整个跳过去。
    """
    i, n = 0, len(blob)
    while i + 2 <= n:
        tag, size = blob[i], blob[i + 1]
        if size & 0x80:                 # 长格式长度,这里的结构里不该出现
            return
        value = blob[i + 2:i + 2 + size]
        if len(value) != size:          # 截断了,别再往下猜
            return
        if tag == 0x04 and size == 16:
            out.append(value.hex())
        elif value[:1] == b"\x30" and depth < 4:
            _der_octets16(value[2:], out, depth + 1)
        i += 2 + size


def extract_media_md5(content) -> str:
    """从消息正文里取**发送方原图的 md5**,取不到返回空串。

    这个 md5 就是缓存目录名(`Caches/Images/<年-月>/<md5>[_HD]/`),所以它同时是
    「去哪找原图」和「对象 key 用什么」两个问题的答案 —— 不用读文件算 hash,也不会
    因为本地只缓存了渲染件就换一个 key(那会让同一张图落到两个对象上)。
    """
    if not content:
        return ""
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    m = _HEX_RUN.search(content)
    if not m:
        return ""
    hexed = m.group(0)
    if len(hexed) % 2:                  # 奇数长度说明多圈进来一个字符
        hexed = hexed[:-1]
    try:
        blob = bytes.fromhex(hexed.decode())
    except ValueError:
        return ""
    if blob[:1] != b"\x30":
        return ""
    found: list[str] = []
    _der_octets16(blob[2:], found)
    # 结构里只有一个 16 字节 OCTET STRING;真出现多个,靠后那个才是 md5
    return found[-1] if found else ""


def _pb_field1(buf: bytes) -> bytes:
    """正文 protobuf 第一个字段的值,前提是它是 length-delimited(wire type 2)。

    只看第一个字段就够了 —— 需要它的场景(下面的 extract_url)本来就依赖
    「链接在最前面」这个位置信息来和别的链接区分开。
    """
    if not buf or buf[0] != 0x0A:
        return b""
    size = shift = 0
    i = 1
    while i < len(buf):
        byte = buf[i]
        i += 1
        size |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 28:                       # varint 长得离谱,别当它是长度
            return b""
    else:
        return b""
    return buf[i:i + size] if len(buf) >= i + size else b""


def extract_url(content) -> str:
    """正文里的企微 CDN 直链,没有则返回空串。

    表情包和转发进来的图没有原始文件名(手机端发的图更是只有一段 ASN.1),
    这类消息把图片地址直接放在 protobuf 字段 1。

    必须**按位置**取,不能全文正则搜 http:// —— 合并转发的聊天记录正文里也有
    一堆链接,但那些是发言人头像(`wwhead`/`mmhead`),藏在嵌套消息里。按位置
    取就天然不会碰到它们,也不用去维护一张域名/路径白名单。
    """
    if not content:
        return ""
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    head = _pb_field1(content)
    if not head.startswith((b"http://", b"https://")):
        return ""
    try:
        return head.decode("ascii")
    except UnicodeDecodeError:
        return ""


@functools.lru_cache(maxsize=1)
def _images_root() -> str:
    from .messages import _vendor
    return os.path.join(_vendor().paths.caches(), "Images")


def _variant_rank(path: str) -> int:
    """`_HD` 优先 —— 它在 3875/3876 的情况下就是原图,先试它能少读几个文件。"""
    d = os.path.basename(os.path.dirname(path))
    return 0 if d.endswith("_HD") else (2 if d.endswith("_THUMB") else 1)


def _candidates(
    filename: str, md5: str, settings: Settings | None = None,
    cache_keys: tuple[str, ...] = (),
) -> tuple[list[str], bool]:
    """缓存里所有可能是这张图的文件,以及「是不是按 md5 命中的」。

    **只 glob,不读文件。**「这张图到没到」是每轮同步都要问的问题(`pending`),
    而挑出哪一份是原图要读文件算 md5 —— 把两件事分开,判断在不在就不必付读文件
    的代价。按文件名那条路要扫遍所有 md5 目录(实测 8.7k 次 scandir,92ms),
    更不该白跑第二遍。
    """
    if md5 and settings is not None:
        # 逐个试已知扩展名,而不是 glob `{md5}.*`:通配会把整个 media-cache 目录
        # scandir 一遍(实测 5k 个文件 2.0ms),而这里问的只是「这几个名字在不在」
        # —— 换成 lexists 是 0.01ms,等图时每 0.25s 就要问一次。
        service_root = settings.data_dir / "media-cache"
        service_hits = [
            str(service_root / f"{md5}{ext}") for ext in media_tcp.EXTENSIONS
            if os.path.isfile(service_root / f"{md5}{ext}")
        ]
        if service_hits:
            return service_hits, True

    root = _images_root()
    if md5:
        # 同理:变体只有 `<md5>`/`_HD`/`_THUMB` 三种(见模块 docstring 和
        # `_variant_rank`),写成字面量就不必为每个年-月目录再 scandir 一遍。
        hits = [
            p for variant in (md5, f"{md5}_HD", f"{md5}_THUMB")
            for p in glob.glob(os.path.join(root, "*", variant, "*"))
            if os.path.isfile(p)
        ]
        if hits:
            return hits, True

    # type101 的个人微信图片由企微客户端按下载 URL 的 md5 落盘,而不是按
    # 消息声明的原图 md5。这里命中的是客户端渲染件,所以 by_md5=False:
    # `_best` 不会误把它当成字节 md5 已验证的原图,但仍可作为上传兜底。
    for cache_key in dict.fromkeys(cache_keys):
        if not _HEX32.fullmatch(cache_key.encode("ascii", "ignore")):
            continue
        hits = [
            p for variant in (cache_key, f"{cache_key}_HD", f"{cache_key}_THUMB")
            for p in glob.glob(os.path.join(root, "*", variant, "*"))
            if os.path.isfile(p)
        ]
        if hits:
            return hits, False

    if not filename:
        return [], False
    # glob 的特殊字符要转义,否则文件名里的 [] 会被当成字符集
    pattern = os.path.join(root, "*", "*", glob.escape(filename))
    return [p for p in glob.glob(pattern) if os.path.isfile(p)], False


def _best(hits: list[str], by_md5: bool, md5: str) -> str:
    """候选里挑原图。**会读文件算 md5**,所以只在真要用这张图时调。"""
    if not hits:
        return ""
    if by_md5:
        for p in sorted(hits, key=_variant_rank):
            if file_md5(p) == md5:
                return p
    return max(hits, key=os.path.getsize)


def find_local(filename: str, md5: str = "", settings: Settings | None = None) -> str:
    """在企微图片缓存里定位原图,找不到返回空串。

    **有 md5 就按 md5 找**,不看文件名:目录名就是原图 md5,而文件名会重
    (`pic.jpg` 一个名字对着 8 张不同的图)。同一个 md5 底下最多三个变体,
    挑**字节 md5 对得上**的那份 —— 那才是发送方原始字节。

    挑不到原图(实测 181/3812 ≈ 4.7% 本地只缓存了渲染件)就退回体积最大的那份:
    有图比没图强。注意这时**对象 key 仍用原图 md5**,所以以后原图到了也不会再传
    一遍 —— 这是明确的取舍,不是疏漏。

    没有 md5 时退回旧的按文件名找。
    """
    hits, by_md5 = _candidates(filename, md5, settings)
    return _best(hits, by_md5, md5)


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── 七牛直传 ───────────────────────────────────────────────────────────

def _ub64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def upload_token(settings: Settings, key: str, expires: int = 3600) -> str:
    """七牛 uploadToken = ak:sign(policy):policy,自己签,不引 SDK。"""
    policy = json.dumps(
        {"scope": f"{settings.kodo_bucket}:{key}", "deadline": int(time.time()) + expires},
        separators=(",", ":"),
    ).encode()
    encoded = _ub64(policy)
    sign = hmac.new(settings.kodo_sk.encode(), encoded.encode(), hashlib.sha1).digest()
    return f"{settings.kodo_ak}:{_ub64(sign)}:{encoded}"


def object_url(settings: Settings, key: str) -> str:
    """key → 公网源文件 URL。"""
    return f"{settings.kodo_domain.rstrip('/')}/{urllib.parse.quote(key)}"


def _display_url(source_url: str, needs_conversion: bool) -> str:
    """返回 Mars 可渲染的 URL,让 Kodo Dora 在访问时完成格式转换。

    imageView2 的 ``format`` 必须和 ``mode`` 一起使用。这里固定输出 jpg,让 HEIC
    和无扩展名图片也能被 Mars 的图片白名单识别。对 Mars 已支持的格式不加处理参数,
    避免额外的实时处理费用。
    """
    if not needs_conversion:
        return source_url
    return f"{source_url}?imageView2/1/format/jpg"


def _exists(url: str, timeout: int) -> bool:
    """已经传过就别再传一遍 —— key 由(群, 时刻, 内容)推导,存在即等价。"""
    try:
        return httpx.head(url, timeout=timeout, follow_redirects=True).status_code == 200
    except httpx.HTTPError:
        return False


def _post_form(stream, filename: str, key: str, token: str, timeout: int) -> None:
    """七牛直传表单。multipart 交给 httpx 编,不自己拼 boundary。"""
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        resp = httpx.post(
            UPLOAD_HOST,
            data={"token": token, "key": key},
            files={"file": (filename, stream, ctype)},
            timeout=timeout,
        )
    except OSError as e:
        raise MediaError(f"读取待上传文件失败: {e}") from e
    except httpx.HTTPError as e:
        raise MediaError(f"上传失败: {e}") from e

    if resp.status_code != 200:
        raise MediaError(f"上传失败 {resp.status_code}: {resp.text[:200]}")


def _object_key(rid, send_time: int, digest: str, ext: str, settings: Settings) -> str:
    """key = `<prefix>/<群 RID>/<YYYYMMDD>/<HHMMSS>-<md5 前 8 位><ext>`。

    按群分目录、按天再按时刻排序,在 kodo 控制台里能直接翻某个群某天的图 ——
    这是这套命名唯一的目的,`<md5>/<文件名>` 那版做不到。

    但**光有「群 + 秒」不够**,末尾那 8 位 md5 是必需的,两个原因:

      1. **同群同秒会撞车。** 多选发图一次就是好几张,全库实测 5590 张图里有 123 张
         挤在 58 个「同群同秒」的槽位上(最多一槽 4 张)。少了区分位,其中 65 张会被
         后来的静默覆盖 —— 本地库只有秒级时间,没有更细的刻度可用。
      2. **桶是公开可读的。** key 全部可推导的话,知道群号按秒遍历一天就能把整个群的
         图扒下来。8 位 md5 = 40 亿分之一,把枚举这条路堵死。

    仍然是**内容 + 位置的纯函数**,所以重推同一条消息落在同一个 key 上,`_exists`
    一次 HEAD 就跳过重传。代价是同一张图出现在两个群会传两份 —— 既然本来就想按群分,
    这不算损失。
    """
    prefix = settings.kodo_prefix.strip("/")
    # send_time 缺失时退回纪元 0(1970-01-01)而不是 now(): key 必须是确定的,
    # 否则同一条消息每重推一次就多出一个对象
    t = time.localtime(send_time or 0)
    group = _KEY_UNSAFE.sub("_", str(rid or "unknown"))
    return (f"{prefix}/{group}/{time.strftime('%Y%m%d', t)}"
            f"/{time.strftime('%H%M%S', t)}-{digest[:8]}{ext}")


def _require_kodo(settings: Settings) -> None:
    """没配对象存储就别做后面那些活(读文件、算 md5)—— 先说清楚再退出。"""
    if not settings.kodo_enabled:
        raise MediaError("未配置对象存储(kodo_ak/kodo_sk/kodo_bucket/kodo_domain)")


def _magic_ext(data: bytes) -> str:
    """从图片文件头识别扩展名,修正企微缓存中不可靠的文件名。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return ".heic"
    return ""


def _file_ext(path: str) -> str:
    """优先使用真实文件格式,再退回路径扩展名。"""
    with open(path, "rb") as stream:
        detected = _magic_ext(stream.read(32))
    return detected or os.path.splitext(path)[1].lower() or ".bin"


def _upload_name(name: str, ext: str) -> str:
    """让 multipart 文件名的 MIME 类型和实际字节保持一致。"""
    stem, _ = os.path.splitext(name)
    return f"{stem or 'image'}{ext}"


def _put(open_stream, name: str, digest: str, ext: str,
         settings: Settings, rid, send_time: int, *, needs_conversion: bool = False) -> str:
    """算 key → 已存在就复用 → 否则直传。两条上传路径唯一的差别只是字节从哪来。

    `open_stream` 是个无参函数而不是现成的流: 命中 `_exists` 时就不必先把文件打开
    (或者把 CDN 那几十 MB 拿在手里)。
    """
    _require_kodo(settings)
    key = _object_key(rid, send_time, digest, ext, settings)
    source_url = object_url(settings, key)
    if not _exists(source_url, settings.kodo_timeout):
        with open_stream() as stream:
            _post_form(stream, name, key, upload_token(settings, key), settings.kodo_timeout)
    return _display_url(source_url, needs_conversion)


def upload(path: str, settings: Settings, rid="", send_time: int = 0, md5: str = "") -> str:
    """上传本地图片,返回公网 URL。已存在则直接复用,不重传。

    `md5` 是**原图**的 md5(来自消息正文)。传了就用它当区分位,不去读文件算 ——
    这样本地只缓存到渲染件时,key 仍然认的是这张图的身份,不会一图两对象。
    """
    _require_kodo(settings)          # 先挡住,免得白读一遍文件算 md5

    def open_stream() -> io.IOBase:
        return open(path, "rb")
    try:
        source_ext = _file_ext(path)
        needs_conversion = source_ext not in _MARS_IMAGE_EXTS
        ext = source_ext if not needs_conversion else ".jpg"
        name = _upload_name(os.path.basename(path), ext)
        return _put(
            open_stream, name, md5 or file_md5(path), ext, settings, rid, send_time,
            needs_conversion=needs_conversion,
        )
    except OSError as e:
        raise MediaError(f"读取待上传文件失败: {e}") from e


def upload_bytes(data: bytes, ext: str, settings: Settings, rid="", send_time: int = 0) -> str:
    """上传一段字节(CDN 拉回来的图),返回公网 URL。

    这条路没有正文 md5(表情包/转发图那类),只能拿收到的字节算 —— 对 key 来说够用,
    它在这里只当区分位,不承担「认出这是同一张图」的职责。转码只换扩展名,
    区分位仍是**原始字节**的 md5,所以同一张图重推落在同一个 key 上。
    """
    source_ext = _magic_ext(data) or ext.lower()
    needs_conversion = source_ext not in _MARS_IMAGE_EXTS
    ext = source_ext if not needs_conversion else ".jpg"
    digest = hashlib.md5(data).hexdigest()

    def open_stream() -> io.BytesIO:
        return io.BytesIO(data)
    return _put(
        open_stream, _upload_name("image", ext), digest, ext, settings, rid, send_time,
        needs_conversion=needs_conversion,
    )


# content-type → 扩展名。不用 mimetypes.guess_extension: 它对 image/jpeg 的
# 返回值随 Python 版本变过(.jpe/.jpg),而这个值会进对象 key,不能飘
CONTENT_TYPE_EXT = {
    "image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/heic": ".heic",
}


def download(url: str, timeout: float) -> tuple[bytes, str]:
    """从企微 CDN 取原图,返回 (内容, 扩展名)。"""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise MediaError(f"下载失败: {e}") from e
    if resp.status_code != 200:
        raise MediaError(f"下载失败 {resp.status_code}")

    data = resp.content
    if not data:
        raise MediaError("下载到的是空内容")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise MediaError(f"图片过大: {len(data)} 字节")

    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    return data, CONTENT_TYPE_EXT.get(ctype, ".jpg")


@dataclass(frozen=True)
class Located:
    """一条正文里认得出的那张图:在哪、叫什么、能不能取到。

    `pending` 和 `resolve` 问的是同一个问题的两面(「还等不等得到」/「现在就取」),
    以前各自解析一遍正文、各自 glob 一遍缓存 —— 同一条消息每轮要走两次。更要命的是
    两份定位逻辑必须手工保持一致:给 `resolve` 加一条取图路径而忘了改 `pending`,
    那些消息就会被永远判定成「还在路上」,每轮都白等满一个窗口。
    """
    md5: str
    filename: str
    url: str                 # 正文里的企微 CDN 直链(表情包/转发图才有)
    hits: list[str]          # 缓存里的候选,只 glob 出来的
    by_md5: bool

    @property
    def known(self) -> bool:
        """正文里认得出这张图 —— 认不出就等不到任何东西。"""
        return bool(self.md5 or self.filename)

    def best(self) -> str:
        """候选里挑原图。会读文件,所以只在真要用这张图时调。"""
        return _best(self.hits, self.by_md5, self.md5)

    def original(self) -> str:
        """只接受字节 md5 对得上的完整原图;客户端写到一半的文件不会命中。"""
        if not self.md5:
            return self.best()
        for path in sorted(self.hits, key=_variant_rank):
            try:
                if file_md5(path) == self.md5:
                    return path
            except OSError:
                continue
        return ""


# 链接卡片的载荷埋在 `pb1 → pb105`:1=跳转链接 2=缩略图 URL 3=标题 4=摘要。
_LINK_CARD_FIELD = 105


def _is_link_card(content) -> bool:
    """这条是链接卡片,不是图片消息。

    卡片正文里能搜到一个 `.png`(Grafana 告警卡是 S3 上的截图,别的卡是
    `icon.png`、`merge_excel.png` 之类),`extract_filename` 会把它当成附件名,
    于是整条消息被判成「有图还没到」:压住本群同步一整个 `mars_image_wait`,
    再触发一次注定落空的企微客户端切会话 —— 卡片缩略图不进本地图片缓存,正文里
    也没有 BigCDN 票据(实测 ct=4 全都没有,0/173)。

    整条按「认不出图」处理不会误伤真图:实测 400 条 ct=4 里 4 条是卡片,**没有
    一条有本地缓存命中**,而那 60 条有命中的全是真截图、一条卡片都不是。

    只认这一个位置、不全文搜 —— 理由同 `extract_url`。
    """
    if not content:
        return False
    if isinstance(content, str):
        content = content.encode("utf-8", "surrogateescape")
    inner = _pb_field1(content)
    if not inner:
        return False
    from .messages import _pb_fields
    return _LINK_CARD_FIELD in _pb_fields(inner)


def locate(content, settings: Settings | None = None) -> Located:
    """正文 → 这张图的去向。一次解析、一次 glob,`pending` 和 `resolve` 共用。"""
    if _is_link_card(content):
        return Located("", "", "", [], False)
    md5 = extract_media_md5(content)
    filename = extract_filename(content)
    hits, by_md5 = _candidates(filename, md5, settings)
    return Located(md5, filename, extract_url(content), hits, by_md5)


def locate_item(item: MediaItem, settings: Settings | None = None) -> Located:
    """一个已抽取媒体项 → 本地缓存位置。"""
    hits, by_md5 = _candidates(item.filename, item.md5, settings, item.cache_keys)
    return Located(item.md5, item.filename, item.source_url, hits, by_md5)


_trigger_lock = threading.Lock()
_triggered_at: dict[int, float] = {}


def _cache_download(data: bytes, ext: str, digest: str, settings: Settings) -> str:
    """原子写服务自己的 TCP 图片缓存;不碰企微管理的缓存目录。"""
    root = settings.data_dir / "media-cache"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{digest}{ext}"
    if path.exists():
        return str(path)
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=f".{digest}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(path)


def _trigger_client(rid: int, lid: int, settings: Settings) -> bool:
    if not settings.mars_image_client_trigger_enabled or not rid:
        return False
    now = time.monotonic()
    cooldown = max(0.0, float(settings.mars_image_trigger_cooldown))
    with _trigger_lock:
        last = _triggered_at.get(rid, 0.0)
        if now - last < cooldown:
            return False
        # 先占冷却位,避免同群并发图片同时切客户端。
        _triggered_at[rid] = now
    try:
        from .local_send import trigger_conversation_load

        ok, error = trigger_conversation_load(rid, lid)
    except Exception as e:
        log.warning("触发企微加载群 %s 图片失败: %s", rid, e)
        return False
    if not ok:
        log.warning("触发企微加载群 %s 图片失败: %s", rid, error)
    return ok


def _ensure(
    relocate: Callable[[], Located], fetches: list[tuple[str, Callable[[], str]]],
    settings: Settings, rid=0, lid: int = 0, deadline: float | None = None,
    *, allow_client: bool = True,
) -> str:
    """本地命中 → 主动下载路径 → 触发企微客户端等它自己下。

    `relocate` 每次调用都重新 glob:等的就是「文件刚刚出现」。`deadline` 是**整条
    消息**共享的取图预算,给 None 表示只受本次 `mars_image_client_wait` 约束。

    单图正文和媒体树里的项走的是同一段逻辑,差别只在怎么定位、怎么下载。分成两份
    抄的话,给其中一条加取图路径而忘了另一条,那些消息会被永远判定成「还在路上」。
    """
    for label, fetch in fetches:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            return fetch()
        except (media_tcp.MediaTCPError, MediaError, OSError) as e:
            log.info("%s拉图失败,继续回退: %s", label, e)

    if not allow_client or (deadline is not None and time.monotonic() >= deadline):
        return ""

    # 切会话和等文件必须处在同一次 UI 独占里。只锁 ``open -g`` 那一瞬间的话,
    # 并发的发消息/开群会马上把企微切走,下载就会随机中断。嵌套调用里的
    # trigger_conversation_load() 会识别本执行流已经持锁,不会自己撞自己。
    from .uilock import ClientBusy, ui_reserve

    try:
        # 用 ui_reserve 而不是裸 ui_session:前者会把本次占用写进 ContextVar,
        # trigger_conversation_load() 里嵌套的 ui_session 才能确认「还是同一条
        # 调用链」并放行。裸 ui_session 虽然拿到了物理锁,却没有这个标记,
        # 内层会把自己误判成并发请求并立即抛 ClientBusy。
        with ui_reserve("加载会话图片"):
            if not _trigger_client(int(rid or 0), int(lid or 0), settings):
                return relocate().best()

            stop = time.monotonic() + max(0.0, float(settings.mars_image_client_wait))
            if deadline is not None:
                stop = min(stop, deadline)
            poll = max(0.05, float(settings.mars_image_client_poll))
            while True:
                current = relocate()
                if path := current.original():
                    return path
                if time.monotonic() >= stop:
                    # 原图一直没到时才接受已存在的渲染件,保持旧的“有图比没图强”策略。
                    return current.best()
                time.sleep(min(poll, max(0.0, stop - time.monotonic())))
    except ClientBusy as e:
        log.info("企微客户端正忙,本轮不触发群 %s 加载图片: %s", rid, e)
        return relocate().best()


def ensure_local(content, settings: Settings, rid=0, lid: int = 0) -> str:
    """确保图片可读:本地命中 → BigCDN TCP → 触发企微客户端。"""
    loc = locate(content, settings)
    if loc.hits:
        return loc.best()
    if not loc.known or loc.url:
        return ""

    def fetch() -> str:
        data, ext = media_tcp.download(content, settings)
        return _cache_download(data, ext, loc.md5, settings)

    fetches = [("BigCDN TCP", fetch)] if loc.md5 and settings.mars_image_tcp_enabled else []
    return _ensure(lambda: locate(content, settings), fetches, settings, rid, lid)


def ensure_item_local(
    item: MediaItem, settings: Settings, rid=0, lid: int = 0,
    deadline: float | None = None, loc: Located | None = None,
) -> str:
    """确保一个媒体项可读。所有项共享一个 monotonic deadline,避免逐图叠加等待。

    `loc` 让调用方把自己刚 glob 出来的结果传进来 —— 这一趟 glob 不便宜
    (`_candidates` 的 docstring 记了实测数字),不该为同一个问题走两遍。
    """
    if item.kind != "image":
        return ""
    found = loc if loc is not None else locate_item(item, settings)
    if found.hits:
        return found.best()

    fetches: list[tuple[str, Callable[[], str]]] = []

    if item.ticket is not None and settings.mars_image_tcp_enabled:

        def fetch_ticket() -> str:
            assert item.ticket is not None
            data, ext = media_tcp.download_ticket(item.ticket, settings, deadline=deadline)
            return _cache_download(data, ext, item.md5, settings)

        fetches.append(("BigCDN TCP", fetch_ticket))

    # type101 的 source_url 仅用于选择图片和计算客户端缓存别名。临时地址实测
    # 即时请求也返回 400,不再用它主动下载;缓存未命中时直接让企微加载会话。
    return _ensure(
        lambda: locate_item(item, settings),
        fetches,
        settings,
        rid,
        lid,
        deadline,
        allow_client=item.md5_verified or bool(item.cache_keys),
    )


def resolve_item(
    item: MediaItem, settings: Settings, rid="", send_time: int = 0, lid: int = 0,
    deadline: float | None = None,
) -> str:
    """单个媒体项 → mars 可识别的公网图片 URL;失败返回空串。"""
    if not settings.kodo_enabled or item.kind != "image":
        return ""
    try:
        path = ensure_item_local(item, settings, rid, lid, deadline)
        if not path:
            return ""
        digest = item.md5 if item.md5_verified else ""
        return upload(path, settings, rid, send_time, md5=digest)
    except (MediaError, OSError) as e:
        log.warning("取图/上传失败(%s),退回占位符: %s", item.filename or item.md5, e)
        return ""


def pending(content, settings: Settings) -> bool:
    """图还没落到本地,但**还有希望** —— 正文里认得出这张图,缓存里还没有它。

    企微收到图片消息时只写消息行,图片文件要等你真的点开那个会话才下载,
    实测能晚上几十秒到几小时。所以「认得出、没文件」的含义是「还没到」,
    值得等一会儿再推(见 mars_sync._ready_count)。

    反过来,正文里既没有 md5 也没有文件名就等不到任何东西,不算 pending ——
    否则每条这样的消息都要白等一个完整的等待窗口。
    有 CDN 直链的也不算:那条路当场就能取到图,不需要等。
    """
    if not settings.kodo_enabled:
        return False
    loc = locate(content, settings)
    return loc.known and not loc.url and not loc.hits


def resolve(content, settings: Settings, rid="", send_time: int = 0, lid: int = 0) -> str:
    """消息正文 → 图片公网 URL。任何一步不成都返回空串,由调用方退回占位符。

    两条取图路径,本地缓存优先:缓存里那份是发送方原始字节,比 CDN 直链可靠。

    `rid`/`send_time` 只决定对象 key 的落点(见 `_object_key`),取哪张图完全不看它们。

    这里刻意不抛:一张图传不上去,不该让整条消息、整轮同步失败。
    """
    if not settings.kodo_enabled:
        return ""

    loc = locate(content, settings)
    try:
        path = ensure_local(content, settings, rid, lid)
        if path:
            return upload(path, settings, rid, send_time, md5=loc.md5)
        if loc.url:
            data, ext = download(loc.url, settings.kodo_timeout)
            return upload_bytes(data, ext, settings, rid, send_time)
    except (MediaError, OSError) as e:
        log.warning("取图/上传失败(%s),退回占位符: %s",
                    loc.filename or loc.md5 or "CDN 直链", e)
        return ""

    if loc.known:
        log.debug("本地缓存里还没有 %s(企微要等你点开会话才下载)", loc.filename or loc.md5)
    return ""
