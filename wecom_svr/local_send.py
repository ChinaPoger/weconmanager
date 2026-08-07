"""以「本人登录账号」身份发消息 —— 本地数据库 + AppleScript 操控客户端。

与官方 API 的区别: 官方 API 消息显示为「应用」发送,这里消息以你本人的身份发出。

依赖 wecom-agent 解密出来的 Info.db / Session.db
(先跑 `python3 decrypt/macos/read_wecom.py`)。仅支持 macOS。

核心做法:
  - 优先用 wxwork://jump URL Scheme 按 RID 精准打开会话
  - 无 RID 时退回搜索框匹配,并用 ↓ 键跳过同名用户/机器人
  - 每次发送前从数据库读最新名称,不受群名变更影响
"""
import os
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlencode

from .paths import find_db
from .settings import get_agent_home

_UI_LOCK = threading.Lock()

_NOT_SYNCED = (
    "解密 OK,但核心表缺失 → 数据未同步(可能是新登录)。"
    "先打开企业微信浏览消息,等同步后重试。"
)


class LocalSendError(RuntimeError):
    """本地发送相关的可预期错误(向客户端回报文案,不是 500)。"""


class LocalDataError(LocalSendError):
    """本地数据库缺失或未同步。"""


def ensure_macos() -> None:
    if sys.platform != "darwin":
        raise LocalSendError("以本人身份发消息仅支持 macOS(需要 osascript 操控客户端)")


# ─── 数据库 ────────────────────────────────────────────────────────────

def _connect(name: str) -> sqlite3.Connection:
    decrypt_dir = get_agent_home().decrypt_dir
    db = find_db(decrypt_dir, name)
    if not db:
        raise LocalDataError(
            f"未找到解密后的 {name}(查找目录 {decrypt_dir})。"
            "请先运行 wecom-agent 的 decrypt/macos/read_wecom.py"
        )
    return sqlite3.connect(db)


def _get_info_conn() -> sqlite3.Connection:
    return _connect("Info.db")


def _get_session_conn() -> sqlite3.Connection:
    return _connect("Session.db")


def list_conversations(search: str = "") -> list[dict]:
    """列出会话,支持模糊搜索名称。"""
    conn = _get_info_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(CONVERSATION)")]
        if search:
            rows = conn.execute(
                "SELECT * FROM CONVERSATION WHERE name LIKE ? ORDER BY sort_time DESC LIMIT 100",
                (f"%{search}%",),
            )
        else:
            rows = conn.execute("SELECT * FROM CONVERSATION ORDER BY sort_time DESC LIMIT 100")
        results = []
        for r in rows:
            d = dict(zip(cols, r))
            results.append({
                "name": d.get("name", ""),
                "rid": d.get("RID"),
                "lid": d.get("LID"),
                "type": d.get("conversationtype"),
                "fw_id": d.get("fw_id", 0),
            })
        return results
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise LocalDataError(_NOT_SYNCED) from e
        raise
    finally:
        conn.close()


def get_conversation_by_rid(rid: int) -> Optional[dict]:
    """按 RID 精确查找对话。"""
    conn = _get_info_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(CONVERSATION)")]
        r = conn.execute("SELECT * FROM CONVERSATION WHERE RID = ?", (rid,)).fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise LocalDataError(_NOT_SYNCED) from e
        raise
    finally:
        conn.close()

    if r:
        d = dict(zip(cols, r))
        return {
            "name": d.get("name", ""), "rid": d.get("RID"),
            "lid": d.get("LID"), "type": d.get("conversationtype"),
            "fw_id": d.get("fw_id", 0),
        }
    return None


