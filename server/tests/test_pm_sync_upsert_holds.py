"""An auto-filed error report is BORN parked (spec 2026-09-18 §5, evidence dispatch).

``_pm_sync_upsert_items`` is where the GitHub mirror first materialises a row for an
issue the error-triage automation filed, and it is the only place that can tell
"this row is new" from "this row already existed". So the birth hold is written
there, on the new-row branch ONLY: re-holding on every sync tick would undo the
investigation that cleared it.
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.automations.error_triage import LABEL as AUTO_ERROR_LABEL
from server.db import db_session, init_db
from server.db.models import AuditEvent, WorkItem
from server.pm.github_sync import WorkItemView
from server.pm.holds import (
    ERROR_TRIAGE_ACTOR, EVIDENCE_BIRTH_REASON, clear_hold,
)

REPO = "steiner385/viberpit"


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator.__new__(SessionOrchestrator)


def _item(number: int, *, labels: tuple[str, ...], state: str = "open",
          title: str = "TypeError in the mirror pass") -> WorkItemView:
    return WorkItemView(
        repo=REPO, number=number, kind="issue", title=title,
        body="stack trace", state=state, labels=labels, author="viberpit-bot",
        url=f"https://github.com/{REPO}/issues/{number}",
        gh_created_at=None, gh_updated_at=None, gh_closed_at=None,
        linked_issue_numbers=())


async def _fetch(number: int) -> WorkItem:
    async with db_session() as db:
        return (await db.execute(select(WorkItem).where(
            WorkItem.repo == REPO, WorkItem.number == number,
            WorkItem.kind == "issue"))).scalar_one()


async def _hold_audits() -> list[AuditEvent]:
    async with db_session() as db:
        return list((await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.set"))).scalars().all())


@pytest.mark.asyncio
async def test_new_auto_error_issue_is_born_as_an_evidence_hold(db_env):
    await init_db(db_env)
    await _orch()._pm_sync_upsert_items(
        [_item(101, labels=(AUTO_ERROR_LABEL, "source:server"))])

    wi = await _fetch(101)
    assert wi.hold_kind == "evidence"
    assert wi.hold_by == ERROR_TRIAGE_ACTOR
    assert wi.hold_reason == EVIDENCE_BIRTH_REASON
    assert wi.hold_set_at is not None
    # The fingerprint is taken from the row as mirrored, so a later body edit is a
    # release signal for this hold exactly as it is for any other.
    assert wi.hold_fingerprint

    audits = await _hold_audits()
    assert len(audits) == 1
    assert audits[0].identity == ERROR_TRIAGE_ACTOR
    assert audits[0].via == "pm_sync"
    assert audits[0].detail["workItemId"] == wi.id
    assert audits[0].detail["kind"] == "evidence"
    assert audits[0].detail["reason"] == EVIDENCE_BIRTH_REASON
    assert audits[0].detail["actorClass"] == "system"


@pytest.mark.asyncio
async def test_existing_auto_error_issue_is_never_re_held(db_env):
    await init_db(db_env)
    orch = _orch()
    item = _item(102, labels=(AUTO_ERROR_LABEL,))
    await orch._pm_sync_upsert_items([item])

    # The investigation ran and cleared the hold. The next sync tick must not
    # resurrect it — the birth hold belongs to the new-row branch alone.
    async with db_session() as db:
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == REPO, WorkItem.number == 102))).scalar_one()
        clear_hold(wi, actor="session:s1")

    await orch._pm_sync_upsert_items([item])

    assert (await _fetch(102)).hold_kind is None
    assert len(await _hold_audits()) == 1


@pytest.mark.asyncio
async def test_non_auto_error_issue_and_closed_issue_get_no_hold(db_env):
    await init_db(db_env)
    await _orch()._pm_sync_upsert_items([
        _item(103, labels=("bug",)),
        _item(104, labels=(AUTO_ERROR_LABEL,), state="closed"),
    ])

    assert (await _fetch(103)).hold_kind is None
    assert (await _fetch(104)).hold_kind is None
    assert await _hold_audits() == []
