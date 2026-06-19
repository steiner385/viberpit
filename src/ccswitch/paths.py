"""Filesystem locations. All env-overridable for testing/isolation."""
import os
from pathlib import Path


def root() -> Path:
    return Path(os.environ.get("CCSWITCH_HOME", str(Path.home() / ".claude-accounts")))


def ensure_root() -> Path:
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    os.chmod(r, 0o700)
    return r


def shared_base() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def store_dir() -> Path:
    return root() / "store"


def cfg_root() -> Path:
    return root() / "cfg"


def ledger_path() -> Path:
    return root() / "ledger.json"


def config_path() -> Path:
    return root() / "config.json"


def slot_path(name: str) -> Path:
    return store_dir() / f"{name}.cred.json"


def shared_mcp_path() -> Path:
    return store_dir() / "mcp.shared.json"


def account_cfg_dir(name: str) -> Path:
    return cfg_root() / name
