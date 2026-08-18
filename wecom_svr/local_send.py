"""以「本人登录账号」身份发消息 —— 本地数据库 + AppleScript 操控客户端。

与官方 API 的区别: 官方 API 消息显示为「应用」发送,这里消息以你本人的身份发出。

依赖 wecom-agent 解密出来的 Info.db / Session.db
(先跑 `python3 decrypt/macos/read_wecom.py`)。仅支持 macOS。

核心做法:
  - 优先用 wxwork://jump URL Scheme 按 RID 精准打开会话
  - 无 RID 时退回搜索框匹配,并用 ↓ 键跳过同名用户/机器人
  - 每次发送前从数据库读最新名称,不受群名变更影响
"""
import ctypes
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from itertools import pairwise
from urllib.parse import urlencode

from .localdb import NOT_SYNCED as _NOT_SYNCED

# 错误类型在 localdb 定义,这里转出供 main/cli/tests 沿用。
# `X as X` 是显式 re-export 的标准写法,比手写一份 __all__ 清单更不容易漂。
from .localdb import LocalDataError as LocalDataError
from .localdb import LocalSendError as LocalSendError
from .localdb import connect
from .uilock import ui_session

log = logging.getLogger(__name__)

_quartz_libraries = None
_accessibility_libraries = None
_wecom_ax_application: tuple[int, int] | None = None
_chat_window_bounds: tuple[float, float, float, float] | None = None


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def ensure_macos() -> None:
    if sys.platform != "darwin":
        raise LocalSendError("以本人身份发消息仅支持 macOS(需要 osascript 操控客户端)")


# ─── 数据库 ────────────────────────────────────────────────────────────

def _get_info_conn() -> sqlite3.Connection:
    return connect("Info.db")


def _get_session_conn() -> sqlite3.Connection:
    return connect("Session.db")


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
            d = dict(zip(cols, r, strict=True))
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


def get_conversation_by_rid(rid: int) -> dict | None:
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
        d = dict(zip(cols, r, strict=True))
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
    return [dict(zip(cols, r, strict=True)) for r in rows]


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


def resolve_conversation_name(identifier: str, rid: int | None = None) -> dict | None:
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
    r = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
    # 剪贴板可能当前存的是图片/富文本等非 UTF-8 数据。按文本读取时不能
    # 让解码异常穿透 HTTP 层;调用方会把替换后的内容识别为“不是群名”。
    return r.stdout.decode("utf-8", errors="replace")


