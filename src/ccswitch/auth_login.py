"""Reauth profile preparation and cleanup for the account-reauthenticator flow."""
from __future__ import annotations

import fcntl
import logging
import os
import re
import select
import shlex
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import termios
from pathlib import Path
from typing import Callable, Sequence

from . import configdir, paths
from .creds import read_json, split_credentials, write_credentials, write_json_secure

log = logging.getLogger(__name__)

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

        claude_cmd: list[str] = shlex.split(
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

        # Warn if not a Max subscription
        sub_type = claude_oauth.get("subscriptionType")
        if sub_type != "max":
            log.warning("account %s is not a Max subscription (got %r)", name, sub_type)

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


# Matches the sk-ant-oat01 token (bytes pattern for PTY buffer)
_TOKEN_RE = re.compile(rb"sk-ant-oat01-[A-Za-z0-9_\-]+")
# Matches the authorize URL (bytes pattern; cut at first whitespace or ESC)
_URL_RE = re.compile(rb"https://claude\.com/cai/oauth/authorize[^\s\x1b]+")


def capture_setup_token(
    name: str,
    *,
    on_url: Callable[[str], None],
    get_code: Callable[[], str],
    claude_bin: Sequence[str] | None = None,
    env_extra: dict | None = None,
) -> bool:
    """Drive ``claude setup-token`` via a PTY, capture the sk-ant-oat01 token, and
    store it ENCRYPTED via crypto.store_oauth_token(name, token).

    Returns True on success, False otherwise.

    Flow:
    - spawn ``claude setup-token`` attached to a PTY (wide winsize so the URL
      doesn't wrap) in an isolated CLAUDE_CONFIG_DIR under reauth_tmp_dir()
    - read output; when the authorize URL appears, call on_url(url)
    - call get_code() (blocking) and write the returned code + "\\n" to the PTY
    - drain remaining PTY output after process exit (token is printed just
      before exit and can be missed by the in-loop reads)
    - extract the sk-ant-oat01 token; store via crypto.store_oauth_token

    The token value and the pasted code are NEVER logged or printed.
    """
    import pty

    from .config import load_config

    if claude_bin is None:
        cfg = load_config()
        claude_cmd: list[str] = shlex.split(
            os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin)
        )
    else:
        claude_cmd = list(claude_bin)

    argv = claude_cmd + ["setup-token"]

    # Create an isolated CLAUDE_CONFIG_DIR under reauth-tmp (not /tmp)
    reauth_root = paths.reauth_tmp_dir()
    reauth_root.mkdir(parents=True, exist_ok=True)
    os.chmod(reauth_root, 0o700)
    capture_dir = tempfile.mkdtemp(prefix="setuptok-", dir=str(reauth_root))
    os.chmod(capture_dir, 0o700)

    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": capture_dir,
        "TERM": "xterm-256color",
        **(env_extra or {}),
    }

    # Open a PTY pair; set a wide window so the URL prints on one line
    mo, sl = pty.openpty()
    fcntl.ioctl(sl, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 400, 0, 0))

    try:
        proc = subprocess.Popen(
            argv,
            stdin=sl,
            stdout=sl,
            stderr=sl,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(sl)  # child owns the slave end; close our copy

        buf = b""
        url_surfaced = False
        code_sent = False

        # Main read loop — runs while the process is alive
        while proc.poll() is None:
            ready, _, _ = select.select([mo], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(mo, 4096)
                    buf += chunk
                except OSError:
                    break  # PTY closed / EIO

            if not url_surfaced:
                m = _URL_RE.search(buf)
                if m:
                    # Decode as latin-1 (safe for arbitrary bytes) and strip
                    # any trailing ANSI escape that slipped in
                    url_raw = m.group(0).decode("latin-1")
                    url_clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]$", "", url_raw)
                    on_url(url_clean)
                    url_surfaced = True

            if url_surfaced and not code_sent:
                code = get_code()  # BLOCKING — caller provides the code
                os.write(mo, (code + "\n").encode())
                code_sent = True

        # Drain remaining PTY output — the token is printed just before exit
        # and can arrive after proc.poll() returns non-None
        while True:
            ready, _, _ = select.select([mo], [], [], 0.5)
            if not ready:
                break
            try:
                chunk = os.read(mo, 4096)
                if not chunk:
                    break
                buf += chunk
            except OSError:
                break

        os.close(mo)

        # Extract the token
        tm = _TOKEN_RE.search(buf)
        if not tm:
            log.debug("capture_setup_token: no sk-ant-oat01 token found in output")
            return False

        token = tm.group(0).decode("ascii")
        # Store encrypted — NEVER log the token value
        from .crypto import store_oauth_token

        store_oauth_token(name, token)
        log.debug("capture_setup_token: token stored for account %r", name)
        return True

    finally:
        # Clean up the isolated config dir
        try:
            shutil.rmtree(capture_dir, ignore_errors=True)
        except Exception:
            pass
