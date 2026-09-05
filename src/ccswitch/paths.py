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
    """Account-INDEPENDENT Claude home that shared assets (plugins, commands,
    settings, projects) are mirrored FROM.

    Reads CLAUDE_CONFIG_DIR (the test harness injects a throwaway mirror source
    this way), but NEVER accepts a value that resolves inside our own account
    store (cfg_root()). ccswitch sets CLAUDE_CONFIG_DIR per-account when launching
    a session, so a value inherited from a running session points at an account
    dir; using that as the mirror source makes reconcile_links() symlink accounts
    to each other — or to themselves, a symlink loop. In that case fall back to
    the real ~/.claude. This keeps the share source a single canonical dir (a star
    topology), so account→account chains and loops are structurally impossible.
    """
    real_home = Path.home() / ".claude"
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return real_home
    candidate = Path(env)
    try:
        candidate.resolve().relative_to(cfg_root().resolve())
    except ValueError:
        return candidate  # outside the account store — a genuine external base
    return real_home  # inside cfg_root() → an account dir, never a shared base


def store_dir() -> Path:
    return root() / "store"


def cfg_root() -> Path:
    return root() / "cfg"


def ledger_path() -> Path:
    return root() / "ledger.json"


def config_path() -> Path:
    return root() / "config.json"


def accounts_manifest_path() -> Path:
    """The versioned, NON-SECRET account manifest (``accounts.json``).

    Deliberately a sibling of — never part of — ``ledger.json``: the ledger is a
    cross-host, multi-writer cache (usage, reservations, per-account toggles) whose
    known failure mode is a lost update. Identity metadata (which accounts exist,
    what provider/engine/auth mode each one is) must not share that write pattern,
    so the manifest has ONE writer (the primary/dobby host) and every runner reads
    the snapshot. See ``ccswitch.account_registry``.
    """
    return root() / "accounts.json"


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


def copilot_store_dir() -> Path:
    return store_dir() / "copilot"


def copilot_token_dir(name: str) -> Path:
    return copilot_store_dir() / name


def reauth_tmp_dir() -> Path:
    return root() / "reauth-tmp"


def archive_dir() -> Path:
    """Directory where archived session working directories are stored.

    Override with CCSWITCH_ARCHIVE_HOME to store archives on a separate
    filesystem (e.g. a larger/slower drive) from live session data.
    Defaults to $CCSWITCH_HOME/archive.
    """
    env = os.environ.get("CCSWITCH_ARCHIVE_HOME")
    d = Path(env) if env else root() / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def attachments_dir() -> Path:
    """Directory for content-addressed image attachments (named by sha256).

    Files are shared across DB Attachment rows: the same image bytes uploaded
    twice produce one on-disk file (dedup). Unlink is therefore reference-aware.
    """
    d = root() / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d
