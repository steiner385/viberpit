"""Read/write the capacity governor's single plan row (spec 2026-09-19 §4)."""
from __future__ import annotations

from datetime import datetime, timezone

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
