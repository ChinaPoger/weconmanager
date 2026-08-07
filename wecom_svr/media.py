"""图片落地 —— 从企微本地缓存取原图,传对象存储,换回一个公网 URL。

为什么要这一步:mars 的 `qq_messages.content` 是纯文本列,前端用 `{{ }}` 渲染,
所以只推 `[图片]` 占位符的话,图永远显示不出来。要让人看见图,就得先给图一个
**别人也能打开的地址** —— 图原本躺在这台 Mac 的企微缓存里,别人访问不到,
企微清缓存或换机器就没了。

两段:
  - **定位**: 消息正文里带着原始文件名,缓存路径是
    `Caches/Images/<年-月>/<md5>[_HD]/<文件名>`。按文件名 glob 两级目录即可
    (实测 8537 个目录、0.1s),不用去解正文里那段 ASN.1 拿 md5。
    同名会命中缩略图和 `_HD` 两份,取大的那份。
  - **上传**: 走七牛直传表单 API,自己签 uploadToken(HMAC-SHA1),不引 SDK。
    对象 key 用**文件内容的 md5**,所以同一张图重复同步只会覆盖同一个对象,
    天然去重;上传前先 HEAD 一次,已经在就直接复用。

⚠️ 目标 bucket 是**公开可读**的(拿到 URL 就能打开,无需鉴权)。群聊图片传上去
等于放到公网上,只是没人知道地址。这是调用方要自己权衡的事,所以默认不开:
不配 kodo 就退回 `[图片]` 占位符。
"""
import base64
import functools
import glob
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import time

import httpx

from .settings import Settings

log = logging.getLogger(__name__)

UPLOAD_HOST = "https://up.qiniup.com"

# 正文里内嵌的原始文件名。前面常有一个 protobuf 长度字节,所以不锚定开头
_MEDIA_NAME = re.compile(rb"([^\x00-\x1f/\\]{1,120}\.(?:png|jpe?g|heic|gif|webp|bmp))")


class MediaError(RuntimeError):
    """取图或上传失败(可预期,调用方退回占位符即可)。"""


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


@functools.lru_cache(maxsize=1)
def _images_root() -> str:
    from .messages import _vendor
    return os.path.join(_vendor().paths.caches(), "Images")


def find_local(filename: str) -> str:
    """在企微图片缓存里按文件名定位原图,找不到返回空串。

    同名会同时命中缩略图和 `_HD` 高清版,取字节数大的那份 —— 推给下游的
    应该是能看清的那张。
    """
    if not filename:
        return ""
    # glob 的特殊字符要转义,否则文件名里的 [] 会被当成字符集
    pattern = os.path.join(_images_root(), "*", "*", glob.escape(filename))
    hits = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    if not hits:
        return ""
    return max(hits, key=lambda p: os.path.getsize(p))


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
    return f"{settings.kodo_domain.rstrip('/')}/{key}"


def _exists(url: str, timeout: int) -> bool:
    """已经传过就别再传一遍 —— key 是内容 md5,存在即等价。"""
    try:
        return httpx.head(url, timeout=timeout, follow_redirects=True).status_code == 200
    except httpx.HTTPError:
        return False


def _post_form(path: str, key: str, token: str, timeout: int) -> None:
    """七牛直传表单。multipart 交给 httpx 编,不自己拼 boundary。"""
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as f:
            resp = httpx.post(
                UPLOAD_HOST,
                data={"token": token, "key": key},
                files={"file": (os.path.basename(path), f, ctype)},
                timeout=timeout,
            )
    except OSError as e:
        raise MediaError(f"读取待上传文件失败: {e}") from e
    except httpx.HTTPError as e:
        raise MediaError(f"上传失败: {e}") from e

    if resp.status_code != 200:
        raise MediaError(f"上传失败 {resp.status_code}: {resp.text[:200]}")


def upload(path: str, settings: Settings) -> str:
    """上传本地图片,返回公网 URL。已存在则直接复用,不重传。"""
    if not settings.kodo_enabled:
        raise MediaError("未配置对象存储(kodo_ak/kodo_sk/kodo_bucket/kodo_domain)")

    ext = os.path.splitext(path)[1].lower() or ".bin"
    key = f"{settings.kodo_prefix.strip('/')}/{file_md5(path)}{ext}"
    url = object_url(settings, key)

    if _exists(url, settings.kodo_timeout):
        return url
    _post_form(path, key, upload_token(settings, key), settings.kodo_timeout)
    return url


def resolve(content, settings: Settings) -> str:
    """消息正文 → 图片公网 URL。任何一步不成都返回空串,由调用方退回占位符。

    这里刻意不抛:一张图传不上去,不该让整条消息、整轮同步失败。
    """
    if not settings.kodo_enabled:
        return ""
    filename = extract_filename(content)
    if not filename:
        return ""
    path = find_local(filename)
    if not path:
        log.debug("本地缓存里没有 %s(可能已被企微清理)", filename)
        return ""
    try:
        return upload(path, settings)
    except (MediaError, OSError) as e:
        log.warning("上传 %s 失败,退回占位符: %s", filename, e)
        return ""
