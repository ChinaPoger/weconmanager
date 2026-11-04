"""PyAutoGUI + OCR backend for the Windows WeCom contact picker."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .windows_group import Member, WindowsGroupError


@dataclass(frozen=True)
class Label:
    text: str
    x: int
    y: int
    left: int
    top: int
    right: int
    bottom: int


class TargetImageChanged(WindowsGroupError):
    """The visual precondition failed before any click was delivered."""


@lru_cache(maxsize=1)
def _ocr():
    from rapidocr import RapidOCR

    return RapidOCR(params={"Global.log_level": "error"})


def _normal(text: str) -> str:
    return re.sub(r"[\s()\uff08\uff09\[\]【】]", "", text or "").casefold()


def _window_visible(window) -> bool:
    """Dialog wrappers from Desktop.windows() have a handle, not WindowSpecification.exists."""
    if window is None:
        return False
    handle = getattr(window, "handle", None)
    try:
        hwnd = int(handle)
    except (TypeError, ValueError):
        hwnd = 0
    if hwnd:
        import win32gui

        return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd))
    exists = getattr(window, "exists", None)
    visible = getattr(window, "is_visible", None)
    try:
        return bool((exists() if callable(exists) else True) and visible())
    except Exception:
        return False


class GroupPicker:
    OPEN_TIMEOUT = 5.0
    SEARCH_TIMEOUT = 4.0
    SETTLE_DELAY = 0.25

    def __init__(self):
        try:
            import pyautogui
            import pyperclip
            from pywinauto import Desktop
        except ImportError as exc:
            raise WindowsGroupError(
                "Windows 建群后端未安装,请执行 `uv sync --extra windows`"
            ) from exc

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.08
        self.gui = pyautogui
        self.clipboard = pyperclip
        self.desktop = Desktop(backend="win32")
        candidates = [
            window
            for window in self.desktop.windows()
            if window.window_text() == "企业微信"
            and window.class_name() == "WeWorkWindow"
            and window.is_visible()
        ]
        if len(candidates) != 1:
            raise WindowsGroupError(f"需要唯一的企业微信主窗口,实际 {len(candidates)} 个")
        self.main = candidates[0]
        self.pid = self.main.process_id()
        self.picker = None
        self.opened_by_us = False
        self.observed_rect = None
        self.selected: list[Member] = []
        self._old_clipboard: str | None = None
        self._rename_window = None
        _ocr()

    def _rect(self):
        return (self.picker or self.main).rectangle()

    def modal(self):
        matches = [window for window in self.desktop.windows()
                   if window.process_id() == self.pid and window.is_visible()
                   and window.class_name() == "weWorkSelectUser"]
        if len(matches) > 1:
            raise WindowsGroupError("有多个选择联系人窗口,已停止")
        return matches[0] if matches else None

    def _focus(self) -> None:
        import win32gui

        target = self.picker or self.main
        target.set_focus()
        time.sleep(0.1)
        if win32gui.GetForegroundWindow() != target.handle:
            raise WindowsGroupError("企业微信窗口没有获得前台焦点,已停止")

    def _check_focus(self) -> None:
        import win32gui

        if win32gui.GetForegroundWindow() != (self.picker or self.main).handle:
            raise WindowsGroupError("企业微信焦点已变化,已停止")

    def _require_no_title_editor(self) -> None:
        from pywinauto import Desktop

        rect = self.main.rectangle()
        if any(edit.is_visible() and edit.has_keyboard_focus()
               and edit.rectangle().top < rect.top + 100
               and edit.rectangle().left > rect.left + rect.width() * 0.30
               for edit in Desktop(backend="uia").window(handle=self.main.handle)
               .descendants(control_type="Edit")):
            raise WindowsGroupError("当前正在编辑会话标题,请结束编辑后再操作")

    def screenshot(self):
        self._check_focus()
        rect = self._rect()
        width, height = self.gui.size()
        if rect.left < 0 or rect.top < 0 or rect.right > width or rect.bottom > height:
            raise WindowsGroupError("企业微信窗口需完整显示在主屏幕内,未继续操作")
        return self.gui.screenshot(region=(rect.left, rect.top, rect.width(), rect.height()))

    def observe(self) -> list[Label]:
        self.frame = self.screenshot()
        result = _ocr()(self.frame)
        self.observed_rect = self._rect()
        labels: list[Label] = []
        if result.txts is None:
            return labels
        for box, text, score in zip(result.boxes, result.txts, result.scores, strict=True):
            if score < 0.78:
                continue
            xs, ys = box[:, 0], box[:, 1]
            left, right = int(min(xs)), int(max(xs))
            top, bottom = int(min(ys)), int(max(ys))
            labels.append(
                Label(
                    str(text).replace(" ", ""),
                    (left + right) // 2,
                    (top + bottom) // 2,
                    left,
                    top,
                    right,
                    bottom,
                )
            )
        return labels

    @staticmethod
    def require(labels: list[Label], *texts: str) -> Label:
        wanted = {_normal(text) for text in texts}
        matches = [label for label in labels if _normal(label.text) in wanted]
        if len(matches) != 1:
            shown = [label.text for label in labels]
            raise WindowsGroupError(f"界面文字 {'/'.join(texts)} 无法唯一定位: {shown}")
        return matches[0]

    def click(self, label: Label) -> None:
        import win32gui

        self._check_focus()
        rect = self._rect()
        if rect != self.observed_rect:
            raise WindowsGroupError("观察后窗口位置已变化,未点击")
        if label.right > label.left and label.bottom > label.top:
            from PIL import ImageChops, ImageStat

            box = (label.left, label.top, label.right, label.bottom)
            delta = ImageChops.difference(self.frame.crop(box), self.screenshot().crop(box))
            if max(ImageStat.Stat(delta).mean) > 8:
                raise TargetImageChanged("目标图像已变化,未使用旧坐标点击")
        x, y = rect.left + label.x, rect.top + label.y
        target = (self.picker or self.main).handle
        hit = win32gui.WindowFromPoint((x, y))
        root = win32gui.GetAncestor(hit, 2)
        import win32process

        search_result = (win32process.GetWindowThreadProcessId(root)[1] == self.pid
                         and win32gui.GetClassName(root) == "SearchResultWindow2")
        if hit != target and not win32gui.IsChild(target, hit) and not search_result:
            raise WindowsGroupError("目标被其他窗口遮挡,未点击")
        self.gui.click(x, y)
        time.sleep(self.SETTLE_DELAY)

    def _is_open(self, labels: list[Label]) -> bool:
        texts = {_normal(label.text) for label in labels}
        return any(text.startswith(("选择联系人", "发起群聊", "已选择")) for text in texts)

    def _wait_labels(self, predicate, timeout: float, error: str) -> list[Label]:
        deadline = time.monotonic() + timeout
        labels: list[Label] = []
        while time.monotonic() < deadline:
            labels = self.observe()
            if predicate(labels):
                return labels
            time.sleep(0.2)
        raise WindowsGroupError(f"{error}: {[label.text for label in labels]}")

    def _click_fresh(self, resolve, timeout: float, error: str) -> Label:
        """Re-resolve a changed image; never retry a click with an unknown outcome."""
        deadline = time.monotonic() + timeout
        for attempt in range(3):
            labels = self._wait_labels(
                lambda items: resolve(items) is not None,
                max(0.0, deadline - time.monotonic()), error,
            )
            target = resolve(labels)
            assert target is not None
            try:
                self.click(target)
            except TargetImageChanged:
                if attempt == 2 or time.monotonic() >= deadline:
                    raise
                time.sleep(self.SETTLE_DELAY)
            else:
                return target
        raise AssertionError("unreachable")

    def open(self) -> None:
        if self.modal():
            raise WindowsGroupError("已有选择联系人窗口,请先关闭,未接管现有选择")
        self._focus()
        self._require_no_title_editor()
        labels = self.observe()
        if self._is_open(labels):
            raise WindowsGroupError("已有选择联系人窗口,请先关闭,未接管现有选择")
        self._preserve_clipboard()
        self._focus()
        self.gui.hotkey("ctrl", "n")
        self._wait_picker()

    def _wait_picker(self) -> None:
        deadline = time.monotonic() + self.OPEN_TIMEOUT
        while time.monotonic() < deadline:
            self.picker = self.modal()
            if self.picker:
                self.opened_by_us = True
                self._focus()
                break
            time.sleep(0.2)
        if self.picker is None:
            raise WindowsGroupError("未打开选择联系人窗口")
        self._wait_labels(self._is_open, self.OPEN_TIMEOUT, "未识别到选择联系人面板")

    def _add_member_icon(self) -> Label:
        """Match the observed person-plus icon; never fall back to a fixed click."""
        return self._toolbar_icon("wecom-add-member.png", "发起群聊图标")

    def _toolbar_icon(self, filename: str, name: str) -> Label:
        template = Path(__file__).with_name("assets") / filename
        try:
            boxes = list(self.gui.locateAll(str(template), self.frame, grayscale=True, confidence=0.92))
        except Exception as exc:
            raise WindowsGroupError(f"图像匹配未找到{name},未点击") from exc
        rect = self._rect()
        boxes = [box for box in boxes if box.left > rect.width() * 0.85 and box.top < 80]
        if len(boxes) != 1:
            raise WindowsGroupError(f"{name}无法唯一匹配,未点击")
        box = boxes[0]
        return Label(name, box.left + box.width // 2, box.top + box.height // 2,
                     box.left, box.top, box.left + box.width, box.top + box.height)

    def _chat_info_icon(self, _items: list[Label]) -> Label:
        return self._toolbar_icon("wecom-chat-info.png", "聊天信息图标")

    def _chat_info_visible(self, labels: list[Label]) -> bool:
        return any(label.text == "聊天信息" and label.x > self._rect().width() * 0.65
                   and label.y < 160 for label in labels)

    def _rename_modal(self):
        import win32gui

        matches = [window for window in self.desktop.windows()
                   if window.process_id() == self.pid and window.is_visible()
                   and window.class_name() == "WXworkWindow"
                   and win32gui.GetWindow(window.handle, 4) == self.main.handle]
        if len(matches) > 1:
            raise WindowsGroupError("有多个企业微信弹窗,未接管")
        return matches[0] if matches else None

    def open_from_contact(self, member: Member) -> None:
        """Seed a group from the first external contact's verified direct chat."""
        if not member.external:
            raise WindowsGroupError("发起群聊的首位成员必须是外部联系人")
        if self.modal():
            raise WindowsGroupError("已有选择联系人窗口,未接管现有选择")
        self._focus()
        self._require_no_title_editor()
        self._preserve_clipboard()
        self.gui.hotkey("ctrl", "f")
        self.gui.hotkey("ctrl", "a")
        self.clipboard.copy(member.name)
        self.gui.hotkey("ctrl", "v")
        time.sleep(0.8)
        display = f"{member.name}@{member.company or '微信'}"

        def contacts(items):
            return [label for label in items if label.text == display
                    and self._rect().width() * 0.16 < label.x < self._rect().width() * 0.52
                    and label.y > 60]

        labels = self._wait_labels(
            lambda items: bool(contacts(items)), 6, "找不到首位外部联系人的精确单聊入口",
        )
        candidates = contacts(labels)
        if len(candidates) != 1:
            raise WindowsGroupError("多个同名外部联系人,未按搜索结果猜测")
        self.click(candidates[0])
        self._wait_labels(
            lambda items: any(label.text == display and label.y < 70
                              and label.x > self._rect().width() * 0.35 for label in items),
            5, "打开后的单聊标题不符,未发起群聊",
        )
        self.click(self._add_member_icon())
        self._wait_picker()
        self.require(self.observe(), "发起群聊")

    def _preserve_clipboard(self) -> None:
        if self._old_clipboard is None:
            self._old_clipboard = self.clipboard.paste()

    def _restore_clipboard(self) -> None:
        if self._old_clipboard is not None:
            try:
                self.clipboard.copy(self._old_clipboard)
            except Exception:
                pass
            self._old_clipboard = None

    def _search(self, member: Member) -> list[Label]:
        labels = self.observe()
        header = self._header(labels)
        search = Label("搜索框", self._rect().width() // 4, header.y, 0, 0, 0, 0)
        self.click(search)
        self._check_focus()
        self.gui.hotkey("ctrl", "a")
        self.clipboard.copy(member.name)
        self.gui.hotkey("ctrl", "v")

        return self._wait_labels(
            lambda items: bool(self._candidate_labels(items, member)),
            self.SEARCH_TIMEOUT,
            f"客户端搜索不到联系人 {member.name}",
        )

    def _header(self, labels: list[Label]) -> Label:
        matches = [label for label in labels if label.x > self._rect().width() // 2
                   and label.text in {"选择联系人", "发起群聊"}]
        if len(matches) != 1:
            shown = [(label.text, label.x, label.y) for label in labels]
            raise WindowsGroupError(f"无法唯一定位选人面板标题: {shown}")
        return matches[0]

    @staticmethod
    def _name_matches(text: str, name: str) -> bool:
        return _normal(text) == _normal(name) or _normal(re.split(r"[\uff08(]", text)[0]) == _normal(name)

    def _candidate_labels(self, labels: list[Label], member: Member) -> list[Label]:
        header = self._header(labels)
        candidates = [label for label in labels if self._name_matches(label.text, member.name)
                      and label.x < self._rect().width() // 2 and label.y > header.y + 30]
        if not member.dept:
            return candidates

        departments = [_normal(part) for part in member.dept.split("/") if part]
        matched = []
        for candidate in candidates:
            row = [
                label
                for label in labels
                if label is not candidate and 0 <= label.y - candidate.y <= 30
                and abs(label.x - candidate.x) < self._rect().width() // 3
            ]
            row_text = "".join(_normal(label.text) for label in row)
            if any(department in row_text for department in departments):
                matched.append(candidate)
        return matched

    def _right_side_has(self, labels: list[Label], member: Member) -> bool:
        rect = self._rect()
        name = _normal(member.name)
        return any(
            self._name_matches(label.text, name) and label.x > rect.width() * 0.50
            and label.y > self._header(labels).y + 25
            and label.y < rect.height() - 70
            for label in labels
        )

    def select(self, members: list[Member]) -> None:
        for member in members:
            self.select_member(member)
        labels = self.observe()
        missing = [member.name for member in members if not self._right_side_has(labels, member)]
        if missing:
            raise WindowsGroupError(f"已选联系人复核失败,右侧缺少: {missing}")
        if self._selected_count(labels) != len(members):
            raise WindowsGroupError("已选人数与请求不一致,未提交")

    @staticmethod
    def _selected_count(labels: list[Label]) -> int:
        counts = [int(match.group(1)) for label in labels
                  if (match := re.fullmatch(r"已选择[:\uff1a]?(\d+)个?联系人", label.text))]
        if len(counts) != 1:
            raise WindowsGroupError("无法唯一识别已选人数,未提交")
        return counts[0]

    def select_member(self, member: Member) -> None:
        labels = self._search(member)
        candidates = self._candidate_labels(labels, member)
        if len(candidates) != 1:
            rows = [
                label.text
                for candidate in candidates
                for label in labels
                if abs(label.y - candidate.y) <= 38
            ]
            raise WindowsGroupError(
                f"客户端无法按姓名和部门唯一定位 {member.name}@{member.dept or '外部联系人'}: {rows}"
            )
        self.click(candidates[0])
        labels = self._wait_labels(
            lambda items: self._right_side_has(items, member),
            self.SEARCH_TIMEOUT,
            f"点击后右侧没有出现联系人 {member.name}",
        )
        if not self._right_side_has(labels, member):
            raise WindowsGroupError(f"联系人 {member.name} 未被选中")
        if self._selected_count(labels) != len(self.selected) + 1:
            raise WindowsGroupError("选人后人数变化不符预期,未提交")
        self.selected.append(member)

    def _submit_label(self, labels: list[Label]) -> Label:
        try:
            return self.require(labels, "确定")
        except WindowsGroupError:
            return self.require(labels, "完成")

    def submit(self) -> None:
        labels = self.observe()
        if not self.selected or any(not self._right_side_has(labels, member) for member in self.selected):
            raise WindowsGroupError("提交前联系人复核失败,未点击确定")
        if self._selected_count(labels) != len(self.selected):
            raise WindowsGroupError("提交前已选人数不一致,未点击确定")
        button = self._submit_label(labels)
        self.click(button)
        deadline = time.monotonic() + self.OPEN_TIMEOUT
        while self.modal():
            if time.monotonic() >= deadline:
                raise WindowsGroupError("点击确定后面板仍未关闭,提交结果未知")
            time.sleep(0.2)
        self.picker = None
        self._focus()

    def cancel(self) -> None:
        try:
            if self.opened_by_us and self.picker is not None and self.modal():
                self.picker.close()
                self.picker = None
        finally:
            self._restore_clipboard()

    def _group_title(self, labels: list[Label], name: str, member_names: list[str]) -> Label | None:
        rect = self._rect()
        wanted = [_normal(member) for member in member_names if member]
        matches = [label for label in labels
                   if label.y < rect.height() * 0.14 and label.x > rect.width() * 0.30
                   and (_normal(label.text) == _normal(name) if name else
                        bool(wanted) and all(member in _normal(label.text) for member in wanted))]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _roster_name_matches(text: str, name: str) -> bool:
        """Member rows sometimes glue an icon mark onto the name, like ``1伴风望海``."""
        if GroupPicker._name_matches(text, name):
            return True
        normal = _normal(text)
        wanted = _normal(name)
        return bool(wanted) and normal.endswith(wanted) and len(normal) - len(wanted) <= 2

    def _verify_group_members(self, labels: list[Label], member_names: list[str], member_count: int) -> None:
        member_counts = [int(match.group(1)) for label in labels
                         if label.x > self._rect().width() * 0.80
                         and (match := re.fullmatch(r"群成员[·:\uff1a]?(\d+)", label.text))]
        roster_ok = member_counts == [member_count] and all(
            any(self._roster_name_matches(label.text, member)
                and label.x > self._rect().width() * 0.80 and label.y > 100
                for label in labels)
            for member in member_names
        )
        if roster_ok:
            return
        # Chat info can cover the member sidebar. An unnamed title that already
        # lists every member is still the open group; do not search for another.
        if not member_counts and self._group_title(labels, "", member_names) is not None:
            return
        if member_counts != [member_count]:
            raise WindowsGroupError("打开的群成员人数与目标不符,未改名")
        raise WindowsGroupError("打开的群成员名单与目标不符,未改名")

    def open_group(self, name: str, member_names: list[str], member_count: int) -> None:
        if self.modal():
            raise WindowsGroupError("已有选人面板,未打开其他群")
        self._focus()
        self._require_no_title_editor()
        self._preserve_clipboard()
        # An unnamed group's search result can truncate its member-based title.
        # Use an already open group only when its full title AND roster agree.
        labels = self.observe()
        if self._group_title(labels, name, member_names) is not None:
            if self._chat_info_visible(labels):
                self._click_fresh(self._chat_info_icon, 4, "未找到聊天信息图标")
                labels = self._wait_labels(lambda items: not self._chat_info_visible(items),
                                           4, "聊天信息面板未收起")
            self._verify_group_members(labels, member_names, member_count)
            return
        query = name or max(member_names, key=len)
        self.gui.hotkey("ctrl", "f")
        self.gui.hotkey("ctrl", "a")
        self.clipboard.copy(query)
        self.gui.hotkey("ctrl", "v")
        time.sleep(0.8)

        def matches(items):
            candidates = []
            for label in items:
                # Search results occupy the left half; never select a contact row.
                if not (self._rect().width() * 0.16 < label.x < self._rect().width() * 0.52):
                    continue
                count = re.search(r"[\uff08(](\d+)[\uff09)]", label.text)
                if not count or int(count.group(1)) != member_count:
                    continue
                text = re.sub(r"[\uff08(]\d+[\uff09)].*$", "", label.text)
                if (name and _normal(text) == _normal(name)) or (
                    not name and all(_normal(member) in _normal(text) for member in member_names)
                ):
                    candidates.append(label)
            return candidates

        labels = self._wait_labels(lambda items: len(matches(items)) > 0, 6, "未找到目标群的精确搜索结果")
        candidates = matches(labels)
        if len(candidates) != 1:
            raise WindowsGroupError("多个同名同人数群,未打开或修改")
        self.click(candidates[0])
        self._wait_labels(
            lambda items: self._group_title(items, name, member_names) is not None,
            5, "搜索点击后目标群标题未出现",
        )
        self._verify_group_members(self.observe(), member_names, member_count)

    def rename(self, new_name: str, member_names: list[str], old_name: str = "") -> tuple[bool, str | None]:
        self._preserve_clipboard()
        try:
            return self._rename(new_name, member_names, old_name)
        finally:
            # Only cancel the rename dialog opened by this operation. A delivered
            # confirmation is never retried, even if verification fails later.
            dialog = self._rename_window
            self._rename_window = None
            try:
                if _window_visible(dialog):
                    self.picker = dialog
                    try:
                        self._check_focus()
                    except Exception:
                        self._focus()
                    self.click(self.require(self.observe(), "取消"))
            except Exception:
                try:
                    self.gui.press("escape")
                except Exception:
                    pass
            self.picker = None
            self._restore_clipboard()

    def _rename_input(self, items: list[Label], old_name: str) -> Label | None:
        """The dialog field, never the conversation title.

        An unnamed group keeps the member list as its header, while the dialog
        shows the placeholder ``请输入群名称``. A named group shows that name.
        """
        self.require(items, "修改群聊名称")
        self.require(items, "取消")
        rect = self._rect()
        band = [
            label for label in items
            if label.x > rect.width() * 0.22
            and rect.height() * 0.35 < label.y < rect.height() * 0.70
        ]
        current = old_name.replace(" ", "")
        named = [label for label in band if current and label.text == current]
        placeholders = [label for label in band if label.text == "请输入群名称"]
        if len(named) == 1:
            return named[0]
        if len(placeholders) == 1 and not named:
            return placeholders[0]
        return None

    def _rename(self, new_name: str, member_names: list[str], old_name: str) -> tuple[bool, str | None]:
        """Screenshot path: chat-info icon, 群聊名称 row, then the rename dialog."""
        if self._rename_modal() is not None:
            raise WindowsGroupError("已有企业微信弹窗,未接管现有输入")
        labels = self._wait_labels(
            lambda items: self._group_title(items, old_name, member_names) is not None,
            8, "没有看到唯一的目标群标题,未改名",
        )
        if not self._chat_info_visible(labels):
            self._click_fresh(self._chat_info_icon, 4, "未找到聊天信息图标")

        def name_row(items):
            if not self._chat_info_visible(items):
                return None
            matches = [label for label in items if label.text == "群聊名称"
                       and label.x > self._rect().width() * 0.65]
            return matches[0] if len(matches) == 1 else None

        self._click_fresh(name_row, 5, "聊天信息里未找到唯一的群聊名称入口")
        deadline = time.monotonic() + self.OPEN_TIMEOUT
        while time.monotonic() < deadline:
            dialog = self._rename_modal()
            if dialog is not None:
                self._rename_window = self.picker = dialog
                self._focus()
                break
            time.sleep(0.2)
        if self._rename_window is None:
            raise WindowsGroupError("未打开修改群聊名称弹窗")

        self._click_fresh(
            lambda items: self._rename_input(items, old_name), 5, "未定位到群名称输入框",
        )
        self._check_focus()
        self.gui.hotkey("ctrl", "a")
        if new_name:
            # This edit control accepts Ctrl+V but does not answer Ctrl+C.
            # The dialog screenshot below is what decides whether 确定 may be clicked.
            self.clipboard.copy(new_name)
            self.gui.hotkey("ctrl", "v")
        else:
            self.gui.press("backspace")
            self.gui.press("delete")

        def confirm(items):
            self.require(items, "修改群聊名称")
            self.require(items, "取消")
            if new_name:
                # OCR must also see the requested value inside the dialog.
                if not any(label.text == new_name.replace(" ", "") for label in items):
                    return None
            else:
                # Empty restores 未设置. The placeholder must be back, and the
                # previous name must be gone, before 确定 is clicked.
                current = old_name.replace(" ", "")
                if current and any(label.text == current for label in items):
                    return None
                if not any(label.text == "请输入群名称" for label in items):
                    return None
            return self.require(items, "确定")

        self._click_fresh(confirm, 5, "弹窗未显示请求的群名称,未确认")
        deadline = time.monotonic() + self.OPEN_TIMEOUT
        while _window_visible(self._rename_window):
            if time.monotonic() >= deadline:
                raise WindowsGroupError("确认后改名弹窗仍存在,结果未知,未重复提交")
            time.sleep(0.2)
        self.picker = None
        self._rename_window = None
        self._focus()
        try:
            shown = new_name or "成员自动名"
            self._wait_labels(
                lambda items: self._group_title(items, new_name, member_names) is not None,
                5.0,
                f"输入群名后界面没有显示 {shown}",
            )
        except WindowsGroupError as exc:
            return False, str(exc)
        return True, None

    def diagnostic(self) -> dict:
        self.picker = self.modal()
        rect = self._rect()
        return {
            "hwnd": self.main.handle,
            "title": self.main.window_text(),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "labels": [vars(label) for label in self.observe()],
        }


if __name__ == "__main__":
    print(json.dumps(GroupPicker().diagnostic(), ensure_ascii=False, indent=2))
