"""从 mars 拉任务 —— 状态查看、手动跑一轮、起停循环。

跟 /mars/sync 那套一样,后台循环按配置在启动时自行拉起(见 main.py);
这里的接口是给人用的:配完 token 先 `/run` 手动跑一轮验证,再开常驻。
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import mars_tasks
from ..schemas_mars import TaskRunRequest
from ..security import require_token
from ..settings import get_settings

router = APIRouter(prefix="/api/v1/mars/tasks", tags=["mars-tasks"])


@router.get("/status", summary="任务拉取状态(累计条数/inflight/上次错误)")
async def status(_: None = Depends(require_token)) -> dict:
    settings = get_settings()
    state = mars_tasks.load_state(settings)
    configured = bool(settings.mars_base_url.strip() and settings.mars_tasks_token.strip())
    return {
        "errcode": 0,
        "errmsg": "ok",
        "enabled": settings.mars_tasks_enabled,
        "running": mars_tasks.loop.running,
        "configured": configured,
        "config": {
            "mars_base_url": settings.mars_base_url,
            "worker": mars_tasks.worker_name(settings),
            "wait": mars_tasks.pull_wait(settings),
            "max": settings.mars_tasks_max,
            "lease": settings.mars_tasks_lease,
            # token 本身不回显,只说配没配
            "token_configured": bool(settings.mars_tasks_token.strip()),
            "allowed_actions": sorted(mars_tasks.allowed_actions(settings)),
            "known_actions": sorted(mars_tasks.ACTIONS),
        },
        "state": state.to_dict(),
        "loop_error": mars_tasks.loop.last_error,
        "last_result": mars_tasks.loop.last_result.to_dict() if mars_tasks.loop.last_result else None,
    }


@router.post("/run", summary="立即拉一轮并执行")
async def run(req: TaskRunRequest | None = None, _: None = Depends(require_token)) -> dict:
    """body 可省略。手动验证时把 wait 设成 0,别干等着长轮询。

    后台循环开着时直接拒:run_once 内部虽然有互斥,但那只保证不并发,
    手动这一轮仍要干等到长轮询那一轮结束。与其让调用方莫名其妙卡 25s,
    不如直接说清楚 —— 要手动跑就先 /stop。
    """
    if mars_tasks.loop.running:
        raise HTTPException(
            status_code=409,
            detail="后台拉取循环正在运行,先 POST /api/v1/mars/tasks/stop 再手动跑",
        )
    result = await mars_tasks.run_once(wait=req.wait if req else 0.0)
    return {"errcode": 0, "errmsg": "ok", **result.to_dict()}


@router.post("/start", summary="启动后台拉取循环")
async def start(_: None = Depends(require_token)) -> dict:
    mars_tasks.loop.start()
    return {"errcode": 0, "errmsg": "ok", "running": mars_tasks.loop.running}


@router.post("/stop", summary="停止后台拉取循环")
async def stop(_: None = Depends(require_token)) -> dict:
    await mars_tasks.loop.stop()
    return {"errcode": 0, "errmsg": "ok", "running": mars_tasks.loop.running}
