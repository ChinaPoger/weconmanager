"""启动服务: `python -m wecom_svr` 或 `wecom-svr`。"""
import argparse
import logging
import sys

import uvicorn
from uvicorn import Config, Server

from .settings import get_settings
from .singleton import InstanceTaken, claim, try_lock


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
        print("⚠️  缺少 corpid/secret,官方 API(/api/v1/app/message/send)会失败")
    if not settings.auth_enabled:
        print("🔓 未启用鉴权(仅本地使用,勿暴露公网)")

    # Uvicorn 默认先跑 lifespan(会弹企业微信)再 bind。端口被占时就会
    # 「先弹框再 exit 3」,KeepAlive 一开就是死循环。正式启动必须先把端口
    # 和文件锁拿到手;--reload 路径里 uvicorn 自己会先 bind,这里只抢文件锁。
    if a.reload:
        _claim_lock_or_exit(settings.data_dir)
        uvicorn.run(
            "wecom_svr.main:app",
            host=a.host,
            port=a.port,
            reload=True,
            log_level=a.log_level,
        )
        return

    try:
        claimed = claim(a.host, a.port, settings.data_dir)
    except InstanceTaken as e:
        print(f"❌ {e}")
        sys.exit(0 if e.ours else 1)

    try:
        config = Config(
            "wecom_svr.main:app",
            host=a.host,
            port=a.port,
            log_level=a.log_level,
        )
        Server(config).run(sockets=[claimed.sock])
    finally:
        claimed.close()


def _claim_lock_or_exit(data_dir) -> None:
    fd = try_lock(data_dir)
    if fd is None:
        print(
            f"❌ 另一份 wecom-svr 正在运行(锁文件 {data_dir / 'wecom-svr.lock'}),"
            "本次启动已放弃,不会再去操控企业微信"
        )
        sys.exit(0)
    # 故意不关 fd:--reload 路径里 uvicorn 自己先 bind,文件锁要一直握到进程退出,
    # 由内核在退出时释放。裸 fd 不是 Python 对象,不存在被 GC 提前收走的问题。


if __name__ == "__main__":
    main()
