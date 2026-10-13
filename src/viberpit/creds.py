"""Credential file I/O and the claudeAiOauth/mcpOAuth split."""
import contextlib
import copy
import fcntl
import json
import logging
import math
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from . import paths
from .mcp_names import short_name

log = logging.getLogger(__name__)

_mcp_write_lock = threading.Lock()


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def write_json_secure(path: Path, data: dict) -> None:
    """Atomically write JSON with mode 0600 (store root + parent dir forced to 0700).

    Writes to a sibling temp file in the same directory, then uses os.replace()
    (atomic rename on POSIX) to swap it in.  A crash or kill mid-write cannot
    corrupt or truncate the existing file — os.replace is the only syscall that
    changes the visible file.
    """
    paths.ensure_root()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    # Write to a UNIQUE temp file in the same directory (same-filesystem so
    # os.replace is atomic). A fixed temp name races when two threads write the
    # SAME path concurrently — one writer's os.replace renames the temp away
    # before the other's chmod/replace runs (FileNotFoundError / corruption).
    # mkstemp gives each writer its own 0600 temp file.
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.fchmod(fd, 0o600)
        os.close(fd)
        # Atomic swap: os.replace is guaranteed atomic on POSIX (rename(2)).
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def split_credentials(full: dict) -> tuple[dict, dict]:
    """Return (claudeAiOauth, mcpOAuth) from a full .credentials.json dict."""
    return dict(full.get("claudeAiOauth", {})), dict(full.get("mcpOAuth", {}))


def compose_credentials(claude_oauth: dict, mcp_oauth: dict) -> dict:
    """Build a .credentials.json dict from the two parts."""
    out: dict = {}
    if mcp_oauth:
        out["mcpOAuth"] = mcp_oauth
    if claude_oauth:
        out["claudeAiOauth"] = claude_oauth
    return out


def merge_keep_later(existing: dict | None, incoming: dict) -> dict:
    """Keep whichever claudeAiOauth blob has the later expiresAt (refresh is monotonic)."""
    if existing is None:
        return incoming
    if existing.get("expiresAt", 0) >= incoming.get("expiresAt", 0):
        return existing
    return incoming


def _mcp_expiry(blob: dict) -> float:
    value = blob.get("expiresAt")
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0


def _mcp_bundle_replaces(old: object, new: dict, *, explicit: bool) -> bool:
    """The ONE per-key MCP bundle policy: does ``new`` replace ``old``?

    Empty/discovery-only snapshots cannot revoke an existing token; an explicit login
    replaces without a later expiry; otherwise ``new`` must prove a strictly later one."""
    if isinstance(old, dict) and old.get("accessToken"):
        if not new.get("accessToken"):
            return False
        if not explicit and _mcp_expiry(new) <= _mcp_expiry(old):
            return False
    return True


@contextlib.contextmanager
def _shared_mcp_locked() -> Iterator[Path]:
    """Hold the shared MCP store's thread lock + flock; yields the store path.

    The lock serializes local threads/processes; flock is not a distributed lock
    on SSHFS. Callers on separate hosts still rely on credential-authority policy.
    """
    with _mcp_write_lock:
        path = paths.shared_mcp_path()
        paths.ensure_root()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def read_shared_mcp() -> dict:
    """The shared MCP store (``store/mcp.shared.json``), or ``{}`` when absent."""
    path = paths.shared_mcp_path()
    return read_json(path) if path.exists() else {}


def update_shared_mcp(update: Callable[[dict], dict]) -> dict:
    """Read-modify-write the shared MCP store under its lock; return the new contents.

    *update* receives a deep copy of the store and returns the desired store. The file is
    rewritten only when the result differs. The one locked writer of the store: the
    per-key merge and the refresher's compare-and-swap both go through it."""
    with _shared_mcp_locked() as path:
        existing = read_json(path) if path.exists() else {}
        updated = update(copy.deepcopy(existing))
        if updated != existing:
            write_json_secure(path, updated)
        return updated


def merge_shared_mcp(incoming: dict, *, authenticated_keys: set[str] | None = None) -> int:
    """Merge account MCP snapshots without rolling back another account's login.

    Keep whole credential bundles together: token, issuer, client and refresh
    metadata must come from the same observation. Empty/discovery-only snapshots
    cannot revoke shared credentials. Explicit server login can replace a bundle
    without a later expiry; background sync must prove its token is newer.
    """
    def merge(existing: dict) -> dict:
        merged = dict(existing)
        for key, new in incoming.items():
            if not isinstance(new, dict):
                continue
            explicit = authenticated_keys is not None and key in authenticated_keys
            if _mcp_bundle_replaces(existing.get(key), new, explicit=explicit):
                merged[key] = new
        return merged

    return len(update_shared_mcp(merge))


def public_view(bundle: dict) -> dict:
    """The copy of an MCP OAuth bundle an account dir may hold: everything but the refresh
    token. The shared store is the only holder of MCP refresh tokens (spec 2026-10-13), so a
    CLI never refreshes a grant ViberPit's single refresher owns. Client registration is
    kept so a later login reuses the DCR client."""
    return {k: v for k, v in bundle.items() if k != "refreshToken"}


