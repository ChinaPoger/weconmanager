"""可选 Bearer 鉴权。

config.json / 环境变量里配了 api_token 才启用;不配则不鉴权(仅本地使用,勿暴露公网)。
"""
import secrets

from fastapi import Header, HTTPException, status

from .settings import get_settings


async def require_token(authorization: str = Header(default="")) -> None:
    token = get_settings().api_token
    if not token:
        return
    if not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"errcode": -1, "errmsg": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
