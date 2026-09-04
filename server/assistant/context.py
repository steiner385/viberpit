"""The context chip: what page the user was on when they asked (spec §4.5).

A leaf module — stdlib imports only, no `server.sessions.*` — so both
`server/sessions/driver.py` and `server/bridge/ws.py` may import it directly without
risking a cycle back through the orchestrator.

Trust boundary: ``validate_context`` is the only way a client-supplied ``context``
dict reaches ``render_context_header``. The route and sessionId patterns are strict
allowlists (no `.`/`/`-escaping tricks, no arbitrary length), and
``render_context_header`` XML-attribute-escapes every value it interpolates via
``quoteattr`` — a transcript, PR body, or tool result containing literal
``<ccswitch-context …>`` text can never be echoed back to the CLI as a second,
forged header because callers only ever pass validated, structured values here,
never raw untrusted text.
"""
from __future__ import annotations

import re
from typing import Any, NotRequired, TypedDict  # Python 3.12 — NotRequired is stdlib
from xml.sax.saxutils import quoteattr

_ROUTE = re.compile(
    r"^/(sessions|pipeline|automations|initiatives|projects|archived|insights|settings)"
    r"(/[A-Za-z0-9._-]+)*$")
_SID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class PromptContext(TypedDict):
    route: str
    sessionId: NotRequired[str]


def validate_context(raw: Any) -> PromptContext | None:
    """Strict allowlist validation of a client-supplied ``context`` payload.

    Returns ``None`` (never raises) for anything malformed — an untrusted or
    malformed context simply means no chip/header for this prompt, not an error
    surfaced to the user."""
    if not isinstance(raw, dict) or not isinstance(raw.get("route"), str):
        return None
    if not _ROUTE.match(raw["route"]):
        return None
    out: PromptContext = {"route": raw["route"]}
    sid = raw.get("sessionId")
    if sid is not None:
        if not isinstance(sid, str) or not _SID.match(sid):
            return None
        out["sessionId"] = sid
    return out


def render_context_header(ctx: PromptContext, host: str | None) -> str:
    """One single-line, self-closing tag, every value XML-attribute-escaped.

    ``host`` is ``None`` on the primary (``local_host_stamp()``'s NULL-host-is-the-
    primary convention — see server/sessions/orchestrator.py:local_host_stamp) — the
    ``host`` attribute is omitted entirely rather than rendered as the literal
    string ``"None"``.
    """
    parts = [f"route={quoteattr(ctx['route'])}"]
    if ctx.get("sessionId"):
        parts.append(f"session={quoteattr(ctx['sessionId'])}")
    if host is not None:
        parts.append(f"host={quoteattr(host)}")
    return f"<ccswitch-context {' '.join(parts)} />"
