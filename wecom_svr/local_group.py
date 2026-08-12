"""以「本人身份」拉群 —— 操控 macOS 客户端的「发起群聊」面板。

为什么不用官方 API: 官方能建群的只有 `appchat`,它的 `userlist` 只收本企业
userid,外部/微信联系人塞不进去;而「创建客户群」官方压根没开接口。
客户端里的 `wxwork://jump?target=openchat` 也走不通 —— 二进制里写死了
`not tencent company, ignore`,只对腾讯自家企业开放。

所以只剩这条路。好在实测下来它比「盲点坐标」结实得多:

  - 面板是主窗口上的一个 AXSheet,搜索框是真 AXTextField(可直接 set value)
  - 搜索结果是 AXOutline,每行能读出姓名与部门 —— **选人前先核对,不靠猜**
  - 在搜索框按回车 = 选中结果里的第一条(勾选框是 AXImage,没有 action,
    合成点击也打不进 CEF 自绘控件,回车是唯一可靠的选中方式)
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

from . import local_send, messages, ui_offsets
from .group import MAX_NAME_LEN
from .local_send import _get_clipboard, _run_applescript, _set_clipboard, ensure_macos
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
SEARCH_SETTLE = 1.6
# 搜索命中多条时,最多读几行明细用于报错列候选(见 _search 里的性能说明)
DETAIL_ROWS = 8

# 选中一个人最多试几次(回车偶尔落空),以及每次判定前等计数刷新的次数/间隔
SELECT_ATTEMPTS = 3
COUNT_SETTLE_TRIES = 3
COUNT_SETTLE_DELAY = 0.7
# 企业微信建群后写入 Info.db/WAL 可能有几十秒延迟。只等 2 秒会把已经
# 建好的群误报成「新增 0 个」,进而跳过改名。
GROUP_APPEAR_TIMEOUT = 90.0
GROUP_APPEAR_POLL = 1.0


class LocalGroupError(LocalSendError):
    """拉群流程里的可预期失败(回 400)。"""


class AmbiguousCandidate(LocalGroupError):
    """一个名字在客户端里搜出多条 —— 回车只会选中第一条,所以必须让调用方说清楚。"""


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

# 每次操作面板前都把客户端顶到前台: keystroke 是发给最前台应用的,服务进程里
# 跑的时候前台随时可能是别的窗口,少了这一步回车就落空(实测过一次)。
_PICKER = _MAIN_WINDOW + '''
  if (count of sheets of mainWin) is 0 then error "NO_SHEET"
  set frontmost to true
  delay 0.3
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


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _real_click(x: int, y: int) -> None:
    """在屏幕坐标发一次**真实**鼠标点击。

    System Events 的 `click at` 是合成点击,CEF 自绘的区域收不到 —— 会话标题和
    成员勾选框都实测过,点上去毫无反应。这里用 JXA 的 ObjC 桥直接调 CoreGraphics
    发真事件: 不引入任何依赖,还是同一个 osascript 二进制。
    """
    jxa = f'''
ObjC.import('CoreGraphics');
var pt = $.CGPointMake({int(x)}, {int(y)});
function post(t) {{ $.CGEventPost(0, $.CGEventCreateMouseEvent($(), t, pt, 0)); }}
post(5); delay(0.15);   // mouseMoved
post(1); delay(0.08);   // leftMouseDown
post(2);                // leftMouseUp
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
    time.sleep(0.8)


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
    试中了记下来,下次第一发就命中。
    """
    if _sheet_open():
        raise LocalGroupError("客户端上已经有一个弹出面板开着,请先关掉再试")

    tried: list[tuple[int, int]] = []
    for i, (dx, dy) in enumerate(ui_offsets.candidates("plus")):
        tried.append((dx, dy))
        _click_window_offset(dx, dy)
        state = _wait_picker(SHEET_TIMEOUT if i == 0 else PROBE_TIMEOUT)
        if state == _SHEET_PICKER:
            ui_offsets.remember("plus", (dx, dy))
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


# ─── 选人 ──────────────────────────────────────────────────────────────

