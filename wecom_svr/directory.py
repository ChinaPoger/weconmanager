"""本地通讯录解析 —— 把「陈涛」翻成官方 API 认的 userid。

官方 appchat 接口只收 userid,而人说的是名字。Session.db 的 `USER` 表里
`name` 与 `acctid` 并存,`acctid` 就是官方 userid,这层翻译因此不用联网。

「我」的识别:Info.db 里单聊会话(conversationtype=0)的 `creater_id` 几乎
全是自己(单聊都是我这边发起的),取众数即可。实测该众数能在 USER 表里
反查到本人记录,比去猜客户端 Profile 目录靠谱。
"""
import functools
import re

from .localdb import LocalSendError, batch, query

# 请求里可以直接写「我」,省得先查自己的 userid
SELF_ALIASES = {"我", "自己", "本人", "me", "self"}

# 名字里出现这些字符,基本是想按 userid/邮箱匹配而不是中文名
_LOOKS_LIKE_ID = re.compile(r"^[A-Za-z0-9._@-]+$")


class DirectoryError(LocalSendError):
    """通讯录解析失败(可预期,回 400)。"""


class MemberNotFound(DirectoryError):
    pass


class AmbiguousMember(DirectoryError):
    """一个名字匹配到多个人 —— 必须让调用方挑,不能替他猜。"""


def _row_to_member(row: dict) -> dict:
    return {
        "userid": row.get("acctid", ""),
        "name": row.get("name", ""),
        "vid": row.get("RID"),
        "alias": row.get("alias", ""),
    }


def _search_users(sql_where: str, params: tuple) -> list[dict]:
    """USER 表查询,只保留有 acctid 的记录(没 acctid 的用不了官方接口)。"""
    rows = query(
        "Session.db",
        f"SELECT RID, name, acctid, alias FROM USER WHERE acctid != '' AND ({sql_where})"
        " ORDER BY hot DESC LIMIT 50",
        params,
    )
    return [_row_to_member(r) for r in rows]


def find_members(keyword: str) -> list[dict]:
    """按名字/userid/邮箱模糊搜通讯录(给调用方消歧用)。"""
    kw = keyword.strip()
    if not kw:
        return []
    like = f"%{kw}%"
    return _search_users(
        "name LIKE ? OR acctid LIKE ? OR alias LIKE ?", (like, like, like)
    )


@functools.lru_cache(maxsize=1)
def get_self() -> dict:
    """识别当前登录用户 —— 单聊会话 creater_id 的众数。"""
    rows = query(
        "Info.db",
        "SELECT creater_id, COUNT(*) AS c FROM CONVERSATION"
        " WHERE conversationtype = 0 AND creater_id != 0"
        " GROUP BY creater_id ORDER BY c DESC LIMIT 1",
    )
    if not rows:
        raise MemberNotFound(
            "无法从本地会话推断当前登录用户。请在请求里显式写出自己的 userid,"
            "而不是用「我」。"
        )

    vid = rows[0]["creater_id"]
    me = _search_users("RID = ?", (vid,))
    if not me:
        raise MemberNotFound(
            f"推断出登录用户 vid={vid},但 Session.db USER 表里查不到对应 acctid。"
            "请在请求里显式写出自己的 userid。"
        )
    return me[0]


def resolve_member(query_text: str) -> dict:
    """把一个称呼解析成唯一的通讯录成员。

    匹配顺序(先精确后模糊,每一档命中就停):
        我/self → 本人 → acctid 精确 → 邮箱 alias 精确 → 姓名精确 → 模糊
    """
    q = (query_text or "").strip()
    if not q:
        raise MemberNotFound("成员名为空")

    if q.lower() in SELF_ALIASES:
        return get_self()

    if _LOOKS_LIKE_ID.match(q):
        for where in ("lower(acctid) = lower(?)", "lower(alias) = lower(?)"):
            hit = _search_users(where, (q,))
            if hit:
                return hit[0]

    exact = _search_users("name = ?", (q,))
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousMember(_ambiguous_message(q, exact))

    fuzzy = find_members(q)
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise AmbiguousMember(_ambiguous_message(q, fuzzy))

    raise MemberNotFound(f"通讯录里找不到「{q}」")


def _ambiguous_message(q: str, candidates: list[dict]) -> str:
    shown = ", ".join(f"{c['name']}({c['userid']})" for c in candidates[:8])
    more = f" 等 {len(candidates)} 人" if len(candidates) > 8 else ""
    return f"「{q}」匹配到多个人: {shown}{more}。请改用 userid 精确指定。"


def resolve_members(queries: list[str], include_self: bool = False) -> list[dict]:
    """批量解析并去重,保持传入顺序;include_self 时把本人放在最前。

    每个成员额外带 `query` 字段,便于调用方看出「陈涛」被解析成了谁。
    """
    members: list[dict] = []
    seen: set[str] = set()

    def add(member: dict, asked_as: str) -> None:
        if member["userid"] in seen:
            return
        seen.add(member["userid"])
        members.append({**member, "query": asked_as})

    # 一个名字最多四次查询,200 人就是几百次 connect+close —— 圈进一个批里复用
    with batch():
        if include_self:
            add(get_self(), "我")
        for q in queries:
            add(resolve_member(q), q.strip())
    return members


def self_userid_or_none() -> str | None:
    """拿不到本人 userid 时返回 None —— 用于「默认群主」这类可降级场景。"""
    try:
        return get_self()["userid"]
    except DirectoryError:
        return None
