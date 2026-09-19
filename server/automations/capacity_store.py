"""Read/write the capacity governor's single plan row (spec 2026-09-19 §4).

``live_plan`` / ``live_plan_sync`` are the ONE way a read seam (queue loader,
dispatch tap, fleet config) fetches a plan to overlay — they apply
``capacity_apply.plan_is_live`` so a stale or disabled plan silently returns
``None`` (no overlay) rather than every caller re-deriving "is this plan
still good" for itself (phase 2, spec §6).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from server.config import get_settings
from server.db import db_session
from server.db.models import CapacityPlanRow

PLAN_ID = "current"


def _to_dict(row: CapacityPlanRow) -> dict:
    return {
        "computedAt": row.computed_at.isoformat() if row.computed_at else None,
        "pushLevel": row.push_level, "applied": bool(row.applied),
        "fleetConcurrency": row.fleet_concurrency,
        "perProduct": dict(row.per_product or {}), "pending": dict(row.pending or {}),
        "budget": dict(row.budget or {}), "binding": row.binding,
        "reasons": list(row.reasons or []), "defects": list(row.defects or []),
    }


async def read_plan() -> dict | None:
    async with db_session() as db:
        row = await db.get(CapacityPlanRow, PLAN_ID)
        return None if row is None else _to_dict(row)


def read_plan_sync(db: Session) -> dict | None:
    """Sync-session counterpart of ``read_plan`` for a caller already holding one
    (the queue loader's ``policies_by_slug``)."""
    row = db.get(CapacityPlanRow, PLAN_ID)
    return None if row is None else _to_dict(row)


async def live_plan() -> dict | None:
    """The current plan, or ``None`` if it is not live (spec §6.3/D3/D5/§6.4)."""
    from server.automations.capacity_apply import plan_is_live
    plan = await read_plan()
    enabled = bool(get_settings().capacity_governor_enabled)
    now = datetime.now(timezone.utc)
    return plan if plan_is_live(plan, now=now, enabled=enabled) else None


def live_plan_sync(db: Session) -> dict | None:
    """Sync-session counterpart of ``live_plan``."""
    from server.automations.capacity_apply import plan_is_live
    plan = read_plan_sync(db)
    enabled = bool(get_settings().capacity_governor_enabled)
    now = datetime.now(timezone.utc)
    return plan if plan_is_live(plan, now=now, enabled=enabled) else None


async def write_plan(plan: dict) -> None:
    async with db_session() as db:
        row = await db.get(CapacityPlanRow, PLAN_ID)
        if row is None:
            row = CapacityPlanRow(id=PLAN_ID)
            db.add(row)
        row.computed_at = datetime.now(timezone.utc)
        row.push_level = plan.get("pushLevel")
        row.applied = bool(plan.get("applied"))
        row.fleet_concurrency = plan.get("fleetConcurrency")
        row.per_product = dict(plan.get("perProduct") or {})
        row.pending = dict(plan.get("pending") or {})
        row.budget = dict(plan.get("budget") or {})
        row.binding = plan.get("binding")
        row.reasons = list(plan.get("reasons") or [])
        row.defects = list(plan.get("defects") or [])
