"""Compose a per-account CLAUDE_CONFIG_DIR: symlink mirror of ~/.claude + real creds."""
import os
from pathlib import Path

from . import paths
from .creds import compose_credentials, read_json, write_json_secure

CREDENTIALS_NAME = ".credentials.json"


def reconcile_links(name: str) -> None:
    """Idempotently symlink every shared-base entry (except .credentials.json) into cfg/<name>."""
    cfg_dir = paths.account_cfg_dir(name)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg_dir, 0o700)
    base = paths.shared_base()
    for entry in base.iterdir():
        if entry.name == CREDENTIALS_NAME:
            continue
        link = cfg_dir / entry.name
        target = entry
        if link.is_symlink():
            if Path(os.readlink(link)) == target:
                continue
            link.unlink()
        elif link.exists():
            # a real file/dir shadowing the shared entry — remove so the symlink wins
            if link.is_dir() and not link.is_symlink():
                import shutil
                shutil.rmtree(link)
            else:
                link.unlink()
        link.symlink_to(target)


def compose(name: str) -> Path:
    """Reconcile links and write the composed .credentials.json. Returns the config dir."""
    reconcile_links(name)
    cfg_dir = paths.account_cfg_dir(name)
    claude_oauth = read_json(paths.slot_path(name))
    mcp = read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
    write_json_secure(cfg_dir / CREDENTIALS_NAME, compose_credentials(claude_oauth, mcp))
    return cfg_dir
