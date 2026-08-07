"""桥接 wecom-agent 的 online/ 包(文档 / 表格 / 智能表)。

online/ 自身不发 HTTP,而是驱动外部 `wecom-cli` 二进制。所以这里要处理三层失败:
  1. agent home 里没有 online/ 包 —— 上游版本太旧
  2. wecom-cli 没装 / 没登录     —— 环境没准备好
  3. wecom-cli 返回非零          —— 调用本身失败

上游用 require_confirmation() 给写操作加了确认闸门,这里原样保留:
请求体不带 confirmed=true 的写操作会被拒(403),不会静默落到线上文档。
"""
import functools
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from .settings import get_agent_home, get_settings

log = logging.getLogger(__name__)


class OnlineError(RuntimeError):
    """online 能力相关的可预期错误。"""


class OnlineUnavailable(OnlineError):
    """online/ 包缺失,或 wecom-cli 没装。"""


class OnlineBadRequest(OnlineError):
    """参数不合法(如 docid/url 未二选一)。"""


class OnlineConfirmationRequired(OnlineError):
    """线上写操作未确认。"""


class OnlineCliError(OnlineError):
    """wecom-cli 执行失败。"""


@dataclass(frozen=True)
class OnlineModules:
    wecom_cli: ModuleType
    docs: ModuleType
    sheets: ModuleType
    smartsheets: ModuleType
    local_docs: ModuleType | None


@functools.lru_cache(maxsize=1)
def load_online() -> OnlineModules:
    """从 agent home 导入 online/ 包(相对 import,必须按包导入)。"""
    root = str(get_agent_home().root)
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        from online import docs, sheets, smartsheets, wecom_cli
    except ImportError as e:
        raise OnlineUnavailable(
            f"{root} 下没有可用的 online/ 包({e})。"
            "该能力需要较新的 wecom-agent —— 试试 scripts/update-agent.sh"
        ) from e

    # local_docs 依赖 decrypt.read_doc,缺了也不该拖垮在线接口
    try:
        from online import local_docs
    except ImportError as e:
        log.warning("online.local_docs 不可用,本地兜底接口将关闭: %s", e)
        local_docs = None

    return OnlineModules(
        wecom_cli=wecom_cli, docs=docs, sheets=sheets,
        smartsheets=smartsheets, local_docs=local_docs,
    )


def _json_runner(mods: OnlineModules) -> Callable:
    settings = get_settings()

    def run(args, payload=None):
        return mods.wecom_cli.run_json(
            args, payload, timeout=settings.online_timeout, cli=settings.wecom_cli_path
        )

    return run


def _text_runner(mods: OnlineModules) -> Callable:
    settings = get_settings()

    def run(args):
        return mods.wecom_cli.run_text(
            args, timeout=settings.online_timeout, cli=settings.wecom_cli_path
        )

    return run


def invoke(pick: Callable[[OnlineModules], Callable], **kwargs) -> Any:
    """调用一个 online/ 函数,并把它的异常翻译成本服务的错误类型。

    Args:
        pick: 从 OnlineModules 里选出目标函数,例如 `lambda m: m.docs.create_document`
        kwargs: 传给该函数的参数(runner 由这里注入)
    """
    mods = load_online()
    func = pick(mods)
    kwargs.setdefault("runner", _json_runner(mods))

    try:
        return func(**kwargs)
    except PermissionError as e:
        # 上游 require_confirmation:写操作未确认
        raise OnlineConfirmationRequired(str(e)) from e
    except FileNotFoundError as e:
        raise OnlineUnavailable(
            f"找不到 wecom-cli({get_settings().wecom_cli_path})。"
            "请先安装并登录,或用 WECOM_SVR_WECOM_CLI_PATH 指定路径"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise OnlineCliError(f"wecom-cli 超时({get_settings().online_timeout}s)") from e
    except ValueError as e:
        raise OnlineBadRequest(str(e)) from e
    except mods.wecom_cli.WecomCliError as e:
        raise OnlineCliError(str(e)) from e


def capabilities() -> dict:
    """探测 wecom-cli 版本、登录状态与命令支持情况。"""
    mods = load_online()
    runner = _text_runner(mods)

    def safe(fn):
        try:
            return fn()
        except FileNotFoundError:
            raise OnlineUnavailable(
                f"找不到 wecom-cli({get_settings().wecom_cli_path})"
            ) from None
        except Exception as e:
            return f"error: {e}"

    from online import selfcheck

    commands = selfcheck.SUPPORTED_DOC_COMMANDS + selfcheck.KNOWN_UNSUPPORTED_DOC_COMMANDS
    return {
        "cli_path": get_settings().wecom_cli_path,
        "version": safe(lambda: mods.wecom_cli.version(runner=runner)),
        "auth_status": safe(lambda: mods.wecom_cli.auth_status(runner=runner)),
        "doc_commands": {
            cmd: "supported" if mods.wecom_cli.doc_command_supported(cmd, runner=runner) else "unsupported"
            for cmd in commands
        },
    }
