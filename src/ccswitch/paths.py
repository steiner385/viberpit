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


def attachments_dir() -> Path:
    return root() / "attachments"


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


def global_claude_json() -> Path:
    """$HOME/.claude.json — the real claude binary's global config file."""
    return Path.home() / ".claude.json"


def store_claude_json_path(name: str) -> Path:
    """Per-account captured .claude.json in the store (identity + caches)."""
    return store_dir() / f"{name}.claude.json"


def shared_claude_json_path() -> Path:
    """Shared allowlist overlay (mcpServers, projects, allowedTools) for all accounts."""
    return store_dir() / "claude.shared.json"


def account_cfg_dir(name: str) -> Path:
    return cfg_root() / name


def reauth_tmp_dir() -> Path:
    return root() / "reauth-tmp"


def archive_dir() -> Path:
    """Directory where archived session working directories are stored."""
    d = root() / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d
