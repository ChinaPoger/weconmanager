"""解密数据库的连接层 —— local_send(发消息)与 directory(查人)共用。

数据来自 wecom-agent 的 `decrypt/macos/read_wecom.py`,两个库分工:
  - Info.db    CONVERSATION —— 会话(单聊/群聊),带 RID/type/fw_id
  - Session.db USER         —— 通讯录,带 name/acctid(= 官方 API 的 userid)

错误类型定义在这里而不是 local_send,是为了让「查人」也能复用同一套
HTTP 映射(LocalSendError→400,LocalDataError→503),又不产生循环 import。
"""
import sqlite3
import threading
from contextlib import contextmanager

from .paths import find_db
from .settings import get_agent_home

NOT_SYNCED = (
    "解密 OK,但核心表缺失 → 数据未同步(可能是新登录)。"
    "先打开企业微信浏览消息,等同步后重试。"
)


class LocalSendError(RuntimeError):
    """本地数据/客户端操作的可预期错误(向客户端回报文案,不是 500)。"""


class LocalDataError(LocalSendError):
    """本地数据库缺失或未同步。"""


def connect(name: str) -> sqlite3.Connection:
    """打开解密后的数据库,找不到就抛 LocalDataError 并指明补救方式。"""
    decrypt_dir = get_agent_home().decrypt_dir
    db = find_db(decrypt_dir, name)
    if not db:
        raise LocalDataError(
            f"未找到解密后的 {name}(查找目录 {decrypt_dir})。"
            "请先运行 wecom-agent 的 decrypt/macos/read_wecom.py"
        )
    return sqlite3.connect(db)


_batch = threading.local()


@contextmanager
def batch():
    """在这个块里 query() 复用同一批连接,退出时统一关掉。

    解析一个名字最多要查四次(acctid/alias/精确名/模糊),批量解析 200 人就是
    几百次 connect+close。逐个开连接的语义和复用完全一样(都是只读),所以
    调用方只要圈一下范围就能省掉这笔开销。线程隔离,嵌套安全。
    """
    if getattr(_batch, "conns", None) is not None:
        yield                                   # 已经在批里,交给最外层收尾
        return
    _batch.conns = {}
    try:
        yield
    finally:
        for conn in _batch.conns.values():
            conn.close()
        _batch.conns = None


def query(db_name: str, sql: str, params: tuple = ()) -> list[dict]:
    """执行只读查询,返回 dict 行;表不存在时翻译成「未同步」。"""
    conns = getattr(_batch, "conns", None)
    conn = conns.get(db_name) if conns is not None else None
    pooled = conn is not None
    if conn is None:
        conn = connect(db_name)
        if conns is not None:
            conns[db_name] = conn
            pooled = True
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise LocalDataError(NOT_SYNCED) from e
        raise
    finally:
        if not pooled:
            conn.close()
