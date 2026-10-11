"""Prompt provenance: who sent this prompt, if it was not the human operator.

Every prompt reaches a session through ``orchestrator.send_prompt``, whether a person
typed it into the composer or autopilot composed it. The driver echoes each one as an
``ev.UserMessage``, so the transcript rendered them identically — a right-aligned blue
bubble that reads as "the user said this". It didn't, and there was no field anywhere
that said so.

The fix has to survive a round-trip the app does not control. ``replay.py`` rebuilds
every user turn by parsing the **Claude CLI's own JSONL transcript**, so provenance held
only on the live event (or only in ViberPit's database) disappears on the next page
load. This module therefore does what the context chip already does for "which page was
the user on" (:mod:`server.assistant.context`): render the fact into a single-line
marker prepended to the SDK-bound text — which the CLI journals verbatim — and split it
back off at replay.

Two consequences are deliberate:

* **The model sees it too.** An automated nudge is not the operator speaking, and until
  now a session could not tell the difference either. The marker is the only channel
  that reaches both the transcript and the model.
* **It is never trusted.** :func:`strip_origin_header` always removes the line but only
  returns an origin for a *known* kind, so a prompt containing a hand-forged marker
  cannot paint itself as autopilot — and a marker written by a newer build cannot leak
  into the bubble as raw text after a rollback.
"""
from __future__ import annotations

import re
from typing import Any, NotRequired, TypedDict
from xml.sax.saxutils import quoteattr, unescape

from viberpit import brand

# The closed set of senders. A kind that is not listed here yields no origin (the
# marker is still stripped), so adding a sender means adding it HERE and giving it a
# label in the web `ORIGIN_LABELS` map — the two ends are meant to be edited together.
ORIGIN_KINDS = frozenset({
    "autopilot",    # a composed nudge from the autopilot control loop
    "completion",   # the completion-readiness nudge (goal not met on self-declared done)
    "assistant",    # the AI assistant drawer acting on the user's behalf
    "automation",   # any other automation (PR doctor, backlog runner, …) — see `label`
})

# Cosmetic only, and rendered into the model's prompt, so it is bounded and single-line.
# An unusable label is DROPPED rather than rejecting the marker: losing "PR Doctor" off
# an otherwise-correct "automation" card is a far better failure than showing that card
# as a human message.
_LABEL = re.compile(r"\A[^\n\r]{1,64}\Z")


class PromptOrigin(TypedDict):
    kind: str
    label: NotRequired[str]
    #: True only for an origin RECONSTRUCTED after the fact by
    #: :mod:`server.sessions.origin_backfill`, for a prompt sent before this marker
    #: existed. Never set by the send path, and deliberately not accepted from a
    #: header — an inferred origin is a claim about the past based on a timestamp
    #: match, and the UI says so rather than presenting it as recorded fact.
    inferred: NotRequired[bool]


def validate_origin(raw: Any) -> PromptOrigin | None:
    """Narrow untrusted input to a known origin, or ``None``."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in ORIGIN_KINDS:
        return None
    out: PromptOrigin = {"kind": kind}
    label = raw.get("label")
    if isinstance(label, str) and _LABEL.match(label):
        out["label"] = label
    return out


def render_origin_header(origin: PromptOrigin) -> str:
    """Single-line, attribute-escaped marker for the head of the SDK-bound prompt.

    Kept to one line, like the context header, because the strip side is anchored to a
    single line — and because the CLI journals this verbatim into a file a human may
    well end up reading.
    """
    parts = [f"kind={_attr(origin['kind'])}"]
    if origin.get("label"):
        parts.append(f"label={_attr(origin['label'])}")
    return f"<{brand.SLUG}-origin {' '.join(parts)} />"


def _attr(value: str) -> str:
    """``quoteattr`` pinned to DOUBLE quotes.

    Bare ``quoteattr`` switches to single quotes when the value contains a ``"``, which
    `_ATTR` below (``="…"`` only) would then fail to read back — a label like
    ``the "fix" bot`` round-tripped to no label at all. Pre-mapping ``"`` to ``&quot;``
    makes the choice unconditional. ``context.render_context_header`` is not exposed to
    this: its values are regex-validated to exclude quotes entirely.
    """
    return quoteattr(value, {'"': "&quot;"})


# Anchored to the start and to a single line: a marker occurring anywhere else is the
# user's own words (or a transcript they pasted), never ours.
#
# Both brand spellings are matched, permanently — same reasoning as
# `context._HEADER_LINE`. Prompts journalled under either name are re-parsed on every
# reload, and a parser that knew only the current slug would show those users a raw
# marker line above the prompt forever.
_HEADER_LINE = re.compile(rf"\A<{brand.EITHER_SLUG_RE}-origin ([^\n>]*?)\s*/>(?:\n|\Z)")
_ATTR = re.compile(r'([A-Za-z]+)="([^"]*)"')


def strip_origin_header(text: str) -> tuple[str, PromptOrigin | None]:
    """Split a leading origin marker off a REPLAYED prompt.

    Returns ``(text_without_marker, origin_or_None)``. The marker always comes off; the
    origin is put back through :func:`validate_origin`, so an unknown or forged kind
    yields ``None``. Never show the marker, and never trust it.
    """
    m = _HEADER_LINE.match(text)
    if m is None:
        return text, None
    attrs = {k: unescape(v, {"&quot;": '"'}) for k, v in _ATTR.findall(m.group(1))}
    raw: dict[str, Any] = {"kind": attrs.get("kind", "")}
    if "label" in attrs:
        raw["label"] = attrs["label"]
    return text[m.end():], validate_origin(raw)