def _holds_refresh_token(blob: object) -> bool:
    return isinstance(blob, dict) and bool(blob.get("refreshToken"))


def capture_cfg_mcp(dir_mcp: dict) -> int:
    """Offer a dir's MCP bundles to the shared store under the bundle policy (newer wins).

    Keeps a login made directly in a terminal ``claude`` under that dir, including one from a
    provider that issues no refresh token. A bundle without a refresh token is offered only
    for a key the store lacks: for any other key it is the store's own public copy, and
    offering it back would resurrect an access token the refresher has since cleared as dead.
    Returns the number of bundles offered."""
    store = read_shared_mcp()
    held = {k: v for k, v in dir_mcp.items()
            if isinstance(v, dict) and (_holds_refresh_token(v) or k not in store)}
    if held:
        merge_shared_mcp(held)
        log.debug("mcp capture: offered %d bundle(s) keys=%s", len(held), sorted(held))
    return len(held)


def publish_mcp(dir_mcp: dict, store: dict, *, server: str | None = None) -> dict:
    """The dir's ``mcpOAuth`` after publishing *store* into it. Pure.

    For each store key the public view replaces the dir copy when that copy is missing,
    holds a refresh token, or loses under the bundle policy (explicit for *server*'s keys
    after a login). Keys only the dir has are kept."""
    merged = dict(dir_mcp)
    for key, blob in store.items():
        if not isinstance(blob, dict):
            continue
        old = dir_mcp.get(key)
        explicit = (server is not None and short_name(str(blob.get("serverName"))) == server
                    and bool(blob.get("accessToken")))
        if (key not in dir_mcp or _holds_refresh_token(old)
                or _mcp_bundle_replaces(old, blob, explicit=explicit)):
            merged[key] = public_view(blob)
    return merged


# ---------------------------------------------------------------------------
# Encrypted credential helpers — apply ONLY to store slots, never to the
# composed cfg/<account>/.credentials.json that claude reads directly.
# ---------------------------------------------------------------------------

def read_credentials(path: Path) -> dict:
    """Read a store slot, decrypting refreshToken if encrypted.

    Returns the in-memory dict with a PLAINTEXT refreshToken.  The on-disk file
    is never modified.
    """
    data = read_json(path)
    rt = data.get("refreshToken")
    if rt is not None:
        try:
            from .crypto import decrypt, is_encrypted
        except ImportError:
            return data
        if is_encrypted(rt):
            data = dict(data)  # shallow copy — only top-level mutation
            data["refreshToken"] = decrypt(rt)
    return data


def write_credentials(path: Path, data: dict) -> None:
    """Write a store slot, encrypting refreshToken if a key is available.

    If no key is configured: writes as-is (plaintext) and emits a one-time
    stderr warning.  Never raises due to missing crypto — fail soft.
    """
    try:
        from .crypto import key_available, encrypt, is_encrypted, _warn_no_key_once
    except ImportError:
        # cryptography not installed — plaintext fallback, no warning
        write_json_secure(path, data)
        return

    rt = data.get("refreshToken")
    if rt is not None and not is_encrypted(rt):
        if key_available():
            out = copy.deepcopy(data)
            out["refreshToken"] = encrypt(rt)
            write_json_secure(path, out)
            return
        else:
            _warn_no_key_once()

    write_json_secure(path, data)


def sync_cfg_mcp_oauth(cfg_dir: Path, *, server: str | None = None) -> bool:
    """The one dir <-> store MCP OAuth sync for ONE cfg dir's .credentials.json, in place.

    1. Capture: the dir's refresh-token-bearing bundles merge into the shared store.
    2. Publish: the store's public view lands in the dir per :func:`publish_mcp`.

    Touches only the ``mcpOAuth`` key: a live CLI may have just rotated ``claudeAiOauth``
    in this same file, and a full compose would clobber it (cf. #2695). If the file is
    replaced between read and write (the CLI rotates it atomically) the write is skipped
    and the next sync catches up. Returns True when the file changed."""
    from .configdir import CREDENTIALS_NAME  # configdir imports this module

    path = Path(cfg_dir) / CREDENTIALS_NAME
    if not path.is_file():
        return False
    before = path.stat()
    current = read_json(path)
    existing = current.get("mcpOAuth")
    existing = existing if isinstance(existing, dict) else {}
    capture_cfg_mcp(existing)
    merged = publish_mcp(existing, read_shared_mcp(), server=server)
    if merged == existing:
        return False
    after = path.stat()
    if (after.st_ino, after.st_mtime_ns) != (before.st_ino, before.st_mtime_ns):
        log.debug("sync_cfg_mcp_oauth: %s changed during merge; skipping write", path)
        return False
    current["mcpOAuth"] = merged
    write_json_secure(path, current)
    return True
