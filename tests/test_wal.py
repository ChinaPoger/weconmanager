"""WAL 回放 —— 用合成 WAL 覆盖三个真实踩过的坑。

不依赖企业微信数据: 这里自己按 SQLite WAL 格式造字节流,页内容用可识别的
假数据(回放不关心页里是什么,只关心哪些页有效)。
"""
import os
import struct

import pytest

from wecom_svr import wal

PGSZ = 512          # 合法的最小页大小,测试里不用搬 4096 字节
SALT = (0xAAAA1111, 0xBBBB2222)
MAGIC_LE = 0x377F0682


def _ck(s0, s1, data, fmt="<II"):
    for i in range(0, len(data), 8):
        x0, x1 = struct.unpack(fmt, data[i:i + 8])
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def build(frames, salt=SALT, magic=MAGIC_LE, page_size=PGSZ, bad_header_ck=False):
    """frames: [(pgno, commit, page_bytes, override_salt|None)] → WAL 字节流。"""
    hdr = struct.pack(">6I", magic, 3007000, page_size, 1, salt[0], salt[1])
    s0, s1 = _ck(0, 0, hdr)
    if bad_header_ck:
        s0 ^= 1
    out = hdr + struct.pack(">2I", s0, s1)

    run0, run1 = _ck(0, 0, hdr)
    for pgno, commit, page, oversalt in frames:
        page = page.ljust(page_size, b"\x00")[:page_size]
        fs = oversalt or salt
        head8 = struct.pack(">2I", pgno, commit)
        n0, n1 = _ck(run0, run1, head8)
        n0, n1 = _ck(n0, n1, page)
        out += head8 + struct.pack(">4I", fs[0], fs[1], n0, n1) + page
        if oversalt is None:
            run0, run1 = n0, n1     # 只有本代帧参与滚动校验和
    return out


def page(tag: bytes) -> bytes:
    return tag


# ─── 解析 ──────────────────────────────────────────────────────────────

def test_takes_pages_up_to_the_last_commit():
    raw = build([
        (5, 0, page(b"five"), None),
        (6, 9, page(b"six"), None),      # commit: 到这里为止持久
        (7, 0, page(b"seven"), None),    # 事务写了一半,不能算
    ])
    w = wal.parse(raw)
    assert set(w.pages) == {5, 6}
    assert w.db_pages == 9
    assert w.page_size == PGSZ


def test_last_write_of_a_page_wins():
    raw = build([
        (5, 0, page(b"old"), None),
        (5, 0, page(b"new"), None),
        (5, 4, page(b"newest"), None),
    ])
    w = wal.parse(raw)
    assert w.pages[5].rstrip(b"\x00") == b"newest"


def test_stops_at_the_previous_generation():
    """checkpoint 后 WAL 从头重写但不截断,后面全是上一代的残留。"""
    raw = build([
        (5, 3, page(b"live"), None),
        (6, 3, page(b"stale"), (0xDEAD, 0xBEEF)),   # salt 变了 = 上一代
        (7, 3, page(b"stale2"), (0xDEAD, 0xBEEF)),
    ])
    w = wal.parse(raw)
    assert set(w.pages) == {5}


def test_stops_at_a_torn_frame():
    """边写边读,最后一帧可能只写了一半 —— 校验和对不上就整段丢掉。"""
    raw = bytearray(build([
        (5, 3, page(b"good"), None),
        (6, 3, page(b"torn"), None),
    ]))
    raw[-1] ^= 0xFF                      # 改坏最后一帧的页内容
    w = wal.parse(bytes(raw))
    assert set(w.pages) == {5}


def test_no_commit_yet_means_nothing_to_apply():
    w = wal.parse(build([(5, 0, page(b"x"), None)]))
    assert not w
    assert w.pages == {}


def test_rejects_a_bad_header():
    assert not wal.parse(b"")
    assert not wal.parse(b"\x00" * 40)
    assert not wal.parse(build([(5, 3, page(b"x"), None)], bad_header_ck=True))


def test_big_endian_checksum_variant():
    """magic 末位决定校验和字节序,认错了会把整个 WAL 判成无效。"""
    frames = [(5, 3, page(b"be"), None)]
    hdr = struct.pack(">6I", 0x377F0683, 3007000, PGSZ, 1, *SALT)
    s0, s1 = _ck(0, 0, hdr, ">II")
    out = hdr + struct.pack(">2I", s0, s1)
    for pgno, commit, pg, _ in frames:
        pg = pg.ljust(PGSZ, b"\x00")
        head8 = struct.pack(">2I", pgno, commit)
        n0, n1 = _ck(s0, s1, head8, ">II")
        n0, n1 = _ck(n0, n1, pg, ">II")
        out += head8 + struct.pack(">4I", SALT[0], SALT[1], n0, n1) + pg
    assert set(wal.parse(out).pages) == {5}


# ─── 覆盖 ──────────────────────────────────────────────────────────────

@pytest.fixture
def base(tmp_path):
    p = tmp_path / "Info.base.db"
    p.write_bytes(b"B" * (PGSZ * 4))
    return str(p)


def test_overlay_patches_pages_and_leaves_base_untouched(base, tmp_path):
    live = str(tmp_path / "Info.db")
    w = wal.parse(build([(2, 4, b"P" * PGSZ, None)]))

    applied = wal.overlay(base, live, w, lambda pg, no: pg)
    assert applied == 1

    got = open(live, "rb").read()
    assert got[:PGSZ] == b"B" * PGSZ            # 别的页原样
    assert got[PGSZ:PGSZ * 2] == b"P" * PGSZ    # 第 2 页被换掉
    assert open(base, "rb").read() == b"B" * (PGSZ * 4)   # 底稿不能被改


def test_overlay_truncates_to_the_committed_size(base, tmp_path):
    live = str(tmp_path / "Info.db")
    w = wal.parse(build([(1, 2, b"P" * PGSZ, None)]))   # commit 说库只有 2 页
    wal.overlay(base, live, w, lambda pg, no: pg)
    assert os.path.getsize(live) == PGSZ * 2


def test_overlay_with_empty_wal_is_just_a_copy(base, tmp_path):
    live = str(tmp_path / "Info.db")
    assert wal.overlay(base, live, wal.Wal(), lambda pg, no: pg) == 0
    assert open(live, "rb").read() == open(base, "rb").read()


def test_overlay_leaves_no_temp_file_when_a_page_fails(base, tmp_path):
    """半盖的库比旧库更危险,所以整体放弃 —— 且不能留下垃圾临时文件。"""
    live = str(tmp_path / "Info.db")

    def boom(pg, no):
        raise ValueError("key mismatch")

    with pytest.raises(ValueError):
        wal.overlay(base, live, wal.parse(build([(2, 4, b"P" * PGSZ, None)])), boom)

    assert not os.path.exists(live)
    assert [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")] == []


def test_signature_changes_when_the_wal_does(tmp_path):
    p = tmp_path / "x-wal"
    assert wal.signature(str(p)) == (0.0, 0)     # 不存在也要有确定值
    p.write_bytes(b"abc")
    first = wal.signature(str(p))
    p.write_bytes(b"abcdef")
    assert wal.signature(str(p)) != first
