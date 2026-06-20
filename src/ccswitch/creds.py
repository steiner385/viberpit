"""Credential file I/O and the claudeAiOauth/mcpOAuth split."""
import copy
import json
import os
from pathlib import Path

from . import paths


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def write_json_secure(path: Path, data: dict) -> None:
    """Write JSON with mode 0600 (store root + parent dir forced to 0700)."""
    paths.ensure_root()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


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
