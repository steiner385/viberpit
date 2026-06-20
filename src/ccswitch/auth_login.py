"""Reauth profile preparation and cleanup for the account-reauthenticator flow."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import paths

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