def _escape(s: str) -> str:
    """把字符串塞进 AppleScript 双引号字面量。

    换行必须转义成 `\\n`:AppleScript 的字面量不能跨行,群名/消息里带一个回车
    就会拼出语法不合法的脚本。
    """
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


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
    """跑一段 AppleScript,返回 (成功, 输出或错误文案)。

    失败时 stdout 和 stderr 都要看: `-s o` 让 AppleScript 把报错写到 **stdout**,
    只回读 stderr 的话每个报错都变成一句空消息(「粘贴/发送失败: 」后面什么都没有)。
    """
    try:
        r = subprocess.run(
            ["osascript", "-s", "o", "-"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"AppleScript 超时({timeout}s)"
    except FileNotFoundError:
        return False, "osascript 不可用(非 macOS?)"

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return False, err or out or f"osascript 退出码 {r.returncode}"
    return True, out


def _write_image_clipboard(image_path: str) -> tuple[bool, str]:
    """用短生命周期 AppKit 脚本把图片写进剪贴板。

    不要把 AppKit 和 System Events 放在同一个 osascript 进程里:实测前者写完只需
    约 0.6 秒,混合脚本却会在企微接收粘贴后卡住直至 15 秒超时。
    """
    script = r'''
    use framework "AppKit"
    use scripting additions
    on run argv
        set imagePath to item 1 of argv
        set imageObj to current application's NSImage's alloc()'s initWithContentsOfFile:imagePath
        if imageObj is missing value then error "无法读取图片: " & imagePath
        -- writeObjects 会注册由当前 osascript 进程提供的惰性 NSImage。脚本一退出,
        -- 后续独立进程发 Cmd+V 时 provider 已消失,企微拿不到图片。必须写入实体 TIFF
        -- bytes,让 pasteboard server 在脚本退出后仍持有完整数据。
        set imageData to imageObj's TIFFRepresentation()
        if imageData is missing value then error "无法编码图片: " & imagePath
        set pb to current application's NSPasteboard's generalPasteboard()
        pb's clearContents()
        set ok to pb's setData:imageData forType:(current application's NSPasteboardTypeTIFF)
        if (ok as boolean) is false then error "写入图片剪贴板失败"
    end run
    '''
    try:
        result = subprocess.run(
            ["osascript", "-l", "AppleScript", "-s", "o", "-", image_path],
            input=script, capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False, "写入图片剪贴板超时"
    except FileNotFoundError:
        return False, "osascript 不可用(非 macOS?)"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "写入图片剪贴板失败"
    return True, "ok"


def _paste_image_current(path: str, paste_delay: float = 0.8) -> tuple[bool, str]:
    """把图片写入剪贴板并粘贴;各阶段拆开执行,避免组合脚本卡死。"""
    try:
        image_path = _normalize_file_path(path)
    except (OSError, ValueError) as e:
        return False, str(e)

    ok, err = _focus_chat_input()
    if not ok:
        return False, f"聚焦聊天输入框失败: {err}"
    ok, _element, err = _focused_chat_input()
    if not ok:
        return False, err
    ok, err = _write_image_clipboard(image_path)
    if not ok:
        return False, err
    ok, err = _run_applescript(
        'tell application "System Events" to tell process "企业微信" '
        'to keystroke "v" using command down',
        timeout=2,
    )
    if not ok:
        return False, f"粘贴图片失败: {err}"

    # 企微 5.x 的附件预览不会改变 AXValue/AXRole,无法像文本一样低成本回读确认;
    # 等缩略图完成后由调用方提交。这里只等待一次,比旧组合脚本的 1.6 秒更快。
    time.sleep(max(0.2, min(float(paste_delay), 3.0)))
    return True, image_path


def _load_quartz_libraries():
    """加载并配置 Quartz 动态库;同一服务进程内只做一次。"""
    global _quartz_libraries
    if _quartz_libraries is not None:
        return _quartz_libraries

    cg = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    cg.CGEventCreateKeyboardEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool,
    ]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventSetFlags.restype = None
    cg.CGEventKeyboardSetUnicodeString.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16),
    ]
    cg.CGEventKeyboardSetUnicodeString.restype = None
    cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32,
    ]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventPost.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    _quartz_libraries = cg, cf
    return _quartz_libraries


def _post_key(keycode: int, flags: int = 0) -> tuple[bool, str]:
    """通过 Quartz HID 事件投递一个按键。"""
    try:
        cg, cf = _load_quartz_libraries()

        down = cg.CGEventCreateKeyboardEvent(None, keycode, True)
        up = cg.CGEventCreateKeyboardEvent(None, keycode, False)
        if not down or not up:
            if down:
                cf.CFRelease(down)
            if up:
                cf.CFRelease(up)
            return False, "无法创建 Quartz 键盘事件"
        try:
            if flags:
                cg.CGEventSetFlags(down, flags)
                cg.CGEventSetFlags(up, flags)
            cg.CGEventPost(0, down)  # kCGHIDEventTap
            time.sleep(0.03)
            cg.CGEventPost(0, up)
        finally:
            cf.CFRelease(down)
            cf.CFRelease(up)
    except OSError as e:
        return False, f"加载 Quartz 失败: {e}"
    return True, "ok"


def _post_return_key(confirm_send: bool = True) -> tuple[bool, str]:
    """投递 Return;发送消息时以输入框清空确认客户端已接受提交。

    @ 候选窗口也用 Return 选人,但选人后正文当然不会清空,调用方必须传
    ``confirm_send=False``。只有最终提交消息时才执行清空确认。

    ``osascript`` 零退出码只表示 Return 事件成功投递给 System Events,不表示企微
    消费了它。后台宿主权限、客户端发送快捷键设置或 CEF 卡顿都可能让事件静默
    无效;旧实现会因此向任务队列回报假成功。

    这里不在确认失败后再按一次 Return:第一下也可能已经发送,只是 UI 状态读取
    滞后;重按会违反任务队列“宁可漏发,不可重复”的约束。
    """
    ok, _element, err = _focused_chat_input()
    if not ok:
        return False, err
    ok, err = _run_applescript(
        'tell application "System Events" to tell process "企业微信" to key code 36',
        timeout=2,
    )
    if not ok:
        return False, err
    if not confirm_send:
        return True, "ok"
    return _wait_current_input_cleared()


