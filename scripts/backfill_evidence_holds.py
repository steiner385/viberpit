"""One-shot: give pre-hold-ladder ``auto-error`` issues their birth hold (#1963).

``_pm_sync_upsert_items`` (server/sessions/orchestrator.py) only writes the
``evidence`` birth hold on the NEW-row branch (spec 2026-09-18 §5) — a tick that
re-held an existing row would undo an investigation that had already cleared the
hold. That is correct for the live stream, but it means every ``auto-error`` issue
the mirror already held BEFORE that code shipped never got parked at all: it
entered (or sits in) the backlog as an ordinary implement candidate instead of an
investigation.

This finds every open, unheld issue mirrored with the ``auto-error`` label and
parks it exactly the way the birth hold would have — same reason, same actor, same
audit shape (``server.pm.holds.set_evidence_hold`` / ``ERROR_TRIAGE_ACTOR`` /
``EVIDENCE_BIRTH_REASON``), so a row backfilled here is indistinguishable from one
born held.

    python scripts/backfill_evidence_holds.py --dry-run
    python scripts/backfill_evidence_holds.py --apply

Reads the DB URL from the app settings (the same env the server runs with); pass
``db_url`` to ``main()`` to override. Idempotent: a row already holding anything
(``evidence`` or otherwise) is never touched, so a second run finds nothing left
to do.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone


async def main(apply: bool, db_url: str | None = None) -> int:
    from sqlalchemy import select

    from server.audit import record_audit
    from server.automations.error_triage import LABEL as AUTO_ERROR_LABEL
    from server.config import get_settings
    from server.db import db_session, init_db
    from server.db.models import WorkItem
    from server.pm.holds import (
        ERROR_TRIAGE_ACTOR, EVIDENCE_BIRTH_REASON, actor_class, set_evidence_hold,
    )

    url = db_url or get_settings().db_url
    await init_db(db_url=url)
    async with db_session() as db:
        rows = (await db.execute(select(WorkItem).where(
            WorkItem.kind == "issue", WorkItem.state == "open",
            WorkItem.hold_kind.is_(None)))).scalars().all()
        targets = [wi for wi in rows if AUTO_ERROR_LABEL in (wi.labels or ())]
        print(f"unheld open auto-error issues: {len(targets)}")
        if not apply:
            for wi in targets:
                print(f"  would park {wi.repo}#{wi.number}")
            print("dry run; pass --apply to park them")
            return 0
        now = datetime.now(timezone.utc)
        for wi in targets:
            set_evidence_hold(wi, reason=EVIDENCE_BIRTH_REASON,
                              actor=ERROR_TRIAGE_ACTOR, now=now)
        await db.flush()
        for wi in targets:
            await record_audit(db=db, type="pm.hold.set", identity=ERROR_TRIAGE_ACTOR,
                               via="backfill_evidence_holds",
                               detail={"workItemId": wi.id, "kind": "evidence",
                                       "reason": EVIDENCE_BIRTH_REASON,
                                       "actorClass": actor_class(ERROR_TRIAGE_ACTOR)})
        print(f"parked {len(targets)} issue(s)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))