_ROW_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def _search(keyword: str) -> tuple[int, list[dict]]:
    """把关键词写进搜索框,返回 (命中总数, 前若干行的姓名与部门)。

    只读前 DETAIL_ROWS 行明细是性能要求,不是偷懒: 每读一个 AX 属性都是一次
    跨进程调用,搜「陈」这种常见姓能出几百行,全量枚举实测直接把超时打爆。
    判重只需要总数,报错列候选只需要前几行。
    """
    out = _osa(_PICKER + f'''
  set tf to text field 1 of group 1 of sg
  set focused of tf to true
  delay 0.3
  set value of tf to "{_escape(keyword)}"
  delay {SEARCH_SETTLE}
  set ol to outline 1 of scroll area 1 of group 1 of sg
  set total to count of rows of ol
  set shown to total
  if shown > {DETAIL_ROWS} then set shown to {DETAIL_ROWS}
  set acc to (total as string) & "{_ROW_SEP}"
  repeat with i from 1 to shown
    set c to UI element 1 of row i of ol
    set nm to ""
    set dp to ""
    try
      set nm to value of UI element 2 of c
    end try
    try
      set dp to value of UI element 7 of c
    end try
    set acc to acc & nm & "{_FIELD_SEP}" & dp & "{_ROW_SEP}"
  end repeat
  return acc
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
    return total, rows


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
  delay 0.2
  keystroke return
''')
    time.sleep(1.2)


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
    opened = False
    with ui_session("启动 UI 预检"):
        _activate()
        try:
            _open_picker()
            opened = True
        finally:
            if opened:
                _close_picker()
                if _sheet_open():
                    raise LocalGroupError(
                        "启动 UI 预检打开了「发起群聊」面板,但取消后面板仍未关闭。"
                        "请手动关闭该面板后重启服务。"
                    )
    point = ui_offsets.primary("plus")
    return {"success": True, "plus_offset": f"{point[0]},{point[1]}"}


# ─── 公开 API ──────────────────────────────────────────────────────────

