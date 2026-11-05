"""Release-milestone sync planner: the definition file vs the DB rows.

Pure code, no DB — the same split as ``persona_index`` / ``persona_sync_seams``.
``plan_ladder_sync`` decides; ``server/pm/milestone_sync_seams.py`` applies.

Sync owns the *definition* half of a milestone row only. ``state``,
``entered_at``, ``exited_at``, ``exit_override`` and ``status_note`` are live
state and never appear in an upsert for an existing row; a brand-new row gets
``state="upcoming"``, and ``set_current`` (the file's ``start``) is honoured
only when the ladder itself is created.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .milestone_def import MILESTONES_PATH, MilestoneDefError, parse_ladder, to_json

FILE_REMOVED_ERROR = "definition file removed"


@dataclass(frozen=True)
class MilestoneRowView:
    key: str
    state: str | None
    removed: bool


@dataclass(frozen=True)
class LadderRowsView:
    exists: bool
    milestones: tuple[MilestoneRowView, ...]


@dataclass(frozen=True)
class LadderSyncPlan:
    create_ladder: bool
    # Column -> value for the ladder row. ``synced_at`` is not here: the seam
    # stamps it from the DB clock on every applied (non-no-op) plan.
    ladder_fields: dict[str, Any]
    parse_error: str | None
    upserts: tuple[dict[str, Any], ...]
    remove_keys: tuple[str, ...]
    revive_keys: tuple[str, ...]
    set_current: str | None
    delete_ladder: bool

    @property
    def is_noop(self) -> bool:
        return not self.create_ladder and not self.ladder_fields


_NOOP = LadderSyncPlan(False, {}, None, (), (), (), None, False)


def _error_plan(rows: LadderRowsView, error: str, sha: str) -> LadderSyncPlan:
    """Record the error and its commit; keep the last good definition and rows."""
    return LadderSyncPlan(
        create_ladder=not rows.exists,
        ladder_fields={"source_path": MILESTONES_PATH, "parse_error": error, "parse_error_sha": sha},
        parse_error=error,
        upserts=(), remove_keys=(), revive_keys=(), set_current=None, delete_ladder=False,
    )


def plan_ladder_sync(
    rows: LadderRowsView, *, text: str | None, sha: str, primary_repo: str, now: datetime,
) -> LadderSyncPlan:
    """Plan one sync of the definition file at commit *sha*. ``text is None``
    means the file is absent at that commit.

    *now* is the pass time; the plan is time-independent today (the seam
    stamps ``synced_at``/``removed_at``/``entered_at`` from the DB clock), but
    the parameter keeps the planner's contract stable for time-aware rules."""
    del now
    if text is None:
        if not rows.exists:
            return _NOOP
        # Deleting history needs an explicit owner action (non-goal for v1).
        return _error_plan(rows, FILE_REMOVED_ERROR, sha)
    try:
        ladder = parse_ladder(text, primary_repo=primary_repo)
    except MilestoneDefError as exc:
        return _error_plan(rows, str(exc), sha)

    definition = to_json(ladder)
    existing = {r.key: r for r in rows.milestones}
    file_keys = {m.key for m in ladder.milestones}
    upserts: list[dict[str, Any]] = []
    for ordinal, m in enumerate(definition["milestones"]):
        up: dict[str, Any] = {
            "key": m["key"],
            "ordinal": ordinal,
            "name": m["name"],
            "label": m["label"],
            "audience": m["audience"],
            "platforms": m["platforms"],
            "epics": m["epics"],
            "refs": m["refs"],
            "gate": m["gate"],
        }
        if m["key"] not in existing:
            up["state"] = "upcoming"
        upserts.append(up)
    return LadderSyncPlan(
        create_ladder=not rows.exists,
        ladder_fields={
            "source_path": MILESTONES_PATH,
            "source_sha": sha,
            "title": ladder.title,
            "spec_path": ladder.spec,
            "definition": definition,
            "parse_error": None,
            "parse_error_sha": None,
        },
        parse_error=None,
        upserts=tuple(upserts),
        remove_keys=tuple(r.key for r in rows.milestones if r.key not in file_keys and not r.removed),
        revive_keys=tuple(r.key for r in rows.milestones if r.key in file_keys and r.removed),
        set_current=ladder.start if not rows.exists else None,
        delete_ladder=False,
    )