def _post_mouse_click(x: float, y: float) -> tuple[bool, str]:
    """通过 Quartz 点击屏幕坐标,避免 System Events 在企微 CEF 上阻塞。"""
    try:
        cg, cf = _load_quartz_libraries()

        point = _CGPoint(float(x), float(y))
        down = cg.CGEventCreateMouseEvent(None, 1, point, 0)  # kCGEventLeftMouseDown
        up = cg.CGEventCreateMouseEvent(None, 2, point, 0)  # kCGEventLeftMouseUp
        if not down or not up:
            if down:
                cf.CFRelease(down)
            if up:
                cf.CFRelease(up)
            return False, "无法创建 Quartz 鼠标事件"
        try:
            cg.CGEventPost(0, down)
            time.sleep(0.03)
            cg.CGEventPost(0, up)
        finally:
            cf.CFRelease(down)
            cf.CFRelease(up)
    except OSError as e:
        return False, f"加载 Quartz 失败: {e}"
    return True, "ok"


def _refresh_chat_window_bounds() -> tuple[bool, str]:
    """读取并缓存企微聊天窗口位置,供后续发送直接复用。"""
    global _chat_window_bounds
    script = r'''
    tell application "System Events"
        if not (exists process "企业微信") then error "企业微信进程不存在"
        tell process "企业微信"
            if (count of windows) is 0 then error "找不到企业微信窗口"
            set frontmost to true
            set chatWindow to window 1
            try
                set value of attribute "AXMinimized" of chatWindow to false
            end try
            try
                perform action "AXRaise" of chatWindow
            end try
            set wp to position of chatWindow
            set ws to size of chatWindow
            return (item 1 of wp as text) & "," & (item 2 of wp as text) & "," & ¬
                (item 1 of ws as text) & "," & (item 2 of ws as text)
        end tell
    end tell
    '''
    ok, out = _run_applescript(script, timeout=5)
    if not ok:
        return False, out
    try:
        win_x, win_y, width, height = (float(part.strip()) for part in out.split(","))
    except (TypeError, ValueError):
        return False, f"无法解析企业微信窗口位置: {out!r}"
    if width < 400 or height < 300:
        return False, f"企业微信窗口尺寸异常: {width:g}x{height:g}"
    _chat_window_bounds = win_x, win_y, width, height
    return True, "ok"


def _focus_chat_input() -> tuple[bool, str]:
    """使用预热的窗口位置点击聊天输入区;无缓存时再读取一次。"""
    if _chat_window_bounds is None:
        ok, err = _refresh_chat_window_bounds()
        if not ok:
            return False, err
    assert _chat_window_bounds is not None
    win_x, win_y, width, height = _chat_window_bounds

    # 输入区位于主窗口右下方。按窗口比例取点可适配缩放/移动后的窗口,
    # y 距底部 45px 落在多行编辑区中部,避开发送按钮和工具栏。
    ok, err = _post_mouse_click(win_x + width * 0.72, win_y + height - 45)
    if ok:
        time.sleep(0.06)
    return ok, err


def _select_current_input() -> tuple[bool, str]:
    """选中输入框现有内容;后续文本输入会直接替换它。"""
    command_flag = 1 << 20  # kCGEventFlagMaskCommand
    ok, err = _post_key(0, command_flag)  # Cmd+A
    if ok:
        time.sleep(0.03)
    return ok, err


def _clear_current_input() -> tuple[bool, str]:
    """用原生按键清空输入框,避开企微 CEF 上很慢的 AXValue 查询。"""
    ok, err = _select_current_input()
    if not ok:
        return False, err
    ok, err = _post_key(51)  # Delete
    if ok:
        time.sleep(0.08)
    return ok, err


