"""以「自建应用」身份发消息 —— 走官方 API,对方看到的是应用。"""
from fastapi import APIRouter, Depends

from ..agent import call_wecom
from ..schemas import RecallMessageRequest, SendMessageRequest
from ..security import require_token

router = APIRouter(prefix="/api/v1/app/message", tags=["app-message"])
RECALL_PATH = "/cgi-bin/message/recall"


@router.post("/send", summary="以应用身份发消息(官方 API)")
async def send_message(req: SendMessageRequest, _: None = Depends(require_token)) -> dict:
    """返回体为企业微信官方响应({"errcode": 0, "errmsg": "ok", ...})。"""
    return await call_wecom("message", req.msgtype, req.to_agent_args())


@router.post("/recall", summary="撤回应用消息(官方 API)")
async def recall_message(req: RecallMessageRequest, _: None = Depends(require_token)) -> dict:
    """用发送接口返回的 msgid 撤回消息,响应体沿用企业微信官方结构。"""
    return await call_wecom(
        "call",
        "POST",
        {"path": RECALL_PATH, "body": {"msgid": req.msgid}},
    )