def create_group(members: list[str], name: str = "") -> dict:
    """以本人身份拉群。你自己自动是群主,不用写进 members。

    Args:
        members: 除自己以外的成员,按客户端通讯录搜索(内部同事、外部联系人、微信都行)
        name: 群名。选人面板里没有命名输入框,所以是「先建群、再改名」两步;
              改名失败不会回滚群 —— 群已经建出来了,回滚反而更吓人。

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

    if len(wanted) < 2:
        return {
            "success": False,
            "error": f"除自己外至少要 2 个人才能建群,当前 {len(wanted)} 个: {wanted}",
        }
    if len(wanted) > MAX_MEMBERS:
        return {"success": False, "error": f"一次最多 {MAX_MEMBERS} 人,当前 {len(wanted)} 个"}

    wanted_name = ""
    if name and name.strip():
        wanted_name = _check_name(name)

    # 快照 → 建群 → 差集 → 改名,全程一次持锁。差集是靠「新出现的群 RID」认新群
    # (新群的 name 是空的,只能这么认),中间放开锁的话,另一个并发建群会让差集
    # 出现两个新 RID —— 两边都只能放弃改名。
    with ui_session("拉群"):
        before = set(_group_names()) if wanted_name else set()

        _activate()
        _open_picker()
        try:
            picked = [_select_member(q, i + 1) for i, q in enumerate(wanted)]
            _confirm()
        except LocalGroupError:
            _close_picker()  # 别把面板留在用户屏幕上
            raise

        time.sleep(2.0)
        if _sheet_open():
            _close_picker()
            raise LocalGroupError("按了「完成」但面板还在,群可能没建成")

        result = {
            "success": True,
            "members": picked,
            "member_count": len(picked) + 1,  # +1 是自己
            "rid": None,
            "name": None,
            "error": None,
        }
        if not wanted_name:
            return result

        new_rids = _wait_new_group_rids(before)
        if len(new_rids) != 1:
            result["name_error"] = (
                "群建好了,等待本地会话落库后仍没能唯一定位新建的会话"
                f"(新增 {len(new_rids)} 个),群名没改成。请手动改名。"
            )
            return result

        result["rid"] = new_rids[0]
        renamed = _rename_locked(new_rids[0], wanted_name)
        if renamed.get("success"):
            result["name"] = wanted_name
        else:
            result["name_error"] = f"群建好了,但改名失败: {renamed.get('error')}"
        return result


# ─── 改群名 ────────────────────────────────────────────────────────────

_SENTINEL = "\x00wecom-svr-clipboard-probe\x00"
_AX_TITLE_MISSING = "__WECOM_SVR_AX_TITLE_MISSING__"


def _rename_title_via_ax(new_name: str) -> str | None:
    """通过新版客户端暴露的标题 AXTextField 改名。

    企业微信 5.0.9 已把聊天区标题变成可写 AXTextField,且支持 AXConfirm。
    标题框和左侧搜索框同属内容 splitter group,用聊天区 splitter 的左边界区分:
    标题框在聊天区内,搜索框在会话列表内。

    返回旧标题;旧版客户端没有这个控件时返回 None,由调用方走坐标兼容路径。
    """
    out = _osa(_MAIN_WINDOW + f'''
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
  if target is missing value then return "{_AX_TITLE_MISSING}"

  set oldValue to value of target as text
  set value of attribute "AXFocused" of target to true
  set value of target to "{_escape(new_name)}"
  delay 0.4
  try
    perform action "AXConfirm" of target
  on error
    keystroke return
  end try
  return oldValue
''')
    value = out.strip().strip('"')
    if value == _AX_TITLE_MISSING:
        return None
    return value


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


def _wait_new_group_rids(before: set[int]) -> list[int]:
    """等待新建会话写入本地库,返回相对 before 新出现的 RID。"""
    deadline = time.monotonic() + GROUP_APPEAR_TIMEOUT
    while True:
        new_rids = sorted(set(_group_names()) - before)
        # 多个新增会话无法安全判断哪个属于本次请求,立即放弃改名。
        if new_rids or time.monotonic() >= deadline:
            return new_rids
        time.sleep(GROUP_APPEAR_POLL)


def _rename_locked(rid: int, new_name: str) -> dict:
    """改名的界面部分 —— **调用方必须已经持有 ui_session**(锁不可重入)。"""
    conv = local_send.get_conversation_by_rid(int(rid))
    if not conv:
        return {"success": False, "error": f"本地库里没有 RID={rid} 的会话"}

    old_clipboard = ""
    try:
        old_clipboard = _get_clipboard()
    except Exception:
        pass
    try:
        _activate()
        ok, msg = local_send._open_conversation_by_scheme(conv)
        if not ok:
            raise LocalGroupError(f"打开会话失败: {msg}")

        was = _rename_title_via_ax(new_name)
        if was is None:
            was = _open_edit_box_and_read_current()
            _set_clipboard(new_name)
            _osa('''
  keystroke "a" using command down
  delay 0.2
  keystroke "v" using command down
  delay 0.4
  keystroke return
''')
        time.sleep(1.5)
    finally:
        if old_clipboard:
            try:
                _set_clipboard(old_clipboard)
            except Exception:
                pass

    # 回库核验: 只信数据库,不信「脚本没报错」
    names = _group_names()
    actual = names.get(int(rid))
    if actual == new_name:
        return {"success": True, "rid": int(rid), "name": new_name, "old_name": was, "error": None}

    wrong = [r for r, n in names.items() if n == new_name and r != int(rid)]
    hint = f" —— 名字落到了 RID={wrong[0]} 上,请手动改回去" if wrong else ""
    return {
        "success": False,
        "rid": int(rid),
        "error": f"改名后核验失败: RID={rid} 的群名仍是 {actual!r},不是 {new_name!r}{hint}",
    }


def _check_name(name: str) -> str:
    new_name = (name or "").strip()
    if not new_name:
        raise LocalGroupError("群名为空")
    if len(new_name) > MAX_NAME_LEN:
        raise LocalGroupError(f"群名最多 {MAX_NAME_LEN} 个字符,当前 {len(new_name)} 个")
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
