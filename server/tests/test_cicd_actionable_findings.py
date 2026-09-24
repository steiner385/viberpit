"""orchestrator.cicd_actionable_findings — the card's "File issue" list.

``check_transition_findings`` marks a graduation verdict non-actionable (there
is nothing for a human to fix), but ``Finding.actionable`` never reaches the
``ci_finding`` row, so the card offered "File issue" on every graduation
candidate anyway — 49 of the 72 items on 2026-09-24, and four KinDash issues
filed from that button. The selection must derive from the same constant the
loop's auto-filing uses.

Same ``__new__`` pattern as test_cicd_file_issue_now (the method touches only
the module-global ``db_session``).
"""
from __future__ import annotations

import asyncio

from server.automations.cicd.findings import NON_ACTIONABLE_KINDS, check_transition_findings
from server.db import db_session, init_db
from server.db.models import CiFinding
from server.sessions.orchestrator import SessionOrchestrator


def _orch():
    return object.__new__(SessionOrchestrator)


def _finding(id_, kind, **kw):
    defaults = dict(id=id_, repo="o/r", kind=kind, fingerprint=f"fp-{id_}",
                    status="open", title=f"{kind} {id_}", detail={})
    defaults.update(kw)
    return CiFinding(**defaults)


def test_non_actionable_kinds_derive_from_the_verdict_table():
    """One source of truth: the kinds the query excludes are exactly the kinds
    check_transition_findings emits with actionable=False."""
    emitted = check_transition_findings("o/r", {"a": "graduate", "b": "demote", "c": "retire"})
    assert {f.kind for f in emitted if not f.actionable} == set(NON_ACTIONABLE_KINDS)
    assert "graduate_check" in NON_ACTIONABLE_KINDS


def test_graduation_candidates_are_not_offered_for_filing(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with db_session() as db:
            db.add(_finding("g1", "graduate_check"))
            db.add(_finding("f1", "flaky_test"))
            db.add(_finding("r1", "retire_check"))
            db.add(_finding("d1", "demote_check", issue_number=7))   # already filed
            db.add(_finding("x1", "flaky_test", status="resolved"))
        got = await _orch().cicd_actionable_findings()
        assert sorted(f["id"] for f in got) == ["f1", "r1"]
    asyncio.run(run())
