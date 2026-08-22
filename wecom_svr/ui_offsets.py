"""只能靠坐标点的那几个界面位置,以及它们的自动校准。

操控客户端的绝大部分动作走 AX 树:元素能按名字找、属性能回读,布局变了顶多报错,
不会点错地方。当前只有 "+" 没有对应的 AX 元素,必须按主窗口左上角取相对偏移:

  - `plus`  —— 会话列表顶部的 "+",点它打开「发起群聊」面板

`title` 是旧版客户端的兼容项。企业微信 5.0.9 起标题是可写 AXTextField,主流程
不再依赖坐标;只有检测不到该控件时才用 `title` 偏移点开改名输入框。

量出来的常量在这里是**起点,不是唯一答案**:客户端改版、换布局,偏移就会变,
而它的失败方式是「点了个寂寞」甚至「点到别的东西上」。所以每个位置都配一条
「候选序列 + 成功信号」:记住的那个点不中,就在附近按候选试,试中了记下来,
下次从记住的那个开始。校准不是锦上添花 —— 没有它,一次界面改版就得改代码发版。

三层优先级(高到低):

  1. 环境变量 `WECOM_SVR_UI_PLUS_OFFSET` / `WECOM_SVR_UI_TITLE_OFFSET`,形如 `"295,29"`。
     **显式配了就只用它**,不再试探也不再记忆 —— 人说了算的地方,程序别自作主张。
  2. `.data/ui_offsets.json` 里学到的值。
  3. 下面 DEFAULTS 里量出来的默认值。
"""
import json
import logging
import os
import tempfile
from pathlib import Path

from .settings import Settings, get_settings

log = logging.getLogger(__name__)

Point = tuple[int, int]

# 量出来的默认偏移(企业微信 macOS 客户端,实测机器是英文界面)。
# 相对主窗口左上角,单位是点。
DEFAULTS: dict[str, Point] = {
    "plus": (295, 29),
    # 取标题最左端稍靠里一点,群名长短都命中(会话列表宽度固定,标题起始位置不随内容变)
    "title": (355, 33),
}

# 候选位移 —— 相对起点的偏移量,按顺序试。
#
# 形状是「十字」而不是网格,因为布局变化本来就是这个形状:侧栏宽度变了整列横移,
# 标题栏高度变了整行竖移,两者同时变的情况少见。而每多一个候选就多一次真实点击,
# 十字把「覆盖面」和「乱点次数」拉到了一个还算合理的位置。
#
# 范围是**故意**收窄的,`title` 尤其:会话标题右边就是通话/更多那几个按钮,点上去
# 是有副作用的。宁可校准不到、让人手工配一次偏移,也不能瞎点出一通视频通话。
PROBES: dict[str, tuple[Point, ...]] = {
    # 会话列表顶部那一行的横向范围,±40 还在列表列里,不会串到右边的聊天区
    "plus": ((0, 0), (-20, 0), (20, 0), (-40, 0), (40, 0), (0, -10), (0, 10)),
    # 只在标题带内横向挪,而且**只往右挪一点**(标题左边是会话列表,右边远处是按钮)
    "title": ((0, 0), (40, 0), (80, 0), (-20, 0), (0, -8), (0, 8)),
}


class UnknownOffset(KeyError):
    """要了一个没定义的位置名 —— 代码写错了,不是运行期状况。"""


def parse_point(raw: str) -> Point | None:
    """解析 `"x,y"`。解析不了返回 None(调用方回落到默认值,别让配置写错就没法用)。"""
    x, sep, y = (raw or "").partition(",")
    if not sep:
        return None
    try:
        return int(x.strip()), int(y.strip())
    except ValueError:
        return None


def _settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def override(name: str, settings: Settings | None = None) -> Point | None:
    """环境变量里显式配的偏移。"""
    _check(name)
    raw = getattr(_settings(settings), f"ui_{name}_offset", "")
    if not raw:
        return None
    point = parse_point(raw)
    if point is None:
        log.warning("WECOM_SVR_UI_%s_OFFSET=%r 不是 \"x,y\" 格式,已忽略", name.upper(), raw)
    return point


def learned(name: str, settings: Settings | None = None) -> Point | None:
    """上次校准记下来的偏移。文件坏了当没有 —— 它只是个加速缓存,丢了会重新学。"""
    _check(name)
    try:
        data = json.loads(_state_file(settings).read_text(encoding="utf-8"))
        return parse_point(str(data[name]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def primary(name: str, settings: Settings | None = None) -> Point:
    """当前该用的那个点(不含试探候选)。"""
    s = _settings(settings)
    return override(name, s) or learned(name, s) or DEFAULTS[_check(name)]


def candidates(name: str, settings: Settings | None = None) -> list[Point]:
    """按顺序要试的偏移。第一个永远是 primary —— 平时只会用到它。

    显式配了 override,或者关掉了 `ui_calibrate`,就只有这一个:配置是人给的
    指令,试探是程序的猜测,猜测不该覆盖指令。
    """
    s = _settings(settings)
    base = primary(name, s)
    if override(name, s) is not None or not s.ui_calibrate:
        return [base]

    bx, by = base
    out: list[Point] = []
    for dx, dy in PROBES[name]:
        p = (bx + dx, by + dy)
        if p not in out:
            out.append(p)
    return out


def remember(name: str, point: Point, settings: Settings | None = None) -> None:
    """记住一个刚被验证过的偏移。

    首次校准即使默认坐标直接命中也会落盘,让新机器明确生成自己的运行时配置。
    已经保存过相同值时不重复写。写失败只吼一嗓子:这个文件丢了无非是下次再
    校准一遍,为它中断一次已经成功的操作不划算。
    """
    s = _settings(settings)
    _check(name)
    if override(name, s) is not None:
        return  # 人配死了的位置不记忆,免得下次读到一个和配置打架的值
    point = (int(point[0]), int(point[1]))
    if learned(name, s) == point:
        return

    path = _state_file(s)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[name] = f"{point[0]},{point[1]}"

    try:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    except OSError as e:
        log.warning("记录界面偏移失败(不影响本次操作): %s", e)
        return
    log.info("界面偏移已校准并记住: %s = %s(文件 %s)", name, data[name], path)


def describe(name: str, settings: Settings | None = None) -> str:
    """给报错用的一句话:现在用的是哪个点、它是哪来的、怎么手工改。"""
    s = _settings(settings)
    env = f"WECOM_SVR_UI_{name.upper()}_OFFSET"
    if (p := override(name, s)) is not None:
        return f"当前偏移 {p}(来自 {env},自动校准已因此关闭)"
    src = "上次校准记住的" if learned(name, s) else "代码里量出来的默认值"
    return f"当前偏移 {primary(name, s)}({src});可用 {env}=\"x,y\" 手工指定"


def _check(name: str) -> str:
    if name not in DEFAULTS:
        raise UnknownOffset(name)
    return name


def _state_file(settings: Settings | None = None) -> Path:
    return _settings(settings).ui_offsets_file


def _atomic_write(path: Path, body: str) -> None:
    """临时文件 + rename。半截 JSON 会让下次启动读不到任何一个学过的偏移。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ui-offsets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
