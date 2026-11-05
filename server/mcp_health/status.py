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
#: At most this many tools are kept per server, and each description is cut to
#: ``TOOL_DESCRIPTION_MAX`` characters (D-16b), so a pathological server cannot bloat a
#: payload. ``tool_count`` still reports the server's real count.
TOOLS_MAX = 200
TOOL_DESCRIPTION_MAX = 300


@dataclass(frozen=True, slots=True)
class McpTool:
    """One tool of an MCP server as ``mcp_status`` lists it (D-16 spike §2.3), trimmed to
    what ViberPit renders: the name, a capped description and the three hint annotations
    (``None`` when the server does not say)."""
    name: str
    description: str | None = None
    read_only: bool | None = None
    destructive: bool | None = None
    open_world: bool | None = None

    def to_wire(self) -> dict:
        return {"name": self.name, "description": self.description,
                "annotations": {"readOnly": self.read_only, "destructive": self.destructive,
                                "openWorld": self.open_world}}


@dataclass(frozen=True, slots=True)
class McpServerState:
    name: str
    status: str
    error: str | None
    tool_count: int | None
    scope: str | None
    auth: str | None = None          # "oauth" when the account holds an OAuth bundle for it
    # The server's tools (D-16b), or None when the reply carried no list: a ``liveReply``
    # that fell back to ``toolCount`` only. Not on ``to_wire``: the live ``mcpStatus``
    # event stays small; the session MCP endpoint serves them (``mcp_scope.resolve``).
    tools: tuple[McpTool, ...] | None = None

    def to_wire(self) -> dict:
        return {"name": self.name, "status": self.status, "error": self.error,
                "toolCount": self.tool_count, "scope": self.scope, "auth": self.auth}


def _flag(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def sanitize_tools(raw: object) -> tuple[McpTool, ...] | None:
    """The capped tool list of one ``mcp_status`` row, or None when it carries no list."""
    if not isinstance(raw, list):
        return None
    out: list[McpTool] = []
    for t in raw:
        if len(out) >= TOOLS_MAX:
            break
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
            continue
        desc = t.get("description")
        raw_ann = t.get("annotations")
        ann: dict = raw_ann if isinstance(raw_ann, dict) else {}
        out.append(McpTool(
            name=t["name"],
            description=desc[:TOOL_DESCRIPTION_MAX] if isinstance(desc, str) else None,
            read_only=_flag(ann.get("readOnly")), destructive=_flag(ann.get("destructive")),
            open_world=_flag(ann.get("openWorld"))))
    return tuple(out)


def tool_from_wire(d: object) -> McpTool | None:
    """Rebuild a tool from its ``to_wire`` dict (the persisted last-seen list)."""
    tools = sanitize_tools([d]) if isinstance(d, dict) else None
    return tools[0] if tools else None


_CLAUDE_AI_PREFIX = "claude.ai "
_UNDERSCORES = re.compile(r"_+")


def normalized_name(name: str) -> str:
    """A server or tool name as the CLI spells it inside ``mcp__<server>__<tool>``: the
    CLI's ``En`` (claude 2.1.318 @280040372) replaces ``[^a-zA-Z0-9_-]`` with ``_`` and,
    for a ``claude.ai `` connector, also collapses ``_`` runs and trims one ``_`` at
    each end. Probed: ``claude.ai My__Conn`` -> ``claude_ai_My_Conn``."""
    out = _NON_NAME.sub("_", name)
    if name.startswith(_CLAUDE_AI_PREFIX):
        out = _UNDERSCORES.sub("_", out)
        out = out[1:] if out.startswith("_") else out
        out = out[:-1] if out.endswith("_") else out
    return out


def mcp_tool_rule(server: str, tool: str) -> str:
    """The CLI tool name — and permission-rule pattern — of *server*'s *tool*."""
    return f"mcp__{normalized_name(server)}__{normalized_name(tool)}"


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


def bundle_expiry(bundle: dict) -> float | None:
    exp = bundle.get("expiresAt")
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def has_token(bundle: dict) -> bool:
    tok = bundle.get("accessToken")
    return isinstance(tok, str) and bool(tok)


def format_utc_ms(ms: float) -> str:
    """A UTC ``YYYY-MM-DD HH:MM`` rendering of an epoch-ms instant (the one expiry format)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _auth_error(bundles: list[dict], now_ms: int) -> str | None:
    """None when some bundle holds a usable token (non-empty, unexpired or no expiry; an
    ``expiresAt`` of 0 is a cleared bundle), else the user-facing reason."""
    if any(has_token(b) and ((exp := bundle_expiry(b)) is None or exp > now_ms) for b in bundles):
        return None
    expired = [exp for b in bundles if has_token(b) and (exp := bundle_expiry(b)) is not None and exp > 0]
    if expired:
        return f"Sign-in expired {format_utc_ms(max(expired))} UTC — re-authenticate."
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
        count = len(tools) if isinstance(tools, list) else s.get("toolCount")
        out.append(McpServerState(name=name, status=status, error=error,
                                  tool_count=count if isinstance(count, int)
                                  and not isinstance(count, bool) else None,
                                  scope=scope, auth=auth, tools=sanitize_tools(tools)))
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
    hits = [n for n in server_names if tool_name.startswith(f"mcp__{normalized_name(n)}__")]
    return max(hits, key=len) if hits else None


def token_fingerprint(credentials: dict | None, name: str) -> str:
    """A hash of server *name*'s current access token(s): detects a re-auth without
    keeping the token. In memory only; never logged."""
    tokens = sorted(b["accessToken"] for b in oauth_bundles(credentials, name) if has_token(b))
    return hashlib.sha256("\0".join(tokens).encode()).hexdigest()


def apply_auth_latch(states: list[McpServerState], latch: dict[str, str]) -> list[McpServerState]:
    """Report each latched server (name -> error) as ``needs-auth`` (``disabled`` untouched)."""
    return [replace(s, status="needs-auth", auth="oauth", error=latch[s.name])
            if s.name in latch and s.status != "disabled" else s for s in states]
