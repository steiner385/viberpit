"""The context chip: what page the user was on when they asked (spec §4.5)."""
from __future__ import annotations

import re
from typing import Any, NotRequired, TypedDict   # Python 3.12 — NotRequired is stdlib
from xml.sax.saxutils import quoteattr

_ROUTE = re.compile(r"^/(sessions|pipeline|automations|initiatives|projects|archived|insights|settings)(/[A-Za-z0-9._-]+)*$")
_SID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class PromptContext(TypedDict):
    route: str
    sessionId: NotRequired[str]


def validate_context(raw: Any) -> PromptContext | None:
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
    """Single-line, attribute-escaped marker prepended to the SDK-bound prompt text
    so the model knows which page the user was looking at. ``host`` is
    ``local_host_stamp()`` — None on a single-host deploy (no Host row stamped this
    daemon) — and is simply omitted rather than rendered as the literal string
    "None"."""
    parts = [f"route={quoteattr(ctx['route'])}"]
    if ctx.get("sessionId"):
        parts.append(f"session={quoteattr(ctx['sessionId'])}")
    if host is not None:
        parts.append(f"host={quoteattr(host)}")
    return f"<ccswitch-context {' '.join(parts)} />"
