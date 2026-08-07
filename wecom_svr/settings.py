"""服务配置。

来源优先级: 环境变量 / .env  >  agent home 的 config.json  >  默认值
凭证同时兼容上游命名(WECOM_CORPID/WECOM_SECRET/WECOM_AGENTID)。
"""
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import REPO_ROOT, AgentHome, AgentHomeError, resolve_agent_home

log = logging.getLogger(__name__)

# 从 agent home config.json 继承的字段(仅在环境变量未提供时生效)
_INHERITED_FROM_CONFIG = ("api_token", "corpid", "secret", "agentid")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WECOM_SVR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8090

    # 留空则不鉴权(仅限本地使用,勿暴露公网)
    api_token: str = ""

    agent_home: Optional[Path] = Field(
        default=None,
        validation_alias=AliasChoices("WECOM_AGENT_HOME", "WECOM_SVR_AGENT_HOME"),
    )

    corpid: str = Field(default="", validation_alias=AliasChoices("WECOM_SVR_CORPID", "WECOM_CORPID"))
    secret: str = Field(default="", validation_alias=AliasChoices("WECOM_SVR_SECRET", "WECOM_SECRET"))
    agentid: str = Field(default="", validation_alias=AliasChoices("WECOM_SVR_AGENTID", "WECOM_AGENTID"))

    data_dir: Path = REPO_ROOT / ".data"
    wecom_timeout: int = 30

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)

    def agent_env(self) -> dict[str, str]:
        """透传给 wecom.py 子进程的凭证环境变量。"""
        pairs = {"WECOM_CORPID": self.corpid, "WECOM_SECRET": self.secret, "WECOM_AGENTID": self.agentid}
        return {k: v for k, v in pairs.items() if v}


def _merge_agent_config(settings: Settings, home: AgentHome) -> Settings:
    """用 agent home 的 config.json 填补未由环境变量提供的字段。"""
    cfg_path = home.config_json
    if not cfg_path:
        return settings

    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("读取 %s 失败,忽略: %s", cfg_path, e)
        return settings
    if not isinstance(cfg, dict):
        return settings

    explicit = settings.model_fields_set
    updates = {
        field: str(cfg[field])
        for field in _INHERITED_FROM_CONFIG
        if field not in explicit and cfg.get(field)
    }
    return settings.model_copy(update=updates) if updates else settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    try:
        settings = _merge_agent_config(settings, get_agent_home())
    except AgentHomeError as e:
        # 启动期不致命:健康检查与错误响应会明确报告该问题
        log.warning("未能解析 wecom-agent: %s", e)
    return settings


@lru_cache(maxsize=1)
def get_agent_home() -> AgentHome:
    """解析 wecom-agent 依赖包位置(失败抛 AgentHomeError)。"""
    return resolve_agent_home(Settings().agent_home)
