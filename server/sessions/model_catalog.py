"""Curated model list for the session model selector — the single source of truth.

The claude-agent-sdk exposes no model catalog (set_model is an opaque pass-through
to the `claude` binary). The stable contract is the binary's /model aliases, which
auto-resolve to the latest and survive binary version bumps (pinned ids churn every
release). Pinned ids (e.g. "claude-haiku-4-5") remain accepted as free-text input;
they are simply not part of the curated alias list. Account authorization is enforced
by the binary/account, not here.
"""
from __future__ import annotations

# Stable aliases the `claude` /model picker accepts (verified against the binary).
ALIASES: list[str] = ["default", "opus", "sonnet", "haiku", "opusplan"]

_LABELS: dict[str, str] = {
    "default": "Default",
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "opusplan": "Opus Plan",
}


def catalog() -> list[dict]:
    """The curated list as [{"id", "label"}], in display order."""
    return [{"id": a, "label": _LABELS[a]} for a in ALIASES]


def is_known_alias(name: str) -> bool:
    return name in ALIASES
