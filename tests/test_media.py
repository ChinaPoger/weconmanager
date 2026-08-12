"""图片落地 —— 文件名解析、缓存定位、上传签名。

不联网、不读真实缓存:上传走打桩,缓存目录用 tmp_path 造。
"""
import base64
import hashlib
import hmac
import json
import os
import time

import pytest

from wecom_svr import media, uilock
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


# ─── CDN 直链解析 ───────────────────────────────────────────────────────

CDN = b"https://wework.qpic.cn/wwpic/393399_B-z8XhrZQ0iNrFM_1605166359/0"


def _pb_field1(v: bytes) -> bytes:
    """protobuf 字段 1(wire type 2)= 0x0A + 长度 varint + 内容。"""
    n, out = len(v), b""
    while True:
        byte, n = n & 0x7F, n >> 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return b"\x0a" + out + v


def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte, n = n & 0x7F, n >> 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def test_url_is_read_from_the_first_protobuf_field():
    """表情包/转发图没有文件名,正文开头放的就是 CDN 直链。"""
    assert media.extract_url(_pb_field1(CDN) + b"\x10\x02 \xef\x84\x01") == CDN.decode()


def test_url_survives_a_multi_byte_length_prefix():
    long_url = CDN + b"?" + b"a" * 200
    assert media.extract_url(_pb_field1(long_url)) == long_url.decode()


def test_avatars_inside_forwarded_records_are_not_mistaken_for_the_image():
    """合并转发的聊天记录里也有一堆 http 链接,但那些是发言人头像。

    它们藏在嵌套消息里,不在字段 1 的位置上 —— 按位置取就天然躲开了。
    """
    nested = _pb_field1(b"\x08\x89\x01" + b"bphttp://wework.qpic.cn/wwhead/duc2Tvp/0")
    assert media.extract_url(nested) == ""


def test_no_url_when_the_body_is_the_usual_asn1_blob():
    assert media.extract_url(_pb_field1(b"3069020102046230600201000204c8")) == ""
    assert media.extract_url(b"") == ""
    assert media.extract_url(None) == ""


def test_truncated_body_does_not_blow_up():
    assert media.extract_url(b"\x0a\xff") == ""
    assert media.extract_url(b"\x0a\x50" + CDN[:10]) == ""    # 长度超出实际字节


# ─── 原图 md5(正文里那段 ASN.1)──────────────────────────────────────────

MD5 = "1ab50be8760ffafff50e203038b94d0b"


def _asn1(md5: str = MD5, name: bytes = b"f1d9f413-0917-40be-89bd-b5e8f7699d98") -> bytes:
    """照抄真实正文的形状: 顶层 SEQUENCE 里套一个 OCTET STRING,md5 在**第二层**。

    整段是十六进制 ASCII 文本,不是二进制 —— 这正是坑所在。
    """
    inner = (
        b"\x02\x01\x00"
        + b"\x04\x24" + name
        + b"\x02\x03\x01\x9d\xd0"
        + b"\x04\x10" + bytes.fromhex(md5)
        + b"\x02\x01\x01\x02\x01\x00\x04\x00"
    )
    inner = b"\x30" + bytes([len(inner)]) + inner
    outer = b"\x02\x01\x02" + b"\x04" + bytes([len(inner)]) + inner
    der = b"\x30" + bytes([len(outer)]) + outer
    return b"\n\xd6\x01" + der.hex().encode()


def _image_body(data: bytes, name: bytes = b"shot.png") -> bytes:
    """能同时被 media 定位器和 BigCDN 票据解析器识别的真实字段子集。"""
    digest = hashlib.md5(data).hexdigest().encode()
    return b"".join([
        _asn1(digest.decode()),
        b"\x12" + _varint(len(name)) + name,
        b"\x20" + _varint(len(data)),
        b"\x42\x20" + b"30313233343536373839616263646566",
        b"\x52\x20" + digest,
    ])