def _load_accessibility_libraries():
    """加载原生 Accessibility/CoreFoundation API;避免慢速 System Events 查询。"""
    global _accessibility_libraries
    if _accessibility_libraries is not None:
        return _accessibility_libraries
    ax = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
    ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
    ax.AXUIElementCopyAttributeValue.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    ax.AXUIElementSetAttributeValue.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    _accessibility_libraries = ax, cf
    return _accessibility_libraries


def _cf_string(value: str) -> int:
    _ax, cf = _load_accessibility_libraries()
    # kCFStringEncodingUTF8
    return int(cf.CFStringCreateWithCString(None, value.encode(), 0x08000100))


def _cf_string_value(ref: int) -> str:
    _ax, cf = _load_accessibility_libraries()
    size = 256
    while size <= 1 << 20:
        buf = ctypes.create_string_buffer(size)
        if cf.CFStringGetCString(ref, buf, size, 0x08000100):
            return buf.value.decode("utf-8")
        size *= 2
    raise ValueError("Accessibility 文本超过 1 MiB")


def _ax_application() -> tuple[bool, int, str]:
    global _wecom_ax_application
    if _wecom_ax_application is not None:
        return True, _wecom_ax_application[1], "ok"
    try:
        result = subprocess.run(
            ["pgrep", "-x", "企业微信"], capture_output=True, text=True, timeout=1,
        )
        pid = int(result.stdout.splitlines()[0]) if result.returncode == 0 else 0
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pid = 0
    if not pid:
        return False, 0, "企业微信进程不存在"
    try:
        ax, _cf = _load_accessibility_libraries()
        app = int(ax.AXUIElementCreateApplication(pid))
    except OSError as e:
        return False, 0, f"加载 Accessibility 失败: {e}"
    if not app:
        return False, 0, "无法连接企业微信辅助功能"
    _wecom_ax_application = pid, app
    return True, app, "ok"


def _ax_attribute(element: int, name: str) -> tuple[bool, int, str]:
    ax, cf = _load_accessibility_libraries()
    attribute = _cf_string(name)
    value = ctypes.c_void_p()
    try:
        code = ax.AXUIElementCopyAttributeValue(element, attribute, ctypes.byref(value))
    finally:
        cf.CFRelease(attribute)
    if code != 0 or not value.value:
        return False, 0, f"读取 {name} 失败(AXError={code})"
    return True, int(value.value), "ok"


def _focused_chat_input() -> tuple[bool, int, str]:
    ok, app, err = _ax_application()
    if not ok:
        return False, 0, err
    ok, element, err = _ax_attribute(app, "AXFocusedUIElement")
    if not ok:
        return False, 0, f"企业微信没有获得输入焦点: {err}"
    ok, role_ref, err = _ax_attribute(element, "AXRole")
    if not ok:
        return False, 0, err
    _ax, cf = _load_accessibility_libraries()
    try:
        role = _cf_string_value(role_ref)
    finally:
        cf.CFRelease(role_ref)
    if role not in {"AXTextArea", "AXTextField"}:
        cf.CFRelease(element)
        return False, 0, f"当前焦点不是聊天输入框: {role}"
    return True, element, "ok"


def _current_input_value() -> tuple[bool, str, str]:
    """读取当前企微聊天输入框;返回 ``(成功, 内容, 错误)``。"""
    ok, element, err = _focused_chat_input()
    if not ok:
        return False, "", err
    _ax, cf = _load_accessibility_libraries()
    try:
        ok, value_ref, err = _ax_attribute(element, "AXValue")
        if not ok:
            return False, "", err
        try:
            return True, _cf_string_value(value_ref), "ok"
        finally:
            cf.CFRelease(value_ref)
    finally:
        cf.CFRelease(element)


def _wait_current_input_cleared(
    attempts: int = 11, interval: float = 0.05,
) -> tuple[bool, str]:
    """等待企微消费提交并清空输入框,避免把“按键已投递”误报成发送成功。

    正常路径第一次原生 AX 读取即成功(约 2ms),不固定 sleep;只有企微尚未消费
    Return 的异常/慢速路径才轮询,且最多等待约 0.5 秒。
    """
    last_value = ""
    last_error = ""
    for attempt in range(max(1, attempts)):
        ok, value, err = _current_input_value()
        if ok:
            if value == "":
                return True, "ok"
            last_value = value
        else:
            last_error = err
        if attempt + 1 < attempts:
            time.sleep(max(0.0, interval))

    if last_value:
        preview = last_value.replace("\n", "\\n")[:80]
        return False, f"Return 已投递但输入框未清空,无法确认发送: {preview!r}"
    return False, f"Return 已投递但无法读取输入框确认发送: {last_error or '未知错误'}"


