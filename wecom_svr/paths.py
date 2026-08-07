"""定位 wecom-agent 依赖包(pkg)及其数据目录。

引用方式(优先级从高到低):
  1. $WECOM_AGENT_HOME       — 指向本地 checkout(通常含 config.json 与已解密的数据库)
  2. <repo>/vendor/wecom-agent — git submodule 固定版本

两种目录布局都支持:
  - 上游 main:  legacy/self-built-app/wecom.py + decrypt/
  - 旧版布局:   wecom.py + decrypt/
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_HOME = REPO_ROOT / "vendor" / "wecom-agent"

# wecom.py 在不同版本中的位置(相对 agent home)
_CLI_CANDIDATES = (
    Path("legacy/self-built-app/wecom.py"),  # 上游 main
    Path("wecom.py"),                        # 旧版布局
)


class AgentHomeError(RuntimeError):
    """agent home 缺失或布局不可识别。"""


@dataclass(frozen=True)
class AgentHome:
    """已解析的 wecom-agent 依赖包位置。"""

    root: Path
    wecom_cli: Path
    decrypt_dir: Path

    @property
    def config_json(self) -> Optional[Path]:
        p = self.root / "config.json"
        return p if p.is_file() else None


def _resolve_root(explicit: Optional[Path] = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise AgentHomeError(f"WECOM_AGENT_HOME 不存在: {root}")
        return root

    env = os.environ.get("WECOM_AGENT_HOME", "").strip()
    if env:
        return _resolve_root(Path(env))

    if VENDOR_HOME.is_dir() and any(VENDOR_HOME.iterdir()):
        return VENDOR_HOME.resolve()

    raise AgentHomeError(
        "未找到 wecom-agent。请设置 WECOM_AGENT_HOME 指向本地 checkout,"
        "或执行 `git submodule update --init` 拉取 vendor/wecom-agent。"
    )


def resolve_agent_home(explicit: Optional[Path] = None) -> AgentHome:
    """解析 agent home,并定位 wecom.py 与 decrypt/ 目录。"""
    root = _resolve_root(explicit)

    cli = next((root / c for c in _CLI_CANDIDATES if (root / c).is_file()), None)
    if cli is None:
        tried = ", ".join(str(c) for c in _CLI_CANDIDATES)
        raise AgentHomeError(f"在 {root} 下未找到 wecom.py(已尝试: {tried})")

    return AgentHome(root=root, wecom_cli=cli, decrypt_dir=root / "decrypt")


_POSITIVE_DB_CACHE: dict[tuple[str, str], str] = {}


def find_db(decrypt_dir: Path, name: str) -> str:
    """在 decrypt/ 下查找解密后的数据库文件,找不到返回空串。

    仅缓存命中结果 —— 数据库是 read_wecom.py 解密后才出现的,
    未命中不能缓存,否则解密完成后仍会一直报缺失。
    """
    key = (str(decrypt_dir), name)
    cached = _POSITIVE_DB_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached

    for root, _dirs, files in os.walk(decrypt_dir):
        if name in files:
            found = os.path.join(root, name)
            _POSITIVE_DB_CACHE[key] = found
            return found

    fallback = decrypt_dir / "macos" / "decrypted" / "Messages1" / name
    if fallback.exists():
        _POSITIVE_DB_CACHE[key] = str(fallback)
        return str(fallback)
    return ""
