"""调用 wecom-agent 的 wecom.py(官方自建应用 API)。

走子进程而不是 import: wecom.py 出错时会 sys.exit,直接 import 会杀掉服务进程。
"""
import asyncio
import json
import logging
import os
import sys

from .settings import get_agent_home, get_settings

log = logging.getLogger(__name__)


async def call_wecom(category: str, method: str, args: dict) -> dict:
    """执行 `wecom.py <category> <method> <json_args>`,返回解析后的响应。

    永不抛 subprocess 异常 —— 失败一律转成 {"errcode": -1, "errmsg": ...},
    与上游 wecom.py 的错误结构保持一致。
    """
    home = get_agent_home()
    settings = get_settings()

    env = {**os.environ, **settings.agent_env()}
    argv = [
        sys.executable,
        str(home.wecom_cli),
        category,
        method,
        json.dumps(args, ensure_ascii=False),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(home.root),
        )
    except OSError as e:
        return {"errcode": -1, "errmsg": f"无法启动 wecom.py: {e}"}

    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=settings.wecom_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"errcode": -1, "errmsg": f"wecom.py 超时({settings.wecom_timeout}s)"}

    stdout = raw_out.decode("utf-8", "replace").strip()
    stderr = raw_err.decode("utf-8", "replace").strip()

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("wecom.py %s/%s 非 JSON 输出: rc=%s", category, method, proc.returncode)
        return {"errcode": -1, "errmsg": stdout or stderr or f"wecom.py 退出码 {proc.returncode}"}
