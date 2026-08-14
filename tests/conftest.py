"""测试期把 `.env` 挡在外面。

`Settings` 的 `model_config` 里写了 `env_file=".env"`,所以 **pydantic-settings
会直接读磁盘上那个文件**,`monkeypatch.delenv` 挡不住它 —— 那只清 os.environ。

后果是测试结果取决于跑测试的人本机 `.env` 填了什么:填了
`WECOM_SVR_MARS_TASKS_ACTIONS=message.send`,"默认只开应用身份"那条断言就红;
填了 `WECOM_SVR_KODO_*`,"没配对象存储时不传图"那条就红。CI 是绿的,只因为
CI 的工作区里没有 `.env`。

`_env_file=None` 只能逐个构造点传,而 `Settings()` 在源码和测试里到处都有,
所以直接改类级配置,一次盖住所有构造点。
"""
import os

import pytest

from wecom_svr.settings import Settings


@pytest.fixture(autouse=True, scope="session")
def _ignore_dotenv() -> None:
    Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True, scope="session")
def _disable_machine_preflight_in_unit_tests() -> None:
    """单测不应读取真实企微进程/数据库;预检本身由专门测试覆盖。"""
    old = os.environ.get("WECOM_SVR_STARTUP_PREFLIGHT_ENABLED")
    os.environ["WECOM_SVR_STARTUP_PREFLIGHT_ENABLED"] = "false"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("WECOM_SVR_STARTUP_PREFLIGHT_ENABLED", None)
        else:
            os.environ["WECOM_SVR_STARTUP_PREFLIGHT_ENABLED"] = old
