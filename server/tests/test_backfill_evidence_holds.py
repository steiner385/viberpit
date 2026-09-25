"""One-shot script for backfilling pre-hold-ladder auto-error birth holds (#1963)."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import AuditEvent, WorkItem

SCRIPT = Path("scripts/backfill_evidence_holds.py")


def _module():
    spec = importlib.util.spec_from_file_location("backfill_evidence_holds", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


async def _seed(tmp_path, name: str) -> str:
    db_url = f"sqlite+aiosqlite:///{tmp_path / name}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(WorkItem(repo="o/r", number=1, kind="issue", state="open",
                        title="ERROR server.a: boom", body="stack trace",
                        labels=["auto-error"], author="steiner385", assignees=[]))
        # A closed auto-error issue must never be parked — the runner only ever
        # dispatches open work.
        db.add(WorkItem(repo="o/r", number=2, kind="issue", state="closed",
                        title="ERROR server.b: old", body="stack trace",
                        labels=["auto-error"], author="steiner385", assignees=[]))
        # An ordinary issue (no auto-error label) is not this script's business.
        db.add(WorkItem(repo="o/r", number=3, kind="issue", state="open",
                        title="please add dark mode", body="",
                        labels=[], author="steiner385", assignees=[]))
        # Already held — even ``evidence`` — must be left exactly alone: a second
        # birth-hold write would blow away whatever progress (or a person's own
        # decision) the existing hold represents.
        db.add(WorkItem(repo="o/r", number=4, kind="issue", state="open",
                        title="ERROR server.c: held already", body="stack trace",
                        labels=["auto-error"], author="steiner385", assignees=[],
                        hold_kind="owner_action", hold_by="tony@example.com",
                        hold_reason="already looked at"))
        await db.flush()
    return db_url


def _row(number: int):
    async def read():
        async with db_session() as db:
            return (await db.execute(select(WorkItem).where(
                WorkItem.repo == "o/r", WorkItem.number == number))).scalar_one()
    return asyncio.run(read())


def test_dry_run_parks_nothing(tmp_path):
    async def run():
        db_url = await _seed(tmp_path, "dry.db")
        mod = _module()
        result = await mod.main(apply=False, db_url=db_url)
        assert result == 0

    asyncio.run(run())
    assert _row(1).hold_kind is None


def test_apply_parks_only_the_open_unheld_auto_error_issue(tmp_path):
    async def run():
        db_url = await _seed(tmp_path, "apply.db")
        mod = _module()
        result = await mod.main(apply=True, db_url=db_url)
        assert result == 0

    asyncio.run(run())

    parked = _row(1)
    assert parked.hold_kind == "evidence"
    assert parked.hold_by == "automation:error-triage"
    assert parked.hold_reason == "auto-filed error report: investigate before implementing"
    assert parked.hold_set_at is not None

    assert _row(2).hold_kind is None       # closed: left alone
    assert _row(3).hold_kind is None       # no auto-error label: left alone
    assert _row(4).hold_kind == "owner_action"  # already held: left alone


def test_apply_writes_the_same_audit_shape_as_the_live_birth_hold(tmp_path):
    async def run():
        db_url = await _seed(tmp_path, "audit.db")
        mod = _module()
        await mod.main(apply=True, db_url=db_url)
        async with db_session() as db:
            events = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 1
        event = events[0]
        assert event.identity == "automation:error-triage"
        assert event.via == "backfill_evidence_holds"
        assert event.detail["kind"] == "evidence"
        assert event.detail["actorClass"] == "system"

    asyncio.run(run())


def test_a_second_run_finds_nothing_left_to_park(tmp_path):
    async def run():
        db_url = await _seed(tmp_path, "idempotent.db")
        mod = _module()
        await mod.main(apply=True, db_url=db_url)
        second = await mod.main(apply=True, db_url=db_url)
        assert second == 0
        async with db_session() as db:
            events = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.hold.set"))).scalars().all()
        assert len(events) == 1  # not re-written by the second run

    asyncio.run(run())
