"""RICE score — the single formula (spec 003 §4 Score; Initiative RICE-lite comment)."""
from __future__ import annotations


def rice_score(reach: int | None, impact: int | None, effort: int | None,
               confidence: int | None) -> float | None:
    if None in (reach, impact, effort, confidence) or not effort:
        return None
    return round(reach * impact * confidence / effort, 2)  # type: ignore[operator]