def _set_current_input(text: str) -> tuple[bool, str]:
    """用原生 AX 写入并回读输入框;实测约 2ms。"""
    ok, element, err = _focused_chat_input()
    if not ok:
        return False, err
    ax, cf = _load_accessibility_libraries()
    attribute = _cf_string("AXValue")
    value = _cf_string(text)
    try:
        code = ax.AXUIElementSetAttributeValue(element, attribute, value)
        if code != 0:
            return False, f"写入企微输入框失败(AXError={code})"
        ok, actual_ref, err = _ax_attribute(element, "AXValue")
        if not ok:
            return False, err
        try:
            actual = _cf_string_value(actual_ref)
        finally:
            cf.CFRelease(actual_ref)
        if actual != text:
            return False, "企微输入框未接受消息正文"
        return True, "ok"
    finally:
        cf.CFRelease(value)
        cf.CFRelease(attribute)
        cf.CFRelease(element)


def _insert_current_input(text: str) -> tuple[bool, str]:
    """在当前光标插入文本,保留输入框里已选择的真实 @ 对象。"""
    if not text:
        return True, "ok"
    ok, element, err = _focused_chat_input()
    if not ok:
        return False, err
    ax, cf = _load_accessibility_libraries()
    attribute = _cf_string("AXSelectedText")
    value = _cf_string(text)
    try:
        code = ax.AXUIElementSetAttributeValue(element, attribute, value)
        if code != 0:
            return False, f"插入企微输入框失败(AXError={code})"
        return True, "ok"
    finally:
        cf.CFRelease(value)
        cf.CFRelease(attribute)
        cf.CFRelease(element)


def _post_unicode_text(text: str) -> tuple[bool, str]:
    """投递一段 Unicode 键盘事件,绕开当前输入法但保留真实输入语义。

    企微的 @ 候选框只会响应真实键盘输入。AXValue 一次性写入 ``@姓名`` 只能得到
    普通文本,而 System Events 的 ``keystroke`` 在中文输入法下可能把英文拆成带空格
    的拼音。CoreGraphics 的 Unicode 事件同时解决这两个问题。
    """
    if not text:
        return True, "ok"
    try:
        cg, cf = _load_quartz_libraries()

        units = text.encode("utf-16-le")
        chars = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
        down = cg.CGEventCreateKeyboardEvent(None, 0, True)
        up = cg.CGEventCreateKeyboardEvent(None, 0, False)
        if not down or not up:
            if down:
                cf.CFRelease(down)
            if up:
                cf.CFRelease(up)
            return False, "无法创建 Unicode 键盘事件"
        try:
            cg.CGEventKeyboardSetUnicodeString(down, len(chars), chars)
            cg.CGEventKeyboardSetUnicodeString(up, len(chars), chars)
            cg.CGEventPost(0, down)  # kCGHIDEventTap
            time.sleep(0.03)
            cg.CGEventPost(0, up)
        finally:
            cf.CFRelease(down)
            cf.CFRelease(up)
    except OSError as e:
        return False, f"加载 Quartz 失败: {e}"
    return True, "ok"


def _mention_candidates() -> list[str]:
    """读取当前 @ 候选窗的第一列姓名。"""
    script = r'''
    tell application "System Events"
        tell process "企业微信"
            repeat with w in windows
                try
                    set ws to size of w
                    if (item 1 of ws) > 600 or (item 2 of ws) > 500 then
                        error "不是 @ 候选窗口"
                    end if
                    set tb to table 1 of scroll area 1 of w
                    set acc to ""
                    repeat with r in rows of tb
                        try
                            set acc to acc & (value of static text 1 of r as text) & linefeed
                        end try
                    end repeat
                    return acc
                end try
            end repeat
            error "找不到 @ 成员候选窗口"
        end tell
    end tell
    '''
    ok, out = _run_applescript(script, timeout=15)
    if not ok:
        raise LocalSendError(f"读取 @ 候选失败: {out}")
    return [line for line in out.splitlines() if line.strip()]


