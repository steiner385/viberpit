"""RICE score — the single formula (spec 003 §4 Score; Initiative RICE-lite comment).

This module owns BOTH the formula and what counts as a valid RICE input. Anything
that accepts reach/impact/effort/confidence from outside (the overlay PATCH, and
later the AI ingest path in server/pm/classification.py) must range-check through
``validate_rice`` here rather than inlining its own bounds — an out-of-range value
flows straight through ``rice_score`` into the GitHub Projects ``Score`` field and
inverts roadmap ordering (a negative ``effort`` yields a negative score).
"""
from __future__ import annotations

# The four RICE inputs, in formula order.
RICE_FIELDS: tuple[str, ...] = ("reach", "impact", "effort", "confidence")

# Lower bound per field. ``effort`` is a divisor, so 0 is not merely odd — it makes
# the score undefined (``rice_score`` already returns None for it) and negatives
# invert the ordering; the other three are counts and may legitimately be 0.
RICE_MIN: dict[str, int] = {"reach": 0, "impact": 0, "effort": 1, "confidence": 0}

# Shared upper bound: high enough that no honest estimate hits it, low enough that
# a typo or a runaway model output is caught before it dominates every ranking.
RICE_MAX = 10000


def validate_rice(field: str, value: int | None) -> str | None:
    """Return a human-readable error for an out-of-range ``value``, else None.

    ``None`` is always valid — it means "not scored".
    """
    if field not in RICE_MIN:
        raise KeyError(f"{field!r} is not a RICE field ({RICE_FIELDS})")
    if value is None:
        return None
    low = RICE_MIN[field]
    if value < low or value > RICE_MAX:
        return f"{field} must be between {low} and {RICE_MAX} (got {value})"
    return None


def rice_score(reach: int | None, impact: int | None, effort: int | None,
               confidence: int | None) -> float | None:
    if None in (reach, impact, effort, confidence) or not effort:
        return None
    return round(reach * impact * confidence / effort, 2)  # type: ignore[operator]