def get_conversation_by_name(name: str) -> list[dict]:
    """按名称精确查找对话。"""
    conn = _get_info_conn()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(CONVERSATION)")]
        rows = conn.execute("SELECT * FROM CONVERSATION WHERE name = ?", (name,)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise LocalDataError(_NOT_SYNCED) from e
        raise
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


def _count_name_collisions(name: str) -> int:
    """统计 USER 表中有多少同名用户/机器人(不区分大小写)。"""
    try:
        sess = _get_session_conn()
        try:
            return sess.execute(
                "SELECT COUNT(*) FROM USER WHERE lower(name) = lower(?)", (name,)
            ).fetchone()[0]
        finally:
            sess.close()
    except Exception:
        return 0


def resolve_conversation_name(identifier: str, rid: Optional[int] = None) -> Optional[dict]:
    """解析对话 → {"name", "rid", "lid", "type", "fw_id", "skip"}。

    skip 表示搜索结果中需跳过的同名用户数(防止选到机器人)。
    """
    if rid:
        conv = get_conversation_by_rid(rid)
        if conv and conv["name"]:
            conv["skip"] = _count_name_collisions(conv["name"])
            return conv

    matches = get_conversation_by_name(identifier) if identifier else []
    if matches:
        if len(matches) > 1:
            groups = [m for m in matches if m.get("conversationtype") == 1]
            m = groups[0] if groups else matches[0]
        else:
            m = matches[0]
        return {
            "name": m.get("name", ""), "rid": m.get("RID"),
            "lid": m.get("LID"), "type": m.get("conversationtype"),
            "fw_id": m.get("fw_id", 0),
            "skip": _count_name_collisions(identifier),
        }

    convs = list_conversations(identifier) if identifier else []
    if convs:
        c = convs[0]
        c["skip"] = _count_name_collisions(c["name"])
        return c
    return None


# ─── 剪贴板 ────────────────────────────────────────────────────────────

def _set_clipboard(text: str):
    subprocess.run(["pbcopy"], input=text, text=True, timeout=5)


def _get_clipboard() -> str:
    r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    return r.stdout


def _normalize_file_path(path: str) -> str:
    """解析并校验本地文件路径。"""
    if not path or not str(path).strip():
        raise ValueError("图片路径为空")
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.exists(p):
        raise FileNotFoundError(f"图片不存在: {p}")
    if not os.path.isfile(p):
        raise ValueError(f"不是普通文件: {p}")
    return p


# ─── AppleScript ────────────────────────────────────────────────────────

def _run_applescript(script: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["osascript", "-s", "o", "-"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript 超时"
    except FileNotFoundError:
        return False, "osascript 不可用(非 macOS?)"


def _set_clipboard_image(path: str) -> tuple[bool, str]:
    """把图片内容写入系统剪贴板,而不是写入文本路径。"""
    try:
        image_path = _normalize_file_path(path)
    except (OSError, ValueError) as e:
        return False, str(e)

    script = '''
    use framework "AppKit"
    use scripting additions

    on run argv
        set imagePath to item 1 of argv
        set imageObj to current application's NSImage's alloc()'s initWithContentsOfFile:imagePath
        if imageObj is missing value then error "无法读取图片: " & imagePath
        set pb to current application's NSPasteboard's generalPasteboard()
        pb's clearContents()
        set ok to pb's writeObjects:{imageObj}
        if (ok as boolean) is false then error "写入图片剪贴板失败"
    end run
    '''
    try:
        r = subprocess.run(
            ["osascript", "-l", "AppleScript", "-s", "o", "-", image_path],
            input=script, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, "写入图片剪贴板超时"
    except FileNotFoundError:
        return False, "osascript 不可用(非 macOS?)"

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or "写入图片剪贴板失败"
    return True, image_path


def _paste_and_send_current(paste_delay: float = 0.25) -> tuple[bool, str]:
    """在当前打开的会话输入框粘贴剪贴板内容并回车发送。"""
    delay = max(0.1, min(float(paste_delay), 3.0))
    script_send = f'''
    tell application "System Events"
        tell process "企业微信"
            delay 0.2
            keystroke "v" using command down
            delay {delay:.2f}
            keystroke return
            delay 0.2
        end tell
    end tell
    '''
    ok, err = _run_applescript(script_send)
    if not ok:
        return False, f"粘贴/发送失败: {err}"
    return True, "ok"


def _conversation_jump_url(conv: dict, msgid: int = 0) -> str:
    """构造企业微信原生 URL Scheme 会话跳转入口。"""
    rid = conv.get("rid") if conv.get("rid") is not None else conv.get("RID")
    conv_type = conv.get("type") if conv.get("type") is not None else conv.get("conversationtype")
    fw_id = conv.get("fw_id") if conv.get("fw_id") is not None else conv.get("fwid", 0)
    if rid is None:
        raise ValueError("会话缺少 RID")
    if conv_type is None:
        raise ValueError("会话缺少 conversation type")
    query = urlencode({
        "target": "conv",
        "type": int(conv_type),
        "roomid": int(rid),
        "msgid": int(msgid or 0),
        "fwid": int(fw_id or 0),
    })
    return f"wxwork://jump?{query}"


def _open_conversation_by_scheme(conv: dict, wait: float = 1.2) -> tuple[bool, str]:
    """使用客户端 URL Scheme 按 RID/type/fw_id 精准打开会话。"""
    try:
        url = _conversation_jump_url(conv)
    except (TypeError, ValueError) as e:
        return False, str(e)

    try:
        r = subprocess.run(["open", url], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return False, "打开会话超时"
    except FileNotFoundError:
        return False, "open 命令不可用(非 macOS?)"

    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        return False, err or f"打开会话失败: {url}"

    time.sleep(wait)
    return True, url


def open_conversation_by_rid(rid: int) -> dict:
    """按 RID 打开企业微信会话,不发送消息。"""
    ensure_macos()
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return {"success": False, "error": f"非法 RID: {rid!r}"}

    conv = get_conversation_by_rid(rid)
    if not conv:
        return {"success": False, "error": f"未找到 RID={rid} 的对话"}

    with _UI_LOCK:
        ok, msg = _open_conversation_by_scheme(conv)
    return {
        "success": ok,
        "conv_name": conv["name"],
        "conv_rid": conv["rid"],
        "conv_lid": conv["lid"],
        "conv_type": conv["type"],
        "conv_fw_id": conv.get("fw_id", 0),
        "open_method": "url_scheme",
        "error": None if ok else msg,
    }


def _send_by_scheme(conv: dict, content: str) -> tuple[bool, str]:
    """按数据库会话 ID 打开目标会话后发送消息。"""
    old_clipboard = ""
    try:
        old_clipboard = _get_clipboard()
    except Exception:
        pass

    try:
        ok, msg = _open_conversation_by_scheme(conv)
        if not ok:
            return False, f"打开对话失败: {msg}"

        _set_clipboard(content)
        return _paste_and_send_current()

    finally:
        if old_clipboard:
            try:
                _set_clipboard(old_clipboard)
            except Exception:
                pass


def _send_image_by_scheme(conv: dict, image_path: str) -> tuple[bool, str]:
    """按数据库会话 ID 打开目标会话后发送图片。"""
    old_clipboard = ""
    try:
        old_clipboard = _get_clipboard()
    except Exception:
        pass

    try:
        ok, msg = _open_conversation_by_scheme(conv)
        if not ok:
            return False, f"打开对话失败: {msg}"

        ok, msg = _set_clipboard_image(image_path)
        if not ok:
            return False, msg

        return _paste_and_send_current(paste_delay=0.8)

    finally:
        if old_clipboard:
            try:
                _set_clipboard(old_clipboard)
            except Exception:
                pass


def _send_one(contact_name: str, content: str, skip_results: int = 0) -> tuple[bool, str]:
    """向一个联系人/群发送消息(搜索框路径)。

    skip_results: 搜索后按 ↓ 的次数,用于跳过同名的用户/机器人。
    """
    old_clipboard = ""
    try:
        old_clipboard = _get_clipboard()
    except Exception:
        pass

    try:
        # Step 1: 复制联系人名称到剪贴板
        _set_clipboard(contact_name)

        # Step 2: 激活 → Esc → Cmd+F → 粘贴 → ↓跳过 → Enter 打开
        down_keys = ""
        for _ in range(skip_results):
            down_keys += "key code 125\n                delay 0.12\n                "

        script_search = f'''
        tell application "企业微信" to activate
        delay 0.3
        tell application "System Events"
            tell process "企业微信"
                set frontmost to true
                delay 0.2
                key code 53
                delay 0.15
                key code 53
                delay 0.15
                keystroke "f" using command down
                delay 0.4
                keystroke "a" using command down
                delay 0.1
                keystroke "v" using command down
                delay 0.6
                {down_keys}
                keystroke return
                delay 0.7
            end tell
        end tell
        '''
        ok, err = _run_applescript(script_search)
        if not ok:
            return False, f"搜索/打开对话失败: {err}"

        # Step 3: 复制消息内容 → 粘贴 → 发送
        _set_clipboard(content)
        ok, err = _paste_and_send_current()
        if not ok:
            return False, err

        return True, "ok"

    finally:
        if old_clipboard:
            try:
                _set_clipboard(old_clipboard)
            except Exception:
                pass


# ─── 公开 API ──────────────────────────────────────────────────────────

def send_message(to: str = "", content: str = "", conv_rid: Optional[int] = None) -> dict:
    """以登录用户身份发送消息。

    Args:
        to: 接收人/群名称(模糊搜索),与 conv_rid 二选一
        conv_rid: 会话 RID(服务端 ID),精准定位
        content: 消息正文

    Returns:
        {"success": bool, "conv_name": str, "conv_rid": int, "error": ...}
    """
    ensure_macos()
    if not content.strip():
        return {"success": False, "error": "消息内容为空"}

    identifier = to.strip()
    if conv_rid is not None:
        try:
            conv_rid = int(conv_rid)
        except (TypeError, ValueError):
            return {"success": False, "error": f"非法 RID: {conv_rid!r}"}

    if not identifier and not conv_rid:
        return {"success": False, "error": "请指定 to 或 conv_rid"}

    conv = resolve_conversation_name(identifier, conv_rid)
    if not conv or not conv.get("name"):
        if identifier:
            matches = list_conversations(identifier)
            return {
                "success": False, "error": f"未找到: {identifier!r}",
                "suggestions": [m["name"] for m in matches[:10]],
            }
        return {"success": False, "error": f"未找到 RID={conv_rid} 的对话"}

    name = conv["name"]
    skip = conv.get("skip", 0)
    open_method = "url_scheme" if conv_rid else "search"
    with _UI_LOCK:
        if conv_rid:
            ok, msg = _send_by_scheme(conv, content)
        else:
            ok, msg = _send_one(name, content, skip_results=skip)

    return {
        "success": ok,
        "conv_name": name,
        "conv_rid": conv["rid"],
        "conv_lid": conv["lid"],
        "conv_type": conv["type"],
        "conv_fw_id": conv.get("fw_id", 0),
        "open_method": open_method,
        "skipped_users": skip,
        "error": msg if not ok else None,
    }


def send_image(image_path: str = "", conv_rid: Optional[int] = None) -> dict:
    """以登录用户身份按 RID 发送本地图片。"""
    ensure_macos()
    if conv_rid is None:
        return {"success": False, "error": "发送图片必须指定 conv_rid"}
    try:
        conv_rid = int(conv_rid)
    except (TypeError, ValueError):
        return {"success": False, "error": f"非法 RID: {conv_rid!r}"}

    try:
        image_path = _normalize_file_path(image_path)
    except (OSError, ValueError) as e:
        return {"success": False, "error": str(e)}

    conv = resolve_conversation_name("", conv_rid)
    if not conv or not conv.get("name"):
        return {"success": False, "error": f"未找到 RID={conv_rid} 的对话"}

    with _UI_LOCK:
        ok, msg = _send_image_by_scheme(conv, image_path)

    return {
        "success": ok,
        "conv_name": conv["name"],
        "conv_rid": conv["rid"],
        "conv_lid": conv["lid"],
        "conv_type": conv["type"],
        "conv_fw_id": conv.get("fw_id", 0),
        "image_path": image_path,
        "open_method": "url_scheme",
        "error": msg if not ok else None,
    }


def search_conversations(keyword: str = "") -> list[dict]:
    """搜索会话列表(从本地数据库)。"""
    return list_conversations(keyword)
