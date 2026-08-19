"""服务配置。

来源优先级: 环境变量 / .env  >  agent home 的 config.json  >  默认值
凭证同时兼容上游命名(WECOM_CORPID/WECOM_SECRET/WECOM_AGENTID)。
"""
import json
import logging
from functools import lru_cache
from pathlib import Path

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

    agent_home: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("WECOM_AGENT_HOME", "WECOM_SVR_AGENT_HOME"),
    )

    corpid: str = Field(default="", validation_alias=AliasChoices("WECOM_SVR_CORPID", "WECOM_CORPID"))
    secret: str = Field(default="", validation_alias=AliasChoices("WECOM_SVR_SECRET", "WECOM_SECRET"))
    agentid: str = Field(default="", validation_alias=AliasChoices("WECOM_SVR_AGENTID", "WECOM_AGENTID"))

    data_dir: Path = REPO_ROOT / ".data"
    wecom_timeout: int = 30

    # ── 新机器启动门禁 ─────────────────────────────────────────────────
    # 默认在已经占住监听端口后验证/初始化本地数据库和客户端 UI。
    # 纯官方 API 部署可整体关闭;只采集数据库、不操控界面的部署可只关 startup_ui_check。
    startup_preflight_enabled: bool = True
    startup_auto_init: bool = True
    startup_ui_check: bool = True
    startup_key_timeout: int = 180

    # online/ 文档表格能力依赖的外部 wecom-cli 二进制
    wecom_cli_path: str = "wecom-cli"
    online_timeout: int = 60

    # ── 群消息同步到 mars ─────────────────────────────────────────────
    # mars 的 hook 是公开路由(无鉴权),所以 base_url 留空 = 关闭,避免误推。
    mars_base_url: str = ""
    mars_timeout: int = 10
    # 后台常驻同步;关掉也能用 POST /api/v1/mars/sync/run 手动跑
    mars_sync_enabled: bool = False
    # 轮询间隔。源库 mtime 没变就只是一次 stat,压到秒级也不费什么
    mars_sync_interval: float = 3.0
    mars_sync_batch: int = 200
    # 图片消息最多等多少秒再推。企微收到图只写消息行,图片文件要等你点开会话
    # 才下载 —— 3 秒的轮询几乎总是跑在下载前面,于是推出去的永远是 `[图片]`
    # 占位符,而下游按 md5(群号+发送人+时间+正文) 去重,事后补推只会多出一条。
    # 所以只能在推之前等。水位按群独立,等待只阻塞本群;0 = 不等,退回旧行为。
    mars_image_wait: float = 60.0
    # 普通图片主动获取顺序:BigCDN TCP → 触发企微客户端渲染。type101 不请求正文
    # 下载 URL,仅用它的摘要定位客户端缓存。TCP 会话凭据只放在 gitignored 的
    # media_tcp.json,不进 .env/日志/状态接口。
    mars_image_tcp_enabled: bool = True
    mars_image_tcp_config: Path | None = None
    mars_image_tcp_timeout: float = 8.0
    mars_image_client_trigger_enabled: bool = True
    mars_image_client_wait: float = 8.0
    mars_image_client_poll: float = 0.25
    mars_image_trigger_cooldown: float = 15.0
    # 单条消息最多展示多少张图。多出的图不下载,正文给出明确的省略提示。
    mars_image_max_per_message: int = 9
    # 白名单:群名或会话 RID,逗号分隔。**留空 = 一条都不同步**
    mars_sync_groups: str = ""
    # 显式开启后忽略白名单,同步本地所有群。默认关闭,避免误把全部历史消息推到 Mars。
    mars_sync_all_groups: bool = False
    # 首次同步往回补多少秒历史(0 = 只从当前最新一条往后)
    mars_sync_backfill: int = 0
    # 群号前缀。mars 的 qq_groups.qq 是全局唯一键,和真实 QQ 群号共用一张表,
    # 纯数字 RID 有撞号风险,必须带前缀隔离
    mars_group_prefix: str = "wework-"
    # 可选的群**名**前缀,默认不改群名。需要沿用 mars 的企微群约定时配置 `work-`:
    #   models.Group.GroupIMCategory(): 名字含 "work-" → 认成 Wework 群
    #   groupmsg.LoadMsgs():   left(name,5) != 'work-' → 未响应告警不管这类群
    mars_group_name_prefix: str = ""
    # mars 侧用 robot 号区分消息来源,并据此决定要不要转发到 mars_alert / 大数据
    mars_robot: str = "wecom-svr"
    mars_client_type: str = "Wework"

    # ── 从 mars 拉任务(mars → 本机的反向通道)────────────────────────
    # 设计见 docs/mars-task-queue.md。token 留空 = 整个功能关闭。
    mars_tasks_token: str = ""
    mars_tasks_enabled: bool = False
    # 认领用的 worker 名。留空取本机 hostname —— 两台 Mac 用同一个名字会各拿
    # 走一半任务(租约保证不重复执行,但顺序不保证),所以默认必须是机器相关的
    mars_tasks_worker: str = ""
    # 长轮询秒数。上限 50:前面 openresty 的 proxy_read_timeout 默认 60s
    mars_tasks_wait: float = 25.0
    # 一次领几条。默认 1 是有原因的:执行是串行的,一次领 5 条会让队尾那条在还没
    # 轮到它时租约就到期 —— 然后被 mars 放回队列、被下一轮的自己再领一次,
    # 于是同一条任务在本机排了两遍队。领一条做一条就没有这个类别的问题。
    mars_tasks_max: int = 1
    # 租约秒数,要盖住最慢的操作(拉群要逐个搜成员,可能几十秒)
    mars_tasks_lease: int = 120
    # 出错后歇多久再拉。正常轮次不 sleep —— 长轮询自己就是节流
    mars_tasks_idle: float = 3.0
    # ⚠️ 动作白名单。默认只允许「以应用身份发消息」。
    # 不带 `app.` 前缀的那几条是**以你本人的身份说话**,让一个远端队列能驱动它,
    # 权限边界比「应用发通知」大一个量级,必须显式写进来。"*" = 全开。
    mars_tasks_actions: str = "app.message.send"

    # ── 界面坐标(仅本人身份操控 macOS 客户端时用得上)──────────────────
    # "+" 没有 AX 元素,只能按主窗口原点取偏移去点(见 ui_offsets.py);
    # title 只保留给旧版客户端的兼容路径。
    # 写成 "x,y" 可覆盖;**配了就只用它**,自动校准会因此关闭 —— 人给的指令
    # 不该被程序的猜测盖过去。
    ui_plus_offset: str = ""
    ui_title_offset: str = ""
    # 「更多」相对窗口右上角往里的偏移,用来打开群详情改「我在本群的昵称」。
    ui_more_offset: str = ""
    # 自动校准:记住的点没点中时,在附近按候选试,试中了记到 ui_offsets.json。
    # 关掉 = 只点那一个点,点不中直接报错。
    ui_calibrate: bool = True

    # ── 图片落地(对象存储)────────────────────────────────────────────
    # 群图片原本只在本机企微缓存里,别人打不开。传到 kodo 换个公网 URL 才看得见。
    # ⚠️ 目标 bucket 公开可读:传上去等于放公网,只是没人知道地址。默认不开。
    kodo_ak: str = ""
    kodo_sk: str = ""
    kodo_bucket: str = ""
    kodo_domain: str = ""
    kodo_prefix: str = "robot"
    kodo_timeout: int = 20

    @property
    def kodo_enabled(self) -> bool:
        return bool(self.kodo_ak and self.kodo_sk and self.kodo_bucket and self.kodo_domain)

    @property
    def mars_group_selectors(self) -> list[str]:
        return [s.strip() for s in self.mars_sync_groups.split(",") if s.strip()]

    @property
    def mars_state_file(self) -> Path:
        return self.data_dir / "mars_sync_state.json"

    @property
    def mars_tasks_state_file(self) -> Path:
        return self.data_dir / "mars_tasks_state.json"

    @property
    def mars_outbox_dir(self) -> Path:
        return self.data_dir / "mars_outbox"

    @property
    def ui_offsets_file(self) -> Path:
        return self.data_dir / "ui_offsets.json"

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

    # `.env.example` 会保留空的凭证项;空字符串不应覆盖 agent config.json。
    # 只有环境变量/.env 真正提供了非空值时才优先使用。
    updates = {
        field: str(cfg[field])
        for field in _INHERITED_FROM_CONFIG
        if not getattr(settings, field, None) and cfg.get(field)
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