def _dismiss_mention_picker() -> None:
    """关闭残留的 @ 候选窗,用于失败清理。"""
    script = r'''
    tell application "System Events"
        tell process "企业微信"
            repeat with w in windows
                try
                    set tb to table 1 of scroll area 1 of w
                    key code 53
                    return
                end try
            end repeat
        end tell
    end tell
    '''
    _run_applescript(script, timeout=10)


def _type_mention(name: str) -> None:
    """触发 @ 候选,确认唯一匹配后选中成员。"""
    # System Events 可稳定触发企微 CEF 的 @ 候选。Quartz 在服务宿主没有
    # 输入监控权限时会静默丢弃,正是普通文本此前“假成功”的同一根因。
    ok, err = _run_applescript(
        'tell application "System Events" to tell process "企业微信" to keystroke "@"',
        timeout=2,
    )
    if not ok:
        raise LocalSendError(f"输入 @ 失败: {err}")
    time.sleep(0.25)

    ok, err = _insert_current_input(name)
    if not ok:
        raise LocalSendError(f"输入 @成员搜索词失败: {err}")
    time.sleep(0.35)

    candidates = _mention_candidates()
    if candidates != [name]:
        shown = ", ".join(candidates[:8]) or "(无结果)"
        raise LocalSendError(
            f"@「{name}」匹配到 {len(candidates)} 个候选: {shown};"
            "为避免误 @,未发送消息"
        )

    ok, err = _post_return_key(confirm_send=False)
    if not ok:
        raise LocalSendError(f"选择 @成员失败: {err}")
    time.sleep(0.4)


def _infer_mentions(content: str) -> list[str]:
    """从常见的 @中文/英文/数字姓名写法推断提及列表。"""
    return re.findall(r"(?<![\w@])@([A-Za-z0-9_\u3400-\u9fff]+)", content)


def _mention_spans(content: str, mentions: list[str] | None) -> list[tuple[int, int, str]]:
    """校验正文里的 @姓名并按正文位置返回跨度。"""
    wanted = list(mentions) if mentions is not None else _infer_mentions(content)
    seen: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    for name in wanted:
        name = name.strip()
        if not name:
            raise LocalSendError("mentions 中不能有空姓名")
        if name in seen:
            raise LocalSendError(f"mentions 中姓名重复: {name}")
        seen.add(name)
        token = f"@{name}"
        start = content.find(token)
        if start < 0 or content.find(token, start + 1) >= 0:
            raise LocalSendError(f"正文中必须恰好出现一次 {token}")
        spans.append((start, start + len(token), name))
    spans.sort()
    for (_, end, _), (start, _, _) in pairwise(spans):
        if start < end:
            raise LocalSendError("mentions 中的 @姓名发生重叠")
    return spans


def _type_rich_message(content: str, mentions: list[str] | None) -> tuple[bool, str]:
    """逐段输入正文,把声明的 @姓名转换为企微内部提及对象。"""
    started = time.perf_counter()
    spans = _mention_spans(content, mentions)
    focus_started = time.perf_counter()
    ok, err = _focus_chat_input()
    focus_elapsed = time.perf_counter() - focus_started
    if not ok:
        return False, f"聚焦聊天输入框失败: {err}"
    if not spans:
        clear_started = time.perf_counter()
        # 普通文本直接写 AXValue 并回读。CGEventPost 在缺少输入监控权限时会
        # 静默丢弃 Unicode 事件,不能作为“输入成功”的依据。
        ok, err = _set_current_input(content)
        clear_elapsed = time.perf_counter() - clear_started
        input_elapsed = 0.0
        if not ok:
            return False, f"输入消息失败: {err}"
        time.sleep(0.08)
        submit_started = time.perf_counter()
        result = _submit_current_input()
        submit_elapsed = time.perf_counter() - submit_started
        log.info(
            "本地文本发送完成: elapsed=%.3fs focus=%.3fs clear=%.3fs "
            "input=%.3fs submit=%.3fs",
            time.perf_counter() - started,
            focus_elapsed,
            clear_elapsed,
            input_elapsed,
            submit_elapsed,
        )
        return result

    ok, err = _clear_current_input()
    if not ok:
        return False, f"输入消息失败: {err}"

    cursor = 0
    try:
        for start, end, name in spans:
            ok, err = _insert_current_input(content[cursor:start])
            if not ok:
                raise LocalSendError(f"输入消息失败: {err}")
            mention_started = time.perf_counter()
            _type_mention(name)
            log.info(
                "本地消息 @ 候选并选择完成: name=%r elapsed=%.3fs",
                name,
                time.perf_counter() - mention_started,
            )
            cursor = end
        ok, err = _insert_current_input(content[cursor:])
        if not ok:
            raise LocalSendError(f"输入消息失败: {err}")
    except LocalSendError as e:
        _dismiss_mention_picker()
        _clear_current_input()
        return False, str(e)

    time.sleep(0.1)
    result = _submit_current_input()
    log.info(
        "本地 @ 消息发送完成: mentions=%d elapsed=%.3fs",
        len(spans),
        time.perf_counter() - started,
    )
    return result


