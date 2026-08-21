"""FastAPI 应用 —— 企业微信消息发送服务。

两套发消息互补:
    /api/v1/app/message/send → 官方 API,消息显示为「应用」发送
    /api/v1/message/send     → 操控本地客户端,消息以你本人的身份发送

错误结构沿用原 api_server.py:
    本人身份的那几条(local.LOCAL_PATHS) → {"success": false, "error": "..."}
    其它                                 → {"errcode": -1, "errmsg": "..."}
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .group import GroupError
from .local_send import LocalDataError, LocalSendError
from .mars_sync import MarsNotConfigured, MarsSyncError
from .mars_sync import loop as mars_loop
from .mars_tasks import loop as mars_task_loop
from .online_bridge import (
    OnlineBadRequest,
    OnlineCliError,
    OnlineConfirmationRequired,
    OnlineUnavailable,
)
from .paths import AgentHomeError
from .routers import contact, doc, group, local, mars, mars_tasks, message, sheet, smartsheet
from .schemas import HealthResponse
from .settings import get_agent_home, get_settings
from .startup_preflight import run as run_startup_preflight
from .uilock import ClientBusy

log = logging.getLogger(__name__)

DESCRIPTION = """
企业微信消息发送 API。底层引用 [wecom-agent](https://github.com/tzwkb/wecom-agent) 作为 pkg。

- **/api/v1/app/message/send** — 以「自建应用」身份发消息(官方 API)
- **/api/v1/message/send** — 以「本人登录账号」身份发消息(操控 macOS 客户端)
- **/api/v1/app/group/create** — 拉群(官方 appchat),成员可直接写中文名和「我」
- **/api/v1/app/contact/search** — 查通讯录
- **/api/v1/mars/sync/** — 把本地群消息同步到 mars 的群消息接口
- **/api/v1/mars/sync/group-info/** — 把本地群档案/成员快照推给 mars
- **/api/v1/mars/tasks/** — 反过来:从 mars 拉任务并执行(mars 让本机发消息)

鉴权可选: 配了 `api_token` 后,请求需带 `Authorization: Bearer <token>`。
"""


def _is_local_path(path: str) -> bool:
    return path in local.LOCAL_PATHS


def _error_body(path: str, message: str) -> dict:
    if _is_local_path(path):
        return {"success": False, "error": message}
    return {"errcode": -1, "errmsg": message}


# 可预期异常 → HTTP 状态码。Starlette 按异常的 MRO 找处理器,所以父子类
# 同时登记没问题(ClientBusy 比 LocalSendError 更具体,命中的是自己那条)。
#
#   400 你得改请求或配置    403 你还没确认      409 客户端界面被占着,原样重试
#   502 下游失败            503 本机环境没就绪
_ERROR_STATUS: dict[type[Exception], int] = {
    LocalDataError: 503,
    AgentHomeError: 503,
    OnlineUnavailable: 503,
    ClientBusy: 409,
    OnlineConfirmationRequired: 403,
    MarsSyncError: 502,
    OnlineCliError: 502,
    LocalSendError: 400,
    GroupError: 400,
    MarsNotConfigured: 400,
    OnlineBadRequest: 400,
}


def _status_handler(code: int):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=code, content=_error_body(request.url.path, str(exc)))
    return handler


def _first_validation_message(exc: RequestValidationError) -> str:
    for err in exc.errors():
        msg = str(err.get("msg", ""))
        # pydantic 会给自定义校验器加 "Value error, " 前缀
        msg = msg.removeprefix("Value error, ")
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body",)]
        return f"{'.'.join(loc)}: {msg}" if loc else msg
    return "invalid request body"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """先完成新机器本地初始化,再拉起两个常驻循环。"""
    settings = get_settings()
    preflight = await asyncio.to_thread(run_startup_preflight, settings)
    app.state.startup_preflight = preflight.to_dict()
    if settings.mars_sync_enabled:
        mars_loop.start()
    if settings.mars_tasks_enabled:
        mars_task_loop.start()
    try:
        yield
    finally:
        await mars_loop.stop()
        await mars_task_loop.stop()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="WeCom 消息发送服务",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=_lifespan,
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
    app.include_router(group.router)
    app.include_router(doc.router)
    app.include_router(sheet.router)
    app.include_router(smartsheet.router)
    app.include_router(mars.router)
    app.include_router(mars_tasks.router)

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

    for exc_type, code in _ERROR_STATUS.items():
        app.add_exception_handler(exc_type, _status_handler(code))

    # ── 健康检查 ──────────────────────────────────────────────────────

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        creds = bool(settings.corpid and settings.secret)
        preflight = getattr(app.state, "startup_preflight", None)
        try:
            home = get_agent_home()
        except AgentHomeError as e:
            return HealthResponse(
                status="degraded",
                auth_enabled=settings.auth_enabled,
                credentials_configured=creds,
                startup_preflight=preflight,
                detail=str(e),
            )
        return HealthResponse(
            status="ok",
            agent_home=str(home.root),
            wecom_cli=str(home.wecom_cli),
            auth_enabled=settings.auth_enabled,
            credentials_configured=creds,
            startup_preflight=preflight,
            detail=None if creds else "缺少 corpid/secret,官方 API 调用会失败",
        )

    return app


app = create_app()
