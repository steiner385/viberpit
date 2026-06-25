"""Per-phase parity field map: which fields the harness is allowed to compare.

Comparing a field the Python side does not yet produce (stage, ETA, deploy) would
be noise, so each migration phase widens this set explicitly.
"""
from __future__ import annotations

# Phase 0 covers ONLY the fields the live Plotroom /api/state genuinely emits per PR.
# Reconciled against the real oracle (Principle III): Plotroom's per-PR object is
# {number, url, checks, title, stage, mergeStateStatus, ...} — it does NOT expose
# headRefName/head_sha, isDraft, or a PR `state` (it has a pipeline `stage` instead).
# So head_sha/is_draft/merged/state/checks_detail are not oracle-comparable yet and are
# deliberately out of the Phase-0 set; they re-enter as later phases make ccswitch the
# source of that data and widen this map.
PHASE_FIELDS: dict[int, frozenset[str]] = {
    0: frozenset({"number", "url", "checks"}),
    1: frozenset({"number", "url", "checks"}) | frozenset({"stage"}),
}


def covered_fields(phase: int) -> frozenset[str]:
    return PHASE_FIELDS[phase]