def _submit_current_input(settle_delay: float = 0.0) -> tuple[bool, str]:
    """提交已组装好的输入内容,不读取或粘贴剪贴板。"""
    ok, err = _post_return_key()
    if not ok:
        return False, f"发送失败: {err}"
    delay = max(0.0, min(float(settle_delay), 3.0))
    if delay:
        time.sleep(delay)
    return True, "ok"


def _paste_and_send_current(
    paste_delay: float = 0.25,
    text: str | None = None,
) -> tuple[bool, str]:
    """在当前打开的会话输入框粘贴剪贴板内容并回车发送。

    osascript 零退出码只说明按键事件发出去了,不代表企业微信真的收到了。服务在
    后台运行时前台应用可能是终端/浏览器,旧实现会把 Cmd+V 和回车发给它们,然后
    仍返回 success=true。这里通过 AXFocusedUIElement 确认企业微信在前台,焦点
    是聊天输入框。AXValue 写入后不再读取控件值,因为企业微信的 CEF 输入框
    可能在内容变化后阻塞辅助功能查询,让脚本永远走不到 Return。

    企业微信新版的 CEF 输入区会忽略 System Events 发出的 Cmd+V,但接受 AXValue
    写入。因此文本消息优先走 AX 直接写入; text=None 时保留剪贴板粘贴路径供图片使用。
    """
    delay = max(0.1, min(float(paste_delay), 3.0))
    if text is not None:
        escaped_text = _escape(text)
        input_action = f'''
            set value of inputBefore to "{escaped_text}"
            delay {delay:.2f}
        '''
    else:
        # 图片粘贴后 AXValue 不是文本, 读取/转换它可能让 AppleScript 卡住。
        # 所以图片路径只保留发送前的焦点确认, 之后纯靠等: 一份等粘贴生效,
        # 一份等缩略图渲染完, 渲染没完就回车会把图发丢。
        input_action = f'''
            keystroke "v" using command down
            delay {delay * 2:.2f}
        '''

    script_send = f'''
    tell application "System Events"
        if not (exists process "企业微信") then error "企业微信进程不存在"
        tell process "企业微信"
            if (count of windows) is 0 then error "找不到企业微信窗口"
            set frontmost to true
            delay 0.3

            set inputBefore to value of attribute "AXFocusedUIElement"
            if inputBefore is missing value then error "企业微信没有获得输入焦点"
            set roleBefore to role of inputBefore
            if roleBefore is not "AXTextArea" and roleBefore is not "AXTextField" then
                error "当前焦点不是聊天输入框: " & roleBefore
            end if
            {input_action}
        end tell
    end tell
    '''
    ok, err = _run_applescript(script_send)
    if not ok:
        return False, f"粘贴/发送失败: {err}"

    ok, err = _submit_current_input()
    if not ok:
        return False, f"粘贴/发送失败: {err.removeprefix('发送失败: ')}"
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


def _open_conversation_by_scheme(conv: dict, wait: float = 0.45) -> tuple[bool, str]:
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

    delay = max(0.0, min(float(wait), 3.0))
    if delay:
        time.sleep(delay)
    return True, url


