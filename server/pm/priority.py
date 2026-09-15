"""Derived initiative priority (spec 2026-09-14-roadmap-auto-execution-design D6, §10).

Never stored. The epic issue's own RICE score when it is classified, else the
highest score among its OPEN sub-issues, else None. Called from the ONE
epic-progress helper (`server/api/sdlc.py:_epic_progress_batch`) so the
initiatives API, the roadmap map and the synthesizer's visiting order cannot
disagree (constitution VI).
"""
from __future__ import annotations

from collections.abc import Iterable


def initiative_priority(epic_score: float | None,
                        open_leaf_scores: Iterable[float | None]) -> float | None:
    if epic_score is not None:
        return epic_score
    scores = [s for s in open_leaf_scores if s is not None]
    return max(scores) if scores else None
