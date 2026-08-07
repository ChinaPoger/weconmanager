"""SQLite WAL 回放 —— 让本地库读到「还没 checkpoint」的最新消息。

**这是整个同步能做到准实时的关键。** 企业微信的 SQLite 开了 WAL: 新消息先写
`Info.db-wal`,要等 checkpoint 才落进 `Info.db`;而解密工具只读 `Info.db`。
实测这个滞后可以到 48 分钟以上(README 记过 >1 小时),轮询间隔调到多小都没用 ——
瓶颈根本不在轮询侧。

WAL 里的页和主库里的页是同一套页级加密(页号入 key,无 HMAC),所以可以把
WAL 帧解密后覆盖到主库副本上,自己算出「客户端此刻看到的库」。实测覆盖后
最新消息只落后墙上时间几十秒,那就是企微自己刷盘的节奏了。

正确性上有三个坑,都踩过:

  1. **上一代残留**: checkpoint 后 WAL 从头重写,但文件不截断。20MB 的文件里
     可能只有前 100 帧是本代的,后面全是旧数据。靠 salt + 校验和判边界,
     不能按文件大小算帧数。
  2. **未提交的尾巴**: 最后一个 commit 帧之后可能是写了一半的事务,必须丢掉。
  3. **撕裂的帧**: 我们是在别人写的同时读,最后一帧可能只写了一半。逐帧验
     SQLite 自己的滚动校验和,第一个对不上就停 —— 和 SQLite 恢复时的判据一致。
"""
import logging
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

HDR_SZ = 32
FRAME_HDR_SZ = 24
# 末位表示校验和的字节序: 0x...82 小端, 0x...83 大端
MAGIC = (0x377F0682, 0x377F0683)

_MASK = 0xFFFFFFFF


@dataclass
class Wal:
    """一个 WAL 里**当前这代、且已提交**的页。"""

    page_size: int = 0
    pages: dict[int, bytes] = field(default_factory=dict)  # 页号 → 仍是密文的页
    db_pages: int = 0        # 最后一个 commit 帧记录的库大小(页数)

    def __bool__(self) -> bool:
        return bool(self.pages)


def _checksum(s0: int, s1: int, data: bytes, fmt: str) -> tuple[int, int]:
    """SQLite 的滚动校验和(每 8 字节一组的斐波那契式累加)。"""
    for i in range(0, len(data), 8):
        x0, x1 = struct.unpack(fmt, data[i:i + 8])
        s0 = (s0 + x0 + s1) & _MASK
        s1 = (s1 + x1 + s0) & _MASK
    return s0, s1


def parse(raw: bytes) -> Wal:
    """解析 WAL 字节流,取出本代已提交的页。解析不了就返回空 Wal(不抛)。"""
    if len(raw) < HDR_SZ:
        return Wal()

    magic, _ver, page_size, _seq, salt1, salt2, ck1, ck2 = struct.unpack(">8I", raw[:HDR_SZ])
    if magic not in MAGIC or page_size <= 0 or page_size % 512:
        return Wal()

    fmt = ">II" if magic & 1 else "<II"
    s0, s1 = _checksum(0, 0, raw[:24], fmt)
    if (s0, s1) != (ck1, ck2):
        log.debug("WAL 头校验和不符,跳过回放")
        return Wal()

    stride = FRAME_HDR_SZ + page_size
    # 按顺序记帧,最后只物化一次「到最后一个 commit 为止」的页表。
    # 每个 commit 都 dict(pages) 是 O(提交数 乘 页数),WAL 一大就退化。
    frames: list[tuple[int, bytes]] = []
    committed_frames = 0
    db_pages = 0
    off = HDR_SZ

    while off + stride <= len(raw):
        pgno, commit, fsalt1, fsalt2, fck1, fck2 = struct.unpack(
            ">6I", raw[off:off + FRAME_HDR_SZ]
        )
        if (fsalt1, fsalt2) != (salt1, salt2):
            break                       # 进入上一代残留
        page = raw[off + FRAME_HDR_SZ:off + stride]
        n0, n1 = _checksum(s0, s1, raw[off:off + 8], fmt)
        n0, n1 = _checksum(n0, n1, page, fmt)
        if (n0, n1) != (fck1, fck2):
            break                       # 撕裂或残留,后面一律不可信

        s0, s1 = n0, n1
        frames.append((pgno, page))
        if commit:                      # 事务边界:到这里为止都是持久的
            committed_frames = len(frames)
            db_pages = commit
        off += stride

    if not committed_frames:
        return Wal(page_size=page_size)
    # 后写的同页覆盖先写的 —— 顺序插入天然满足
    pages = dict(frames[:committed_frames])
    return Wal(page_size=page_size, pages=pages, db_pages=db_pages)


def read(wal_path: str) -> Wal:
    try:
        with open(wal_path, "rb") as f:
            return parse(f.read())
    except OSError as e:
        log.debug("读 %s 失败,跳过回放: %s", wal_path, e)
        return Wal()


def signature(wal_path: str) -> tuple[float, int]:
    """(mtime, size) —— 用来判断 WAL 有没有动过,避免无谓回放。"""
    try:
        st = os.stat(wal_path)
        return st.st_mtime, st.st_size
    except OSError:
        return (0.0, 0)


def _clone(base: str, dst: str) -> None:
    """把 base 复制到 dst,能用 APFS 写时复制就别真拷字节。

    `shutil.copyfile` 在 macOS 上走的是 `fcopyfile(COPYFILE_DATA)`,**不是**
    `clonefile` —— 464MB 实测 0.246s 且真写 464MB。这个函数每轮 WAL 变化都要
    跑一次(活跃客户端下基本每轮),折算约 150MB/s 的持续写入,只为盖几百 KB
    的增量。`cp -c` 走 clonefile,同样的库实测 0.054s、零写入。
    非 APFS(或跨卷)时 `cp -c` 会失败,退回真复制。
    """
    r = subprocess.run(["cp", "-c", base, dst], capture_output=True, text=True)
    if r.returncode == 0:
        return
    log.debug("clonefile 不可用(%s),退回字节复制", (r.stderr or "").strip())
    shutil.copyfile(base, dst)


def overlay(base: str, dst: str, wal: Wal, decrypt_page) -> int:
    """把 base 克隆一份、盖上 WAL 页、原子替换到 dst。返回覆盖的页数。

    盖页只有几十到几百页,开销都在克隆上,见 `_clone`。
    先克隆再替换而不是就地改 base,是因为 base 是下一轮的干净底稿 ——
    就地改会让 WAL 一代代叠上去,越叠越不对。
    """
    if not os.path.exists(base):
        raise FileNotFoundError(base)

    out_dir = os.path.dirname(dst) or "."
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-", suffix=".db")
    os.close(fd)
    try:
        # cp -c 要自己建目标文件,mkstemp 占的位得先让开
        os.unlink(tmp)
        _clone(base, tmp)
        applied = 0
        if wal.pages:
            with open(tmp, "r+b") as f:
                for pgno, page in wal.pages.items():
                    try:
                        plain = decrypt_page(page, pgno)
                    except ValueError as e:
                        # 单页解不开就整体放弃回放:半盖的库比旧库更危险
                        raise ValueError(f"WAL 第 {pgno} 页解密失败: {e}") from e
                    f.seek((pgno - 1) * wal.page_size)
                    f.write(plain)
                    applied += 1
                if wal.db_pages:
                    f.truncate(wal.db_pages * wal.page_size)
        os.replace(tmp, dst)
        return applied
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
