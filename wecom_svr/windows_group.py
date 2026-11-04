"""Windows group creation: exact identity, durable retries and DB verification."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import windows_local
from .group import MAX_NAME_LEN
from .localdb import LocalDataError, LocalSendError
from .uilock import ui_session

log = logging.getLogger(__name__)


class WindowsGroupError(LocalSendError):
    pass


@dataclass(frozen=True)
class Member:
    query: str
    name: str
    userid: str
    dept: str = ""
    external: bool = False
    company: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _resolve_members(queries: list[str]) -> list[Member]:
    """Resolve in input order; the first query must identify an external contact."""
    if not queries or not queries[0].strip():
        raise WindowsGroupError("members[0] 必须是外部联系人,未操作客户端")
    store = windows_local.store()
    own = store.self_id()
    if own is None:
        raise WindowsGroupError("无法识别当前登录账号,未操作客户端")
    conn = store.db("user.db")
    try:
        own_row = conn.execute("SELECT corp_id FROM user_table WHERE id=?", (own,)).fetchone()
        if own_row is None:
            raise WindowsGroupError("通讯录中没有当前登录账号,未操作客户端")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(user_table)")}
        company_column = "external_corp_name" if "external_corp_name" in columns else "''"
        members: dict[str, Member] = {}
        for index, query in enumerate(queries):
            q = query.strip()
            if not q:
                continue
            label, _, department = q.partition("@")
            if label.startswith("userid:"):
                rows = conn.execute(
                    f"SELECT id,name,corp_id,{company_column} FROM user_table WHERE CAST(id AS TEXT)=?",
                    (label[7:],),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT id,name,corp_id,{company_column} FROM user_table WHERE name=?", (label,)
                ).fetchall()
            candidates = []
            for uid, name, corp, company in rows:
                depts = [r[0] for r in conn.execute(
                    "SELECT d.name FROM department_tableV2 d JOIN user_dept_tableV2 u "
                    "ON d.id=u.department_id WHERE u.user_id=?", (uid,)
                )]
                if department and department not in depts:
                    continue
                candidates.append(Member(q, str(name), str(uid), "/".join(depts),
                                         corp != own_row[0], str(company or "")))
            if not candidates:
                raise WindowsGroupError(f"通讯录中找不到精确匹配的联系人: {q}")
            if len(candidates) != 1:
                shown = "; ".join(f"{m.name}@{m.dept or '外部联系人'} userid:{m.userid}" for m in candidates)
                raise WindowsGroupError(f"联系人 {q} 有同名候选: {shown}。请用 姓名@部门 或 userid:ID")
            member = candidates[0]
            if index == 0 and (member.userid == str(own) or not member.external):
                raise WindowsGroupError("members[0] 必须是外部联系人,不能是自己或本企业同事,未操作客户端")
            if member.userid != str(own):
                members.setdefault(member.userid, member)
        if len(members) < 2:
            raise WindowsGroupError("除自己外至少需要 2 个不同成员")
        return list(members.values())
    finally:
        conn.close()


def _groups() -> dict[str, dict]:
    conn = windows_local.store().db("session.db")
    try:
        groups = {
            cid: {"rid": rid, "conversation_id": cid, "name": name or "", "userids": set()}
            for rid, cid, name in conn.execute(
                "SELECT con_numeric_id,id,name FROM conversation_table WHERE id LIKE 'R:%'"
            )
        }
        for cid, uid in conn.execute("SELECT conversation_id,user_id FROM conversation_user_table"):
            if cid in groups:
                groups[cid]["userids"].add(str(uid))
        return groups
    finally:
        conn.close()


def _matching_group(groups: dict, expected: set[str], before: set[str]) -> dict | None:
    matches = [g for cid, g in groups.items() if cid not in before and g["userids"] == expected]
    if len(matches) > 1:
        raise WindowsGroupError("发现多个成员完全相同的新群,无法确定本次结果,请人工核对")
    return matches[0] if matches else None


def _verify_created(expected: set[str], before: set[str], timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        group = _matching_group(_groups(), expected, before)
        if group:
            return group
        if time.monotonic() >= deadline:
            raise WindowsGroupError("已提交,但未查到成员完全一致的新群。结果待核验,禁止自动再次提交")
        time.sleep(0.5)


def _save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _success(group: dict, members: list[Member], reused: bool = False) -> dict:
    return {
        "success": True, "verified": True, "reused": reused,
        "rid": group["rid"], "conversation_id": group["conversation_id"],
        "name": group["name"], "members": [m.as_dict() for m in members],
        "member_userids": sorted(group["userids"]), "member_count": len(group["userids"]),
        "error": None,
    }


def _recovered(group: dict, members: list[Member], name: str) -> dict:
    result = _success(group, members, reused=True)
    if name:
        result.update(name_verified=group["name"] == name, requested_name=name)
        if not result["name_verified"]:
            result["name_error"] = "已恢复之前创建的群,请按返回的 RID 调用改名接口"
    return result


def _verify_name(cid: str, expected: set[str], name: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        group = _groups().get(cid)
        if group is None or group["userids"] != expected:
            raise WindowsGroupError("改名核验时群成员或会话发生变化,请人工检查")
        if group["name"] == name:
            return group
        if time.monotonic() >= deadline:
            raise WindowsGroupError("群名已提交,但本地库尚未确认目标群的新名称,请核验后再操作")
        time.sleep(0.5)


def _name_valid(name: str) -> bool:
    return len(name) <= MAX_NAME_LEN and all(ord(char) >= 32 for char in name)


def _rename_created(picker, group: dict, members: list[Member], name: str) -> dict:
    result = _success(group, members)
    if not name:
        return result
    result["requested_name"] = name
    result["name_verified"] = False
    try:
        ok, error = picker.rename(name, [member.name for member in members])
        if not ok:
            raise WindowsGroupError(error or "改名失败")
        renamed = _verify_name(group["conversation_id"], group["userids"], name)
        result.update(name=renamed["name"], name_verified=True)
    except (LocalSendError, LocalDataError, OSError, ValueError, RuntimeError) as exc:
        result["name_error"] = str(exc)
    return result


def _create_locked(members: list[Member], name: str = "", *, prepare_only: bool = False) -> dict:
    if not members or not members[0].external:
        raise WindowsGroupError("members[0] 必须是外部联系人,未操作客户端")
    from .windows_group_ui import GroupPicker

    store = windows_local.store()
    expected = {m.userid for m in members} | {str(store.self_id())}
    fingerprint = hashlib.sha256(json.dumps(sorted(expected)).encode("utf-8")).hexdigest()
    path = store.cache_dir / "group-requests" / f"{fingerprint}.json"
    if path.exists() and not prepare_only:
        record = json.loads(path.read_text(encoding="utf-8"))
        groups = _groups()
        if record.get("conversation_id"):
            group = groups.get(record["conversation_id"])
            if group and group["userids"] == expected:
                return _recovered(group, members, name)
            raise WindowsGroupError("上次创建的群成员已变化,未重复建群,请人工核对记录")
        group = _matching_group(groups, expected, set(record["before"]))
        if group:
            _save_record(path, {**record, "conversation_id": group["conversation_id"]})
            return _recovered(group, members, name)
        raise WindowsGroupError("上次提交结果仍待核验,未重复建群,请人工检查客户端和 group-requests 记录")

    if name and not prepare_only:
        existing = [group for group in _groups().values()
                    if group["name"] == name and group["userids"] == expected]
        if len(existing) > 1:
            raise WindowsGroupError("已有多个同名且成员相同的群,未重复建群,请人工核对")
        if existing:
            return _recovered(existing[0], members, name)

    picker = GroupPicker()
    submitted = False
    try:
        # The first external contact is included by starting from their direct
        # conversation. Preserve caller order even with several external members.
        picker.open_from_contact(members[0])
        picker.select(members[1:])
        if prepare_only:
            return {"success": True, "prepared": True, "submitted": False,
                    "members": [m.as_dict() for m in members]}
        before = set(_groups())
        record = {"before": sorted(before), "expected": sorted(expected), "submitted_at": time.time()}
        _save_record(path, record)
        submitted = True  # An exception from submit can still mean the click was delivered.
        picker.submit()
        group = _verify_created(expected, before)
        _save_record(path, {**record, "conversation_id": group["conversation_id"]})
        return _rename_created(picker, group, members, name)
    finally:
        if not submitted:
            try:
                picker.cancel()
            except Exception:
                log.exception("关闭本次选人面板失败")
        else:
            picker._restore_clipboard()


def create_group(member_queries: list[str], name: str = "", nickname: str = "",
                 *, prepare_only: bool = False) -> dict:
    if sys.platform != "win32":
        return {"success": False, "error": "Windows 本人身份拉群仅支持 Windows"}
    name = name.strip()
    if not _name_valid(name):
        return {"success": False, "error": "群名过长或含有控制字符,未操作客户端"}
    if nickname.strip():
        return {"success": False, "error": "Windows 建群暂不支持设置群昵称,未操作客户端"}
    with ui_session("Windows 拉群"):
        try:
            members = _resolve_members(member_queries)
            return _create_locked(members, name=name, prepare_only=prepare_only)
        except (LocalSendError, LocalDataError, OSError, ValueError, RuntimeError) as exc:
            return {"success": False, "verified": False, "error": str(exc)}


def rename_group(rid: int, name: str) -> dict:
    """Open a unique group result, verify its title, then verify the exact DB row."""
    if sys.platform != "win32":
        return {"success": False, "error": "Windows 群改名仅支持 Windows"}
    name = name.strip()
    if not name or not _name_valid(name):
        return {"success": False, "error": "群名为空、过长或含有控制字符"}
    with ui_session("Windows 改群名"):
        picker = None
        try:
            groups = _groups()
            matches = [group for group in groups.values() if group["rid"] == rid]
            if len(matches) != 1:
                raise WindowsGroupError(f"未找到唯一的群会话 RID={rid}")
            target = matches[0]
            if target["name"] == name:
                return {"success": True, "verified": True, "rid": rid, "name": name,
                        "old_name": name, "error": None}
            if target["name"] and sum(group["name"] == target["name"] for group in groups.values()) != 1:
                raise WindowsGroupError("有多个同名群,无法通过名称精确打开目标 RID")
            # Visually identical groups cannot be distinguished by search.
            if sum(group["name"] == target["name"] and group["userids"] == target["userids"]
                   for group in groups.values()) != 1:
                raise WindowsGroupError("有多个相同名称和成员的群,未按搜索结果猜测")
            conn = windows_local.store().db("user.db")
            try:
                names = []
                for uid in sorted(target["userids"]):
                    row = conn.execute("SELECT name FROM user_table WHERE id=?", (uid,)).fetchone()
                    if row:
                        names.append(row[0])
            finally:
                conn.close()
            if len(names) != len(target["userids"]) or not names:
                raise WindowsGroupError("群成员姓名不完整,未打开或改名")
            from .windows_group_ui import GroupPicker

            picker = GroupPicker()
            picker.open_group(target["name"], names, len(names))
            ok, error = picker.rename(name, names, old_name=target["name"])
            if not ok:
                raise WindowsGroupError(error or "改名失败")
            verified = _verify_name(target["conversation_id"], target["userids"], name)
            return {"success": True, "verified": True, "rid": rid, "name": verified["name"],
                    "old_name": target["name"], "error": None}
        except (LocalSendError, LocalDataError, OSError, ValueError, RuntimeError) as exc:
            return {"success": False, "verified": False, "rid": rid, "error": str(exc)}
        finally:
            if picker is not None:
                picker._restore_clipboard()
