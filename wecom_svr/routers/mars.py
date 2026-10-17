"""群消息同步到 mars —— 状态查看、手动触发、水位管理。

后台循环在应用启动时按配置自行拉起(见 main.py),这里的接口是给人用的:
配白名单前先 `/groups` 看有哪些群,配完用 `/run?dry_run` 确认会推什么,
出问题时看 `/status` 的 last_error,必要时 `/watermark` 重置。
"""
import asyncio

from fastapi import APIRouter, Depends, Query

from .. import mars_group_info, mars_sync, messages
from ..mars_sync import Cursor
from ..schemas_mars import SyncRunRequest, WatermarkRequest
from ..security import require_token
from ..settings import get_settings

router = APIRouter(prefix="/api/v1/mars/sync", tags=["mars-sync"])

# 消息里只给 media 用的原始二进制,不进 HTTP 响应
_RAW_FIELDS = frozenset(messages.MEDIA_FIELDS)


@router.get("/status", summary="同步状态(水位/累计条数/上次错误)")
async def status(
    rid: int | None = Query(None, ge=0, description="只看一个群"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: None = Depends(require_token),
) -> dict:
    settings = get_settings()
    try:
        state = mars_sync.load_state(settings)
    except mars_sync.MarsStateError as e:
        # 状态损坏时同步会 fail closed;状态页本身仍要能把原因说出来。
        state = None
        state_error = str(e)
    else:
        state_error = ""

    group_states: list[dict] = []
    summary: dict[str, object] = {
        "version": state.version if state else None,
        "active": 0,
        "blocked": 0,
        "failed": 0,
        "oldest_lag_seconds": None,
    }
    state_summary = None
    pagination: dict[str, object] = {"page": page, "page_size": page_size, "total": 0}
    if state is not None:
        summary["active"] = sum(1 for group in state.groups.values() if group.active)
        summary["blocked"] = sum(1 for group in state.groups.values() if group.active and group.held)
        summary["failed"] = sum(1 for group in state.groups.values() if group.active and group.last_error)
        now = mars_sync.time.time()
        summary["backing_off"] = sum(
            1 for group in state.groups.values() if group.active and group.next_attempt > now
        )
        lags = [
            max(0.0, now - group.cursor.send_time)
            for group in state.groups.values()
            if group.active and group.cursor.send_time
        ]
        summary["oldest_lag_seconds"] = max(lags) if lags else None
        keys = sorted(state.groups, key=lambda key: (not state.groups[key].active, int(key)))
        if rid is not None:
            keys = [str(rid)] if str(rid) in state.groups else []
        pagination["total"] = len(keys)
        keys = keys[(page - 1) * page_size:page * page_size]
        active_cursors = {
            int(key): state.groups[key].cursor for key in keys if state.groups[key].active
        }
        try:
            backlog = await asyncio.to_thread(messages.count_since, active_cursors)
            backlog_error = ""
        except Exception as e:  # 状态接口不能因为本地解密库暂时不可用而整体失败
            backlog = {}
            backlog_error = mars_sync.error_text(e)
        for key in keys:
            item = state.groups[key].to_dict()
            item.update({
                "rid": int(key),
                "backlog": backlog.get(int(key)),
                "lag_seconds": (
                    max(0.0, now - state.groups[key].cursor.send_time)
                    if state.groups[key].cursor.send_time else None
                ),
            })
            group_states.append(item)
        state_summary = state.to_dict()
        state_summary.pop("groups", None)
        pagination["backlog_error"] = backlog_error
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
            "image_wait": settings.mars_image_wait,
            "groups": settings.mars_group_selectors,
            "sync_all_groups": settings.mars_sync_all_groups,
            "group_prefix": settings.mars_group_prefix,
            "robot": mars_sync.robot_id(settings),
            "client_type": settings.mars_client_type,
            "group_info_enabled": settings.mars_group_info_enabled,
            "group_info_url": settings.mars_base_url and mars_group_info.group_info_url(settings),
        },
        "state": state_summary,
        "state_error": state_error,
        "summary": summary,
        "group_states": group_states,
        "pagination": pagination,
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
    rid: int | None = Query(None, ge=0, description="只预览一个活动群"),
    _: None = Depends(require_token),
) -> dict:
    settings = get_settings()
    async with mars_sync.sync_lock:
        try:
            before_rids, _before_names, _before_missing = await asyncio.to_thread(
                mars_sync.target_rids, settings
            )
        except mars_sync.MarsNotConfigured:
            before_rids = []
        await asyncio.to_thread(messages.refresh)
        rids, names, missing = await asyncio.to_thread(mars_sync.target_rids, settings)
        if rid is not None:
            if rid not in rids:
                raise mars_sync.MarsNotConfigured(f"群 {rid} 不在当前同步范围内")
            index = rids.index(rid)
            rids, names = [rid], [names[index]]
        state = mars_sync.load_state(settings)
        state = await asyncio.to_thread(
            lambda: mars_sync.reconcile_state(
                state,
                rids,
                settings,
                persist=False,
                fresh_rids=set(rids) - set(before_rids),
            )
        )
        cursors = {group_rid: state.groups[str(group_rid)].cursor for group_rid in rids}
        batch = await asyncio.to_thread(messages.fetch_since, cursors, limit, False, state.round_robin)
        # 和真实一轮共用同一个规划器,否则改了扣图规则 preview 就会开始说谎
        due, held_by_group = await asyncio.to_thread(mars_sync.plan_round, batch, rids, settings)
        held = sum(held_by_group.values())
        # frozen_hook 而不是 to_hook:已经落过 outbox 的消息推的是冻结的那份,
        # 重算一遍的话 preview 说的就不是「下一轮会推什么」了。
        pending = await asyncio.to_thread(
            lambda: [
                {**message, "hook": mars_sync.frozen_hook(message, settings)}
                for message in due
            ]
        )
    return {
        "errcode": 0,
        "errmsg": "ok",
        "groups": names,
        "unmatched": missing,
        "cursor": state.cursor.to_dict(),
        "group_cursors": {str(group_rid): cursor.to_dict() for group_rid, cursor in cursors.items()},
        "scanned": batch.scanned,
        "held": held,
        # raw/quote_raw 是原始 protobuf 二进制,留在响应里既序列化不了也没人看
        "pending": [{k: v for k, v in m.items() if k not in _RAW_FIELDS} for m in pending],
    }