def trigger_conversation_load(rid: int, msgid: int = 0) -> tuple[bool, str]:
    """后台切到目标会话,让最新消息气泡进入渲染窗口并触发图片下载。

    ``open -g`` 不抢走当前应用焦点,但企微内部仍会选中目标会话。实测 URL Scheme
    会忽略 msgid,所以它只能可靠覆盖会话末尾的实时图片;更深的历史图片由调用方
    继续保持 pending/超时降级。
    """
    ensure_macos()
    try:
        rid = int(rid)
        msgid = int(msgid or 0)
    except (TypeError, ValueError):
        return False, f"非法 RID/msgid: {rid!r}/{msgid!r}"
    conv = get_conversation_by_rid(rid)
    if not conv:
        return False, f"未找到 RID={rid} 的对话"
    try:
        url = _conversation_jump_url(conv, msgid=msgid)
        with ui_session("触发会话图片加载"):
            result = subprocess.run(
                ["open", "-g", url], capture_output=True, text=True, timeout=5,
            )
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return False, str(e)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "打开会话失败"
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

    with ui_session("打开会话"):
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


def _send_by_scheme(
    conv: dict, content: str, mentions: list[str] | None = None
) -> tuple[bool, str]:
    """按数据库会话 ID 打开目标会话后发送消息。

    文本走 Quartz 原生键盘事件,全程不碰剪贴板和 CEF 的慢速 AXValue 查询。
    """
    started = time.perf_counter()
    ok, msg = _open_conversation_by_scheme(conv)
    log.info("本地消息打开会话完成: elapsed=%.3fs", time.perf_counter() - started)
    if not ok:
        return False, f"打开对话失败: {msg}"

    return _type_rich_message(content, mentions)


def _send_image_by_scheme(conv: dict, image_path: str) -> tuple[bool, str]:
    """按数据库会话 ID 打开目标会话后发送图片。"""
    ok, msg = _open_conversation_by_scheme(conv)
    if not ok:
        return False, f"打开对话失败: {msg}"

    ok, msg = _paste_image_current(image_path, paste_delay=0.8)
    if not ok:
        return False, msg
    # 预览生成后企微会把 AX 焦点移到附件表格;重新点回输入区再提交。
    ok, msg = _focus_chat_input()
    if not ok:
        return False, f"图片已粘贴但无法聚焦输入框发送: {msg}"
    return _post_return_key()


def _send_one(
    contact_name: str,
    content: str,
    skip_results: int = 0,
    mentions: list[str] | None = None,
) -> tuple[bool, str]:
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

        # Step 3: AX 写入消息内容 → 发送(不经剪贴板)
        ok, err = _type_rich_message(content, mentions)
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

def send_message(
    to: str = "",
    content: str = "",
    conv_rid: int | None = None,
    mentions: list[str] | None = None,
) -> dict:
    """以登录用户身份发送消息。

    Args:
        to: 接收人/群名称(模糊搜索),与 conv_rid 二选一
        conv_rid: 会话 RID(服务端 ID),精准定位
        content: 消息正文
        mentions: 要转换为真实提及的姓名;不传时自动识别常见的 @姓名

    Returns:
        {"success": bool, "conv_name": str, "conv_rid": int, "error": ...}
    """
    started = time.perf_counter()
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
    with ui_session("发消息"):
        if conv_rid:
            if mentions is None:
                ok, msg = _send_by_scheme(conv, content)
            else:
                ok, msg = _send_by_scheme(conv, content, mentions)
        else:
            if mentions is None:
                ok, msg = _send_one(name, content, skip_results=skip)
            else:
                ok, msg = _send_one(
                    name, content, skip_results=skip, mentions=mentions
                )

    return {
        "success": ok,
        "conv_name": name,
        "conv_rid": conv["rid"],
        "conv_lid": conv["lid"],
        "conv_type": conv["type"],
        "conv_fw_id": conv.get("fw_id", 0),
        "open_method": open_method,
        "skipped_users": skip,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "error": msg if not ok else None,
    }


def send_image(image_path: str = "", conv_rid: int | None = None) -> dict:
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

    with ui_session("发图片"):
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
