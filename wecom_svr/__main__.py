"""启动服务: `python -m wecom_svr` 或 `wecom-svr`。"""
import argparse
import logging

import uvicorn

from .settings import get_settings


def main() -> None:
    settings = get_settings()

    ap = argparse.ArgumentParser(description="企业微信消息发送服务")
    ap.add_argument("--host", default=settings.host, help=f"监听地址 (默认 {settings.host})")
    ap.add_argument("--port", type=int, default=settings.port, help=f"监听端口 (默认 {settings.port})")
    ap.add_argument("--reload", action="store_true", help="代码变更自动重载(开发用)")
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args()

    logging.basicConfig(level=a.log_level.upper())

    if not (settings.corpid and settings.secret):
        print("⚠️  缺少 corpid/secret,官方 API(/api/v1/message/send)会失败")
    if not settings.auth_enabled:
        print("🔓 未启用鉴权(仅本地使用,勿暴露公网)")

    uvicorn.run(
        "wecom_svr.main:app",
        host=a.host,
        port=a.port,
        reload=a.reload,
        log_level=a.log_level,
    )


if __name__ == "__main__":
    main()
