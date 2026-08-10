"""群聊 —— 官方 appchat 接口封装。

走 wecom.py 的 `call` 逃生舱(上游 ROUTES 里没有 appchat),因此这里自己
持有路径常量。四个动作: 建群 / 查群 / 改群 / 往群里发消息。

要点(来自官方文档,已实测过权限):
  - 成员必须在自建应用的可见范围内,否则 create 直接失败
  - userlist 至少 2 人;把自己算进去时,「我 + 一个人」就已经满足
  - chatid 不传则由服务端随机生成;传了就必须全局唯一且只含 0-9a-zA-Z
  - 这样建出来的群,消息只能由本应用通过 appchat/send 推送(显示为应用);
    要以本人身份发言,等群同步到本地库后走 /api/v1/message/send
"""
import re

from .agent import call_wecom

CREATE = "/cgi-bin/appchat/create"
UPDATE = "/cgi-bin/appchat/update"
GET = "/cgi-bin/appchat/get"
SEND = "/cgi-bin/appchat/send"

# 官方限制:群聊名最多 50 个 utf8 字符
MAX_NAME_LEN = 50
MIN_MEMBERS = 2
MAX_MEMBERS = 2000

_CHATID_RE = re.compile(r"^[0-9a-zA-Z]{1,32}$")


class GroupError(RuntimeError):
    """建群参数不合法(可预期,回 400)。"""


def validate_chatid(chatid: str) -> str:
    if not _CHATID_RE.match(chatid):
        raise GroupError("chatid 只能是 1-32 位的 0-9a-zA-Z")
    return chatid


def validate_members(userids: list[str]) -> list[str]:
    if len(userids) < MIN_MEMBERS:
        raise GroupError(
            f"群成员至少 {MIN_MEMBERS} 人(含自己),当前 {len(userids)} 人: {userids}"
        )
    if len(userids) > MAX_MEMBERS:
        raise GroupError(f"群成员最多 {MAX_MEMBERS} 人,当前 {len(userids)} 人")
    return userids


def validate_name(name: str) -> str:
    if len(name) > MAX_NAME_LEN:
        raise GroupError(f"群名最多 {MAX_NAME_LEN} 个字符,当前 {len(name)} 个")
    return name


async def _post(path: str, body: dict) -> dict:
    return await call_wecom("call", "POST", {"path": path, "body": body})


async def create(name: str, userlist: list[str], owner: str = "", chatid: str = "") -> dict:
    """建群。返回官方响应,成功时带 chatid。"""
    body: dict = {"name": validate_name(name), "userlist": validate_members(userlist)}
    if owner:
        body["owner"] = owner
    if chatid:
        body["chatid"] = validate_chatid(chatid)
    return await _post(CREATE, body)


async def get(chatid: str) -> dict:
    return await call_wecom("call", "GET", {"path": GET, "params": {"chatid": chatid}})


async def update(
    chatid: str,
    name: str = "",
    owner: str = "",
    add_user_list: list[str] | None = None,
    del_user_list: list[str] | None = None,
) -> dict:
    """改群名/换群主/加人/踢人 —— 只提交实际给了的字段。"""
    body: dict = {"chatid": chatid}
    if name:
        body["name"] = validate_name(name)
    if owner:
        body["owner"] = owner
    if add_user_list:
        body["add_user_list"] = add_user_list
    if del_user_list:
        body["del_user_list"] = del_user_list
    if len(body) == 1:
        raise GroupError("没有要改的内容(name / owner / add / remove 至少给一个)")
    return await _post(UPDATE, body)


async def send(chatid: str, content: str, msgtype: str = "text") -> dict:
    """往群里推消息 —— 以「应用」身份,不是本人。"""
    if msgtype not in ("text", "markdown"):
        raise GroupError(f"暂不支持的 msgtype: {msgtype}(可用: text, markdown)")
    return await _post(SEND, {"chatid": chatid, "msgtype": msgtype, msgtype: {"content": content}})
