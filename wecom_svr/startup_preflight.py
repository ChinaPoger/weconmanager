"""新 Mac 首次部署时的本机能力预检与自动初始化。

服务的本地能力依赖三类机器状态,它们都不能跟着代码仓库复制:

* 当前登录 profile 的数据库 key
* 解密后的 Info.db / Session.db
* macOS 辅助功能授权与企业微信窗口布局

默认在 FastAPI lifespan 启动阶段完成检查。任何一步失败都会让 Uvicorn 启动失败,
避免端口已经监听、健康检查却说 ok,直到第一条真实任务才暴露环境没准备好。
"""
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

from . import local_group, messages
from .localdb import LocalDataError
from .settings import Settings, get_agent_home

log = logging.getLogger(__name__)


class StartupPreflightError(RuntimeError):
    """本机环境未准备好,服务不应进入 ready 状态。"""


@dataclass(frozen=True)
class PreflightResult:
    database_ready: bool
    key_discovered: bool
    ui_ready: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


def _redact_secrets(text: str) -> str:
    """find_key_fast 会输出 16B key,错误日志里不能把它带出去。"""
    return re.sub(r"\b[0-9a-fA-F]{32}\b", "<redacted-key>", text)


def _discover_key(settings: Settings) -> None:
    home = get_agent_home()
    script = home.decrypt_dir / "macos" / "find_key_fast.py"
    if not script.is_file():
        raise StartupPreflightError(f"缺少自动取 key 脚本: {script}")

    log.warning("当前企业微信数据库 key 不可用,正在自动扫描本机已登录进程")
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            timeout=settings.startup_key_timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise StartupPreflightError(
            f"自动获取企业微信数据库 key 超过 {settings.startup_key_timeout}s。"
            f"请手工运行: {sys.executable} {script}"
        ) from e
    except OSError as e:
        raise StartupPreflightError(f"无法运行自动取 key 脚本 {script}: {e}") from e

    if result.returncode != 0:
        detail = _redact_secrets((result.stderr or result.stdout).strip())
        raise StartupPreflightError(
            "自动获取企业微信数据库 key 失败"
            f"{f': {detail}' if detail else ''}。请确认企业微信已运行并登录、"
            "客户端已按 wecom-agent 文档正确重签。"
        )


def _prepare_database(settings: Settings) -> bool:
    """验证 key 并刷新明文库。返回本轮是否执行过 key discovery。"""
    discovered = False
    try:
        messages.refresh(force=True)
        return discovered
    except messages.LocalKeyError as first_error:
        if not settings.startup_auto_init:
            raise StartupPreflightError(str(first_error)) from first_error

    _discover_key(settings)
    discovered = True
    try:
        messages.refresh(force=True)
    except messages.LocalKeyError as e:
        raise StartupPreflightError(
            f"自动扫描完成后 key 仍不能解开当前 Info.db: {e}"
        ) from e
    except LocalDataError as e:
        raise StartupPreflightError(f"自动获取 key 后解密数据库失败: {e}") from e
    return discovered


def run(settings: Settings) -> PreflightResult:
    """完成启动门禁。返回结果只用于健康状态与日志,失败直接抛异常。"""
    if not settings.startup_preflight_enabled:
        log.warning(
            "已关闭本机启动预检(WECOM_SVR_STARTUP_PREFLIGHT_ENABLED=false);"
            "本地数据库与客户端 UI 能力可能到首个请求才报错"
        )
        return PreflightResult(database_ready=False, key_discovered=False, ui_ready=False)

    try:
        discovered = _prepare_database(settings)
    except StartupPreflightError:
        raise
    except LocalDataError as e:
        raise StartupPreflightError(f"企业微信数据库初始化失败: {e}") from e
    except Exception as e:
        raise StartupPreflightError(f"企业微信数据库预检异常: {e}") from e

    ui_ready = False
    if settings.startup_ui_check:
        try:
            local_group.calibrate_ui()
        except Exception as e:
            raise StartupPreflightError(
                "企业微信 UI 预检/校准失败: "
                f"{e}。请确认客户端已运行并登录、主窗口可见,且服务宿主已获得"
                "“辅助功能”和“自动化”权限。"
            ) from e
        ui_ready = True

    result = PreflightResult(
        database_ready=True,
        key_discovered=discovered,
        ui_ready=ui_ready,
    )
    log.info(
        "启动预检通过: database_ready=%s key_discovered=%s ui_ready=%s",
        result.database_ready,
        result.key_discovered,
        result.ui_ready,
    )
    return result