@router.post("/watermark", summary="重设水位(重推一段历史,或跳过积压)")
async def set_watermark(req: WatermarkRequest, _: None = Depends(require_token)) -> dict:
    settings = get_settings()
    if req.resume_from_tombstone and req.rid is None:
        raise mars_sync.MarsNotConfigured("resume_from_tombstone 必须同时指定 rid")
    if req.resume_from_tombstone and (
        req.send_time is not None or req.lid is not None or req.backfill_seconds is not None
    ):
        raise mars_sync.MarsNotConfigured("resume_from_tombstone 不能与 send_time/backfill_seconds 同用")
    if req.send_time is not None and req.backfill_seconds is not None:
        raise mars_sync.MarsNotConfigured("send_time 与 backfill_seconds 只能选一个")
    if req.lid is not None and req.send_time is None:
        raise mars_sync.MarsNotConfigured("lid 必须与 send_time 同时使用")

    async with mars_sync.sync_lock:
        await asyncio.to_thread(messages.refresh)
        rids, _names, _missing = await asyncio.to_thread(mars_sync.target_rids, settings)
        if req.rid is not None and req.rid not in rids:
            raise mars_sync.MarsNotConfigured(f"群 {req.rid} 不在当前同步范围内")
        state = mars_sync.load_state(settings)
        state = await asyncio.to_thread(
            lambda: mars_sync.reconcile_state(state, rids, settings, persist=True)
        )
        keys = [str(req.rid)] if req.rid is not None else [str(rid) for rid in rids]
        if any(key not in state.groups for key in keys):
            raise mars_sync.MarsNotConfigured(f"群 {req.rid} 没有同步水位")

        changes: dict[str, dict] = {}
        for key in keys:
            group = state.groups[key]
            old = group.cursor
            try:
                if req.resume_from_tombstone:
                    group.resume_from_tombstone(force=req.force)
                elif req.send_time is not None:
                    group.rewind(Cursor(req.send_time, req.lid or 0), force=req.force)
                elif req.backfill_seconds is not None:
                    group.rewind(mars_sync.cursor_for_backfill(req.backfill_seconds),
                                 force=req.force)
                else:
                    latest = await asyncio.to_thread(messages.latest_cursor, int(key))
                    group.rewind(latest, force=req.force)
            except mars_sync.MarsSyncError as e:
                raise mars_sync.MarsNotConfigured(f"群 {key} {e}") from e
            group.last_error = ""
            group.fail_streak = 0
            changes[key] = {"old": old.to_dict(), "new": group.cursor.to_dict()}

        state.last_error = ""
        state.refresh_shadow_cursor()
        mars_sync.save_state(state, settings)
    return {
        "errcode": 0,
        "errmsg": "ok",
        "scope": "one" if req.rid is not None else "all_active",
        "cursor": state.cursor.to_dict(),
        "changes": changes,
    }


@router.get("/group-info/status", summary="群信息同步状态(指纹/上次推送)")
async def group_info_status(_: None = Depends(require_token)) -> dict:
    settings = get_settings()
    try:
        state = mars_group_info.load_state(settings)
        state_error = ""
    except mars_sync.MarsStateError as e:
        state = None
        state_error = str(e)
    return {
        "errcode": 0,
        "errmsg": "ok",
        "enabled": settings.mars_group_info_enabled,
        "config": {
            "mars_base_url": settings.mars_base_url,
            "group_info_url": settings.mars_base_url and mars_group_info.group_info_url(settings),
            "groups": settings.mars_group_selectors,
            "sync_all_groups": settings.mars_sync_all_groups,
        },
        "state": state.to_dict() if state else None,
        "state_error": state_error,
    }


@router.post("/group-info/preview", summary="预览待推的群信息(不推、不动指纹)")
async def group_info_preview(_: None = Depends(require_token)) -> dict:
    async with mars_sync.sync_lock:
        result = await mars_group_info.sync_once(dry_run=True)
    return {"errcode": 0, "errmsg": "ok", **result.to_dict()}


@router.post("/group-info/run", summary="立即推一轮群信息(可 dry_run)")
async def group_info_run(req: SyncRunRequest, _: None = Depends(require_token)) -> dict:
    async with mars_sync.sync_lock:
        result = await mars_group_info.sync_once(dry_run=req.dry_run)
    return {"errcode": 0, "errmsg": "ok", **result.to_dict()}


@router.post("/start", summary="启动后台同步循环")
async def start(_: None = Depends(require_token)) -> dict:
    mars_sync.loop.start()
    return {"errcode": 0, "errmsg": "ok", "running": mars_sync.loop.running}


@router.post("/stop", summary="停止后台同步循环")
async def stop(_: None = Depends(require_token)) -> dict:
    await mars_sync.loop.stop()
    return {"errcode": 0, "errmsg": "ok", "running": mars_sync.loop.running}
