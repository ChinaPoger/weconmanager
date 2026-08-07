"""FastAPI 应用 —— 企业微信消息发送服务。

两套发消息互补:
    /api/v1/message/send       → 官方 API,消息显示为「应用」发送
    /api/v1/local/message/send → 操控本地客户端,消息以你本人的身份发送

错误结构沿用原 api_server.py:
    /api/v1/local/*  → {"success": false, "error": "..."}
    其它             → {"errcode": -1, "errmsg": "..."}
"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .local_send import LocalDataError, LocalSendError
from .paths import AgentHomeError
from .routers import contact, local, message
from .schemas import HealthResponse
from .settings import get_agent_home, get_settings

log = logging.getLogger(__name__)

DESCRIPTION = """
企业微信消息发送 API。底层引用 [wecom-agent](https://github.com/tzwkb/wecom-agent) 作为 pkg。

- **/api/v1/message/send** — 以「自建应用」身份发消息(官方 API)
- **/api/v1/local/message/send** — 以「本人登录账号」身份发消息(操控 macOS 客户端)
- **/api/v1/contact/search** — 查通讯录

鉴权可选: 配了 `api_token` 后,请求需带 `Authorization: Bearer <token>`。
"""


def _is_local_path(path: str) -> bool:
    return path.startswith("/api/v1/local")


def _error_body(path: str, message: str) -> dict:
    if _is_local_path(path):
        return {"success": False, "error": message}
    return {"errcode": -1, "errmsg": message}


def _first_validation_message(exc: RequestValidationError) -> str:
    for err in exc.errors():
        msg = str(err.get("msg", ""))
        # pydantic 会给自定义校验器加 "Value error, " 前缀
        msg = msg.removeprefix("Value error, ")
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body",)]
        return f"{'.'.join(loc)}: {msg}" if loc else msg
    return "invalid request body"


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="WeCom 消息发送服务",
        description=DESCRIPTION,
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    app.include_router(message.router)
    app.include_router(local.router)
    app.include_router(contact.router)

    # ── 错误处理: 保持与原 api_server.py 一致的 body 结构 ────────────

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        # 原实现对缺字段返回 400,而非 FastAPI 默认的 422
        return JSONResponse(
            status_code=400,
            content=_error_body(request.url.path, _first_validation_message(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        content = detail if isinstance(detail, dict) else _error_body(request.url.path, str(detail))
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(LocalDataError)
    async def _on_local_data_error(request: Request, exc: LocalDataError):
        return JSONResponse(status_code=503, content=_error_body(request.url.path, str(exc)))

    @app.exception_handler(LocalSendError)
    async def _on_local_send_error(request: Request, exc: LocalSendError):
        return JSONResponse(status_code=400, content=_error_body(request.url.path, str(exc)))

    @app.exception_handler(AgentHomeError)
    async def _on_agent_home_error(request: Request, exc: AgentHomeError):
        return JSONResponse(status_code=503, content=_error_body(request.url.path, str(exc)))

    # ── 健康检查 ──────────────────────────────────────────────────────

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        creds = bool(settings.corpid and settings.secret)
        try:
            home = get_agent_home()
        except AgentHomeError as e:
            return HealthResponse(
                status="degraded",
                auth_enabled=settings.auth_enabled,
                credentials_configured=creds,
                detail=str(e),
            )
        return HealthResponse(
            status="ok",
            agent_home=str(home.root),
            wecom_cli=str(home.wecom_cli),
            auth_enabled=settings.auth_enabled,
            credentials_configured=creds,
            detail=None if creds else "缺少 corpid/secret,官方 API 调用会失败",
        )

    return app


app = create_app()
