"""图片落地 —— 文件名解析、缓存定位、上传签名。

不联网、不读真实缓存:上传走打桩,缓存目录用 tmp_path 造。
"""
import base64
import hashlib
import hmac
import json

import pytest

from wecom_svr import media
from wecom_svr.settings import Settings

KODO = {
    "kodo_ak": "AK", "kodo_sk": "SK",
    "kodo_bucket": "ticket", "kodo_domain": "https://cdn.example.com/",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, **KODO)


def _pb_str(s: bytes) -> bytes:
    """protobuf 里的字符串 = 单字节长度前缀 + 内容(短串场景)。"""
    return bytes([len(s)]) + s


# ─── 文件名解析 ─────────────────────────────────────────────────────────

def test_filename_is_cut_at_the_protobuf_length_prefix():
    """踩过的坑: 长度前缀字节会被正则一起圈进来。

    59 = 0x3B = ';',肉眼看着像文件名的一部分,拿去 glob 就永远找不到文件。
    """
    name = "企业微信截图_4b36e11f-0a5d-4901-aa7c-a78f595e1a03.png".encode()
    blob = b"\x12\x34" + _pb_str(name) + b"\x99\x88"
    assert media.extract_filename(blob) == name.decode()


def test_plain_filename_without_a_usable_prefix_still_works():
    assert media.extract_filename(b"\x00\x00shot.png\x00").endswith("shot.png")


def test_no_image_name_returns_empty():
    assert media.extract_filename(b"") == ""
    assert media.extract_filename(None) == ""
    assert media.extract_filename(b"\x01\x02just binary\xff") == ""


def test_qq_relayed_images_have_no_filename():
    """type=29 的 QQ 中转图正文里只有 URL,没有文件名 —— 不该硬凑一个出来。"""
    blob = b"http://wxapp.tc.qq.com/270/20304/stodownload?m=383a3a4e&bizid=1023"
    assert media.extract_filename(blob) == ""


# ─── 缓存定位 ───────────────────────────────────────────────────────────

@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "Caches" / "Images"
    monkeypatch.setattr(media, "_images_root", lambda: str(root))
    return root


def test_prefers_the_hd_copy(cache):
    """同名会同时命中缩略图和 _HD,推给下游的该是看得清的那张。"""
    small = cache / "2026-08" / "abc"
    big = cache / "2026-08" / "abc_HD"
    for d, size in ((small, 100), (big, 5000)):
        d.mkdir(parents=True)
        (d / "shot.png").write_bytes(b"x" * size)

    assert media.find_local("shot.png") == str(big / "shot.png")


def test_missing_file_returns_empty(cache):
    cache.mkdir(parents=True)
    assert media.find_local("nope.png") == ""
    assert media.find_local("") == ""


def test_glob_metacharacters_in_a_filename_are_escaped(cache):
    """文件名里出现 [] 是常事,不转义会被 glob 当成字符集而匹配不到。"""
    d = cache / "2026-08" / "abc"
    d.mkdir(parents=True)
    (d / "shot[1].png").write_bytes(b"x")
    assert media.find_local("shot[1].png") == str(d / "shot[1].png")


# ─── 上传 ───────────────────────────────────────────────────────────────

def test_upload_token_is_a_valid_qiniu_token(settings):
    token = media.upload_token(settings, "k/1.png")
    ak, sign, policy = token.split(":")
    assert ak == "AK"

    raw = base64.urlsafe_b64decode(policy + "=" * (-len(policy) % 4))
    assert json.loads(raw)["scope"] == "ticket:k/1.png"
    expected = base64.urlsafe_b64encode(
        hmac.new(b"SK", policy.encode(), hashlib.sha1).digest()
    ).decode()
    assert sign == expected


def test_object_key_is_the_content_md5_so_reuploads_dedup(tmp_path, settings, monkeypatch):
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")
    digest = hashlib.md5(b"hello").hexdigest()

    posted = []
    monkeypatch.setattr(media, "_exists", lambda url, t: False)
    monkeypatch.setattr(media, "_post_form", lambda p, k, tok, t: posted.append(k))

    url = media.upload(str(f), settings)
    assert url == f"https://cdn.example.com/wecom-group-media/{digest}.png"
    assert posted == [f"wecom-group-media/{digest}.png"]


def test_already_uploaded_is_not_sent_again(tmp_path, settings, monkeypatch):
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")

    monkeypatch.setattr(media, "_exists", lambda url, t: True)
    monkeypatch.setattr(media, "_post_form", lambda *a: pytest.fail("不该重传"))
    assert media.upload(str(f), settings).endswith(".png")


def test_upload_without_config_is_refused(tmp_path):
    with pytest.raises(media.MediaError):
        media.upload(str(tmp_path), Settings(data_dir=tmp_path))


# ─── resolve: 失败一律降级,不能拖垮整轮同步 ─────────────────────────────

def test_resolve_returns_empty_when_kodo_is_off(tmp_path):
    assert media.resolve(b"\x08shot.png", Settings(data_dir=tmp_path)) == ""


def test_resolve_swallows_upload_failures(cache, settings, monkeypatch):
    d = cache / "2026-08" / "abc"
    d.mkdir(parents=True)
    (d / "shot.png").write_bytes(b"x")

    def boom(*a, **kw):
        raise media.MediaError("网络炸了")

    monkeypatch.setattr(media, "upload", boom)
    # 一张图传不上去,不该让整条消息、整轮同步失败
    assert media.resolve(_pb_str(b"shot.png"), settings) == ""


def test_resolve_returns_empty_when_the_file_was_cleaned_up(cache, settings):
    cache.mkdir(parents=True)
    assert media.resolve(_pb_str(b"gone.png"), settings) == ""
