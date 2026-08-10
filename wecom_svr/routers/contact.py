"""通讯录查询 —— 走官方 API。"""
from fastapi import APIRouter, Depends

from ..agent import call_wecom
from ..schemas import ContactSearchRequest
from ..security import require_token

router = APIRouter(prefix="/api/v1/app/contact", tags=["app-contact"])


@router.post("/search", summary="查通讯录(官方 API)")
async def search_contact(req: ContactSearchRequest, _: None = Depends(require_token)) -> dict:
    return await call_wecom(
        "contact", "search", {"keyword": req.keyword, "department_id": req.department_id}
    )
