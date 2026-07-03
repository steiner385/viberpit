"""Model list for the session model selector — a 1-to-1 mirror of the `claude`
binary's `/model` picker.

The claude-agent-sdk exposes no model catalog (set_model is an opaque pass-through
to the `claude` binary). The stable contract is the binary's `/model` aliases, which
auto-resolve to the latest and survive binary version bumps (pinned ids churn every
release). This list surfaces EVERY alias the picker offers so ccswitch exposes exactly
what the binary does — no more, no less. Pinned ids (e.g. "claude-haiku-4-5") remain
accepted as free-text input; they are simply not part of the alias list. Account
authorization (Max/plan gating, 1M-context beta, etc.) is enforced by the
binary/account, not here.

Verified against the real binary (v2.1.197): the picker's alias array is
``["sonnet","opus","haiku","fable","best","sonnet[1m]","opus[1m]","fable[1m]","opusplan"]``
plus the special ``default`` row. The ``[1m]`` suffix requests the 1M-context beta for
that family; the binary strips it internally. Order matches the picker.
"""
from __future__ import annotations

# Every alias the `claude` /model picker accepts, in picker order (verified against
# the binary). "default" is the picker's recommended/account-default row.
ALIASES: list[str] = [
    "default",
    "sonnet",
    "opus",
    "haiku",
    "fable",
    "best",
    "sonnet[1m]",
    "opus[1m]",
    "fable[1m]",
    "opusplan",
]

_LABELS: dict[str, str] = {
    "default": "Default",
    "sonnet": "Sonnet",
    "opus": "Opus",
    "haiku": "Haiku",
    "fable": "Fable",
    "best": "Best",
    "sonnet[1m]": "Sonnet (1M context)",
    "opus[1m]": "Opus (1M context)",
    "fable[1m]": "Fable (1M context)",
    "opusplan": "Opus Plan",
}


def catalog() -> list[dict]:
    """The full alias list as [{"id", "label"}], in picker order."""
    return [{"id": a, "label": _LABELS[a]} for a in ALIASES]


def is_known_alias(name: str) -> bool:
    return name in ALIASES
