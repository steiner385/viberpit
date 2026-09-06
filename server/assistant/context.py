"""The context chip: what page the user was on when they asked (spec §4.5)."""
from __future__ import annotations

import re
from typing import Any, NotRequired, TypedDict   # Python 3.12 — NotRequired is stdlib
from xml.sax.saxutils import quoteattr, unescape

# \Z (not $): `$` matches just before a single trailing "\n" too, so a route or
# sessionId with a smuggled newline would pass validation and reach the wire
# (UserMessage.context) unescaped — quoteattr only protects the SDK-bound header.
_ROUTE = re.compile(r"\A/(sessions|pipeline|automations|initiatives|projects|insights|settings|cli)(/[A-Za-z0-9._-]+)*\Z")
_SID = re.compile(r"\A[A-Za-z0-9-]{8,64}\Z")

# A route is rendered verbatim into the model's prompt on every context change, and the
# client chooses it — so cap it. Not a ReDoS guard (the regex above is unambiguous:
# every alternative starts with a distinct literal and the tail is a simple star over a
# character class); this is purely about token burn from a pathologically long path.
_MAX_ROUTE_LEN = 256


class PromptContext(TypedDict):
    route: str
    sessionId: NotRequired[str]


def validate_context(raw: Any) -> PromptContext | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("route"), str):
        return None
    if len(raw["route"]) > _MAX_ROUTE_LEN:
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
    so the model knows which page the user was looking at.

    ``host`` is ``server.hostname.self_host_name()`` — the name this process answers to,
    always a string. (The plan named ``local_host_stamp()``; that is an OWNERSHIP marker
    which returns None on the primary by design, so ``host=`` would never have rendered
    in any topology — assistants are pinned to the primary. Spec §4.5's rendered output
    is binding, so the source changed.) ``None`` is still accepted and simply omits the
    attribute rather than rendering the literal string "None"."""
    parts = [f"route={quoteattr(ctx['route'])}"]
    if ctx.get("sessionId"):
        parts.append(f"session={quoteattr(ctx['sessionId'])}")
    if host is not None:
        parts.append(f"host={quoteattr(host)}")
    return f"<ccswitch-context {' '.join(parts)} />"


# The rendered header, as it appears at the head of a JSONL user entry. Anchored to the
# start and to a single line: the header is always emitted as one line followed by "\n"
# and the user's own text, and a `<ccswitch-context` occurring anywhere else is the
# user's own words, not ours.
_HEADER_LINE = re.compile(r"\A<ccswitch-context ([^\n>]*?)\s*/>(?:\n|\Z)")
_ATTR = re.compile(r'([A-Za-z]+)="([^"]*)"')


def strip_context_header(text: str) -> tuple[str, PromptContext | None]:
    """Split a leading ``<ccswitch-context …/>`` header off a REPLAYED user prompt.

    ``driver._start_turn`` prepends the header to the copy of the text handed to the SDK
    (never to the live ``UserMessage``), so the CLI journals the DECORATED text. Replay
    rebuilds ``UserMessage`` straight from that JSONL, which without this would show the
    user a literal ``<ccswitch-context route="/sessions/abc" … />`` line above their own
    prompt on every reload — the same artifact class as the #1000 owner-prompt prefix —
    and would carry no ``context`` for the client to render the chip from.

    Returns ``(text_without_header, context_or_None)``. The context is put back through
    ``validate_context`` so a malformed or hand-forged header yields ``None`` while the
    line is still stripped: never show the marker, and never trust it.
    """
    m = _HEADER_LINE.match(text)
    if m is None:
        return text, None
    attrs = {k: unescape(v, {"&quot;": '"'}) for k, v in _ATTR.findall(m.group(1))}
    raw: dict[str, Any] = {"route": attrs.get("route", "")}
    if "session" in attrs:
        raw["sessionId"] = attrs["session"]
    return text[m.end():], validate_context(raw)
