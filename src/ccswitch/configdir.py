"""Compose a per-account CLAUDE_CONFIG_DIR: symlink mirror of ~/.claude + real creds."""
import os
import shutil
from pathlib import Path

from . import paths
from .creds import compose_credentials, read_json, write_json_secure

CREDENTIALS_NAME = ".credentials.json"
CLAUDE_JSON_NAME = ".claude.json"

# Only these names from shared_base() are symlinked into the per-account cfg dir.
# Everything else (sessions/, agents/, tasks/, .credentials.json, .claude.json, …)
# stays isolated to each account.  (Principle VI — no over-sharing.)
SHARE_ALLOWLIST = {"plugins", "commands", "settings.json", "settings.local.json", "projects"}


def reconcile_links(name: str) -> None:
    """Idempotently symlink ONLY the share-allowlist entries from shared_base() into cfg/<name>.

    Never symlinks .credentials.json or .claude.json; never symlinks entries not in
    SHARE_ALLOWLIST even if they exist in shared_base().

    Also prunes stale over-share symlinks: any symlink inside the cfg dir whose target
    resolves inside shared_base() but whose name is NOT in SHARE_ALLOWLIST is removed.
    This corrects cfg dirs composed by older code that symlinked sessions/, agents/, etc.
    """
    cfg_dir = paths.account_cfg_dir(name)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg_dir, 0o700)
    base = paths.shared_base()
    base_resolved = base.resolve()

    # --- prune stale over-share symlinks -----------------------------------------
    # Entries that are protected real files — never touch these.
    _PROTECTED = {CREDENTIALS_NAME, CLAUDE_JSON_NAME}

    for child in cfg_dir.iterdir():
        if child.name in _PROTECTED:
            continue
        if child.name in SHARE_ALLOWLIST:
            continue
        if not child.is_symlink():
            continue
        # It's a symlink whose name is not on the allowlist. Check whether its
        # target resolves inside shared_base() — i.e. it's a stale over-share.
        try:
            target_resolved = Path(os.readlink(child)).resolve()
        except OSError:
            continue
        # target_resolved is inside base_resolved if base_resolved is one of its parents
        # (or equal to it, for the exact shared_base dir itself)
        try:
            target_resolved.relative_to(base_resolved)
        except ValueError:
            continue  # target is NOT under shared_base — leave it alone
        # Stale over-share: remove.
        child.unlink()

    # --- ensure allowlist symlinks ------------------------------------------------
    for entry_name in SHARE_ALLOWLIST:
        source = base / entry_name
        if not source.exists():
            continue  # only symlink entries that actually exist in shared_base
        link = cfg_dir / entry_name
        if link.is_symlink():
            if Path(os.readlink(link)) == source:
                continue  # already correct — nothing to do
            link.unlink()
        elif link.exists():
            # a real file/dir shadowing the shared entry — remove so the symlink wins
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
            else:
                link.unlink()
        link.symlink_to(source)


def compose_claude_json(name: str) -> dict:
    """Build the per-account .claude.json content.

    Priority (highest → lowest):
    1. Start from the per-account captured file (store/<name>.claude.json) if it exists.
       This preserves the correct oauthAccount / userID / local caches for this account.
    2. If no per-account file (migration path), fall back to the real global ~/.claude.json.
    3. If neither exists, start from an empty dict (launch will still work; identity just
       won't be pre-populated until the user logs in once).

    Then OVERLAY the shared allowlist keys (mcpServers, projects, allowedTools) from
    store/claude.shared.json so that MCP servers and project trust are shared across accounts.
    """
    per_account = paths.store_claude_json_path(name)
    if per_account.exists():
        base: dict = read_json(per_account)
    else:
        global_path = paths.global_claude_json()
        base = read_json(global_path) if global_path.exists() else {}

    # Overlay shared allowlist keys
    shared_path = paths.shared_claude_json_path()
    if shared_path.exists():
        shared: dict = read_json(shared_path)
        for key in ("mcpServers", "projects", "allowedTools"):
            if key in shared:
                base[key] = shared[key]

    return base


def compose(name: str) -> Path:
    """Reconcile links and write the composed .credentials.json + .claude.json.

    Returns the config dir path (set CLAUDE_CONFIG_DIR to this when launching claude).
    """
    reconcile_links(name)
    cfg_dir = paths.account_cfg_dir(name)

    # Write .credentials.json (real file, never a symlink)
    claude_oauth = read_json(paths.slot_path(name))
    mcp = read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
    write_json_secure(cfg_dir / CREDENTIALS_NAME, compose_credentials(claude_oauth, mcp))

    # Write .claude.json (real file, never a symlink)
    write_json_secure(cfg_dir / CLAUDE_JSON_NAME, compose_claude_json(name))

    # Remove backups/ if present — it's claude's corruption-recovery artifact from an unstable
    # compose; a clean stable .claude.json makes it unnecessary and it can confuse reloads.
    backups = cfg_dir / "backups"
    if backups.exists():
        if backups.is_symlink():
            backups.unlink()
        else:
            shutil.rmtree(backups)

    return cfg_dir
