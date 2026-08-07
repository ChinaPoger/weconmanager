"""群聊 —— 官方 appchat 接口 + 本地通讯录的名字解析。

拉群 = 「解析名字 → 调 appchat/create」。名字解析读本地解密库(离线、快),
建群走官方 API(联网)。响应在官方 body 上补一个 `members` 字段,让调用方
看得见「陈涛」到底被解析成了谁 —— 拉错人是不可撤销的,不能只回一个 chatid。
"""
import asyncio

from fastapi import APIRouter, Depends

from .. import directory, group
from ..schemas_group import (
    CreateGroupRequest,
    GroupTarget,
    ResolveMembersRequest,
    SearchMemberRequest,
    SendGroupRequest,
    UpdateGroupRequest,
)
from ..security import require_token

router = APIRouter(prefix="/api/v1/group", tags=["group"])


async def _resolve_owner(name: str | None) -> str:
    if not name:
        return ""
    member = await asyncio.to_thread(directory.resolve_member, name)
    return member["userid"]


async def _resolve(names: list[str], include_self: bool = False) -> list[dict]:
    """解析成员名 —— sqlite 查询,一个名字最多四次,批量时不能占着事件循环。

    路由是 async def,FastAPI 不会替它开线程,得自己 to_thread。
    """
    return await asyncio.to_thread(directory.resolve_members, names, include_self=include_self)


@router.post("/members/search", summary="模糊搜本地通讯录(建群前消歧)")
async def search_members(req: SearchMemberRequest, _: None = Depends(require_token)) -> dict:
    matched = await asyncio.to_thread(directory.find_members, req.keyword)
    return {"errcode": 0, "errmsg": "ok", "matched": matched}


@router.post("/members/resolve", summary="干跑:只解析名字,不建群")
async def resolve_members(req: ResolveMembersRequest, _: None = Depends(require_token)) -> dict:
    members = await _resolve(req.members, include_self=req.include_self)
    return {
        "errcode": 0,
        "errmsg": "ok",
        "members": members,
        "userlist": [m["userid"] for m in members],
    }


@router.post("/create", summary="拉群(官方 appchat/create)")
async def create_group(req: CreateGroupRequest, _: None = Depends(require_token)) -> dict:
    """成员必须在自建应用可见范围内;群主默认是自己。"""
    members = await _resolve(req.members, include_self=req.include_self)
    userlist = [m["userid"] for m in members]

    owner = await _resolve_owner(req.owner)
    if not owner and req.include_self:
        owner = directory.self_userid_or_none() or ""

    result = await group.create(
        name=req.name, userlist=userlist, owner=owner, chatid=req.chatid or ""
    )
    # 官方成功响应只有 chatid,把解析结果一并回给调用方便于核对
    return {**result, "name": req.name, "owner": owner, "members": members, "userlist": userlist}


@router.post("/get", summary="查群信息")
async def get_group(req: GroupTarget, _: None = Depends(require_token)) -> dict:
    return await group.get(req.chatid)


@router.post("/update", summary="改群名/换群主/加人/踢人")
async def update_group(req: UpdateGroupRequest, _: None = Depends(require_token)) -> dict:
    add = await _resolve(req.add) if req.add else []
    remove = await _resolve(req.remove) if req.remove else []
    owner = await _resolve_owner(req.owner)

    result = await group.update(
        chatid=req.chatid,
        name=req.name or "",
        owner=owner,
        add_user_list=[m["userid"] for m in add],
        del_user_list=[m["userid"] for m in remove],
    )
    return {**result, "added": add, "removed": remove, "owner": owner}


@router.post("/send", summary="往群里发消息(应用身份)")
async def send_to_group(req: SendGroupRequest, _: None = Depends(require_token)) -> dict:
    """以「应用」身份发。要以本人身份发,等群同步到本地库后走 /api/v1/local/message/send。"""
    return await group.send(req.chatid, req.content, req.msgtype)
