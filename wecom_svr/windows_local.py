"""Windows 本地企业微信适配层。

Windows 版企业微信的本地库和 macOS 版使用同一套 wxSQLite3 加密核心，
但库名、表结构和桌面自动化接口不同。本模块只在 Windows 的本人身份路径
上加载，避免影响原有 macOS 实现。

安全边界：这里使用当前登录的企业微信桌面客户端和它自己的会话，不构造
企业微信网络协议，也不保存账号密码。key 只落在 gitignored 的 .data 目录。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import wal
from .localdb import LocalDataError, LocalSendError
from .paths import AgentHome, resolve_agent_home
from .settings import get_settings

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^KEY=([0-9a-fA-F]{32})$", re.MULTILINE)
_CONV_ID_RE = re.compile(r"^[A-Z]:([0-9]+)$")
_TEXT_TYPES = {0, 2}
_IMAGE_TYPES = {4, 14, 29}
_CLICK_DELAY_RANGE = (0.5, 1.0)
_CHAR_DELAY_RANGE = (0.5, 1.0)


def ensure_windows() -> None:
    if sys.platform != "win32":
        raise LocalSendError("Windows 本人身份适配仅支持 Windows")


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _vendor_modules(home: AgentHome):
    """按需导入 wecom-agent 的跨平台解密/正文解析模块。"""
    decrypt_dir = home.decrypt_dir
    for path in (decrypt_dir / "windows", decrypt_dir):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    try:
        import export_wxwork  # type: ignore[import-not-found]
        import wxwork_crypto  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LocalDataError(f"加载 Windows 企业微信解密模块失败: {exc}") from exc
    return export_wxwork, wxwork_crypto


def _profile_candidates() -> list[Path]:
    configured = os.environ.get("WECOM_WINDOWS_PROFILE", "").strip()
    if configured:
        root = Path(configured).expanduser()
        if (root / "Data" / "message.db").is_file():
            return [root.resolve()]
        raise LocalDataError(f"WECOM_WINDOWS_PROFILE 不包含 Data/message.db: {root}")

    root = Path.home() / "Documents" / "WXWork"
    candidates: list[Path] = []
    if root.is_dir():
        for path in root.iterdir():
            db = path / "Data" / "message.db"
            if path.is_dir() and db.is_file():
                candidates.append(path)
    candidates.sort(
        key=lambda path: (path / "Data" / "message.db").stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise LocalDataError(
            f"未找到 Windows 企业微信数据库: {root}/*/Data/message.db。"
            "请确认企业微信已安装、登录并运行。"
        )
    return candidates


def _read_page(path: Path, size: int = 4096) -> bytes:
    with path.open("rb") as stream:
        return stream.read(size)


def _source_db(profile: Path, name: str) -> Path:
    path = profile / "Data" / name
    if not path.is_file():
        raise LocalDataError(f"Windows 企业微信数据库不存在: {path}")
    return path


@dataclass(frozen=True)
class WindowsConversation:
    numeric_id: int
    conversation_id: str
    name: str
    conversation_type: int
    last_message_time: int

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "rid": self.numeric_id,
            "lid": 0,
            "type": self.conversation_type,
            "fw_id": 0,
            "conversation_id": self.conversation_id,
        }


class WindowsStore:
    """当前 Windows 企业微信账号的 key、解密库和查询兼容层。"""

    def __init__(self):
        ensure_windows()
        self.home = resolve_agent_home()
        self.profile = _profile_candidates()[0]
        self.cache_dir = get_settings().data_dir / "windows" / self.profile.name
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = self.cache_dir / "wxwork_key.json"
        self._key: bytes | None = None
        self._db_cache: dict[str, Path] = {}
        self._vendor = None

    @property
    def vendor(self):
        if self._vendor is None:
            self._vendor = _vendor_modules(self.home)
        return self._vendor

    def _cached_key(self, page: bytes) -> bytes | None:
        try:
            value = json.loads(self.key_path.read_text(encoding="utf-8"))
            key = bytes.fromhex(str(value.get("key", "")))
            if len(key) == 16 and self.vendor[1].verify_key(key, page):
                return key
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _discover_key(self) -> bytes:
        script = self.home.decrypt_dir / "windows" / "find_key.ps1"
        if not script.is_file():
            raise LocalDataError(f"缺少 Windows key 探测脚本: {script}")
        try:
            env = os.environ.copy()
            env["WECOM_WINDOWS_PROFILE"] = str(self.profile)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=str(script.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=get_settings().startup_key_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LocalDataError(f"Windows 企业微信 key 探测失败: {exc}") from exc
        matches = _KEY_RE.findall((result.stdout or "") + "\n" + (result.stderr or ""))
        if result.returncode != 0 or not matches:
            raise LocalDataError(
                "未能从 WXWork.exe 找到数据库 key。"
                "请确认企业微信已登录，并用与企业微信相同权限运行服务。"
            )
        key = bytes.fromhex(matches[0])
        self.key_path.write_text(
            json.dumps({"profile": str(self.profile), "key": key.hex()}),
            encoding="utf-8",
        )
        return key

    def key(self) -> bytes:
        if self._key is not None:
            return self._key
        _export, crypto = self.vendor
        page = _read_page(_source_db(self.profile, "message.db"))
        self._key = self._cached_key(page) or self._discover_key()
        if not crypto.verify_key(self._key, page):
            self._key = self._discover_key()
        if not crypto.verify_key(self._key, page):
            raise LocalDataError("找到的 key 无法解开当前 Windows message.db")
        return self._key

    def db(self, name: str):
        import sqlite3

        source = _source_db(self.profile, name)
        target = self.cache_dir / name
        base = self.cache_dir / f"{Path(name).stem}.base.db"
        stamp = self.cache_dir / f".{name}.stamp"
        wal_path = Path(f"{source}-wal")
        source_stat = source.stat()
        source_sig = f"{source_stat.st_size}:{source_stat.st_mtime_ns}"
        try:
            wal_stat = wal_path.stat()
            wal_sig = f"{wal_stat.st_size}:{wal_stat.st_mtime_ns}"
        except OSError:
            wal_sig = "0:0"
        current_sig = {"source": source_sig, "wal": wal_sig}
        try:
            previous_sig = json.loads(stamp.read_text(encoding="ascii"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            previous_sig = {}
        if not target.is_file() or previous_sig != current_sig:
            _export, crypto = self.vendor
            if not base.is_file() or previous_sig.get("source") != source_sig:
                fd, temp_name = tempfile.mkstemp(
                    dir=self.cache_dir, prefix=f".{name}-", suffix=".base.db"
                )
                os.close(fd)
                temp = Path(temp_name)
                try:
                    crypto.decrypt_database(str(source), str(temp), self.key())
                    os.replace(temp, base)
                finally:
                    temp.unlink(missing_ok=True)
            frames = wal.read(str(wal_path))
            try:
                wal.overlay(
                    str(base), str(target), frames,
                    lambda page, pgno: crypto.decrypt_page(self.key(), page, pgno),
                )
            except (OSError, ValueError) as exc:
                log.warning("Windows WAL 回放失败，退回已 checkpoint 的数据库: %s", exc)
                wal.overlay(str(base), str(target), wal.Wal(), lambda page, pgno: page)
            stamp.write_text(json.dumps(current_sig), encoding="ascii")
        conn = sqlite3.connect(target)
        conn.text_factory = lambda value: _text(value)
        return conn

    def _direct_name(self, conversation_id: str, fallback: str = "") -> str:
        """Resolve a one-to-one session's display name from user.db."""
        if fallback.strip() or not conversation_id.startswith("S:"):
            return fallback.strip()
        own_id = self.self_id()
        user_ids = []
        for part in conversation_id[2:].split("_"):
            try:
                value = int(part)
            except ValueError:
                continue
            if value != own_id:
                user_ids.append(value)
        if not user_ids:
            return conversation_id
        conn = self.db("user.db")
        try:
            placeholders = ",".join("?" for _ in user_ids)
            row = conn.execute(
                f"SELECT name FROM user_table WHERE id IN ({placeholders}) LIMIT 1",
                tuple(user_ids),
            ).fetchone()
        finally:
            conn.close()
        return _text(row[0]).strip() if row and _text(row[0]).strip() else conversation_id

    def conversations(self, keyword: str = "") -> list[dict]:
        conn = self.db("session.db")
        try:
            sql = (
                "SELECT con_numeric_id,id,name,last_message_time "
                "FROM conversation_table "
            )
            sql += "ORDER BY last_message_time DESC, con_numeric_id DESC"
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
        result = []
        for numeric_id, conversation_id, name, last_time in rows:
            conversation_id = _text(conversation_id)
            name = self._direct_name(conversation_id, _text(name)) or conversation_id
            item = WindowsConversation(
                int(numeric_id), conversation_id, name,
                1 if conversation_id.startswith("R:") else 0,
                int(last_time or 0),
            ).as_dict()
            if not keyword.strip() or keyword.strip().casefold() in name.casefold():
                result.append(item)
        return result

    def conversation_by_rid(self, rid: int) -> dict | None:
        conn = self.db("session.db")
        try:
            row = conn.execute(
                "SELECT con_numeric_id,id,name,last_message_time FROM conversation_table "
                "WHERE con_numeric_id = ?",
                (int(rid),),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        numeric_id, conversation_id, name, last_time = row
        conversation_id = _text(conversation_id)
        return WindowsConversation(
            int(numeric_id), conversation_id,
            self._direct_name(conversation_id, _text(name)) or conversation_id,
            1 if conversation_id.startswith("R:") else 0, int(last_time or 0),
        ).as_dict()

    def conversation_by_name(
        self, name: str, target_type: str | None = None,
    ) -> dict | None:
        matches = self.conversations(name)
        if target_type in {"user", "group"}:
            expected_type = 0 if target_type == "user" else 1
            matches = [item for item in matches if item["type"] == expected_type]
        exact = [item for item in matches if item["name"].casefold() == name.casefold()]
        if target_type in {"user", "group"}:
            return (exact or [None])[0]
        return (exact or matches or [None])[0]

    def self_id(self) -> int | None:
        conn = self.db("session.db")
        try:
            counts = Counter()
            for (conversation_id,) in conn.execute(
                "SELECT id FROM conversation_table WHERE id LIKE 'S:%'"
            ):
                parts = _text(conversation_id)[2:].split("_")
                for part in parts:
                    try:
                        counts[int(part)] += 1
                    except ValueError:
                        pass
        finally:
            conn.close()
        return counts.most_common(1)[0][0] if counts else None

    def latest_own_message(self, conversation_id: str, lid: int | None = None) -> dict | None:
        own_id = self.self_id()
        if own_id is None:
            raise LocalDataError("无法从 Windows 企业微信会话库识别当前登录账号")
        conn = self.db("message.db")
        try:
            sql = (
                "SELECT message_id,content_type,send_time,content FROM message_table "
                "WHERE conversation_id = ? AND sender_id = ?"
            )
            params: list[object] = [conversation_id, own_id]
            if lid is not None:
                sql += " AND message_id = ?"
                params.append(int(lid))
            sql += " ORDER BY send_time DESC, message_id DESC LIMIT 1"
            row = conn.execute(sql, tuple(params)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        message_id, content_type, send_time, content = row
        export, _crypto = self.vendor
        try:
            text = export.render(int(content_type or 0), content).strip()
            label = export.type_name(int(content_type or 0))
        except Exception:
            text = _text(content).strip()
            label = str(content_type)
        return {"lid": int(message_id), "type": label, "text": text, "send_time": int(send_time or 0)}


def _desktop_window():
    """返回企业微信主窗口；pywinauto 是 Windows extra 的可选依赖。"""
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise LocalSendError(
            "Windows UI 后端未安装，请执行 `uv sync --extra windows`"
        ) from exc
    try:
        windows = Desktop(backend="uia").windows()
    except Exception as exc:
        raise LocalSendError(f"无法枚举 Windows UI Automation 窗口: {exc}") from exc
    visible = []
    for window in windows:
        try:
            title = _text(window.window_text()).strip().casefold()
            class_name = _text(window.class_name()).strip().casefold()
            if window.is_visible() and (
                "企业微信" in title
                or "wecom" in title
                or "wxwork" in title
                or class_name == "weworkwindow"
            ):
                visible.append(window)
        except Exception:
            continue
    if not visible:
        raise LocalSendError("未找到可操作的企业微信主窗口")
    visible.sort(key=lambda item: item.rectangle().width() * item.rectangle().height(), reverse=True)
    return visible[0]


def _send_keys(value: str) -> None:
    try:
        from pywinauto.keyboard import send_keys
    except ImportError as exc:
        raise LocalSendError("Windows UI 后端未安装，请执行 `uv sync --extra windows`") from exc
    send_keys(value)


def _random_delay(delay_range: tuple[float, float]) -> None:
    time.sleep(random.uniform(*delay_range))


def _click_with_random_delay(click, *args, **kwargs) -> None:
    _random_delay(_CLICK_DELAY_RANGE)
    click(*args, **kwargs)


def _type_text(text: str) -> None:
    """逐字符输入任意 Unicode 文本，并避免 send_keys 解释特殊字符。"""
    for char in text:
        if char == "\r":
            continue
        if char == "\n":
            _send_keys("+{ENTER}")
        else:
            _send_keys(f"{{{char}}}")
        _random_delay(_CHAR_DELAY_RANGE)


def _edit_controls(window):
    try:
        controls = window.descendants(control_type="Edit")
    except Exception:
        return []
    return [control for control in controls if control.is_visible() and control.is_enabled()]


class WindowsUI:
    """企业微信 Windows CEF 界面的 UIA + 键盘后端。"""

    def __init__(self):
        self.window = _desktop_window()

    def focus(self) -> None:
        try:
            self.window.set_focus()
        except Exception as exc:
            raise LocalSendError(f"无法激活企业微信窗口: {exc}") from exc

    def open_conversation(self, name: str) -> None:
        self.focus()
        _send_keys("^f")
        time.sleep(0.2)
        _type_text(name)
        time.sleep(0.7)
        # WXWork's CEF search result list is exposed as a separate native
        # SearchResultWindow2 window, while its contents are not visible to UIA.
        # Clicking the first contact result is more reliable than pressing Enter,
        # which can leave the search box active on some client builds.
        try:
            from pywinauto import Desktop, mouse

            result_windows = [
                window
                for window in Desktop(backend="win32").windows()
                if window.class_name() == "SearchResultWindow2" and window.is_visible()
            ]
        except Exception as exc:
            raise LocalSendError(f"无法读取企业微信搜索结果窗口: {exc}") from exc
        if not result_windows:
            raise LocalSendError(f"搜索企业微信会话失败: {name}")
        result = result_windows[0]
        rect = result.rectangle()
        _click_with_random_delay(
            mouse.click,
            coords=(rect.left + min(220, max(100, rect.width() // 2)), rect.top + 68),
        )
        time.sleep(0.8)

    def input_control(self):
        controls = _edit_controls(self.window)
        if controls:
            # 搜索框离开后，聊天输入框通常是最后一个 Edit。
            return controls[-1]
        return None

    def focus_input(self) -> None:
        self.focus()
        control = self.input_control()
        if control is not None:
            try:
                _click_with_random_delay(control.click_input)
                return
            except Exception:
                pass
        rect = self.window.rectangle()
        # CEF 输入区的保守兜底位置；发送前仍需保证企业微信窗口是前台窗口。
        from pywinauto import mouse
        _click_with_random_delay(
            mouse.click,
            coords=(rect.left + int(rect.width() * 0.72), rect.bottom - 55),
        )

    def send_text(self, content: str, mentions: list[str] | None = None) -> None:
        self.focus_input()
        if not mentions:
            _type_text(content)
        else:
            spans = []
            cursor = 0
            for name in mentions:
                token = f"@{name}"
                start = content.find(token, cursor)
                if start < 0 or content.find(token, start + len(token)) >= 0:
                    raise LocalSendError(f"正文中必须恰好出现一次 {token}")
                spans.append((start, start + len(token), name))
                cursor = start + len(token)
            cursor = 0
            for start, end, name in spans:
                _type_text(content[cursor:start])
                _type_text("@")
                _type_text(name)
                time.sleep(0.4)
                _send_keys("{DOWN}{ENTER}")
                cursor = end
            _type_text(content[cursor:])
        time.sleep(0.1)
        _send_keys("{ENTER}")

    def send_image(self, path: str) -> None:
        try:
            import win32clipboard
            import win32con
            from PIL import Image
        except ImportError as exc:
            raise LocalSendError("图片发送需要 Windows extra(Pillow/pywin32)") from exc
        image = Image.open(path).convert("RGB")
        with tempfile.NamedTemporaryFile(suffix=".bmp", delete=False) as temp:
            bmp_path = temp.name
        try:
            image.save(bmp_path, "BMP")
            with open(bmp_path, "rb") as stream:
                dib = stream.read()[14:]
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
            finally:
                win32clipboard.CloseClipboard()
            self.focus_input()
            _send_keys("^v")
            time.sleep(0.8)
            _send_keys("{ENTER}")
        finally:
            Path(bmp_path).unlink(missing_ok=True)

    def recall(self, text: str) -> None:
        self.focus()
        controls = []
        try:
            controls = self.window.descendants()
        except Exception:
            pass
        target = None
        for control in controls:
            try:
                if text and text in control.window_text():
                    target = control
            except Exception:
                continue
        if target is None:
            raise LocalSendError("当前会话中未找到目标消息气泡，拒绝按坐标猜测")
        try:
            _click_with_random_delay(target.click_input, button="right")
        except Exception as exc:
            raise LocalSendError(f"打开消息菜单失败: {exc}") from exc
        time.sleep(0.2)
        menu_items = []
        try:
            menu_items = self.window.descendants(control_type="MenuItem")
        except Exception:
            pass
        item = next(
            (x for x in menu_items if _text(x.window_text()).strip() in {"撤回", "Recall"}),
            None,
        )
        if item is None:
            raise LocalSendError("消息菜单中未找到“撤回”项")
        _click_with_random_delay(item.click_input)
        time.sleep(0.2)
        for control in self.window.descendants():
            try:
                if _text(control.window_text()).strip() in {"确定", "确认", "OK"}:
                    _click_with_random_delay(control.click_input)
                    break
            except Exception:
                continue


_STORE: WindowsStore | None = None


def store() -> WindowsStore:
    global _STORE
    if _STORE is None:
        _STORE = WindowsStore()
    return _STORE


def list_conversations(search: str = "") -> list[dict]:
    return store().conversations(search)


def get_conversation_by_rid(rid: int) -> dict | None:
    return store().conversation_by_rid(int(rid))


def resolve_conversation(
    identifier: str = "",
    rid: int | None = None,
    target_type: str | None = None,
) -> dict | None:
    if rid is not None:
        return get_conversation_by_rid(rid)
    return (
        store().conversation_by_name(identifier.strip(), target_type)
        if identifier.strip()
        else None
    )


def send_message(
    to: str = "", content: str = "", conv_rid: int | None = None,
    mentions: list[str] | None = None,
    target_type: str | None = None,
) -> dict:
    ensure_windows()
    if not content.strip():
        return {"success": False, "error": "消息内容为空"}
    conv = resolve_conversation(to, conv_rid, target_type)
    if not conv:
        return {"success": False, "error": f"未找到会话: {to or conv_rid!r}"}
    before = store().latest_own_message(conv["conversation_id"])
    ui = WindowsUI()
    ui.open_conversation(conv["name"])
    ui.send_text(content, mentions)
    deadline = time.monotonic() + max(
        0.0, get_settings().windows_send_confirm_timeout
    )
    confirmed = False
    while time.monotonic() < deadline:
        latest = store().latest_own_message(conv["conversation_id"])
        is_new = latest and (before is None or latest["lid"] != before["lid"])
        if is_new and content.strip() in latest.get("text", ""):
            confirmed = True
            break
        time.sleep(0.4)
    if not confirmed:
        return {
            "success": False,
            "conv_name": conv["name"],
            "conv_rid": conv["rid"],
            "error": "企业微信界面已完成输入，但数据库未确认消息落库",
        }
    return {
        "success": True,
        "conv_name": conv["name"],
        "conv_rid": conv["rid"],
        "conv_lid": conv["lid"],
        "conv_type": conv["type"],
        "open_method": "uia_search",
        "error": None,
    }


def send_image(image_path: str = "", conv_rid: int | None = None) -> dict:
    ensure_windows()
    if conv_rid is None:
        return {"success": False, "error": "发送图片必须指定 conv_rid"}
    conv = resolve_conversation(rid=conv_rid)
    if not conv:
        return {"success": False, "error": f"未找到会话 RID={conv_rid}"}
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        return {"success": False, "error": f"图片不存在: {path}"}
    ui = WindowsUI()
    ui.open_conversation(conv["name"])
    ui.send_image(str(path))
    return {"success": True, "conv_name": conv["name"], "conv_rid": conv["rid"], "error": None}


def recall_message(conv_rid: int, lid: int | None = None) -> dict:
    ensure_windows()
    conv = resolve_conversation(rid=conv_rid)
    if not conv:
        return {"success": False, "error": f"未找到会话 RID={conv_rid}"}
    target = store().latest_own_message(conv["conversation_id"], lid)
    if not target:
        return {"success": False, "error": "目标会话中没有找到本人消息"}
    if target["type"] not in {"文本", "text"} or not target["text"]:
        return {"success": False, "error": "Windows 当前仅支持撤回可定位的文本消息"}
    ui = WindowsUI()
    ui.open_conversation(conv["name"])
    ui.recall(target["text"])
    return {
        "success": True,
        "conv_name": conv["name"],
        "conv_rid": conv["rid"],
        "lid": target["lid"],
        "error": None,
    }


def open_conversation_by_rid(rid: int) -> dict:
    ensure_windows()
    conv = resolve_conversation(rid=rid)
    if not conv:
        return {"success": False, "error": f"未找到会话 RID={rid}"}
    WindowsUI().open_conversation(conv["name"])
    return {"success": True, "conv_name": conv["name"], "conv_rid": conv["rid"], "error": None}

