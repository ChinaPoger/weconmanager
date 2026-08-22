"""以「本人身份」拉群 —— 操控 macOS 客户端的「发起群聊」面板。

为什么不用官方 API: 官方能建群的只有 `appchat`,它的 `userlist` 只收本企业
userid,外部/微信联系人塞不进去;而「创建客户群」官方压根没开接口。
客户端里的 `wxwork://jump?target=openchat` 也走不通 —— 二进制里写死了
`not tencent company, ignore`,只对腾讯自家企业开放。

所以只剩这条路。好在实测下来它比「盲点坐标」结实得多:

  - 面板是主窗口上的一个 AXSheet,搜索框是真 AXTextField(可直接 set value)
  - 搜索结果是 AXOutline,每行能读出姓名与部门 —— **选人前先核对,不靠猜**
  - 搜索框支持空格一次填多人,顺序不改(「陈涛 陈挺 SMB售前bot」)。中文全名完全
    匹配会自动勾上;弹框里还没勾的英文/拼音联系人,用真鼠标点选。自动勾上的
    人数对不上就中止,绝不凑合。对不上再退回逐个搜索+回车。
  - 逐个搜时:回车 = 选中结果里的第一条(勾选框是 AXImage,没有 action,
    合成点击也打不进 CEF 自绘控件;批量结果里用 CoreGraphics 真点)
  - 右侧标题里的「N contact(s) selected」能读出来,每步都能回读校验

坐标依赖只剩不在 AX 树里的 "+" 按钮,按主窗口原点取相对偏移。新版客户端已把
会话标题暴露成可写的 AXTextField,直接写值并 AXConfirm;旧版找不到该控件时才
回退到坐标点击。坐标动作都不是「点了就算」,必须看到成功信号,看不到就换候选
偏移再试,试中了记下来 —— 见 ui_offsets.py。
"""
import logging
import re
import subprocess
import time
from typing import NoReturn

from . import directory, local_send, messages, ui_offsets
from .group import MAX_NAME_LEN
from .local_send import (
    _escape,
    _get_clipboard,
    _run_applescript,
    _set_clipboard,
    ensure_macos,
)
from .localdb import LocalSendError

# 和发消息共用同一把 UI 闸门: 操控的是同一个客户端界面,必须串行,
# 否则并发请求会互相抢焦点、把人选到别的会话里去。
from .uilock import ui_session

log = logging.getLogger(__name__)

# 群名长度上限沿用 group.MAX_NAME_LEN(官方 API 与客户端 UI 是同一个 50),
# 这里还兼作「复制回来的文本像不像群名」的判据

MAX_MEMBERS = 200
SHEET_TIMEOUT = 8.0
# 试探性候选的等待上限。第一个候选(记住的那个)给足 SHEET_TIMEOUT,后面的只是
# 在猜,再给 8s 会把一次「界面改版」拖成一分钟的静默等待。
PROBE_TIMEOUT = 2.5
# 写入搜索框后轮询结果;上限约等于旧的固定 SEARCH_SETTLE=1.6,有结果立刻停
SEARCH_POLL = 0.12
SEARCH_POLL_TRIES = 15
# 搜索命中多条时,最多读几行明细用于报错列候选(见 _search 里的性能说明)
DETAIL_ROWS = 8
# 批量结果里点选时多读几行,英文名靠前也要看到
PICKER_ROW_CAP = 24

# 选中一个人最多试几次(回车偶尔落空),以及每次判定前等计数刷新的次数/间隔
SELECT_ATTEMPTS = 3
COUNT_SETTLE_TRIES = 8
COUNT_SETTLE_DELAY = 0.15
# 企业微信建群后写入 Info.db/WAL 可能有几十秒延迟。只等 2 秒会把已经
# 建好的群误报成「新增 0 个」,进而跳过改名。
GROUP_APPEAR_TIMEOUT = 90.0
# 轮询间隔逐次翻倍到上限: 每一轮都是一次全量重解密,密集轮询换不来更早的答案。
GROUP_APPEAR_POLL = 1.0
GROUP_APPEAR_POLL_MAX = 8.0
# 确认后客户端切到新群可能慢于面板关掉。改名/改昵称前先等标题变成新群。
TITLE_SWITCH_TIMEOUT = 8.0
TITLE_SWITCH_POLL = 0.2


class LocalGroupError(LocalSendError):
    """拉群流程里的可预期失败(回 400)。"""


class AmbiguousCandidate(LocalGroupError):
    """一个名字在客户端里搜出多条 —— 回车只会选中第一条,所以必须让调用方说清楚。"""


_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def _looks_like_chinese_fullname(query: str) -> bool:
    """客户端会自动勾上「纯中文全名」;带拼音/英文的(如 SMB售前bot)不会。

    单字「陈」太容易撞人,不当成全名,交给逐个搜索去判歧义。
    """
    return len(_CJK.findall(query)) >= 2 and not _LATIN.search(query)


def _without_self(members: list[str]) -> list[str]:
    """尽力从成员列表剔除当前登录用户。

    本人本来就自动在新群里。客户端允许搜索并勾选本人,但右侧的「已选联系人」
    计数不会增加,会让后续的安全校验误判为选人失败。外部联系人不一定存在于
    Session.db;解析不了时原样保留,继续交给客户端搜索。
    """
    try:
        self_userid = str(directory.get_self().get("userid") or "").strip().lower()
    except LocalSendError:
        return members
    if not self_userid:
        return members

    filtered: list[str] = []
    with directory.batch():
        for query in members:
            try:
                member = directory.resolve_member(query)
            except LocalSendError:
                filtered.append(query)
                continue
            userid = str(member.get("userid") or "").strip().lower()
            if userid == self_userid:
                log.info("拉群成员「%s」是当前登录用户,已忽略(本人会自动入群)", query)
                continue
            filtered.append(query)
    return filtered


# ─── AppleScript ───────────────────────────────────────────────────────

# 主窗口按「面积最大」挑,不按名字 —— 客户端语言会变(实测英文版叫 "WeCom")
_MAIN_WINDOW = '''
  set mainWin to missing value
  set bestArea to -1
  repeat with w in windows
    set sz to size of w
    set a to (item 1 of sz) * (item 2 of sz)
    if a > bestArea then
      set bestArea to a
      set mainWin to contents of w
    end if
  end repeat
  if mainWin is missing value then error "找不到企业微信主窗口"
'''

# 选人面板定位。frontmost 只在打开面板时做一次:每次搜人/读计数都 delay 0.3
# 的话,3 个人就要白付一两秒,而且 keystroke 仍然可能落到别的应用上。
_PICKER = _MAIN_WINDOW + '''
  if (count of sheets of mainWin) is 0 then error "NO_SHEET"
  set sg to splitter group 1 of sheet 1 of mainWin
'''


def _osa(body: str, timeout: int = 20) -> str:
    """跑一段 System Events 脚本,失败抛 LocalGroupError。"""
    script = (
        'tell application "System Events" to tell process "企业微信"\n'
        f"{body}\n"
        "end tell"
    )
    ok, out = _run_applescript(script, timeout=timeout)
    if not ok:
        if "NO_SHEET" in out:
            raise LocalGroupError("「发起群聊」面板不在了(可能被手动关闭)")
        raise LocalGroupError(f"操控客户端失败: {out}")
    return out


