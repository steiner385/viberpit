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
        # Route error/warn activity through the notification rules (#602). Silent
        # unless a rule matches — enabling rules must not spam every warning. Best
        # effort: a notify failure must never propagate into the automation loop.
        if level in ("error", "warn"):
            try:
                from server.sessions import push as push_mod
                await push_mod.notify_event(
                    "automation_error" if level == "error" else "automation_warn",
                    f"ccswitch — {self.automation_id}", summary,
                    automation=self.automation_id, fanout_when_unconfigured=False)
            except Exception:
                log.debug("automation notify_event failed", exc_info=True)

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

    async def delete_target_state(self, target: str) -> None:
        """Drop the durable state row for *target* (idempotent). Used when a target
        reaches terminal rest (merged / gave-up) so the table stays bounded."""
        async with db_session() as db:
            await db.execute(delete(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))

    async def list_target_states(self) -> dict[str, dict]:
        """All durable target states for this automation, keyed by target."""
        async with db_session() as db:
            rows = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id))).scalars().all()
        return {r.target: dict(r.state) for r in rows}

    async def spawn_session(self, *, origin_ref: str, dedupe_key: str | None = None,
                            detail: dict | None = None, **create_kwargs) -> Any:
        """Find-or-create a session for this automation. If a NON-terminal session
        with the same (origin, dedupe_key) already exists, return it and emit a
        'skipped_duplicate' event instead of creating a second. Otherwise create
        via the injected create_session (which stamps origin/origin_ref), then emit
        a linked 'spawned' event (carrying the optional ``detail`` payload)."""
        from server.db.models import INACTIVE_SESSION_STATUSES, Session
        key = dedupe_key or origin_ref
        async with db_session() as db:
            existing = (await db.execute(select(Session).where(
                Session.origin == self.automation_id,
                Session.origin_ref == key,
                Session.status.not_in(INACTIVE_SESSION_STATUSES)))).scalars().first()
        if existing is not None:
            await self.emit("info", f"skipped duplicate spawn for {key}",
                            kind="skipped_duplicate", target=key, session_id=existing.id)
            return existing
        if self._create_session is None:
            raise RuntimeError("AutomationContext has no create_session bound")
        rec = await self._create_session(origin=self.automation_id, origin_ref=origin_ref,
                                         **create_kwargs)
        await self.emit("info", f"spawned session for {origin_ref}",
                        kind="spawned", target=origin_ref,
                        session_id=getattr(rec, "id", None), detail=detail)
        return rec
