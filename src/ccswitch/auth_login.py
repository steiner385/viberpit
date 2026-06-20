"""Reauth profile preparation and cleanup for the account-reauthenticator flow."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from . import configdir, paths
from .creds import read_json, split_credentials, write_credentials, write_json_secure

_STRIP_SQL = "DELETE FROM cookies WHERE host_key LIKE '%claude%' OR host_key LIKE '%anthropic%'"


def _reauth_root() -> Path:
    """Ensure and return the reauth-tmp root directory (mode 0700)."""
    r = paths.reauth_tmp_dir()
    r.mkdir(parents=True, exist_ok=True)
    os.chmod(r, 0o700)
    return r


def _under_reauth_tmp(path: str) -> bool:
    """Check if path is under reauth-tmp and exists."""
    try:
        return Path(path).resolve(strict=True).is_relative_to(paths.reauth_tmp_dir().resolve())
    except (OSError, ValueError):
        return False


def prepare_reauth_profile(source_profile_dir: str) -> str:
    """Copy a Chrome profile directory to a temp location and strip Claude cookies.

    Args:
        source_profile_dir: Path to a Chrome profile directory (e.g. ~/.config/google-chrome/Default)

    Returns:
        Path to the temp profile directory (under reauth-tmp)

    Raises:
        FileNotFoundError: If the source Cookies database is missing
    """
    src = Path(source_profile_dir)
    src_cookies = src / "Cookies"
    if not src_cookies.exists():
        raise FileNotFoundError(f"chrome profile Cookies not found: {src_cookies}")

    # Create temp dir under reauth-tmp with 0700 perms
    old_umask = os.umask(0o077)
    try:
        temp = tempfile.mkdtemp(prefix="reauth-", dir=str(_reauth_root()))
        default = Path(temp) / "Default"
        default.mkdir()
    finally:
        os.umask(old_umask)
    os.chmod(temp, 0o700)
    os.chmod(default, 0o700)

    # Copy Cookies database (0600)
    dst_cookies = default / "Cookies"
    shutil.copy(src_cookies, dst_cookies)
    os.chmod(dst_cookies, 0o600)

    # Copy Local State from parent directory if it exists (0600)
    local_state = src.parent / "Local State"
    if local_state.exists():
        dst_ls = Path(temp) / "Local State"
        shutil.copy(local_state, dst_ls)
        os.chmod(dst_ls, 0o600)

    # Strip Claude/Anthropic cookies from the copied database
    conn = sqlite3.connect(str(dst_cookies))
    try:
        conn.execute(_STRIP_SQL)
        conn.commit()
    finally:
        conn.close()

    return temp


def cleanup_reauth_profile(path: str) -> None:
    """Remove a temp reauth profile directory.

    Only removes if the path is under reauth-tmp (safety guard).

    Args:
        path: Path to the temp profile directory
    """
    if _under_reauth_tmp(path):
        shutil.rmtree(path, ignore_errors=True)


def capture_login(
    name: str,
    *,
    env_extra: dict | None = None,
    claude_bin: Sequence[str] | None = None,
    extra_login_args: Sequence[str] | None = None,
) -> bool:
    """Run `claude auth login --claudeai` in a capture dir under reauth-tmp and store creds.

    The capture CLAUDE_CONFIG_DIR is created under `~/.config/ccswitch/reauth-tmp/`
    (never /tmp) and is cleaned up in a try/finally regardless of success or failure.

    The captured credential blob is NEVER logged or printed.

    Args:
        name: Account slot name to store credentials under.
        env_extra: Additional environment variables to merge (e.g. BROWSER, DISPLAY).
        claude_bin: Override the claude command argv list (default: ["claude"]).

    Returns:
        True if credentials were captured and stored; False otherwise.
    """
    from .config import load_config

    if claude_bin is None:
        cfg = load_config()
        import shlex as _shlex

        claude_cmd: list[str] = _shlex.split(
            os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin)
        )
    else:
        claude_cmd = list(claude_bin)

    login_args = ["auth", "login", "--claudeai"] + list(extra_login_args or [])

    # Create capture dir under reauth-tmp (NOT /tmp)
    capture_dir = tempfile.mkdtemp(prefix="capture-", dir=str(_reauth_root()))
    os.chmod(capture_dir, 0o700)
    try:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": capture_dir, **(env_extra or {})}
        subprocess.run(claude_cmd + login_args, env=env, shell=False)

        cred_path = os.path.join(capture_dir, ".credentials.json")
        if not os.path.exists(cred_path):
            return False

        # Extract and store credentials — blob is never logged
        full_blob = read_json(Path(cred_path))
        claude_oauth, mcp = split_credentials(full_blob)

        paths.store_dir().mkdir(parents=True, exist_ok=True)
        write_credentials(paths.slot_path(name), claude_oauth)

        if mcp:
            existing = (
                read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
            )
            existing.update(mcp)
            write_json_secure(paths.shared_mcp_path(), existing)

        # Capture per-account .claude.json (identity + caches)
        captured_claude_json = os.path.join(capture_dir, ".claude.json")
        if os.path.exists(captured_claude_json):
            claude_json_data = read_json(Path(captured_claude_json))
            write_json_secure(paths.store_claude_json_path(name), claude_json_data)
            # merge shareable keys into claude.shared.json (deferred import to avoid cycle)
            from .accounts import _merge_shared_claude_json

            _merge_shared_claude_json(claude_json_data)

        configdir.compose(name)
        return True

    finally:
        cleanup_reauth_profile(capture_dir)
