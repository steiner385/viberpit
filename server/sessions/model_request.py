"""Single source of truth for the session-initiated model-request marker.

The same literal is appended to every session's system prompt (so the agent knows
how to ask for a different model) and scanned for by the Translator (so the app
can act on it). Keeping both in one module prevents drift — the same guarantee
``completion.py`` provides for the completion marker. That guarantee is why the
rebrand's emit half and parse half had to ship in one PR: the instruction below
teaches ``viberpit:``, and the regex above it accepts both spellings.

The tier rubric below is deliberately identical to
``server.automations.model_pick._ASSESS_SYSTEM`` so a session's self-assessment and
the spawn-time complexity assessor speak one vocabulary rather than two.
"""
from __future__ import annotations

import re

from ccswitch import brand

# Matches: <!-- viberpit:model=<alias-or-id> [reason="..."] --> and its pre-rebrand
# `ccswitch:` spelling, permanently (stored transcripts are re-parsed on replay).
# The model token excludes whitespace and '-->' so a malformed marker cannot swallow
# the rest of the message; the reason is a non-greedy run inside double quotes.
MODEL_REQUEST_RE = re.compile(
    rf'<!--\s*{brand.EITHER_SLUG_RE}:model=([A-Za-z0-9._\[\]-]+)'
    r'(?:\s+reason="([^"]*)")?\s*-->'
)

MODEL_REQUEST_INSTRUCTION = (
    "If partway through this session you determine the work needs a different model "
    "tier than the one you are running on, end a message with "
    f'`<!-- {brand.SLUG}:model=<tier> reason="<short reason>" -->` on its own line. Tiers, '
    "cheapest first:\n"
    "- haiku: trivial/mechanical work — typo or copy fixes, config bumps, single-line "
    "changes, dependency pins.\n"
    "- sonnet: a typical single-concern change — one feature or bugfix with its tests, "
    "limited blast radius.\n"
    "- opus: hard multi-file work — cross-module changes, tricky debugging, CI failures "
    "with unclear cause, merge-conflict resolution.\n"
    "- fable: the hardest architectural or cross-cutting work — large refactors, subtle "
    "concurrency/data-integrity issues, multi-PR consolidation.\n"
    "Ask only when the work has turned out materially harder or easier than it looked — "
    "never routinely, and never more than once for the same conclusion. A request for a "
    "cheaper tier takes effect on your next turn; a request for a more expensive tier is "
    "held for a human, who may approve it at a different tier than you asked for. Keep "
    "working either way — never wait for the switch."
)


def parse_model_request(text: str) -> dict | None:
    """Extract the LAST model request from *text*.

    Returns ``{"model": str, "reason": str | None}``, or ``None`` when *text* carries
    no well-formed marker. Last-wins because a session that reconsiders within one
    turn means the final word.

    Returns ``None`` for any non-str input (fail-open: never raises).
    """
    if not isinstance(text, str) or not text:
        return None
    matches = MODEL_REQUEST_RE.findall(text)
    if not matches:
        return None
    model, reason = matches[-1]
    return {"model": model, "reason": reason or None}
