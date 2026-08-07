"""以「自建应用」身份发消息 —— 走官方 API,对方看到的是应用。"""
from fastapi import APIRouter, Depends

from ..agent import call_wecom
from ..schemas import SendMessageRequest
from ..security import require_token

router = APIRouter(prefix="/api/v1/message", tags=["message"])


@router.post("/send", summary="以应用身份发消息(官方 API)")
async def send_message(req: SendMessageRequest, _: None = Depends(require_token)) -> dict:
    """返回体为企业微信官方响应({"errcode": 0, "errmsg": "ok", ...})。"""
    return await call_wecom("message", req.msgtype, req.to_agent_args())