def test_media_md5_is_read_from_the_nested_der():
    """md5 埋在第二层: 顶层 OCTET STRING 的值本身又是个 SEQUENCE,只走顶层会跳过去。"""
    assert media.extract_media_md5(_asn1()) == MD5


def test_media_md5_survives_a_misaligned_0410_in_the_hex_text():
    """踩过的坑: 整段是十六进制 ASCII,`\\x30\\x41\\x0f` 会在文本里拼出错位的 "0410"。

    正则会从那里起匹配,又因为不允许重叠,把真正那个连同后面 32 位一起吞掉 ——
    实测抓出来的是错位 6 个字符的一串。按 DER 走就不受文本层面的巧合影响。
    """
    body = _asn1(name=b"\x30\x41\x0f" * 12)          # 36 字节,长度前缀对得上
    assert b"0410" in body                            # 坑确实埋进去了
    assert media.extract_media_md5(body) == MD5


def test_media_md5_takes_the_last_16_byte_octet_string():
    """结构里真出现多个 16 字节 OCTET STRING 时,靠后那个才是 md5。"""
    decoy = "9" * 32
    inner = (
        b"\x04\x10" + bytes.fromhex(decoy)
        + b"\x04\x10" + bytes.fromhex(MD5)
    )
    inner = b"\x30" + bytes([len(inner)]) + inner
    outer = b"\x04" + bytes([len(inner)]) + inner
    der = b"\x30" + bytes([len(outer)]) + outer
    assert media.extract_media_md5(b"\n" + der.hex().encode()) == MD5


def test_media_md5_absent_or_unparsable_returns_empty():
    assert media.extract_media_md5(b"") == ""
    assert media.extract_media_md5(None) == ""
    assert media.extract_media_md5(b"\x01\x02just binary\xff") == ""
    # 十六进制串在,但不是 DER
    assert media.extract_media_md5(b"\x0a" + b"ff" * 40) == ""


def test_media_md5_tolerates_a_truncated_body():
    assert media.extract_media_md5(_asn1()[:60]) == ""


# ─── 缓存定位 ───────────────────────────────────────────────────────────

@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "Caches" / "Images"
    monkeypatch.setattr(media, "_images_root", lambda: str(root))
    return root


def test_prefers_the_hd_copy(cache):
    """没有 md5 可用时只能退回按文件名 + 体积挑。"""
    small = cache / "2026-08" / "abc"
    big = cache / "2026-08" / "abc_HD"
    for d, size in ((small, 100), (big, 5000)):
        d.mkdir(parents=True)
        (d / "shot.png").write_bytes(b"x" * size)

    assert media.find_local("shot.png") == str(big / "shot.png")


def _put(cache, dirname: str, name: str, data: bytes):
    d = cache / "2026-08" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return d / name


def test_md5_picks_the_original_even_when_the_rendition_is_bigger(cache):
    """实测 12.1% 的 `_HD` 比渲染件小(有一张原图 407x138、渲染件放大到 800x271)。

    旧的「取大的那份」因此挑错了 526 张 —— 按 md5 认原图就没有这个问题。
    """
    orig = b"original-bytes"
    md5 = hashlib.md5(orig).hexdigest()
    hd = _put(cache, f"{md5}_HD", "shot.png", orig)
    _put(cache, md5, "shot.png", b"re-encoded-and-much-much-larger")   # 渲染件,更大

    assert media.find_local("shot.png", md5) == str(hd)


def test_md5_lookup_ignores_the_filename_entirely(cache):
    """`pic.jpg` 一个名字对着 8 张不同的图,文件名不能当身份。"""
    orig = b"original-bytes"
    md5 = hashlib.md5(orig).hexdigest()
    right = _put(cache, f"{md5}_HD", "pic.jpg", orig)
    _put(cache, "f" * 32, "pic.jpg", b"a totally different image, much bigger")

    assert media.find_local("pic.jpg", md5) == str(right)


