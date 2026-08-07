"""群消息同步到 mars —— 状态查看、手动触发、水位管理。

后台循环在应用启动时按配置自行拉起(见 main.py),这里的接口是给人用的:
配白名单前先 `/groups` 看有哪些群,配完用 `/run?dry_run` 确认会推什么,
出问题时看 `/status` 的 last_error,必要时 `/watermark` 重置。
"""
import asyncio

from fastapi import APIRouter, Depends, Query

from .. import mars_sync, messages
from ..mars_sync import Cursor
from ..schemas_mars import SyncRunRequest, WatermarkRequest
from ..security import require_token
from ..settings import get_settings

router = APIRouter(prefix="/api/v1/mars/sync", tags=["mars-sync"])

# 消息里只给 media 用的原始二进制,不进 HTTP 响应
_RAW_FIELDS = frozenset({"raw", "quote_raw"})


@router.get("/status", summary="同步状态(水位/累计条数/上次错误)")
async def status(_: None = Depends(require_token)) -> dict:
    settings = get_settings()
    state = mars_sync.load_state(settings)
    return {
        "errcode": 0,
        "errmsg": "ok",
        "enabled": settings.mars_sync_enabled,
        "running": mars_sync.loop.running,
        "config": {
            "mars_base_url": settings.mars_base_url,
            "hook_url": settings.mars_base_url and mars_sync.hook_url(settings),
            "interval": settings.mars_sync_interval,
            "batch": settings.mars_sync_batch,
            "groups": settings.mars_group_selectors,
            "sync_all_groups": settings.mars_sync_all_groups,
            "group_prefix": settings.mars_group_prefix,
            "robot": settings.mars_robot,
            "client_type": settings.mars_client_type,
        },
        "state": state.to_dict(),
        # 循环里的错误不落盘(重启即清),和 state.last_error 分开看
        "loop_error": mars_sync.loop.last_error,
        "last_result": mars_sync.loop.last_result.to_dict() if mars_sync.loop.last_result else None,
    }


@router.get("/groups", summary="本地群会话列表(挑同步白名单用)")
async def groups(
    search: str = Query("", description="按群名模糊搜"),
    _: None = Depends(require_token),
) -> dict:
    settings = get_settings()
    found = await asyncio.to_thread(messages.list_groups, search)
    # selected 必须和同步循环认的是同一套规则,否则这个「挑白名单用」的列表
    # 会显示得和实际推的对不上。resolve_groups 是那条规则的唯一实现。
    hit, _missing = await asyncio.to_thread(messages.resolve_groups, settings.mars_group_selectors)
    selected = {g["rid"] for g in hit}
    return {
        "errcode": 0,
        "errmsg": "ok",
        "groups": [{**g, "selected": g["rid"] in selected} for g in found],
    }


@router.post("/run", summary="立即同步一轮(可 dry_run)")
async def run(req: SyncRunRequest, _: None = Depends(require_token)) -> dict:
    result = await mars_sync.sync_once(dry_run=req.dry_run, limit=req.limit)
    return {"errcode": 0, "errmsg": "ok", **result.to_dict()}


@router.post("/preview", summary="预览水位后待推的消息(不推、不动水位)")
async def preview(
    limit: int = Query(20, ge=1, le=200),
    _: None = Depends(require_token),
) -> dict:
    settings = get_settings()
    rids, names, missing = await asyncio.to_thread(mars_sync.target_rids, settings)
    await asyncio.to_thread(messages.refresh)

    state = mars_sync.load_state(settings)
    # 水位没落过盘时要和第一轮真同步取同一个起点 —— 自己算会漏掉 backfill 配置,
    # 那样预览的和实际推的就不是一批消息,这个接口也就白做了
    cursor = state.cursor
    if cursor == Cursor():
        cursor = await asyncio.to_thread(mars_sync.initial_cursor, settings)
    batch = await asyncio.to_thread(messages.fetch_since, cursor, rids, limit)
    pending = await asyncio.to_thread(
        lambda: [{**m, "hook": mars_sync.to_hook(m, settings)} for m in batch]
    )
    return {
        "errcode": 0,
        "errmsg": "ok",
        "groups": names,
        "unmatched": missing,
        "cursor": cursor.to_dict(),
        "scanned": batch.scanned,
        # raw/quote_raw 是原始 protobuf 二进制,留在响应里既序列化不了也没人看
        "pending": [{k: v for k, v in m.items() if k not in _RAW_FIELDS} for m in pending],
    }


@router.post("/watermark", summary="重设水位(重推一段历史,或跳过积压)")
async def set_watermark(req: WatermarkRequest, _: None = Depends(require_token)) -> dict:
    settings = get_settings()
    state = mars_sync.load_state(settings)

    if req.send_time is not None:
        state.cursor = Cursor(req.send_time, req.lid or 0)
    elif req.backfill_seconds is not None:
        state.cursor = mars_sync.cursor_for_backfill(req.backfill_seconds)
    else:
        await asyncio.to_thread(messages.refresh)
        state.cursor = await asyncio.to_thread(messages.latest_cursor)

    state.last_error = ""
    mars_sync.save_state(state, settings)
    return {"errcode": 0, "errmsg": "ok", "cursor": state.cursor.to_dict()}


@router.post("/start", summary="启动后台同步循环")
async def start(_: None = Depends(require_token)) -> dict:
    mars_sync.loop.start()
    return {"errcode": 0, "errmsg": "ok", "running": mars_sync.loop.running}


@router.post("/stop", summary="停止后台同步循环")
async def stop(_: None = Depends(require_token)) -> dict:
    await mars_sync.loop.stop()
    return {"errcode": 0, "errmsg": "ok", "running": mars_sync.loop.running}
