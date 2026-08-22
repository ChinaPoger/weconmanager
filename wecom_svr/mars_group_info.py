"""群信息同步到 mars —— 群档案 / 成员快照 / 群公告。

和 mars_sync(群消息)分开:消息是高频增量,群信息是低频快照。把两者拆开后,
以后加字段(群公告、群主、标签、群头像……)不会碰到消息水位那套复杂约束。

下游是 mars 的 `PUT {base}/api/qq/group-infos`。群档案落库与消息 hook 共用同一套规则。

当前可从本地库稳定拿到的内容:
  - 群号 / 群名: Info.db CONVERSATION
  - 成员快照: CONVERSATION.member_bin / CONV_MEMBER_DATA.member_bin + Session.db USER
  - 群公告: 目前本地库里还没定位到可靠字段,先留空(不更新)

推送策略:
  - 默认跟消息同步同一白名单 / 同一轮触发,但用指纹跳过没变化的群
  - 空 members 且空 note 不推:mars 约定空 members 不改花名册,空 note 不改公告,
    推过去等于白打一次 HTTP
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field

import httpx

from . import directory, mars_sync, messages
from .settings import Settings, get_settings

log = logging.getLogger(__name__)

GROUP_INFO_PATH = "/api/qq/group-infos"


def group_info_url(settings: Settings) -> str:
    return mars_sync.mars_base(settings) + GROUP_INFO_PATH


@dataclass
class GroupInfoState:
    fingerprint: str = ""
    last_push: float = 0.0
    last_error: str = ""
    member_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> GroupInfoState:
        d = d or {}
        return cls(
            fingerprint=str(d.get("fingerprint") or ""),
            last_push=float(d.get("last_push") or 0),
            last_error=mars_sync.error_text(d.get("last_error")),
            member_count=int(d.get("member_count") or 0),
        )


@dataclass
class InfoState:
    version: int = 1
    groups: dict[str, GroupInfoState] = field(default_factory=dict)
    pushed: int = 0
    last_run: float = 0.0
    last_push: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "groups": {rid: g.to_dict() for rid, g in self.groups.items()},
            "pushed": self.pushed,
            "last_run": self.last_run,
            "last_push": self.last_push,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> InfoState:
        raw = d.get("groups") or {}
        if not isinstance(raw, dict):
            raise TypeError("groups 不是 JSON object")
        return cls(
            version=int(d.get("version") or 1),
            groups={str(rid): GroupInfoState.from_dict(g) for rid, g in raw.items()},
            pushed=int(d.get("pushed") or 0),
            last_run=float(d.get("last_run") or 0),
            last_push=float(d.get("last_push") or 0),
            last_error=mars_sync.error_text(d.get("last_error")),
        )


@dataclass
class GroupInfoResult:
    scanned: int = 0
    pushed: int = 0
    unchanged: int = 0
    skipped: int = 0
    groups: list[str] = field(default_factory=list)
    error: str = ""
    dry_run: bool = False
    pending: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def load_state(settings: Settings | None = None) -> InfoState:
    path = (settings or get_settings()).mars_group_info_state_file
    parsed = mars_sync.read_json_state(path, "群信息水位")
    return InfoState() if parsed is None else InfoState.from_dict(parsed)


def save_state(state: InfoState, settings: Settings | None = None) -> None:
    path = (settings or get_settings()).mars_group_info_state_file
    mars_sync.write_json_state(path, state.to_dict(), "群信息水位")


def fingerprint(payload: dict) -> str:
    """决定「这个群信息有没有变」的唯一摘要。

    只看 mars 会真正落库的字段。avatar 变动也算 —— 头像 URL 偶尔轮换,
    不跟的话 mars 侧会一直显示旧图。
    """
    body = {
        "group_number": payload.get("group_number") or "",
        "group_name": payload.get("group_name") or "",
        "uid": int(payload.get("uid") or 0),
        "note": payload.get("note") or "",
        "members": [
            {
                "member_id": m.get("member_id") or "",
                "name": m.get("name") or "",
                "userid": m.get("userid") or "",
                "corpid": m.get("corpid") or "",
                "avatar": m.get("avatar") or "",
            }
            for m in (payload.get("members") or [])
        ],
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def round_source(settings: Settings) -> str:
    """本轮的 source。整轮是同一个值,别在每个群里重算 —— 它要查通讯录。"""
    return directory.self_vid_or_none() or mars_sync.robot_id(settings)


def build_payload(rid: int, settings: Settings, source: str | None = None) -> dict:
    conv = messages.group_profile(rid)
    members = sorted(messages.group_members(rid), key=lambda m: m.get("member_id") or "")
    return {
        "group_number": mars_sync.group_number_text(settings.mars_group_prefix, rid),
        "group_name": mars_sync.group_name(conv.get("name") or "", settings, rid),
        "uid": 0,
        "client_type": settings.mars_client_type,
        "source": round_source(settings) if source is None else source,
        # 还没从本地库稳定解出群公告时留空。mars 侧约定:空串 = 本次不改公告。
        "note": "",
        "members": members,
    }


def should_push(payload: dict) -> bool:
    """空 members 且空 note 推过去等于空操作,省一次 HTTP。"""
    return bool(payload.get("members") or str(payload.get("note") or "").strip())


def build_round(rids: list[int], settings: Settings) -> list[tuple[int, dict, str]]:
    """一次线程切换里算完整轮的 payload 和指纹,返回 (rid, payload, 指纹)。

    指纹为空串表示这个群没什么可推。逐群 to_thread 在 sync_all_groups 打开时
    是几百次空转派发(plan_round 已经因为同样的理由合并过一次),而这个循环
    每 mars_sync_interval 秒就要来一遍。
    """
    source = round_source(settings)
    built: list[tuple[int, dict, str]] = []
    for rid in rids:
        payload = build_payload(rid, settings, source)
        built.append((rid, payload, fingerprint(payload) if should_push(payload) else ""))
    return built


async def push_one(client: httpx.AsyncClient, payload: dict, settings: Settings) -> None:
    await mars_sync.put_json(client, group_info_url(settings), payload, "mars 群信息接口")


async def sync_once(
    settings: Settings | None = None,
    dry_run: bool = False,
    *,
    rids: list[int] | None = None,
    names: list[str] | None = None,
) -> GroupInfoResult:
    """推一轮群信息。`rids` 由消息同步传入时复用同一白名单,避免再查一遍。"""
    settings = settings or get_settings()
    if not settings.mars_group_info_enabled:
        return GroupInfoResult(dry_run=dry_run)

    result = GroupInfoResult(dry_run=dry_run)
    if rids is None:
        rids, names, missing = await asyncio.to_thread(mars_sync.target_rids, settings)
        if missing:
            log.warning("这些群在本地会话里找不到,已跳过: %s", ", ".join(missing))
        if not rids:
            raise mars_sync.MarsNotConfigured(
                f"配置的群一个都没匹配上本地会话: {', '.join(missing)}"
            )
    result.groups = list(names or [])
    result.scanned = len(rids)

    state = load_state(settings)
    before = state.to_dict()
    due: list[tuple[int, dict, str]] = []

    for rid, payload, fp in await asyncio.to_thread(build_round, rids, settings):
        if not fp:
            result.skipped += 1
            continue
        prev = state.groups.get(str(rid))
        if prev and prev.fingerprint == fp:
            result.unchanged += 1
            continue
        due.append((rid, payload, fp))

    if dry_run:
        # 只有试运行会看 pending。真同步里带上它等于把整份成员名单再回传一遍。
        result.pending = [payload for _rid, payload, _fp in due]
        return result

    errors: list[str] = []
    now = time.time()
    if due:
        # 不能复用消息循环那个 10s 客户端:BackgroundLoop.http_client 在循环
        # 跑着时会忽略传入的 timeout。500 人快照上过 20s。
        timeout = max(1.0, float(settings.mars_group_info_timeout))
        async with httpx.AsyncClient(timeout=timeout) as client:
            for rid, payload, fp in due:
                try:
                    await push_one(client, payload, settings)
                except mars_sync.MarsSyncError as e:
                    err = mars_sync.error_text(e)
                    errors.append(err)
                    group = state.groups.setdefault(str(rid), GroupInfoState())
                    group.last_error = err
                    log.warning("群信息推送失败 rid=%s: %s", rid, e)
                    continue
                group = state.groups.setdefault(str(rid), GroupInfoState())
                group.fingerprint = fp
                group.last_push = now
                group.last_error = ""
                group.member_count = len(payload.get("members") or [])
                result.pushed += 1

    if result.pushed:
        state.pushed += result.pushed
        state.last_push = now
    state.last_error = mars_sync.summarize_errors(errors)
    result.error = state.last_error

    if state.to_dict() != before or result.pushed or errors:
        state.last_run = now
        save_state(state, settings)
    return result
