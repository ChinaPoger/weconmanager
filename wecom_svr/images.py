"""把请求里的图片(本地路径、URL 或 base64)落成服务端本地文件。"""
import base64
import binascii
import ipaddress
import os
import socket
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .schemas import LocalSendRequest
from .settings import get_settings

MAX_BASE64_CHARS = 40 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_IMAGE_SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


def _validate_public_url(url: str) -> None:
    """只允许公网 HTTP(S),避免发送接口成为访问本机/内网的 SSRF 代理。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("image_url 必须是无用户凭证的公网 http(s) 地址")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port)
    except socket.gaierror as e:
        raise ValueError(f"image_url 域名解析失败: {e}") from e
    if not addresses:
        raise ValueError("image_url 域名没有可用地址")
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        if not ip.is_global:
            raise ValueError("image_url 不允许访问本机或内网地址")


def _download_image(url: str, directory: Path) -> str:
    current = url.strip()
    with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=False) as client:
        for redirects in range(MAX_REDIRECTS + 1):
            _validate_public_url(current)
            try:
                request = client.build_request("GET", current)
                response = client.send(request, stream=True)
            except httpx.HTTPError as e:
                raise ValueError(f"image_url 下载失败: {e}") from e

            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirects == MAX_REDIRECTS:
                        raise ValueError("image_url 重定向过多或缺少 Location")
                    current = urljoin(current, location)
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise ValueError(f"image_url 下载失败: HTTP {response.status_code}")

                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith("image/"):
                    raise ValueError(f"image_url 返回的不是图片: {content_type or '缺少 Content-Type'}")
                length = response.headers.get("content-length")
                if length and int(length) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"image_url 图片超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB")

                suffix = Path(unquote(urlsplit(current).path)).suffix.lower()
                if not suffix or len(suffix) > 10:
                    suffix = _IMAGE_SUFFIXES.get(content_type, ".img")
                fd, path = tempfile.mkstemp(prefix="wecom-image-", suffix=suffix, dir=directory)
                try:
                    size = 0
                    with os.fdopen(fd, "wb") as f:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_DOWNLOAD_BYTES:
                                raise ValueError(
                                    f"image_url 图片超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB"
                                )
                            f.write(chunk)
                    return path
                except Exception:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    raise
            finally:
                response.close()

    raise ValueError("image_url 下载失败")


@contextmanager
def materialized_image(req: LocalSendRequest) -> Iterator[str]:
    """产出可发送的图片路径;URL/base64 场景退出时删除临时文件。"""
    if req.image_path.strip():
        yield req.image_path.strip()
        return

    upload_dir = get_settings().upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)

    if req.image_url.strip():
        path = _download_image(req.image_url, upload_dir)
    else:
        raw = req.image_base64.strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        if len(raw) > MAX_BASE64_CHARS:
            raise ValueError("image_base64 过大")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"image_base64 无效: {e}") from e
        if not data:
            raise ValueError("image_base64 为空")

        filename = os.path.basename(req.filename or "image.png")
        suffix = os.path.splitext(filename)[1] or ".png"
        fd, path = tempfile.mkstemp(prefix="wecom-image-", suffix=suffix, dir=upload_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
