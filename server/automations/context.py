"""AutomationContext — the sole outward seam for an automation: activity feed,
per-target state, and (Task 4) de-duplicated session spawning."""
from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import delete, select

from server.db import db_session
from server.db.models import AutomationEvent, AutomationTargetState

log = logging.getLogger(__name__)


class AutomationContext:
    def __init__(self, automation_id: str, *,
                 create_session: Callable[..., Any] | None = None,
                 event_cap: int = 500) -> None:
        self.automation_id = automation_id
        self._create_session = create_session
        self._event_cap = event_cap

    async def emit(self, level: str, summary: str, *, kind: str | None = None,
                   target: str | None = None, session_id: str | None = None,
                   detail: dict | None = None) -> None:
        """Append one activity event, then prune to the cap. Best-effort:
        swallows + logs all errors; never propagates into the caller."""
        try:
            async with db_session() as db:
                db.add(AutomationEvent(
                    automation_id=self.automation_id, level=level, summary=summary,
                    kind=kind, target=target, session_id=session_id, detail=detail))
                await db.flush()
                await self._prune(db)
        except Exception:
            log.warning("automation emit failed (automation=%s)", self.automation_id,
                        exc_info=True)

    async def _prune(self, db) -> None:
        ids = (await db.execute(
            select(AutomationEvent.id)
            .where(AutomationEvent.automation_id == self.automation_id)
            .order_by(AutomationEvent.ts.desc(), AutomationEvent.id.desc())
            .offset(self._event_cap)
        )).scalars().all()
        if ids:
            await db.execute(delete(AutomationEvent).where(AutomationEvent.id.in_(ids)))

    async def list_events(self, *, limit: int = 100, target: str | None = None,
                          kind: str | None = None) -> list[dict]:
        stmt = (select(AutomationEvent)
                .where(AutomationEvent.automation_id == self.automation_id)
                .order_by(AutomationEvent.ts.desc(), AutomationEvent.id.desc()))
        if target is not None:
            stmt = stmt.where(AutomationEvent.target == target)
        if kind is not None:
            stmt = stmt.where(AutomationEvent.kind == kind)
        stmt = stmt.limit(limit)
        async with db_session() as db:
            rows = (await db.execute(stmt)).scalars().all()
        return [{"ts": r.ts.isoformat(), "level": r.level, "kind": r.kind,
                 "summary": r.summary, "target": r.target, "sessionId": r.session_id}
                for r in rows]

    async def get_target_state(self, target: str) -> dict:
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))).scalar_one_or_none()
            return dict(row.state) if row else {}

    async def set_target_state(self, target: str, state: dict) -> None:
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))).scalar_one_or_none()
            if row is None:
                db.add(AutomationTargetState(
                    automation_id=self.automation_id, target=target, state=state))
            else:
                row.state = state
            await db.flush()
