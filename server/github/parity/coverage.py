"""Per-phase parity field map: which fields the harness is allowed to compare.

Comparing a field the Python side does not yet produce (stage, ETA, deploy) would
be noise, so each migration phase widens this set explicitly.
"""
from __future__ import annotations

PHASE_FIELDS: dict[int, frozenset[str]] = {
    0: frozenset(
        {"number", "url", "state", "is_draft", "merged", "head_sha",
         "checks", "checks_detail"}
    ),
}


def covered_fields(phase: int) -> frozenset[str]:
    return PHASE_FIELDS[phase]
