"""新 Mac 首次部署时的本机能力预检与自动初始化。

服务的本地能力依赖三类机器状态,它们都不能跟着代码仓库复制:

* 当前登录 profile 的数据库 key
* 解密后的 Info.db(消息与会话,硬门禁)
* 解密后的 Session.db(通讯录,软门禁 —— 见 _check_contacts)
* macOS 辅助功能授权与企业微信窗口布局

默认在 FastAPI lifespan 启动阶段完成检查。数据库初始化是硬门禁,失败会让 Uvicorn
启动失败;客户端 UI 校准是软门禁,失败只会让依赖本地 UI 的接口在实际调用时报错。
"""
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

from . import local_group, local_send, localdb, messages
from .localdb import LocalDataError
from .settings import Settings, get_agent_home

log = logging.getLogger(__name__)


class StartupPreflightError(RuntimeError):
    """本机环境未准备好,服务不应进入 ready 状态。"""


@dataclass(frozen=True)
class PreflightResult:
    database_ready: bool
    key_discovered: bool
    contacts_ready: bool
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
    try:
        messages.refresh(force=True)
        return False
    except messages.LocalKeyError as first_error:
        if not settings.startup_auto_init:
            raise StartupPreflightError(str(first_error)) from first_error

    _discover_key(settings)
    try:
        messages.refresh(force=True)
    except messages.LocalKeyError as e:
        raise StartupPreflightError(
            f"自动扫描完成后 key 仍不能解开当前 Info.db: {e}"
        ) from e
    except LocalDataError as e:
        raise StartupPreflightError(f"自动获取 key 后解密数据库失败: {e}") from e
    return True


def _check_contacts() -> bool:
    """确认解密后的 Session.db 通讯录能查 —— 查不到只降级,不挡启动。

    messages.refresh() 对 Session.db 是**故意**只记一条 warning 就放过的(它挡不住
    收消息)。但 directory.py 靠这张 USER 表把名字翻成 userid,官方建群和通讯录搜索
    没它就用不了,所以这里单独探一次,把结论摆进 /health,而不是等第一个请求 503。

    不做硬门禁: 纯发消息/纯采集的部署本来就用不到通讯录,不该因此起不来。
    """
    try:
        localdb.query("Session.db", "SELECT RID FROM USER LIMIT 1")
    except Exception as e:
        log.warning("通讯录(Session.db USER)不可用,按名字查人/官方建群会失败: %s", e)
        return False
    return True


def run(settings: Settings) -> PreflightResult:
    """完成启动预检;数据库失败阻止启动,UI 失败则降级并继续启动。"""
    if not settings.startup_preflight_enabled:
        log.warning(
            "已关闭本机启动预检(WECOM_SVR_STARTUP_PREFLIGHT_ENABLED=false);"
            "本地数据库与客户端 UI 能力可能到首个请求才报错"
        )
        return PreflightResult(
            database_ready=False, key_discovered=False, contacts_ready=False, ui_ready=False
        )

    try:
        discovered = _prepare_database(settings)
    except StartupPreflightError:
        raise
    except Exception as e:
        raise StartupPreflightError(f"企业微信数据库初始化失败: {e}") from e

    contacts_ready = _check_contacts()

    ui_ready = False
    if settings.startup_ui_check:
        try:
            local_group.calibrate_ui()
        except Exception as e:
            log.warning(
                "企业微信 UI 预检/校准失败,服务继续启动;本地 UI 接口调用时将报错: %s。"
                "请确认客户端已运行并登录、主窗口可见,且服务宿主已获得"
                "“辅助功能”和“自动化”权限。",
                e,
            )
        else:
            ui_ready = True
            bounds_ready, bounds_error = local_send._refresh_chat_window_bounds()
            if bounds_ready:
                try:
                    local_send._load_quartz_libraries()
                except OSError as e:
                    log.warning("Quartz 运行时预热失败,本地发送首请求将重试: %s", e)
            else:
                log.warning(
                    "企业微信聊天窗口位置预热失败,本地发送首请求将重试: %s",
                    bounds_error,
                )

    result = PreflightResult(
        database_ready=True,
        key_discovered=discovered,
        contacts_ready=contacts_ready,
        ui_ready=ui_ready,
    )
    log.info(
        "启动预检通过: database_ready=%s key_discovered=%s contacts_ready=%s ui_ready=%s",
        result.database_ready,
        result.key_discovered,
        result.contacts_ready,
        result.ui_ready,
    )
    return result