def _post_click(x: int, y: int, *, move_delay: float, down_delay: float, settle: float) -> None:
    """在屏幕坐标发一次**真实**鼠标点击。

    System Events 的 `click at` 是合成点击,CEF 自绘的区域收不到 —— 会话标题和
    成员勾选框都实测过,点上去毫无反应。这里用 JXA 的 ObjC 桥直接调 CoreGraphics
    发真事件: 不引入任何依赖,还是同一个 osascript 二进制。

    延迟和点完的静置时间由调用方给:能轮询成功信号的路径不该固定睡。
    """
    jxa = f'''
ObjC.import('CoreGraphics');
var pt = $.CGPointMake({int(x)}, {int(y)});
function post(t) {{ $.CGEventPost(0, $.CGEventCreateMouseEvent($(), t, pt, 0)); }}
post(5); delay({move_delay});   // mouseMoved
post(1); delay({down_delay});   // leftMouseDown
post(2);                        // leftMouseUp
'''
    try:
        r = subprocess.run(
            ["osascript", "-l", "JavaScript", "-"],
            input=jxa, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise LocalGroupError("发送鼠标事件超时") from None
    if r.returncode != 0:
        raise LocalGroupError(f"发送鼠标事件失败: {(r.stderr or r.stdout).strip()}")
    if settle:
        time.sleep(settle)


def _real_click(x: int, y: int) -> None:
    """发一次真鼠标,再固定静置 —— 给没有成功信号可轮询的路径。"""
    _post_click(x, y, move_delay=0.15, down_delay=0.08, settle=0.8)


def _ui_click(x: int, y: int) -> None:
    """发一次真鼠标,立刻返回。点完由调用方轮询成功信号,别固定睡。"""
    _post_click(x, y, move_delay=0.05, down_delay=0.05, settle=0.0)


def _activate() -> None:
    """把客户端拉到前台,并把主窗口抬到它自己所有窗口的最前面。

    抬窗口不是可有可无: 企业微信可以把会话弹成独立窗口,那些窗口尺寸和主窗口
    接近、位置又常常盖住主窗口左上角 —— 实测就发生过一次,点「+」的坐标正好
    落在另一个窗口上被吞掉,面板不开,报错还指向了「界面布局变了」这种错方向。
    AXRaise 之后主窗口在最前,按坐标点才是点在它身上。
    """
    ok, err = _run_applescript('tell application "企业微信" to activate')
    if not ok:
        raise LocalGroupError(f"无法激活企业微信客户端: {err}")
    time.sleep(0.6)
    _osa(_MAIN_WINDOW + '''
  perform action "AXRaise" of mainWin
''')
    time.sleep(0.4)


# ─── 面板 ──────────────────────────────────────────────────────────────

_SHEET_NONE, _SHEET_PICKER, _SHEET_OTHER = 0, 1, 2


def _sheet_state() -> int:
    """主窗口上有没有面板,以及它是不是「发起群聊」那个。

    只数 sheet 不够: 校准要靠这个信号判断「刚才那一下点对了没有」,而客户端上
    **任何**弹窗都会让计数变成 1 —— 认错了就会拿一个不相干的面板去选人。
    所以多问一句搜索框在不在,分得清 0=没面板 / 1=选人面板 / 2=别的面板。
    """
    out = _osa(_MAIN_WINDOW + '''
  if (count of sheets of mainWin) is 0 then return "0"
  set sh to sheet 1 of mainWin
  try
    get text field 1 of group 1 of (splitter group 1 of sh)
    return "1"
  on error
    return "2"
  end try
''')
    code = out.strip().strip('"')
    return int(code) if code.isdigit() else _SHEET_NONE


def _sheet_open() -> bool:
    return _sheet_state() != _SHEET_NONE


def _click_window_offset(dx: int, dy: int) -> None:
    """在「主窗口原点 + 偏移」处点一下(合成点击,原生控件收得到)。"""
    _osa(_MAIN_WINDOW + f'''
  set p to position of mainWin
  click at {{(item 1 of p) + {dx}, (item 2 of p) + {dy}}}
''')


def _wait_picker(timeout: float) -> int:
    deadline = time.time() + timeout
    state = _SHEET_NONE
    while time.time() < deadline:
        time.sleep(0.4)
        state = _sheet_state()
        if state != _SHEET_NONE:
            return state
    return state


def _open_picker() -> None:
    """点会话列表顶部的 "+" 打开「发起群聊」面板,并确认它真的开了。

    点不中就按候选偏移再试(见 ui_offsets.py):"+" 不在 AX 树里,偏移是量出来的,
    而这里的成功信号又干净又便宜 —— 面板开没开一问就知道,正适合拿来自动校准。
    试中了记下来,下次第一发就命中。已有面板会先自动取消,避免上一次异常中断
    把后续操作永久卡住。
    """
    if _sheet_open():
        log.warning("打开「发起群聊」前发现残留面板,正在自动关闭")
        _recover_open_sheet()

    tried: list[tuple[int, int]] = []
    for i, (dx, dy) in enumerate(ui_offsets.candidates("plus")):
        tried.append((dx, dy))
        _click_window_offset(dx, dy)
        state = _wait_picker(SHEET_TIMEOUT if i == 0 else PROBE_TIMEOUT)
        if state == _SHEET_PICKER:
            ui_offsets.remember("plus", (dx, dy))
            _focus_picker()
            return
        if state == _SHEET_OTHER:
            # 点开了别的东西。继续试下一个候选只会在一个陌生面板上乱点,
            # 而且那个面板还挡着 "+" —— 停下来让人看一眼。
            raise LocalGroupError(
                f"点 {dx, dy} 打开的不是「发起群聊」面板,已中止。"
                "请手动关掉客户端上多出来的那个面板再试。"
            )
    raise LocalGroupError(
        f"点了「+」但「发起群聊」面板没出现(试过的窗口偏移: {tried})。"
        "常见原因: 终端没拿到「辅助功能」授权;有别的企业微信窗口盖住了主窗口左上角"
        "(已经先做过 AXRaise,仍被盖住的话把那个弹出的会话窗口关掉);"
        f"或者客户端版本改了界面布局 —— {ui_offsets.describe('plus')}。"
    )


def _button(names: str, pick_rightmost: bool) -> str:
    """生成一段「按标题找按钮,找不到就按位置兜底」的 AppleScript。

    标题会随客户端语言变(实测英文版是 Done/Cancel),所以位置兜底不能省:
    面板右下角两个按钮里,右边是确认、左边是取消。
    """
    tries = "\n".join(
        f'  if target is missing value then\n'
        f'    try\n      set target to button "{n}" of g2\n    end try\n  end if'
        for n in names.split("|")
    )
    cmp_op = ">" if pick_rightmost else "<"
    init = "-1" if pick_rightmost else "999999"
    return f'''
  set g2 to group 2 of sg
  set target to missing value
{tries}
  if target is missing value then
    set bestX to {init}
    repeat with b in (buttons of g2)
      set bp to position of b
      if (item 1 of bp) {cmp_op} bestX then
        set bestX to item 1 of bp
        set target to contents of b
      end if
    end repeat
  end if
'''


def _close_picker() -> None:
    """按「取消」关面板。

    Esc 对这个 sheet 无效(实测按了没反应),所以必须点按钮 —— 失败时至少
    要留一条日志,别让用户屏幕上莫名其妙杵着一个面板还不知道为什么。
    """
    try:
        _osa(_PICKER + _button("Cancel|取消", pick_rightmost=False) + '''
  if target is missing value then error "找不到「取消」按钮"
  perform action "AXPress" of target
''')
        time.sleep(0.8)
    except LocalGroupError as e:
        log.warning("关闭「发起群聊」面板失败,客户端上可能残留一个面板: %s", e)


def _recover_open_sheet() -> None:
    """清理进入发起群聊流程前残留的客户端面板,并确认它真的关闭。

    已知的发起群聊面板必须点「取消」,因为这个 sheet 对 Escape 不响应。其它
    面板只发一次 Escape:它是通用的取消动作,不会点击未知控件。关不掉就失败,
    继续校准会有把输入送进错误窗口的风险。
    """
    state = _sheet_state()
    if state == _SHEET_NONE:
        return

    if state == _SHEET_PICKER:
        log.warning("发现残留的「发起群聊」面板,正在自动取消")
        _close_picker()
    else:
        log.warning("发现无法识别的企业微信弹出面板,正在尝试 Escape 关闭")
        _osa(_MAIN_WINDOW + '''
  set frontmost to true
  key code 53
  delay 0.5
''')

    if _sheet_state() != _SHEET_NONE:
        raise LocalGroupError(
            "客户端上的残留弹出面板无法自动关闭,请手动关闭后再试"
        )


# ─── 选人 ──────────────────────────────────────────────────────────────

_ROW_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def _focus_picker() -> None:
    """把客户端顶到前台,保证后续 keystroke 落在选人面板上。只在打开面板时做一次。"""
    _osa(_PICKER + '''
  set frontmost to true
  delay 0.15
''')


def _set_search_text(keyword: str) -> None:
    """把搜索框写成 keyword,立刻返回,不等结果。"""
    _osa(_PICKER + f'''
  set tf to text field 1 of group 1 of sg
  set focused of tf to true
  set value of tf to "{_escape(keyword)}"
''')


def _search_rows() -> tuple[int, list[dict]]:
    """读当前搜索结果:(命中总数, 前若干行的姓名与部门)。

    只读前 DETAIL_ROWS 行明细是性能要求,不是偷懒: 每读一个 AX 属性都是一次
    跨进程调用,搜「陈」这种常见姓能出几百行,全量枚举实测直接把超时打爆。
    判重只需要总数,报错列候选只需要前几行。
    """
    out = _osa(_PICKER + f'''
  -- SEARCH_ROWS
  set ol to outline 1 of scroll area 1 of group 1 of sg
  set total to 0
  try
    set total to count of rows of ol
  end try
  set shown to total
  if shown > {DETAIL_ROWS} then set shown to {DETAIL_ROWS}
  set acc to ""
  set n to 0
  repeat with i from 1 to shown
    try
      set rw to row i of ol
      set c to UI element 1 of rw
      set nm to ""
      set dp to ""
      try
        set nm to value of UI element 2 of c
      end try
      try
        set dp to value of UI element 7 of c
      end try
      if nm is not "" then
        set n to n + 1
        set acc to acc & nm & "{_FIELD_SEP}" & dp & "{_ROW_SEP}"
      end if
    end try
  end repeat
  -- 虚拟列表的 count 经常虚高;真正读到的行更少时,以读到的为准
  if n > 0 and n < {DETAIL_ROWS} and total > n then set total to n
  return (total as string) & "{_ROW_SEP}" & acc
''')
    parts = out.strip().strip('"').split(_ROW_SEP)
    total = int(parts[0]) if parts and parts[0].strip().isdigit() else 0
    rows = []
    for chunk in parts[1:]:
        if not chunk:
            continue
        name, _, dept = chunk.partition(_FIELD_SEP)
        # 没有部门的联系人(外部/微信)读回来是 AppleScript 的 missing value
        rows.append({"name": name, "dept": "" if dept == "missing value" else dept})
    if rows and len(rows) < DETAIL_ROWS and total > len(rows):
        total = len(rows)
    return total, rows


def _search(keyword: str) -> tuple[int, list[dict]]:
    """把关键词写进搜索框,等结果从上一批名单里换过来再读。"""
    _before_total, before_rows = _search_rows()
    before_names = tuple(r.get("name") for r in before_rows)
    _set_search_text(keyword)
    last: tuple[int, list[dict]] = (0, [])
    for i in range(max(1, SEARCH_POLL_TRIES)):
        last = _search_rows()
        names = tuple(r.get("name") for r in last[1])
        changed = names != before_names or not before_names
        if last[0] > 0 and (changed or i == SEARCH_POLL_TRIES - 1):
            return last
        time.sleep(SEARCH_POLL)
    return last


def _bulk_search_text(queries: list[str]) -> str:
    """空格一次搜,名字顺序跟调用方给的一样。"""
    return " ".join(queries)


def _picker_result_rows() -> list[dict]:
    """批量结果里每一行:姓名、部门、是否已选、点击坐标。"""
    out = _osa(_PICKER + f'''
  -- PICKER_RESULT_ROWS
  set ol to outline 1 of scroll area 1 of group 1 of sg
  set total to 0
  try
    set total to count of rows of ol
  end try
  set shown to total
  if shown > {PICKER_ROW_CAP} then set shown to {PICKER_ROW_CAP}
  set acc to ""
  set n to 0
  repeat with i from 1 to shown
    try
      set rw to row i of ol
      set c to UI element 1 of rw
      set nm to ""
      set dp to ""
      try
        set nm to value of UI element 2 of c
      end try
      try
        set dp to value of UI element 7 of c
      end try
      if nm is not "" then
        set sel to "0"
        try
          if selected of rw then set sel to "1"
        end try
        set p to position of rw
        set sz to size of rw
        set loc to (item 1 of p as string) & "," & (item 2 of p as string)
        set loc to loc & "," & (item 1 of sz as string) & "," & (item 2 of sz as string)
        set n to n + 1
        set acc to acc & nm & "{_FIELD_SEP}" & dp & "{_FIELD_SEP}" & sel & "{_FIELD_SEP}" & loc & "{_ROW_SEP}"
      end if
    end try
  end repeat
  return (n as string) & "{_ROW_SEP}" & acc
''')
    parts = (out or "").strip().strip('"').split(_ROW_SEP)
    rows: list[dict] = []
    for chunk in parts[1:]:
        if not chunk:
            continue
        name, _, rest = chunk.partition(_FIELD_SEP)
        dept, _, rest = rest.partition(_FIELD_SEP)
        sel, _, pos = rest.partition(_FIELD_SEP)
        x = y = w = h = 0
        bits = pos.split(",")
        try:
            x, y, w, h = (int(bits[0]), int(bits[1]), int(bits[2]), int(bits[3]))
        except (ValueError, IndexError):
            pass
        rows.append({
            "name": name,
            "dept": "" if dept == "missing value" else dept,
            "selected": sel == "1",
            "x": x + max(w, 40) // 2,
            "y": y + max(h, 20) // 2,
        })
    return rows


def _await_picker_rows(min_rows: int) -> list[dict]:
    rows: list[dict] = []
    for _ in range(max(1, SEARCH_POLL_TRIES)):
        rows = _picker_result_rows()
        if len(rows) >= min_rows:
            return rows
        time.sleep(SEARCH_POLL)
    return rows


def _click_picker_row(row: dict) -> None:
    """点选人结果里的一行。先走可打桩的 AppleScript 标记,再发真鼠标。"""
    x, y = int(row.get("x") or 0), int(row.get("y") or 0)
    if x <= 0 or y <= 0:
        raise LocalGroupError(
            f"结果行「{row.get('name') or ''}」没有可点的坐标,已中止,没有建群。"
        )
    name = _escape(str(row.get("name") or ""))
    _osa(_PICKER + f'''
  -- CLICK_PICKER_ROW
  set clickName to "{name}"
''')
    _ui_click(x, y)


def _resolved_display_name(query: str) -> str:
    try:
        return str(directory.resolve_member(query).get("name") or "").strip()
    except LocalSendError:
        return ""


def _row_matches_query(row: dict, query: str, resolved: str) -> bool:
    """`resolved` 是这个查询在通讯录里的显示名(查不到给空串),由调用方先解好。"""
    name = str(row.get("name") or "").strip()
    q = (query or "").strip()
    if not name or not q:
        return False
    n, ql = name.lower(), q.lower()
    if ql == n or ql in n or n in ql:
        return True
    if not resolved:
        return False
    r = resolved.lower()
    return r == n or r in n or n in r or n.startswith(r)


def _english_candidate_rows(rows: list[dict], queries: list[str]) -> list[dict]:
    """弹框里对上英文/拼音查询、还没勾上的那几行。一对多就当歧义。"""
    used: set[int] = set()
    hits: list[dict] = []
    # 每个查询只解一次通讯录:resolve_member 不带缓存,一次要发好几条 SQL,
    # 放在行循环里就是「查询数 * 行数」次 open/close,而这条路上有人在等界面。
    with directory.batch():
        resolved = {q: _resolved_display_name(q) for q in queries}
    for query in queries:
        matched = [
            (i, row) for i, row in enumerate(rows)
            if i not in used and _row_matches_query(row, query, resolved.get(query, ""))
        ]
        if len(matched) > 1:
            shown = ", ".join(f"{r['name']}({r['dept']})" for _i, r in matched)
            raise AmbiguousCandidate(
                f"「{query}」在客户端里搜出多条: {shown}。"
                "请写得更精确(全名/工号/手机号)。"
            )
        if len(matched) == 1:
            i, row = matched[0]
            used.add(i)
            hits.append({**row, "query": query})
    return hits


def _click_english_candidates(targets: list[dict], expect_total: int) -> list[dict]:
    """点弹框里未勾上的英文/拼音联系人。每点一个人必须看到已选+1,多了就停。"""
    clicked: list[dict] = []
    got = _selected_count()
    for row in targets:
        if got >= expect_total:
            break
        _click_picker_row(row)
        now = _await_selected_count(got + 1)
        label = row.get("name") or row.get("query") or ""
        if now > expect_total:
            raise LocalGroupError(
                f"点选「{label}」后已选人数是 {now},超过预期 {expect_total} —— "
                "可能多选了人,已中止,没有建群。"
            )
        if now < got:
            raise LocalGroupError(
                f"点选「{label}」把已选人数从 {got} 降到了 {now} —— "
                "可能点到了已勾上的人,已中止,没有建群。"
            )
        if now == got + 1:
            clicked.append(row)
            got = now
            continue
        log.warning("点选「%s」后已选仍是 %d(预期 %d),跳过", label, now, got + 1)
    return clicked


def _selected_count() -> int:
    """读右侧标题里的「N contact(s) selected」。

    只抓数字,不认文案 —— 客户端语言变了(「已选择 N 个联系人」)照样能用。
    """
    out = _osa(_PICKER + '''
  set acc to ""
  repeat with e in (UI elements of (UI element 1 of group 2 of sg))
    try
      set acc to acc & (value of e) & "|"
    end try
  end repeat
  return acc
''')
    numbers = re.findall(r"\d+", out)
    return int(numbers[0]) if numbers else 0


def _pick_first_result() -> None:
    """回车 = 选中结果里的第一条。勾选框是 AXImage,点不动,只能走这条。"""
    _osa(_PICKER + '''
  set tf to text field 1 of group 1 of sg
  set focused of tf to true
  keystroke return
''')


def _await_selected_count(expect: int) -> int:
    """等已选计数涨到 expect,返回最后读到的值。

    **重试前必须先等**: 「计数没涨」有两种可能 —— 回车真的落空了,或者只是界面
    还没刷新到。后者如果直接重按回车,会把已经选上的人**取消掉**,把一次小抖动
    变成一次错误的成员名单。先等,把「读早了」这种可能排除掉,再谈重试。
    """
    got = _selected_count()
    for _ in range(COUNT_SETTLE_TRIES - 1):
        if got >= expect:
            return got
        time.sleep(COUNT_SETTLE_DELAY)
        got = _selected_count()
    return got


def _select_member(query: str, expect_count: int) -> dict:
    """搜一个人 → 核对唯一 → 回车选中 → 校验计数涨了 1,没涨就重试。

    回车偶尔会落空(焦点被别的窗口抢走、界面还没 settle),实测两次里发生过一次。
    每轮都重新搜一遍不是浪费: 上一次回车之后搜索框会被客户端清空,不重搜就没有
    结果行可选。
    """
    got = 0
    for attempt in range(1, SELECT_ATTEMPTS + 1):
        total, rows = _search(query)
        if total == 0:
            raise LocalGroupError(f"客户端通讯录里搜不到「{query}」")
        if total > 1:
            shown = ", ".join(f"{r['name']}({r['dept']})" for r in rows)
            more = f" 等 {total} 条" if total > len(rows) else ""
            raise AmbiguousCandidate(
                f"「{query}」在客户端里搜出多条: {shown}{more}。"
                "回车只会选中第一条,请写得更精确(全名/工号/手机号)。"
            )

        _pick_first_result()
        got = _await_selected_count(expect_count)
        if got == expect_count:
            return {"query": query, "name": rows[0]["name"], "dept": rows[0]["dept"]}
        if got > expect_count:
            # 多选了人比少选危险得多 —— 绝不能靠再点几下去凑,直接停
            raise LocalGroupError(
                f"选中「{query}」后已选人数是 {got},超过预期 {expect_count} —— "
                "可能多选了人,已中止,没有建群。"
            )
        log.warning(
            "选中「%s」第 %d/%d 次没生效(已选 %d,预期 %d),重试",
            query, attempt, SELECT_ATTEMPTS, got, expect_count,
        )

    raise LocalGroupError(
        f"选中「{query}」试了 {SELECT_ATTEMPTS} 次,已选人数仍是 {got}(预期 {expect_count})"
        " —— 已中止,没有建群。客户端可能正被别的窗口抢焦点。"
    )


def _member_from_query(query: str, row: dict | None = None) -> dict:
    if row:
        return {"query": query, "name": row["name"], "dept": row["dept"]}
    return {"query": query, "name": query, "dept": ""}


def _members_from_bulk(
    queries: list[str], auto: list[str], clicked: list[dict],
) -> list[dict]:
    by_query: dict[str, dict] = {}
    for row in clicked:
        q = str(row.get("query") or "")
        if q:
            by_query[q] = _member_from_query(q, row)
    for q in auto:
        by_query.setdefault(q, _member_from_query(q))
    return [by_query.get(q, _member_from_query(q)) for q in queries]


def _diagnose_bulk_mismatch(
    auto: list[str], rest: list[str], got: int, total: int,
) -> NoReturn:
    """人数对不上时逐个搜一遍,让「搜不到/歧义」走原来的错误。"""
    for q in auto + rest:
        hits, rows = _search(q)
        if hits == 0:
            raise LocalGroupError(f"客户端通讯录里搜不到「{q}」")
        if hits > 1:
            shown = ", ".join(f"{r['name']}({r['dept']})" for r in rows)
            more = f" 等 {hits} 条" if hits > len(rows) else ""
            raise AmbiguousCandidate(
                f"「{q}」在客户端里搜出多条: {shown}{more}。"
                "回车只会选中第一条,请写得更精确(全名/工号/手机号)。"
            )
    raise LocalGroupError(
        f"空格一次选人后已选人数是 {got},预期中文全名 {len(auto)} / 全部 {total} —— "
        "可能多选或少选了人,已中止,没有建群。请把名字写得更精确后重试。"
    )


def _select_members(queries: list[str]) -> list[dict]:
    """空格一次填入全部名字,顺序不改。中文全名自动勾上;弹框里未勾的英文点选。

    一个都没勾上且点选也没涨 → 退回逐个搜索+回车。
    """
    if len(queries) == 1:
        return [_select_member(queries[0], 1)]

    auto = [q for q in queries if _looks_like_chinese_fullname(q)]
    rest = [q for q in queries if q not in auto]
    _set_search_text(_bulk_search_text(queries))
    rows = _await_picker_rows(len(queries))
    target = len(auto) if auto else 0
    got = _await_selected_count(target) if target else _selected_count()

    if got > len(queries):
        raise LocalGroupError(
            f"空格一次选人后已选人数是 {got},超过预期 {len(queries)} —— "
            "可能多选了人,已中止,没有建群。"
        )
    if got == len(queries):
        log.info("拉群空格一次选中全部 %d 人", got)
        return [_member_from_query(q) for q in queries]

    names = [str(r.get("name") or "") for r in rows]
    clicked: list[dict] = []
    if rest and rows:
        targets = _english_candidate_rows(rows, rest)
        if targets:
            log.info(
                "拉群空格一次选中 %d 人,点选未勾英文联系人 %s",
                got, [t.get("name") for t in targets],
            )
            clicked = _click_english_candidates(targets, expect_total=len(queries))
            got = _selected_count()
            if got == len(queries):
                return _members_from_bulk(queries, auto, clicked)
            if got > len(queries):
                raise LocalGroupError(
                    f"点选后已选人数是 {got},超过预期 {len(queries)} —— "
                    "可能多选了人,已中止,没有建群。"
                )
            raise LocalGroupError(
                f"点选未勾英文联系人后已选 {got}/{len(queries)},看到的结果是 {names} —— "
                "已中止,没有建群。"
            )

    if got == 0:
        log.info("拉群空格一次选人未生效,改走逐个搜索")
        return [_select_member(q, i + 1) for i, q in enumerate(queries)]

    _diagnose_bulk_mismatch(auto, rest, got, len(queries))


def _wait_sheet_closed(timeout: float = 2.0) -> None:
    """按「完成」后面板应自行关掉。轮询,别固定睡 2 秒。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _sheet_open():
            return
        time.sleep(0.15)
    _close_picker()
    raise LocalGroupError("按了「完成」但面板还在,群可能没建成")


def _confirm() -> None:
    """按「完成」建群。"""
    _osa(_PICKER + _button("Done|完成", pick_rightmost=True) + '''
  if target is missing value then error "找不到「完成」按钮"
  if (enabled of target) is false then error "「完成」按钮是灰的,一个人都没选中"
  perform action "AXPress" of target
''')


def calibrate_ui() -> dict:
    """无副作用验证客户端 UI,并校准本机的 "+" 坐标。

    打开「发起群聊」面板后立即取消,不选择联系人、不创建群。能完成这一步说明:
    企业微信正在运行且有主窗口、服务宿主拿到了辅助功能/自动化权限,并且当前
    窗口布局的 "+" 偏移已经验证并记入 ui_offsets.json。
    """
    ensure_macos()
    with ui_session("启动 UI 预检"):
        _activate()
        _open_picker()
        _close_picker()
        if _sheet_open():
            raise LocalGroupError(
                "启动 UI 预检打开了「发起群聊」面板,但取消后面板仍未关闭。"
                "请手动关闭该面板后重启服务。"
            )
    point = ui_offsets.primary("plus")
    return {"success": True, "plus_offset": f"{point[0]},{point[1]}"}


# ─── 公开 API ──────────────────────────────────────────────────────────

def create_group(
    members: list[str], name: str = "", return_rid: bool = False, nickname: str = ""
) -> dict:
    """以本人身份拉群。你自己自动是群主,不用写进 members。

    ``return_rid`` 用于需要把新群关联回业务单据的调用方。客户端建群
    成功后不会直接返回 RID,需要用建群前后的会话差集定位。

    Args:
        members: 除自己以外的成员,按客户端通讯录搜索(内部同事、外部联系人、微信都行)
        name: 群名。选人面板里没有命名输入框,所以是「先建群、再改名」两步;
              改名失败不会回滚群 —— 群已经建出来了,回滚反而更吓人。
        return_rid: 是否定位并返回新群的本地会话 RID。
        nickname: 我在该群中的昵称。建群确认后当前窗口就是新群,先改界面再等 RID。

    Returns:
        {"success": bool, "members": [...], "member_count": int, "rid": ..., "error": ...}
    """
    ensure_macos()

    seen: set[str] = set()
    wanted: list[str] = []
    for m in members:
        q = (m or "").strip()
        if q and q not in seen:
            seen.add(q)
            wanted.append(q)

    wanted = _without_self(wanted)

    if len(wanted) < 2:
        return {
            "success": False,
            "error": f"除自己外至少要 2 个人才能建群,当前 {len(wanted)} 个: {wanted}",
        }
    if len(wanted) > MAX_MEMBERS:
        return {"success": False, "error": f"一次最多 {MAX_MEMBERS} 人,当前 {len(wanted)} 个"}

    wanted_name = ""
    if name and name.strip():
        wanted_name = _check_name(name, "群名")
    wanted_nickname = ""
    if nickname and nickname.strip():
        wanted_nickname = _check_name(nickname, "群昵称")

    # 确认后客户端已停在新群上。改名/改昵称只看当前窗口,不等 RID。
    # 等 RID 放到最后: 改界面这几秒里 Info.db 几乎已经写出新会话。
    #
    # 快照仍要,用来最后定位 RID。锁只管本机并发; 别人这会儿把你拉进群
    # 时差集可能不唯一,但当前窗口仍是刚建的群,改名不该因此放弃。
    t0 = time.monotonic()

    def _phase(label: str, started: float) -> None:
        log.info(
            "拉群 %s %.1fs (累计 %.1fs)",
            label, time.monotonic() - started, time.monotonic() - t0,
        )

    with ui_session("拉群"):
        t = time.monotonic()
        before = set(_group_names()) if (wanted_name or wanted_nickname or return_rid) else set()
        _phase("snapshot", t)

        t = time.monotonic()
        _activate()
        _open_picker()
        _phase("open_picker", t)
        try:
            t = time.monotonic()
            picked = _select_members(wanted)
            _phase("select", t)
            t = time.monotonic()
            _confirm()
            _wait_sheet_closed()
            _phase("confirm", t)
        except LocalGroupError:
            _close_picker()  # 别把面板留在用户屏幕上
            raise

        result = {
            "success": True,
            "members": picked,
            "member_count": len(picked) + 1,  # +1 是自己
            "rid": None,
            "name": None,
            "error": None,
        }
        if not wanted_name and not wanted_nickname and not return_rid:
            log.info("拉群完成 %.1fs", time.monotonic() - t0)
            return result

        expected = [p["name"] for p in picked]
        # 确认后先等标题切到新群,再改界面。RID 放到最后再查:改名/改昵称
        # 这几秒里 Info.db 几乎已经写出新会话,等 RID 多半变成一次查库。
        t = time.monotonic()
        if not _await_new_group_title(expected):
            what = "群名" if wanted_name else "昵称"
            result["name_error" if wanted_name else "nickname_error"] = (
                f"群建好了,但当前窗口标题还没切到新群,{what}没改成。请手动修改。"
            )
            rid = _unique_new_rid(before, prefer_name=None)
            _phase("wait_rid", t)
            if rid is not None:
                result["rid"] = rid
            else:
                result["rid_error"] = "群建好了,但等待本地会话落库后仍没能唯一定位新建的会话"
            log.info("拉群完成 %.1fs (标题未切到新群)", time.monotonic() - t0)
            return result
        _phase("title", t)
        expect_title = None
        if wanted_name:
            t = time.monotonic()
            renamed = _rename_locked(
                None, wanted_name, expect_members=expected, already_open=True,
            )
            _phase("rename", t)
            if renamed.get("success"):
                result["name"] = wanted_name
                expect_title = wanted_name
            else:
                result["name_error"] = f"群建好了,但改名失败: {renamed.get('error')}"
                if renamed.get("unrelated"):
                    t = time.monotonic()
                    result["rid"] = _unique_new_rid(before, prefer_name=None)
                    _phase("wait_rid", t)
                    log.info("拉群完成 %.1fs (改名拒绝)", time.monotonic() - t0)
                    return result

        if wanted_nickname:
            t = time.monotonic()
            nicked = _set_nickname_locked(
                None,
                wanted_nickname,
                expect_members=None if expect_title else expected,
                expect_title=expect_title,
                already_open=True,
            )
            _phase("nickname", t)
            if nicked.get("success"):
                result["nickname"] = wanted_nickname
            else:
                result["nickname_error"] = f"群建好了,但设置我在本群的昵称失败: {nicked.get('error')}"

        t = time.monotonic()
        rid = _unique_new_rid(before, prefer_name=expect_title)
        _phase("wait_rid", t)
        if rid is not None:
            result["rid"] = rid
        else:
            locate_err = "等待本地会话落库后仍没能唯一定位新建的会话"
            result["rid_error"] = f"群建好了,但{locate_err}"
            log.info("拉群完成 %.1fs (未定位 RID)", time.monotonic() - t0)
            return result
        log.info("拉群完成 %.1fs", time.monotonic() - t0)
        return result


# ─── 改群名 ────────────────────────────────────────────────────────────

_SENTINEL = "\x00wecom-svr-clipboard-probe\x00"
_AX_TITLE_MISSING = "__WECOM_SVR_AX_TITLE_MISSING__"


# 新版客户端把聊天区标题暴露成可写 AXTextField。标题框和左侧搜索框同属内容
# splitter group,用聊天区 splitter 的左边界区分: 标题框在聊天区内,搜索框在
# 会话列表内。读和写分成两步是为了「先看清是哪个会话,再决定改不改」——
# 建群后认新群靠的就是改名前读回来的这个标题。
_TITLE_FIELD = _MAIN_WINDOW + '''
  set contentSplit to splitter group 1 of (splitter group 1 of mainWin)
  set chatSplit to splitter group 1 of contentSplit
  set chatPos to position of chatSplit
  set chatX to item 1 of chatPos
  set target to missing value
  repeat with tf in (text fields of contentSplit)
    set tp to position of tf
    set ts to size of tf
    if (item 1 of tp) ≥ chatX and (item 1 of ts) > 40 then
      set target to contents of tf
      exit repeat
    end if
  end repeat
'''


def _read_ax_title() -> str | None:
    """读当前会话标题。旧版客户端没有这个可写控件时返回 None(走坐标兼容路径)。

    注意和「标题是空串」区分开: 刚建出来的群标题可能就是空的,那是合法值。
    """
    out = _osa(_TITLE_FIELD + f'''
  if target is missing value then return "{_AX_TITLE_MISSING}"
  return value of target as text
''')
    value = out.strip().strip('"')
    return None if value == _AX_TITLE_MISSING else value


def _write_ax_title(new_name: str) -> None:
    """把新群名写进标题 AXTextField 并确认。"""
    out = _osa(_TITLE_FIELD + f'''
  if target is missing value then return "{_AX_TITLE_MISSING}"
  set value of attribute "AXFocused" of target to true
  set value of target to "{_escape(new_name)}"
  delay 0.12
  try
    perform action "AXConfirm" of target
  on error
    keystroke return
  end try
  return "ok"
''')
    if out.strip().strip('"') == _AX_TITLE_MISSING:
        raise LocalGroupError("刚才还读到的会话标题不见了(界面被切走?),改名已中止")


def _await_ax_title(expected: str, timeout: float = 1.2) -> bool:
    """改名写完后轮询 AX 标题,替代固定 sleep。"""
    deadline = time.monotonic() + timeout
    for _ in range(12):
        current = _read_ax_title()
        if current is not None and current.strip() == expected.strip():
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.12)
    return False




def _name_variants(name: str) -> set[str]:
    """一个人在客户端里的几种写法。

    选人面板给的是「陈涛(水淼淼)」这种带备注/别名的整串,而群标题里只会出现其中
    一种。拆开括号,哪一种命中都算数。
    """
    raw = name.strip()
    parts = (p.strip() for p in re.split(r"[()()\[\]【】]", raw))
    return {raw} | {p for p in parts if p}


def _looks_like_a_just_created_group(title: str, member_names: list[str]) -> bool:
    """标题是不是「客户端拿这几个人的名字现拼出来的」。

    这是建群流程认新群的**唯一实质证据**: 新群在库里没有名字,客户端显示的标题
    是成员名用「、」拼的。远端在这期间把你拉进的群,要么带着它自己的名字,要么
    拼的是别人的成员名单 —— 两种都过不了这一关。

    判据是「每个选中的人都出现在标题里」而不是「标题的每一段都认识」: 有的版本会
    把自己也拼进去,而自己的显示名这里拿不到。方向上宁可错杀 —— 认不出来只是不改
    名(带着说明让人手动改),认错了却是把名字改到别人的群上。
    """
    text = (title or "").strip()
    if not text:
        return True                                   # 有的版本干脆不给标题
    text = re.sub(r"[(\uff08]\s*\d+\s*[)\uff09]\s*$", "", text).strip()   # 去掉「(5)」这种人数后缀
    wanted = [_name_variants(n) for n in member_names if n and n.strip()]
    if not wanted:
        return False

    if not text.endswith(("…", "...")):
        return all(any(v in text for v in variants) for variants in wanted)

    # 人多时标题会被截断,只能核对露出来的部分: 每一段都得是这几个人之一
    # (末段可能只露了半个名字,按前缀算)。陌生人一冒头就判负。
    known = {v for variants in wanted for v in variants}
    segments = [s.strip() for s in re.split(r"[、,,]", text.rstrip(".…").strip()) if s.strip()]
    return bool(segments) and all(
        any(name.startswith(seg) for name in known) for seg in segments
    )


def _window_point(dx: int, dy: int) -> tuple[int, int]:
    """把「相对主窗口左上角的偏移」换算成屏幕坐标。"""
    out = _osa(_MAIN_WINDOW + f'''
  set p to position of mainWin
  return ((item 1 of p) + {dx} as string) & "," & ((item 2 of p) + {dy} as string)
''')
    x, _, y = out.strip().strip('"').partition(",")
    return int(x), int(y)


def _probe_title(dx: int, dy: int) -> str:
    """点一次标题候选位置,把输入框里的当前群名读回来;不对劲就抛错。

    读回来这一步是**安全闸门**,不是锦上添花: 客户端是和人共用的,请求发出到点击
    之间会话完全可能被切走。用哨兵值占住剪贴板再全选复制 —— 剪贴板没变就说明压根
    没有可编辑区域拿到焦点,这时候要是硬往下走,Cmd+V + 回车会把群名当成一条消息
    发进别人的聊天里。

    这道闸同时也是校准的成功信号: 只做「选中 + 复制」,不打字、不回车,所以一次
    没点中的试探什么都改不了。
    """
    _set_clipboard(_SENTINEL)
    _real_click(*_window_point(dx, dy))
    _osa('''
  keystroke "a" using command down
  delay 0.2
  keystroke "c" using command down
  delay 0.4
''')
    time.sleep(0.4)
    got = _get_clipboard()

    if got == _SENTINEL:
        raise LocalGroupError(f"点 {dx, dy} 后没有输入框拿到焦点")
    text = got.strip()
    if not text or "\ufffd" in text or "\n" in text or len(text) > MAX_NAME_LEN * 2:
        raise LocalGroupError(f"点 {dx, dy} 复制回来的内容不像群名(可能选中的是聊天记录)")
    return text


def _open_edit_box_and_read_current() -> str:
    """点标题打开改名输入框,并把里面的当前群名读回来。

    点不中就换候选偏移再试(见 ui_offsets.py)。候选只在标题带里横向挪一点点:
    再往右就是通话/更多那几个**有副作用**的按钮,那不是能拿来试探的地方。
    """
    reasons: list[str] = []
    for dx, dy in ui_offsets.candidates("title"):
        try:
            text = _probe_title(dx, dy)
        except LocalGroupError as e:
            reasons.append(str(e))
            continue
        ui_offsets.remember("title", (dx, dy))
        return text

    raise LocalGroupError(
        "点了会话标题但没能进入改名输入框,已中止(没有改动任何东西): "
        + ";".join(reasons)
        + f"。别的窗口抢焦点时会这样,重试一次即可;一直这样就是布局变了 —— "
        f"{ui_offsets.describe('title')}"
    )


def _group_names() -> dict[int, str]:
    """当前(含 WAL 未落盘部分)的群会话 RID → 群名。

    要 include_unnamed: **刚建出来的群名字正是空的**(客户端按成员现拼出显示名,
    库里不存),不带上就永远认不出新群。limit=None 是因为改名后还要回库核验任意
    一个老群,不能只看最近 200 个。

    refresh() 不加 force: 它已经按 Info.db 的 mtime 和 WAL 签名判断要不要重来,
    而「新群落库了没」问的正是这个。force 会每次都全量重解密(实测 0.7s CPU +
    464MB 写盘),一次带名字的建群要走三遍。
    """
    messages.refresh()
    return {g["rid"]: g["name"] for g in messages.list_groups(include_unnamed=True, limit=None)}


def _await_new_group_title(member_names: list[str], timeout: float = TITLE_SWITCH_TIMEOUT) -> bool:
    """确认后等当前窗口标题变成刚建的群(成员拼名)。

    面板关掉不等于焦点已经切走。空标题也不能算数: 切会话的间隙标题经常是空的,
    上一轮的群还开着时立刻改名会改到旧群上。
    """
    deadline = time.monotonic() + timeout
    for _ in range(40):
        title = _read_ax_title()
        if title and title.strip() and _looks_like_a_just_created_group(title, member_names):
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(TITLE_SWITCH_POLL)
    title = _read_ax_title()
    return bool(title and title.strip() and _looks_like_a_just_created_group(title, member_names))


def _unique_new_rid(before: set[int], prefer_name: str | None = None) -> int | None:
    """改名/改昵称之后再认 RID。界面已经耗了几秒,库多半写好了。

    差集只有一个就认它;改过名时优先用新名字在差集里筛。筛不唯一就放弃,
    不要猜 —— 把别人刚拉你进的群 RID 交回去比不交更糟。
    """
    deadline = time.monotonic() + GROUP_APPEAR_TIMEOUT
    poll = GROUP_APPEAR_POLL
    while True:
        names = _group_names()
        new_rids = sorted(set(names) - before)
        if prefer_name:
            named = [r for r in new_rids if names.get(r) == prefer_name]
            if len(named) == 1:
                return named[0]
            if len(named) > 1:
                return None
        if len(new_rids) == 1:
            return new_rids[0]
        if len(new_rids) > 1 and not prefer_name:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        poll = min(poll * 2, GROUP_APPEAR_POLL_MAX)


def _cancel_edit_box() -> None:
    """把已经打开、但还没打字的改名输入框原样退掉。"""
    try:
        _osa("  key code 53\n")
    except LocalGroupError as e:
        log.warning("退出改名输入框失败,客户端上可能留着一个打开的输入框: %s", e)


def _rid_label(rid: int | None) -> str:
    return f"RID={rid}" if rid is not None else "当前会话"


def _rename_locked(
    rid: int | None,
    new_name: str,
    expect_members: list[str] | None = None,
    already_open: bool = False,
) -> dict:
    """改名的界面部分 —— **调用方必须已经持有 ui_session**(锁不可重入)。

    expect_members 给了值就先核对归属: 打开的这个会话的标题必须是这些成员名拼出来
    的,否则一个键都不敲就返回(带 unrelated 标记)。建群刚完成时窗口就是新群,
    用它挡住「确认后焦点跑到了别的会话」。

    already_open=True 表示当前会话已经是这个群(建群刚完成),跳过 activate、
    scheme 打开,也不查本地库 —— 这时 RID 经常还没落库。
    """
    conv = None
    if not already_open:
        if rid is None:
            return {"success": False, "error": "改名需要会话 RID"}
        conv = local_send.get_conversation_by_rid(int(rid))
        if not conv:
            return {"success": False, "error": f"本地库里没有 RID={rid} 的会话"}

    old_clipboard = ""
    try:
        old_clipboard = _get_clipboard()
    except Exception:
        pass
    try:
        if not already_open:
            assert conv is not None
            _activate()
            ok, msg = local_send._open_conversation_by_scheme(conv)
            if not ok:
                raise LocalGroupError(f"打开会话失败: {msg}")

        ax_title = _read_ax_title()
        via_ax = ax_title is not None
        # 旧版没有可写标题控件: 点开改名输入框把当前群名读回来。这一步只做
        # 「选中 + 复制」,还没有任何改动落地,所以下面的归属核对仍然来得及。
        was = ax_title if ax_title is not None else _open_edit_box_and_read_current()

        if expect_members is not None and not _looks_like_a_just_created_group(was, expect_members):
            if not via_ax:
                _cancel_edit_box()
            return {
                "success": False,
                "unrelated": True,
                "rid": rid,
                "old_name": was,
                "error": (
                    f"{_rid_label(rid)} 的标题是 {was!r},不像刚用「{'、'.join(expect_members)}」"
                    "建出来的新群(这期间可能有别人把你拉进了新群)—— "
                    "一个字都没改,请手动改名。"
                ),
            }

        if via_ax:
            attempts = 3 if already_open else 1
            ax_ok = False
            for i in range(attempts):
                _write_ax_title(new_name)
                ax_ok = _await_ax_title(new_name, timeout=1.2 if already_open else 0.8)
                if ax_ok:
                    break
                log.info("改名第 %d 次写入后标题仍是 %r,再试", i + 1, _read_ax_title())
        else:
            _set_clipboard(new_name)
            _osa('''
  keystroke "a" using command down
  delay 0.12
  keystroke "v" using command down
  delay 0.15
  keystroke return
''')
            ax_ok = _await_ax_title(new_name, timeout=1.5 if rid is None else 0.8)
    finally:
        if old_clipboard:
            try:
                _set_clipboard(old_clipboard)
            except Exception:
                pass

    # 回库核验只问一次:全量重解密不便宜,刚改完库里经常还是空名。
    # AX 标题已经对上就按成功走,除非这个新名字落到了另一个 RID 上。
    # already_open 且还没有 RID 时只信 AX,库名可能还没跟上。
    names = _group_names() if rid is not None else {}
    if rid is not None:
        actual = names.get(int(rid))
        if actual == new_name:
            return {"success": True, "rid": int(rid), "name": new_name, "old_name": was, "error": None}
        wrong = [r for r, n in names.items() if n == new_name and r != int(rid)]
        if wrong:
            return {
                "success": False,
                "rid": int(rid),
                "error": (
                    f"改名后核验失败: RID={rid} 的群名仍是 {actual!r},不是 {new_name!r}"
                    f" —— 名字落到了 RID={wrong[0]} 上,请手动改回去"
                ),
            }
    else:
        actual = None
    if ax_ok:
        if rid is None:
            log.info("改名 AX 已是 %r,还没有 RID,按成功继续", new_name)
        elif actual != new_name:
            log.info("改名 AX 已是 %r,本地库还是 %r,按成功继续", new_name, actual)
        return {"success": True, "rid": rid, "name": new_name, "old_name": was, "error": None}
    title_now = _read_ax_title()
    if (title_now or "").strip() == new_name.strip():
        return {"success": True, "rid": rid, "name": new_name, "old_name": was, "error": None}
    seen = title_now if rid is None else actual
    return {
        "success": False,
        "rid": rid,
        "error": (
            f"改名后核验失败: {_rid_label(rid)} 的群名仍是 {seen!r},不是 {new_name!r}"
        ),
    }




def _dump_ui_hints() -> str:
    """浅扫所有窗口和 sheet 的控件提示。群详情是独立对话框,不能只看主窗口。

    不用 entire contents:主窗口那棵 CEF 树又大又慢,还容易把 role 这类保留字踩爆。
    坐标必须在 process tell 里当场读,丢给外面的 handler 会变成 0,0。
    """
    ok, out = _run_applescript(
        '''
tell application "System Events" to tell process "企业微信"
  set acc to {}
  repeat with w in windows
    set winW to 9999
    try
      set winW to item 1 of (size of w)
    end try
    set maxDepth to 3
    if winW < 600 then set maxDepth to 5
    try
      repeat with e1 in (UI elements of w)
        set end of acc to my lineOf(e1)
        if maxDepth ≥ 2 then
          try
            repeat with e2 in (UI elements of e1)
              set end of acc to my lineOf(e2)
              if maxDepth ≥ 3 then
                try
                  repeat with e3 in (UI elements of e2)
                    set end of acc to my lineOf(e3)
                    if maxDepth ≥ 4 then
                      try
                        repeat with e4 in (UI elements of e3)
                          set end of acc to my lineOf(e4)
                          if maxDepth ≥ 5 then
                            try
                              repeat with e5 in (UI elements of e4)
                                set end of acc to my lineOf(e5)
                              end repeat
                            end try
                          end if
                        end repeat
                      end try
                    end if
                  end repeat
                end try
              end if
            end repeat
          end try
        end if
      end repeat
    end try
    try
      repeat with sh in (sheets of w)
        try
          repeat with e1 in (UI elements of sh)
            set end of acc to my lineOf(e1)
            try
              repeat with e2 in (UI elements of e1)
                set end of acc to my lineOf(e2)
                try
                  repeat with e3 in (UI elements of e2)
                    set end of acc to my lineOf(e3)
                    try
                      repeat with e4 in (UI elements of e3)
                        set end of acc to my lineOf(e4)
                      end repeat
                    end try
                  end repeat
                end try
              end repeat
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  set AppleScript's text item delimiters to linefeed
  return acc as text
end tell

on lineOf(theElem)
  tell application "System Events"
    set elemRole to ""
    try
      set elemRole to role of theElem as text
    end try
    set hintText to ""
    try
      set hintText to hintText & (description of theElem as text)
    end try
    try
      set hintText to hintText & "/" & (name of theElem as text)
    end try
    try
      set hintText to hintText & "/" & (value of theElem as text)
    end try
    set posX to 0
    set posY to 0
    try
      set posX to item 1 of (position of theElem)
      set posY to item 2 of (position of theElem)
    end try
    return elemRole & tab & hintText & tab & (posX as string) & "," & (posY as string)
  end tell
end lineOf
''',
        timeout=25,
    )
    if not ok:
        raise LocalGroupError(f"扫描群详情控件失败: {out}")
    return out


def _parse_field_line(out: str | None) -> dict | None:
    """解 `role \t value \t x,y` 这行 AppleScript 回包。"NONE"/空 → None。

    这个行格式是几个 AX 探测脚本共用的,解析只写一份 —— 加字段或换分隔符时
    不至于只改到其中一个。role 不是文本框就当作「找到了那一行」(ROW)。
    """
    raw = (out or "").strip().strip('"')
    if not raw or raw == "NONE":
        return None
    role, sep, rest = raw.partition("\t")
    if not sep:
        return None
    value, _, pos = rest.partition("\t")
    x_s, _, y_s = pos.partition(",")
    try:
        x, y = int(x_s), int(y_s)
    except ValueError:
        x, y = 0, 0
    kind = "FIELD" if role in {"AXTextField", "AXTextArea"} else "ROW"
    return {"kind": kind, "value": value, "x": x, "y": y, "role": role}


def _nickname_dialog_field() -> dict | None:
    """找独立的「我在本群的昵称」小窗里的输入框。

    点群详情里那一行后,企微会再弹出这个 360x190 的对话框,带确定/取消。
    """
    out = _osa('''
  repeat with w in windows
    set s to size of w
    -- 昵称小窗约 360x190;跳过主窗口和 320x699 的群详情
    if (item 1 of s) ≥ 300 and (item 1 of s) ≤ 420 and (item 2 of s) ≥ 140 and (item 2 of s) ≤ 260 then
      set fieldRole to ""
      set fieldVal to ""
      set fieldPos to "0,0"
      set hit to false
      try
        repeat with e in (UI elements of w)
          set hintText to ""
          try
            set hintText to hintText & (value of e as text)
          end try
          try
            set hintText to hintText & (name of e as text)
          end try
          if hintText contains "我在本群的昵称" or hintText contains "My Alias" then set hit to true
          set elemRole to ""
          try
            set elemRole to role of e as text
          end try
          if elemRole is "AXTextField" or elemRole is "AXTextArea" then
            set fieldRole to elemRole
            try
              set fieldVal to value of e as text
            end try
            try
              set p to position of e
              set fieldPos to (item 1 of p as string) & "," & (item 2 of p as string)
            end try
          end if
        end repeat
      end try
      if hit then
        if fieldRole is not "" then return fieldRole & tab & fieldVal & tab & fieldPos
        return "ROW\t\t0,0"
      end if
    end if
  end repeat
  return "NONE"
''')
    return _parse_field_line(out)


def _await_nickname_dialog(timeout: float = 1.2) -> dict | None:
    deadline = time.monotonic() + timeout
    last = None
    for _ in range(8):
        last = _nickname_dialog_field()
        if last is not None:
            return last
        if time.monotonic() >= deadline:
            break
        time.sleep(0.12)
    return last


def _await_nickname_dialog_gone(timeout: float = 1.0) -> dict | None:
    """写完确定后等小窗关掉。超时仍在就返回当前字段,给调用方报错。"""
    deadline = time.monotonic() + timeout
    last = _nickname_dialog_field()
    for _ in range(6):
        last = _nickname_dialog_field()
        if last is None:
            return None
        if time.monotonic() >= deadline:
            break
        time.sleep(0.12)
    return last


def _scan_nickname_ui() -> dict:
    """在所有窗口里找「我在本群的昵称」控件。

    返回 {kind, value, x, y}:
      FIELD —— 已经是可写输入框
      ROW   —— 还是一行静态文字,要点一下才会变成输入框
      NONE  —— 详情面板大概还没打开

    文字嵌在群详情表格单元格里,顶层元素读不到。按行文字定位,不要全树 AX 扫描。
    """
    dialog = _nickname_dialog_field()
    if dialog is not None:
        return dialog
    row = _group_info_labeled_row(("我在本群的昵称", "My Alias"))
    if row is not None:
        x, y, w, h = row
        return {"kind": "ROW", "value": "我在本群的昵称", "x": x + w - 40, "y": y + h // 2}
    return {"kind": "NONE", "value": "", "x": 0, "y": 0}


def _front_window_mentions(text: str) -> bool:
    """群详情里昵称和标签常常是两个控件,不能只盯着带「昵称」字样的那一行。"""
    if not text:
        return False
    blob = _dump_ui_hints() or ""
    return text in blob


def _header_action_button_points() -> list[tuple[int, int]]:
    """聊天区标题栏右侧那排约 28x28 的按钮,按从左到右排序。

    实测是: 更多 / 智能总结 / 添加成员。点最右边会弹出加人面板。
    """
    out = _osa(_MAIN_WINDOW + '''
  set contentSplit to splitter group 1 of (splitter group 1 of mainWin)
  set hits to {}
  repeat with b in (buttons of contentSplit)
    set p to position of b
    set s to size of b
    if (item 1 of s) ≥ 26 and (item 1 of s) ≤ 30 and (item 2 of s) ≥ 26 and (item 2 of s) ≤ 30 then
      set hx to ((item 1 of p) + (item 1 of s) / 2) as integer as string
      set hy to ((item 2 of p) + (item 2 of s) / 2) as integer as string
      set end of hits to hx & "," & hy
    end if
  end repeat
  set AppleScript's text item delimiters to linefeed
  return hits as text
''')
    points: list[tuple[int, int]] = []
    for line in (out or "").splitlines():
        x_s, _, y_s = line.strip().strip('"').partition(",")
        try:
            points.append((int(x_s), int(y_s)))
        except ValueError:
            continue
    points.sort(key=lambda p: p[0])
    return points


def _try_press_named_menu_item(names: tuple[str, ...]) -> bool:
    quoted = ", ".join(f'"{_escape(n)}"' for n in names)
    out = _osa(f'''
  set wanted to {{{quoted}}}
  repeat with m in menus
    repeat with mi in menu items of m
      set n to ""
      try
        set n to name of mi as text
      end try
      if n is in wanted then
        perform action "AXPress" of mi
        return "OK"
      end if
    end repeat
  end repeat
  return "NONE"
''')
    return out.strip().strip('"') == "OK"


def _group_info_dialog_open() -> bool:
    """群详情是独立的约 320 宽对话框,不是主窗口里的侧栏。"""
    out = _osa('''
  repeat with w in windows
    set s to size of w
    if (item 1 of s) ≥ 280 and (item 1 of s) ≤ 360 and (item 2 of s) ≥ 500 then
      return "YES"
    end if
  end repeat
  return "NO"
''')
    return out.strip().strip('"') == "YES"


def _nickname_ui_ready() -> bool:
    """群详情是否已经打开。先问窗口尺寸,别每次都扫表格。"""
    return _group_info_dialog_open() or _nickname_dialog_field() is not None


def _close_stray_windows() -> None:
    """关掉挡在主窗口前面的无关弹窗(智能总结/群管理/二维码/改名框)。

    上一轮失败会把这些窗口留着;下一次再点「更多」时,front window 就不是主窗口,
    「更多」按钮会点到名片卡上。群详情(约 320x500+)先留着。
    关不掉只记日志:改昵称本身不该被清场失败拖死。
    """
    try:
        _osa('''
  if (count of windows) ≤ 1 then return
  set mainArea to -1
  repeat with w in windows
    set s to size of w
    set a to (item 1 of s) * (item 2 of s)
    if a > mainArea then set mainArea to a
  end repeat
  repeat 3 times
    set closedOne to false
    repeat with w in windows
      set s to size of w
      set a to (item 1 of s) * (item 2 of s)
      if a < mainArea then
        set keep to false
        if (item 1 of s) ≥ 280 and (item 1 of s) ≤ 360 and (item 2 of s) ≥ 500 then set keep to true
        if keep is false then
          try
            click (first button of w whose description is "关闭按钮")
            set closedOne to true
            delay 0.2
            exit repeat
          end try
          try
            click (first button of w whose name is "取消")
            set closedOne to true
            delay 0.2
            exit repeat
          end try
        end if
      end if
    end repeat
    if closedOne is false then exit repeat
  end repeat
''')
    except LocalGroupError as e:
        log.warning("清理残留弹窗失败,继续改昵称: %s", e)


def _header_more_click_point() -> tuple[int, int] | None:
    """标题栏「更多」的点击位置。

    侧栏打开后这个按钮会从 AX 里消失,剩下智能总结/添加成员。这时取最左按钮再往左一格。
    """
    points = _header_action_button_points()
    if not points:
        return None
    # 3 个时最左是「…」;侧栏开着只剩 2 个,最左那个仍是「…」(再往左偏会点空)
    return points[0]


def _open_group_info_panel(*, skip_stray_close: bool = False) -> None:
    """打开群详情。成功信号是扫到昵称行,或出现约 320 宽的详情对话框。

    不能点会话标题:那是改群名的入口,会把昵称写进群名。也不能点最右按钮:
    那是添加成员。入口是标题栏右侧那排里最左边的「更多」。
    skip_stray_close=True 给建群刚完成用:这时只有主窗口,关残留窗口是空转。
    """
    if _group_info_dialog_open() or _nickname_dialog_field() is not None:
        return
    if not skip_stray_close:
        _close_stray_windows()
        if _group_info_dialog_open() or _nickname_dialog_field() is not None:
            return

    more = _header_more_click_point()
    if more is None:
        raise LocalGroupError(
            "找不到聊天区标题栏的「更多」按钮,无法打开群详情改昵称"
        )
    _ui_click(*more)
    deadline = time.monotonic() + 1.6
    for _ in range(14):
        if _group_info_dialog_open() or _nickname_dialog_field() is not None:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.12)
    if _try_press_named_menu_item(("群聊信息", "Chat Info", "群信息")):
        deadline = time.monotonic() + 0.8
        for _ in range(8):
            if _nickname_ui_ready():
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.12)
    raise LocalGroupError(
        f"点了标题栏「更多」({more[0]},{more[1]}) 后仍找不到群详情。"
        "请确认会话已打开,且右上角没有挡住「更多」的面板。"
    )


def _focused_text_field() -> dict | None:
    out = _osa('''
  set fe to missing value
  try
    set fe to value of attribute "AXFocusedUIElement"
  end try
  if fe is missing value then return "NONE"
  set r to ""
  try
    set r to role of fe as text
  end try
  if r is not "AXTextField" and r is not "AXTextArea" then return "NONE"
  set v to ""
  try
    set v to value of fe as text
  end try
  set p to position of fe
  return r & tab & v & tab & (item 1 of p as string) & "," & (item 2 of p as string)
''')
    return _parse_field_line(out)


def _group_info_labeled_row(labels: tuple[str, ...]) -> tuple[int, int, int, int] | None:
    """按文字找群详情里的某一行,返回行矩形。找不到返回 None。

    必须按文字点:上面「群聊名称」也是 40px 行,按高度盲点会先打开改群名对话框,
    然后把真正的昵称行挡住。只读每行第一个静态文字,别把整棵单元格树扫一遍。
    """
    quoted = ", ".join(f'"{_escape(n)}"' for n in labels)
    out = _osa(f'''
  set wanted to {{{quoted}}}
  repeat with w in windows
    set s to size of w
    if (item 1 of s) ≥ 280 and (item 1 of s) ≤ 360 and (item 2 of s) ≥ 500 then
      try
        set tbl to table 1 of scroll area 1 of w
        repeat with rw in rows of tbl
          set sz to size of rw
          if (item 2 of sz) ≥ 30 and (item 2 of sz) ≤ 50 then
            set txt to ""
            try
              set txt to value of static text 1 of UI element 1 of rw as text
            end try
            if txt is "" then
              try
                set txt to value of static text 1 of rw as text
              end try
            end if
            set hit to false
            repeat with needle in wanted
              if txt contains needle then
                set hit to true
                exit repeat
              end if
            end repeat
            if hit then
              set p to position of rw
              set hitLine to (item 1 of p as string) & "," & (item 2 of p as string)
              set hitLine to hitLine & "," & (item 1 of sz as string) & "," & (item 2 of sz as string)
              return hitLine
            end if
          end if
        end repeat
      end try
    end if
  end repeat
  return "NONE"
''')
    raw = (out or "").strip().strip('"')
    if not raw or raw == "NONE":
        return None
    parts = raw.split(",")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        return None


def _probe_nickname_in_dialog() -> dict:
    """打开群详情后点「我在本群的昵称」那一行。

    必须按文字点:「群聊名称」也是 40px 行,按高度从上往下点会先弹出改群名对话框。
    """
    row = _group_info_labeled_row(("我在本群的昵称", "My Alias"))
    if row is None:
        return {"kind": "NONE", "value": "", "x": 0, "y": 0}
    x, y, w, h = row
    cx, cy = x + w - 40, y + h // 2
    _ui_click(cx, cy)
    dialog = _await_nickname_dialog(timeout=1.2)
    if dialog is not None:
        return dialog
    focused = _focused_text_field()
    if focused is not None:
        return {
            "kind": "FIELD",
            "value": focused.get("value") or "",
            "x": focused["x"],
            "y": focused["y"],
        }
    return {"kind": "ROW", "value": "我在本群的昵称", "x": cx, "y": cy}


def _nickname_write_verified(nickname: str, focused_before: dict | None = None) -> bool:
    """CEF 群详情经常不把新昵称暴露成静态文字,所以核验要同时看焦点框和整窗文本。"""
    if _front_window_mentions(nickname):
        return True
    focused = _focused_text_field()
    if focused and nickname in (focused.get("value") or ""):
        if focused_before is None:
            return True
        return focused.get("x") == focused_before.get("x") and focused.get("y") == focused_before.get("y")
    return False


def _write_nickname_dialog(nickname: str) -> None:
    """往「我在本群的昵称」小窗的输入框写入并点确定。"""
    out = _osa(f'''
  set targetWin to missing value
  repeat with w in windows
    set s to size of w
    if (item 1 of s) ≥ 300 and (item 1 of s) ≤ 420 and (item 2 of s) ≥ 140 and (item 2 of s) ≤ 260 then
      try
        repeat with e in (UI elements of w)
          set hintText to ""
          try
            set hintText to hintText & (value of e as text)
          end try
          try
            set hintText to hintText & (name of e as text)
          end try
          if hintText contains "我在本群的昵称" or hintText contains "My Alias" then
            set targetWin to contents of w
            exit repeat
          end if
        end repeat
      end try
    end if
    if targetWin is not missing value then exit repeat
  end repeat
  if targetWin is missing value then return "NODIALOG"
  set field to missing value
  repeat with e in (UI elements of targetWin)
    set elemRole to ""
    try
      set elemRole to role of e as text
    end try
    if elemRole is "AXTextField" or elemRole is "AXTextArea" then
      set field to contents of e
      exit repeat
    end if
  end repeat
  if field is missing value then return "NOFIELD"
  set value of attribute "AXFocused" of field to true
  set value of field to "{_escape(nickname)}"
  delay 0.25
  try
    click (first button of targetWin whose name is "确定")
    return "OK"
  on error
    try
      perform action "AXPress" of (first button of targetWin whose name is "确定")
      return "OK"
    on error
      try
        click (first button of targetWin whose name is "OK")
        return "OK"
      on error
        return "NOCONFIRM"
      end try
    end try
  end try
''')
    status = out.strip().strip('"')
    if status == "OK":
        return
    if status == "NODIALOG":
        raise LocalGroupError("找不到「我在本群的昵称」对话框")
    if status == "NOFIELD":
        raise LocalGroupError("昵称对话框里没有输入框,已中止,没有改动")
    raise LocalGroupError(f"写入群昵称失败: {status}")


def _write_focused_nickname(nickname: str) -> None:
    """把已经拿到焦点的昵称输入框改掉。

    和改群名一样先确认真有输入框:否则 Cmd+V + 回车会把昵称当成一条消息发出去。
    """
    out = _osa(f'''
  set fe to missing value
  try
    set fe to value of attribute "AXFocusedUIElement"
  end try
  if fe is missing value then return "NOFOCUS"
  set r to ""
  try
    set r to role of fe as text
  end try
  if r is not "AXTextField" and r is not "AXTextArea" then return "NOTFIELD"
  set fy to 0
  try
    set fy to item 2 of (position of fe)
  end try
  if fy > 680 then return "CHATBOX"
  set value of attribute "AXFocused" of fe to true
  set value of fe to "{_escape(nickname)}"
  delay 0.3
  try
    perform action "AXConfirm" of fe
  on error
    keystroke return
  end try
  return "OK"
''')
    status = out.strip().strip('"')
    if status == "OK":
        return
    if status in {"NOFOCUS", "NOTFIELD", "CHATBOX"}:
        raise LocalGroupError("点了群昵称但没有输入框拿到焦点,已中止,没有改动")
    raise LocalGroupError(f"写入群昵称失败: {status}")


def _close_group_info() -> None:
    try:
        _osa("  key code 53\n")
    except LocalGroupError as e:
        log.warning("关闭群详情失败,客户端上可能留着侧栏: %s", e)


def _read_current_title() -> tuple[str, bool]:
    """读当前会话标题。返回 (title, via_ax)。

    via_ax=False 表示走了旧版点标题路径,调用方如果决定不改名,必须把输入框退掉。
    """
    ax_title = _read_ax_title()
    if ax_title is not None:
        return ax_title, True
    return _open_edit_box_and_read_current(), False


def _title_matches_expectation(
    title: str,
    expect_members: list[str] | None,
    expect_title: str | None,
) -> bool:
    if expect_title is not None:
        return (title or "").strip() == expect_title.strip()
    if expect_members is not None:
        return _looks_like_a_just_created_group(title, expect_members)
    return True


def _set_nickname_locked(
    rid: int | None,
    nickname: str,
    expect_members: list[str] | None = None,
    expect_title: str | None = None,
    already_open: bool = False,
) -> dict:
    """在群详情里修改「我在本群的昵称」(调用方须持有 UI 锁)。

    企微没有改群内昵称的官方接口,只能走客户端详情页。昵称也不进本地会话库,
    所以核验只能回读 AX,不能像改群名那样查库。

    expect_title 给「同一轮已经改过群名」用:标题这时已经不是成员拼名,
    再拿 expect_members 去对会把刚改好名的新群误判成别人的群。
    already_open=True 跳过 activate / scheme 打开,也不查本地库
    (建群刚完成时已经停在新群上,RID 经常还没落库)。
    """
    if not already_open:
        if rid is None:
            return {"success": False, "error": "改昵称需要会话 RID"}
        conv = local_send.get_conversation_by_rid(int(rid))
        if not conv:
            return {"success": False, "error": f"本地库里没有 RID={rid} 的会话"}
        _activate()
        ok, msg = local_send._open_conversation_by_scheme(conv)
        if not ok:
            return {"success": False, "error": f"打开会话失败: {msg}"}

    opened_info = False

    if expect_members is not None or expect_title is not None:
        was, via_ax = _read_current_title()
        if not _title_matches_expectation(was, expect_members, expect_title):
            if not via_ax:
                _cancel_edit_box()
            expected = expect_title if expect_title is not None else "、".join(expect_members or [])
            return {
                "success": False,
                "unrelated": True,
                "rid": rid,
                "error": (
                    f"{_rid_label(rid)} 的标题是 {was!r},不是预期的 {expected!r} —— "
                    "一个字都没改,请手动修改我在本群的昵称。"
                ),
            }
        if not via_ax:
            _cancel_edit_box()

    try:
        _open_group_info_panel(skip_stray_close=already_open)
        opened_info = True
        found = _scan_nickname_ui()
        if found["kind"] == "NONE":
            found = _probe_nickname_in_dialog()
        if found["kind"] == "NONE":
            return {"success": False, "rid": rid, "error": "群详情中找不到「我在本群的昵称」"}
        if found["kind"] == "ROW" and _nickname_dialog_field() is None:
            _ui_click(int(found["x"]), int(found["y"]))
            _await_nickname_dialog(timeout=1.2)
        if _nickname_dialog_field() is not None:
            _write_nickname_dialog(nickname)
            leftover = _await_nickname_dialog_gone(timeout=0.8)
            current = (leftover or {}).get("value") or ""
            if leftover is None or nickname in current:
                return {"success": True, "rid": rid, "nickname": nickname, "error": None}
            return {
                "success": False,
                "rid": rid,
                "error": f"点了确定但昵称对话框还在,读到的是 {current!r}",
            }
        focused_before = _focused_text_field()
        _write_focused_nickname(nickname)
        time.sleep(0.25)
        if _nickname_write_verified(nickname, focused_before):
            return {"success": True, "rid": rid, "nickname": nickname, "error": None}
        found = _scan_nickname_ui()
        return {
            "success": False,
            "rid": rid,
            "error": (
                f"改昵称后核验失败: 群详情里没有读到 {nickname!r}"
                f"(扫到的是 {found.get('value')!r})"
            ),
        }
    except LocalGroupError as e:
        return {"success": False, "rid": rid, "error": str(e)}
    finally:
        if opened_info:
            _close_group_info()


def _check_name(name: str, kind: str = "群名") -> str:
    new_name = (name or "").strip()
    if not new_name:
        raise LocalGroupError(f"{kind}为空")
    if len(new_name) > MAX_NAME_LEN:
        raise LocalGroupError(f"{kind}最多 {MAX_NAME_LEN} 个字符,当前 {len(new_name)} 个")
    return new_name


def rename_group(rid: int, name: str) -> dict:
    """给一个群改名 —— 打开会话 → 点标题 → 覆写 → 回车,最后回库核验。

    官方 API 改不了这种群(`appchat/update` 只认自己建的 chatid),所以只能走客户端。
    """
    ensure_macos()
    try:
        new_name = _check_name(name)
    except LocalGroupError as e:
        return {"success": False, "error": str(e)}

    with ui_session("改群名"):
        return _rename_locked(rid, new_name)