def test_falls_back_to_the_rendition_when_no_original_is_cached(cache):
    """4.7% 的图本地只有渲染件 —— 有图比没图强。"""
    md5 = "c" * 32
    _put(cache, f"{md5}_THUMB", "shot.png", b"thumb")
    big = _put(cache, md5, "shot.png", b"rendition-larger")

    assert media.find_local("shot.png", md5) == str(big)


def test_md5_miss_falls_back_to_the_filename(cache):
    """正文里解不出 md5 的那类(实测 17/165)还得靠文件名。"""
    p = _put(cache, "deadbeef", "shot.png", b"x")
    assert media.find_local("shot.png", "e" * 32) == str(p)


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


# 2026-08-11 10:33:02 +08:00 —— 最初那张丢掉的图的时刻
TS = 1786415582


@pytest.fixture
def shanghai(monkeypatch):
    """key 里的日期时刻按本机时区渲染,所以断言前得把时区钉死。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    yield
    monkeypatch.undo()      # 环境变量恢复了,还得让 libc 重新读一遍
    time.tzset()


def _stub_post(sink):
    """`_post_form` 的打桩 —— 它的签名只在这一处出现。"""
    return lambda stream, name, key, token, timeout: sink(key, stream)


@pytest.fixture
def posted(monkeypatch):
    """记录直传表单收到的 key。"""
    keys: list[str] = []
    monkeypatch.setattr(media, "_exists", lambda url, t: False)
    monkeypatch.setattr(media, "_post_form", _stub_post(lambda key, _stream: keys.append(key)))
    return keys


def test_object_key_is_group_day_then_time(tmp_path, settings, shanghai, posted):
    """key = <群 RID>/<YYYYMMDD>/<HHMMSS>-<md5前8><ext>,能按群按天翻。"""
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")
    digest = hashlib.md5(b"hello").hexdigest()

    url = media.upload(str(f), settings, rid=1688, send_time=TS)
    assert url == (f"https://cdn.example.com/robot"
                   f"/1688/20260811/103302-{digest[:8]}.png")
    assert posted == [f"robot/1688/20260811/103302-{digest[:8]}.png"]


def test_the_same_second_in_one_group_does_not_collide(tmp_path, settings, shanghai, posted):
    """多选发图会让好几张落在同一秒 —— 全库实测 123 张挤在 58 个槽位上。

    末尾 8 位 md5 是唯一的区分位: 少了它,同槽的图会互相静默覆盖。
    """
    for name, body in (("a.png", b"one"), ("b.png", b"two"), ("c.png", b"three")):
        (tmp_path / name).write_bytes(body)
        media.upload(str(tmp_path / name), settings, rid=1688, send_time=TS)

    assert len(set(posted)) == 3
    assert all(k.startswith("robot/1688/20260811/103302-") for k in posted)


def test_key_uses_the_original_md5_not_the_bytes_we_happen_to_have(tmp_path, settings, posted):
    """本地只缓存到渲染件时,区分位仍取原图 md5 —— 否则同一张图会落到两个对象上。"""
    f = tmp_path / "shot.png"
    f.write_bytes(b"downscaled-rendition")

    url = media.upload(str(f), settings, rid=1688, send_time=TS, md5="a" * 32)
    assert url.endswith("-aaaaaaaa.png")
    assert hashlib.md5(b"downscaled-rendition").hexdigest()[:8] not in url


def test_the_key_is_a_pure_function_so_a_repush_hits_the_same_object(tmp_path, settings, posted):
    """重推同一条消息必须落在同一个 key 上,否则 _exists 去不了重,对象无限增殖。"""
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")

    first = media.upload(str(f), settings, rid=1688, send_time=TS)
    assert media.upload(str(f), settings, rid=1688, send_time=TS) == first


def test_a_missing_timestamp_still_yields_a_stable_key(tmp_path, settings, posted):
    """没有时刻时退回纪元 0,不是 now() —— now() 会让每次重推都多一个对象。"""
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")

    first = media.upload(str(f), settings, rid=1688)
    assert media.upload(str(f), settings, rid=1688) == first
    assert "/19700101/" in first or "/19700102/" in first  # 取决于本机时区


def test_a_hostile_rid_cannot_escape_the_prefix(tmp_path, settings, posted):
    """RID 理论上是纯数字,但它进对象 key,不能指望这一点。"""
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")

    media.upload(str(f), settings, rid="../../etc", send_time=TS)
    assert posted == [k for k in posted if k.startswith("robot/______etc/")]
    assert ".." not in posted[0]


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


# ─── CDN 直链兜底: 拉回来再传自己的 bucket,不直接把企微的地址给下游 ────────

@pytest.fixture
def cdn(monkeypatch):
    """打桩下载 + 上传,记录经过它们的东西。"""
    seen = {"downloaded": [], "uploaded": []}

    def fake_download(url, timeout):
        seen["downloaded"].append(url)
        return b"GIF89a", ".gif"

    monkeypatch.setattr(media, "download", fake_download)
    monkeypatch.setattr(media, "_exists", lambda url, t: False)
    monkeypatch.setattr(
        media, "_post_form",
        _stub_post(lambda key, stream: seen["uploaded"].append((key, stream.read()))),
    )
    return seen


def test_cdn_image_is_reuploaded_to_our_own_bucket(cache, settings, shanghai, cdn):
    """企微 CDN 的地址寿命和权限都不归我们管,不能直接推给 mars。"""
    cache.mkdir(parents=True)
    url = media.resolve(_pb_field1(CDN), settings, rid=1688, send_time=TS)

    # 这条路没有正文 md5,区分位只能拿收到的字节算
    key = f"robot/1688/20260811/103302-{hashlib.md5(b'GIF89a').hexdigest()[:8]}.gif"
    assert url == f"https://cdn.example.com/{key}"
    assert cdn["downloaded"] == [CDN.decode()]
    assert cdn["uploaded"] == [(key, b"GIF89a")]


def test_local_hd_copy_wins_over_the_cdn_link(cache, settings, cdn, monkeypatch):
    """缓存里那份是 _HD 原图,比 CDN 直链清楚 —— 两条路都通时用它。"""
    d = cache / "2026-08" / "abc"
    d.mkdir(parents=True)
    (d / "shot.png").write_bytes(b"x" * 99)

    body = _pb_field1(CDN) + _pb_str(b"shot.png")
    assert media.resolve(body, settings).endswith(".png")
    assert cdn["downloaded"] == []


def test_cdn_download_failure_degrades_to_the_placeholder(cache, settings, monkeypatch):
    def boom(url, t):
        raise media.MediaError("404")

    cache.mkdir(parents=True)
    monkeypatch.setattr(media, "download", boom)
    assert media.resolve(_pb_field1(CDN), settings) == ""


# ─── pending: 「还没到」和「没有」是两回事 ──────────────────────────────

def test_a_named_image_not_yet_in_the_cache_is_pending(cache, settings):
    """企微要等你点开会话才下载图片 —— 有名字没文件 = 还在路上,值得等。"""
    cache.mkdir(parents=True)
    assert media.pending(_pb_str(b"shot.png"), settings) is True


def test_an_image_already_in_the_cache_is_not_pending(cache, settings):
    d = cache / "2026-08" / "abc"
    d.mkdir(parents=True)
    (d / "shot.png").write_bytes(b"x")
    assert media.pending(_pb_str(b"shot.png"), settings) is False


def test_a_body_without_a_filename_is_not_pending(cache, settings):
    """手机端发的图正文里只有 ASN.1,等不到任何东西 —— 白等一个窗口才是 bug。"""
    cache.mkdir(parents=True)
    assert media.pending(b"3069020102046230600201000204c8", settings) is False
    assert media.pending(_pb_field1(CDN), settings) is False
    assert media.pending(None, settings) is False


def test_nothing_is_pending_when_kodo_is_off(cache, tmp_path):
    cache.mkdir(parents=True)
    assert media.pending(_pb_str(b"shot.png"), Settings(data_dir=tmp_path)) is False


# ─── 主动取图:TCP 优先,客户端触发兜底 ─────────────────────────────────

def test_ensure_local_uses_tcp_before_touching_the_client(cache, settings, monkeypatch):
    data = b"\x89PNG\r\n\x1a\n" + b"tcp-image"
    body = _image_body(data)
    monkeypatch.setattr(media.media_tcp, "download", lambda raw, s: (data, ".png"))
    monkeypatch.setattr(
        media, "_trigger_client", lambda *_a: pytest.fail("TCP 成功时不该触发客户端"),
    )

    path = media.ensure_local(body, settings, rid=1688, lid=99)

    assert media.file_md5(path) == hashlib.md5(data).hexdigest()
    assert str(settings.data_dir / "media-cache") in path
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert media.pending(body, settings) is False


def test_tcp_failure_triggers_client_and_waits_for_the_complete_original(
    cache, settings, monkeypatch,
):
    data = b"\x89PNG\r\n\x1a\n" + b"client-image"
    body = _image_body(data)
    digest = hashlib.md5(data).hexdigest()
    order: list[str] = []

    def tcp_fails(*_args):
        order.append("tcp")
        raise media.media_tcp.MediaTCPError("expired auth")

    def client_lands_original(rid, lid, _settings):
        order.append(f"client:{rid}:{lid}")
        assert uilock.current() is not None
        assert uilock.current().operation == "加载会话图片"
        target = cache / "2026-08" / f"{digest}_HD" / "shot.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        return True

    monkeypatch.setattr(media.media_tcp, "download", tcp_fails)
    monkeypatch.setattr(media, "_trigger_client", client_lands_original)
    configured = settings.model_copy(update={
        "mars_image_client_wait": 1.0,
        "mars_image_client_poll": 0.01,
    })

    path = media.ensure_local(body, configured, rid=1688, lid=99)

    assert order == ["tcp", "client:1688:99"]
    assert path.endswith("shot.png")
    assert media.file_md5(path) == digest


def test_client_trigger_can_reenter_the_reserved_ui_session(cache, settings, monkeypatch):
    """真实触发函数自己也会进 ui_session,外层预留必须让同调用链重入。"""
    data = b"\x89PNG\r\n\x1a\n" + b"nested-ui-session"
    body = _image_body(data)
    digest = hashlib.md5(data).hexdigest()

    monkeypatch.setattr(
        media.media_tcp, "download",
        lambda *_args: (_ for _ in ()).throw(media.media_tcp.MediaTCPError("offline")),
    )

    def real_shape_trigger(rid, lid, _settings):
        # local_send.trigger_conversation_load 的关键形状:自己再进一次 ui_session。
        with uilock.ui_session("触发会话图片加载"):
            assert uilock.current().operation == "加载会话图片"
            target = cache / "2026-08" / f"{digest}_HD" / "shot.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(data)
        return True

    monkeypatch.setattr(media, "_trigger_client", real_shape_trigger)
    configured = settings.model_copy(update={
        "mars_image_client_wait": 1.0,
        "mars_image_client_poll": 0.01,
    })

    path = media.ensure_local(body, configured, rid=1688, lid=99)

    assert media.file_md5(path) == digest
    assert uilock.current() is None


def test_both_active_paths_failing_keeps_the_image_pending(cache, settings, monkeypatch):
    body = _image_body(b"missing-image")

    def tcp_fails(*_args):
        raise media.media_tcp.MediaTCPError("offline")

    monkeypatch.setattr(media.media_tcp, "download", tcp_fails)
    monkeypatch.setattr(media, "_trigger_client", lambda *_a: False)

    assert media.ensure_local(body, settings, rid=1688, lid=99) == ""
    assert media.pending(body, settings) is True
