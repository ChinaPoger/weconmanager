"""把请求里的图片(路径或 base64)落成服务端本地文件。"""
import base64
import binascii
import os
import tempfile
from contextlib import contextmanager
from typing import Iterator

from .schemas import LocalImageRequest
from .settings import get_settings

MAX_BASE64_CHARS = 40 * 1024 * 1024


@contextmanager
def materialized_image(req: LocalImageRequest) -> Iterator[str]:
    """产出可用的图片路径;base64 场景下退出时清理临时文件。

    Raises:
        ValueError: base64 缺失/超限/无法解码
    """
    if req.image_path:
        yield str(req.image_path)
        return

    raw = req.image_base64.strip()
    if not raw:
        raise ValueError("缺少 image_path 或 image_base64")
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
    upload_dir = get_settings().upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)

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
