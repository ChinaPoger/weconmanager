"""以「本人登录账号」身份操作客户端 —— 对方看到的是你本人。

这些接口会真实操控本机 UI(激活窗口、剪贴板、按键),因此:
  - 全部是同步 def,由 FastAPI 放线程池执行
  - local_send 内部有全局 UI 锁,并发请求会串行化
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from .. import local_send
from ..images import materialized_image
from ..schemas import (
    ConversationsRequest,
    ConversationsResponse,
    LocalImageRequest,
    LocalSendRequest,
    LocalSendResponse,
    OpenConversationRequest,
)
from ..security import require_token

router = APIRouter(prefix="/api/v1/local", tags=["local"])


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
    result = local_send.send_message(to=req.to, content=req.content, conv_rid=req.conv_rid)
    return _result_response(result)


@router.post(
    "/image/send",
    summary="以本人身份发图片(操控客户端)",
    response_model=LocalSendResponse,
)
def send_local_image(req: LocalImageRequest, _: None = Depends(require_token)):
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
    req: Optional[ConversationsRequest] = None,
    _: None = Depends(require_token),
) -> ConversationsResponse:
    """body 可省略,等价于 {"keyword": ""}(返回最近会话)。"""
    keyword = req.keyword if req else ""
    conversations = local_send.search_conversations(keyword)
    return ConversationsResponse(success=True, conversations=conversations)


@router.post(
    "/conversation/open",
    summary="按 RID 打开会话但不发送",
    response_model=LocalSendResponse,
)
def open_local_conversation(req: OpenConversationRequest, _: None = Depends(require_token)):
    result = local_send.open_conversation_by_rid(req.conv_rid)
    return _result_response(result)
