"""Sanitized MCP server status — the one vocabulary every surface uses (spec 2026-10-09).

The SDK's McpServerStatus carries the server ``config`` (headers, env: secrets). Nothing
downstream ever sees it: this module is the only place a raw status dict is read.

It is also the one place an account dir's ``mcpOAuth`` bundles are read for status
(spec 2026-10-13 "Truthful status"): an OAuth server whose token is dead still reports
``connected`` (the server accepts ``initialize`` without auth), so the overlay below
derives ``needs-auth`` from the credentials themselves. Token values never leave here."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from server.mcp_spec import SERVER_NAME

ERROR_MAX = 500
_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
# An MCP tool result that says the server refused the token (spec 2026-10-13).
_AUTH_FAILURE = re.compile(r"TOKEN_EXPIRED|invalid_token|\b401\b|Unauthorized", re.IGNORECASE)
# An ok=True result only counts when its text clearly is an error, not data that mentions 401.
_ERRORISH = re.compile(r"error|fail|expired|denied|invalid|unauthorized", re.IGNORECASE)
_RESULT_TEXT_MAX = 20_000
_NON_NAME = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True, slots=True)
class McpServerState:
    name: str
    status: str
    error: str | None
    tool_count: int | None
    scope: str | None
    auth: str | None = None          # "oauth" when the account holds an OAuth bundle for it

    def to_wire(self) -> dict:
        return {"name": self.name, "status": self.status, "error": self.error,
                "toolCount": self.tool_count, "scope": self.scope, "auth": self.auth}


def redact_error(text: str | None) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    return _QUERY.sub(r"\1?<redacted>", text)[:ERROR_MAX]


def read_mcp_oauth(cfg_dir: str | Path | None) -> dict:
    """The ``mcpOAuth`` dict of ``<cfg_dir>/.credentials.json``; ``{}`` on any error.
    Blocking file IO: call it off the event loop."""
    if not cfg_dir:
        return {}
    try:
        data = json.loads((Path(cfg_dir) / ".credentials.json").read_text())
    except Exception:
        return {}
    mcp = data.get("mcpOAuth") if isinstance(data, dict) else None
    return mcp if isinstance(mcp, dict) else {}


def oauth_bundles(credentials: dict | None, name: str) -> list[dict]:
    """The ``mcpOAuth`` bundles that belong to server *name* (a server can have several)."""
    return [b for b in (credentials or {}).values()
            if isinstance(b, dict) and b.get("serverName") == name]


def _expiry(bundle: dict) -> float | None:
    exp = bundle.get("expiresAt")
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def _token(bundle: dict) -> bool:
    tok = bundle.get("accessToken")
    return isinstance(tok, str) and bool(tok)


def _auth_error(bundles: list[dict], now_ms: int) -> str | None:
    """None when some bundle holds a usable token (non-empty, unexpired or no expiry; an
    ``expiresAt`` of 0 is a cleared bundle), else the user-facing reason."""
    if any(_token(b) and ((exp := _expiry(b)) is None or exp > now_ms) for b in bundles):
        return None
    expired = [exp for b in bundles if _token(b) and (exp := _expiry(b)) is not None and exp > 0]
    if expired:
        when = datetime.fromtimestamp(max(expired) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        return f"Sign-in expired {when} UTC — re-authenticate."
    return "Not signed in — authenticate to use this server."


def sanitize_statuses(raw: object, *, credentials: dict | None = None,
                      now_ms: int | None = None) -> list[McpServerState]:
    """Sanitize a raw ``get_mcp_status`` answer. *credentials* is the account dir's
    ``mcpOAuth`` dict: a server with a bundle is ``auth="oauth"``, and one with no usable
    token is ``needs-auth`` whatever the CLI says (a ``disabled`` server is left alone)."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    entries = raw.get("mcpServers") if isinstance(raw, dict) else None
    out: list[McpServerState] = []
    for s in entries if isinstance(entries, list) else []:
        if not isinstance(s, dict):
            continue
        name, status = s.get("name"), s.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        if name == SERVER_NAME:
            continue
        scope = s.get("scope") if isinstance(s.get("scope"), str) else None
        tools = s.get("tools")
        error, auth = redact_error(s.get("error")), None
        bundles = oauth_bundles(credentials, name) if status != "disabled" else []
        if bundles:
            auth = "oauth"
            reason = _auth_error(bundles, now)
            if reason is not None:
                status, error = "needs-auth", reason
        out.append(McpServerState(name=name, status=status, error=error,
                                  tool_count=len(tools) if isinstance(tools, list) else None,
                                  scope=scope, auth=auth))
    return out


# --- Observed auth failures (a live session's tool results) ------------------------------

def tool_result_text(payload: object) -> str:
    """The text of a ToolResult payload (``{"content": str | [blocks]}``), capped."""
    content = payload.get("content") if isinstance(payload, dict) else payload
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b["text"] if isinstance(b, dict) and isinstance(b.get("text"), str)
                         else json.dumps(b, default=str) for b in content)
    else:
        text = json.dumps(content, default=str)
    return text[:_RESULT_TEXT_MAX]


def observed_auth_failure(ok: bool, payload: object) -> str | None:
    """The matched marker (e.g. ``TOKEN_EXPIRED``) when a tool result is an auth rejection."""
    text = tool_result_text(payload)
    m = _AUTH_FAILURE.search(text)
    if m is None or (ok and not _ERRORISH.search(text)):
        return None
    return m.group(0)


def rejected_error(marker: str) -> str:
    return f"Sign-in rejected by the server ({marker}) — re-authenticate."


def server_for_tool(tool_name: str | None, server_names: list[str]) -> str | None:
    """The server an ``mcp__<normalized server>__<tool>`` name belongs to (longest match)."""
    if not tool_name or not tool_name.startswith("mcp__"):
        return None
    hits = [n for n in server_names if tool_name.startswith(f"mcp__{_NON_NAME.sub('_', n)}__")]
    return max(hits, key=len) if hits else None


def token_fingerprint(credentials: dict | None, name: str) -> str:
    """A hash of server *name*'s current access token(s): detects a re-auth without
    keeping the token. In memory only; never logged."""
    tokens = sorted(b["accessToken"] for b in oauth_bundles(credentials, name) if _token(b))
    return hashlib.sha256("\0".join(tokens).encode()).hexdigest()


def apply_auth_latch(states: list[McpServerState], latch: dict[str, str]) -> list[McpServerState]:
    """Report each latched server (name -> error) as ``needs-auth`` (``disabled`` untouched)."""
    return [replace(s, status="needs-auth", auth="oauth", error=latch[s.name])
            if s.name in latch and s.status != "disabled" else s for s in states]
