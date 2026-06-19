"""Credential file I/O and the claudeAiOauth/mcpOAuth split."""
import json
import os
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def write_json_secure(path: Path, data: dict) -> None:
    """Write JSON with mode 0600 (parent dir forced to 0700)."""
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
    if not existing:
        return incoming
    if existing.get("expiresAt", 0) >= incoming.get("expiresAt", 0):
        return existing
    return incoming
