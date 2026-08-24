"""以「本人登录账号」身份操作客户端 —— 对方看到的是你本人。

这些接口会真实操控本机 UI(激活窗口、剪贴板、按键),因此:
  - 全部是同步 def,由 FastAPI 放线程池执行
  - local_send 内部有全局 UI 锁,并发请求会串行化
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from .. import local_group, local_send
from ..images import materialized_image
from ..schemas import (
    ConversationsRequest,
    ConversationsResponse,
    LocalCreateGroupRequest,
    LocalRenameGroupRequest,
    LocalSendRequest,
    LocalSendResponse,
    OpenConversationRequest,
)
from ..security import require_token
from ..uilock import current as ui_current

router = APIRouter(prefix="/api/v1", tags=["local"])


def _result_response(result: dict) -> JSONResponse:
    """沿用原 API 语义: 业务失败 → HTTP 400,body 结构不变。"""
    code = status.HTTP_200_OK if result.get("success") else status.HTTP_400_BAD_REQUEST
    return JSONResponse(status_code=code, content=result)


@router.post(
    "/message/send",
    summary="以本人身份发消息(操控客户端)",
    response_model=LocalSendResponse,
)
def send_local_message(req: LocalSendRequest, _: None = Depends(require_token)):
    if req.msgtype == "text":
        result = local_send.send_message(
            to=req.to, content=req.content, conv_rid=req.conv_rid, mentions=req.mentions
        )
        return _result_response(result)

    try:
        with materialized_image(req) as image_path:
            result = local_send.send_image(image_path=image_path, conv_rid=req.conv_rid)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"success": False, "error": str(e)},
        ) from e
    return _result_response(result)


@router.post(
    "/conversations",
    summary="查询本地会话列表(解密数据库)",
    response_model=ConversationsResponse,
)
def list_local_conversations(
    req: ConversationsRequest | None = None,
    _: None = Depends(require_token),
) -> ConversationsResponse:
    """body 可省略,等价于 {"keyword": ""}(返回最近会话)。"""
    keyword = req.keyword if req else ""
    # search_conversations 直接吐 sqlite 行(dict),交给 pydantic 校验成 Conversation
    return ConversationsResponse.model_validate({
        "success": True,
        "conversations": local_send.search_conversations(keyword),
    })


@router.post(
    "/conversation/open",
    summary="按 RID 打开会话但不发送",
    response_model=LocalSendResponse,
)
def open_local_conversation(req: OpenConversationRequest, _: None = Depends(require_token)):
    result = local_send.open_conversation_by_rid(req.conv_rid)
    return _result_response(result)


@router.get("/busy", summary="客户端界面是否正被占用")
def local_busy(_: None = Depends(require_token)) -> dict:
    """先问再发,省得撞上 409。

    这些操作要独占客户端界面(抢焦点/剪贴板/键盘),拉一次群可能占用几十秒。
    """
    busy = ui_current()
    if busy is None:
        return {"success": True, "busy": False, "operation": None, "seconds": 0}
    return {"success": True, "busy": True, "operation": busy.operation, "seconds": busy.seconds}


@router.post(
    "/group/create",
    summary="以本人身份拉群(操控客户端)",
    response_model=LocalSendResponse,
)
def create_local_group(req: LocalCreateGroupRequest, _: None = Depends(require_token)):
    """能拉外部联系人和微信用户 —— 官方 appchat 做不到这点。

    传了 `name` 就「先建群、再改名」两步走(面板里没有命名输入框);传了 `nickname`
    再打开群详情改「我在本群的昵称」。自己自动是群主。
    每个成员在选中前都会核对客户端搜出来的姓名与部门,搜出多条会 400 并列出候选。
    """
    result = local_group.create_group(
        req.members, name=req.name, nickname=req.nickname, return_rid=True
    )
    if result.get("success"):
        rid = result.get("rid")
        if rid is None:
            # 群已建成,但本地会话还没落库;不能猜 RID,否则可能把“1”发到别的群。
            result["message_error"] = "群已建好,但尚未定位到会话 RID,未发送初始化消息"
        else:
            message = local_send.send_message(conv_rid=rid, content="1")
            result["message_result"] = message
            if not message.get("success"):
                result["message_error"] = message.get("error") or "发送初始化消息失败"
    return _result_response(result)


@router.post(
    "/group/rename",
    summary="以本人身份改群名(操控客户端)",
    response_model=LocalSendResponse,
)
def rename_local_group(req: LocalRenameGroupRequest, _: None = Depends(require_token)):
    """官方 `appchat/update` 只认自己建的 chatid,别的群只能这么改。

    改完会回库核验:名字没落到目标群上就报错(并指出落到了哪个 RID),
    不会因为「脚本没报错」就当成功。
    """
    result = local_group.rename_group(req.conv_rid, req.name)
    return _result_response(result)


# 这些路径的错误 body 用 {"success": false, "error": ...},其余用 errcode/errmsg
# (main._error_body)。以前靠 "/api/v1/local" 前缀判断,本人身份的接口挪到
# `/api/v1/*` 裸路径之后前缀没了 —— 只能按实际注册的路由列出来,免得两处漂。
LOCAL_PATHS: frozenset[str] = frozenset(
    path for r in router.routes if (path := getattr(r, "path", None))
)
