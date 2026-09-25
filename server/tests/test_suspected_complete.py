"""The suspected-complete overlay on a handed-back session.

Prod 2026-09-25 (session 2971d6a6): PR merged, follow-up PR ready with auto-merge
armed, CI still running, and the session ended with ``hand_back("Status only, no
action needed: …")``. PR #2473 fixed the tool contract that produced it; this is the
safety net for the shape recurring: a deterministic judge (the hand-back reason asks
for nothing AND a merged/armed PR meets the completion bar) writes the bar's reason
beside ``hand_back_reason``, the card offers "Mark complete" / "Not done", and nothing
ever archives on the verdict alone.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.sessions.completion import hand_back_reason_asks
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


# --------------------------------------------------------------------------- #
# The precision guard: a hand-back reason is an ask unless it plainly is not.  #
# --------------------------------------------------------------------------- #

STATUS_ONLY = (
    "Status only, no action needed: PR #2466 (#2313, #2054) is merged. PR #2469 (#2150 — "
    "remote-control-disable fix) is open, ready, auto-merge armed; all completed checks are "
    "green (lint-and-test-core, frontend, changes, amendment) and the four server-tests "
    "shards are still running with no failures. Nothing further needed from me right now."
)


@pytest.mark.parametrize("reason", [
    STATUS_ONLY,
    "Update only: PR #12 merged, nothing else changed and nothing is required.",
    "PR #7 is open, ready and armed; CI is green so far. No action needed.",
])
def test_a_pure_status_report_asks_nothing(reason):
    assert hand_back_reason_asks(reason) is False


@pytest.mark.parametrize("reason", [
    # The real audit trail, 2026-09-20..25: every one of these is an ask.
    "Nothing new to do: PR 2 (#2097) is already open, auto-merge armed, CI mid-run. "
    "Decide whether to start PR 3 or stop.",
    "PR 5 (#2116) is open with auto-merge armed pending CI; decide whether to continue "
    "straight into PR 6 or stop.",
    "Need go-ahead to restart viberpit-daemon.service on dobby to pick up VIBERPIT_GH_CONFIG_DIR.",
    "PR #16821 is ready with auto-merge armed and uses Refs, not Fixes. AC2–AC4 need people "
    "to run seeded state checks. Please run them.",
    "PR #2286 is open, ready, and auto-merge is armed, but the required amendment check will "
    "block merge until you paste an owner acknowledgment line into the PR body.",
    "Should I arm auto-merge on #16995, or push the same one-word fix into #16994?",
    "You confirmed the shared-account theory for #2150. I opened PR #2469 (ready, armed).",
    "Implementation plan is written and committed — needs your review and a choice of "
    "execution method before implementation starts.",
    "Both PRs are open with auto-merge on, but the completion nudge keeps firing. Which "
    "branch name should the PR carry?",
])
def test_anything_ask_shaped_stays_a_hand_back(reason):
    assert hand_back_reason_asks(reason) is True


@pytest.mark.parametrize("reason", [None, "", "   ", 7])
def test_a_blank_or_non_string_reason_reads_as_an_ask(reason):
    """A silent hand-back gives the overlay nothing to judge; doubt goes to the ask."""
    assert hand_back_reason_asks(reason) is True


# --------------------------------------------------------------------------- #
# The judge, on a real DB and record.                                          #
# --------------------------------------------------------------------------- #

def _rec(sid="s1", *, repo="o/r"):
    r = SessionRecord(id=sid, name="run", account="a", engine=AgentEngine.CLAUDE_CODE,
                      engine_session_id="", cols=80, rows=24, cwd="/tmp", repo=repo,
                      branch="feat/x")
    r.status = "handed_back"
    return r


def _pr(state="open", *, armed=True, checks="passing"):
    return types.SimpleNamespace(number=42, state=state, is_draft=False,
                                 auto_merge_enabled=armed, in_merge_queue=False,
                                 checks=checks, url="https://github.com/o/r/pull/42",
                                 docs_only=False)


def _facts(*prs):
    from server.automations.completion_bar import PrFact, PrFacts
    return PrFacts(has_repo=True, prs=tuple(
        PrFact(number=p.number, state=p.state, is_draft=p.is_draft,
               auto_merge_enabled=p.auto_merge_enabled, in_merge_queue=p.in_merge_queue,
               url=p.url, checks=p.checks) for p in prs))


async def _orch(tmp_path, rec, facts, *, refreshed=None):
    import server.sessions.orchestrator as orch_mod
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sc.db'}")
    async with db_session() as db:
        db.add(DbSession(id=rec.id, name="run", account_name="a", cwd="/tmp",
                         status="handed_back", hand_back_reason=rec.hand_back_reason))
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch.published: list[dict] = []
    orch._publish_event = orch.published.append  # type: ignore[method-assign]
    orch._autopilot_pr_facts = lambda r: facts  # type: ignore[method-assign]

    async def _refresh(session_id):
        return refreshed
    orch._autopilot_refresh_pr_facts = _refresh  # type: ignore[method-assign]
    return orch


async def _row(sid):
    async with db_session() as db:
        return (await db.execute(sa_select(DbSession).where(DbSession.id == sid))).scalar_one()


async def _audits(type_):
    async with db_session() as db:
        return list((await db.execute(
            sa_select(AuditEvent).where(AuditEvent.type == type_))).scalars().all())


def test_status_only_hand_back_over_an_armed_pr_is_flagged_likely_complete(tmp_path):
    async def run():
        rec = _rec()
        rec.hand_back_reason = STATUS_ONLY
        orch = await _orch(tmp_path, rec, _facts(_pr()))
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        return rec, await _row("s1"), await _audits("session.suspected_complete"), orch.published
    rec, row, audits, published = asyncio.run(run())
    assert rec.suspected_complete_reason == "PR #42 open with auto-merge armed"
    assert row.suspected_complete_reason == "PR #42 open with auto-merge armed"
    assert row.status == "handed_back" and rec.status == "handed_back"   # never acted on
    assert len(audits) == 1
    assert audits[0].detail["hand_back_reason"] == STATUS_ONLY
    assert audits[0].detail["reason"] == "PR #42 open with auto-merge armed"
    # The verdict is pushed on the status frame so the pill appears without the poll.
    assert published[-1]["type"] == "status" and published[-1]["status"] == "handed_back"
    assert published[-1]["suspectedCompleteReason"] == "PR #42 open with auto-merge armed"


def test_a_merged_pr_qualifies_too(tmp_path):
    async def run():
        rec = _rec()
        rec.hand_back_reason = STATUS_ONLY
        orch = await _orch(tmp_path, rec, _facts(_pr("merged", armed=False)))
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        return rec.suspected_complete_reason
    assert asyncio.run(run()) == "code landed (PR #42 merged)"


def test_an_ask_shaped_reason_is_never_flagged_even_over_an_armed_pr(tmp_path):
    async def run():
        reason = "PR #42 is armed pending CI; decide whether to continue into PR 3 or stop."
        rec = _rec()
        rec.hand_back_reason = reason
        orch = await _orch(tmp_path, rec, _facts(_pr()))
        await orch._judge_suspected_complete("s1", reason)
        return rec.suspected_complete_reason, (await _row("s1")).suspected_complete_reason
    assert asyncio.run(run()) == (None, None)


def test_no_pr_evidence_means_no_verdict_even_when_the_bar_is_met(tmp_path):
    """A research goal's "non-code goal — no PR expected" is a bar verdict but not
    evidence the session finished; only a merged/armed PR qualifies."""
    async def run():
        rec = _rec(repo=None)
        rec.hand_back_reason = STATUS_ONLY
        from server.automations.completion_bar import PrFacts
        orch = await _orch(tmp_path, rec, PrFacts(has_repo=False))
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        return rec.suspected_complete_reason
    assert asyncio.run(run()) is None


def test_an_armed_pr_with_red_ci_is_not_finished(tmp_path):
    async def run():
        rec = _rec()
        rec.hand_back_reason = STATUS_ONLY
        orch = await _orch(tmp_path, rec, _facts(_pr(checks="failing")))
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        return rec.suspected_complete_reason
    assert asyncio.run(run()) is None


def test_a_cache_miss_is_refreshed_once_before_giving_up(tmp_path):
    """Mirrors _auto_archive_completed: the per-tick cache may lag a just-armed PR."""
    async def run():
        rec = _rec()
        rec.hand_back_reason = STATUS_ONLY
        orch = await _orch(tmp_path, rec, _facts(), refreshed=_facts(_pr()))
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        return rec.suspected_complete_reason
    assert asyncio.run(run()) == "PR #42 open with auto-merge armed"


def test_a_session_that_moved_on_while_the_judge_ran_gains_no_verdict(tmp_path):
    async def run():
        rec = _rec()
        rec.hand_back_reason = STATUS_ONLY
        orch = await _orch(tmp_path, rec, _facts())

        async def _refresh_and_resume(session_id):
            rec.status = "in_progress"          # the user re-prompted meanwhile
            rec.hand_back_reason = None
            return _facts(_pr())
        orch._autopilot_refresh_pr_facts = _refresh_and_resume  # type: ignore[method-assign]
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        return rec.suspected_complete_reason, (await _row("s1")).suspected_complete_reason
    assert asyncio.run(run()) == (None, None)


def test_dismiss_clears_the_record_and_the_row_and_pushes_the_change(tmp_path):
    async def run():
        rec = _rec()
        rec.hand_back_reason = STATUS_ONLY
        orch = await _orch(tmp_path, rec, _facts(_pr()))
        await orch._judge_suspected_complete("s1", STATUS_ONLY)
        assert rec.suspected_complete_reason
        ok = await orch.dismiss_suspected_complete("s1")
        return ok, rec, await _row("s1"), orch.published[-1]
    ok, rec, row, frame = asyncio.run(run())
    assert ok is True
    assert rec.suspected_complete_reason is None and row.suspected_complete_reason is None
    assert rec.status == "handed_back"                    # still a hand-back, just unhinted
    assert frame["suspectedCompleteReason"] is None and frame["status"] == "handed_back"


def test_dismiss_on_an_unknown_session_reports_false(tmp_path):
    async def run():
        orch = await _orch(tmp_path, _rec(), _facts())
        return await orch.dismiss_suspected_complete("nope")
    assert asyncio.run(run()) is False


# --------------------------------------------------------------------------- #
# Lifetime: the verdict lives exactly as long as the hand-back it judged.      #
# --------------------------------------------------------------------------- #

def test_every_status_write_clears_the_verdict_and_a_hand_back_schedules_the_judge(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sc.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="New Session", account_name="acct1",
                             engine_session_id="claude-s1", status="handed_back", cwd="/repo/s1",
                             hand_back_reason=STATUS_ONLY, suspected_complete_reason="stale"))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        orch.record("s1").suspected_complete_reason = "stale"
        spawned: list[str] = []

        def _spawn(coro, *, name=None):
            spawned.append(name or "")
            coro.close()
            return None
        orch._spawn = _spawn  # type: ignore[method-assign]

        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=None)
        await orch._persist_status("s1", "in_progress")
        after_resume = (orch.record("s1").suspected_complete_reason,
                        (await _row("s1")).suspected_complete_reason)

        orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=STATUS_ONLY)
        await orch._persist_status("s1", "handed_back")
        return after_resume, spawned
    after_resume, spawned = asyncio.run(run())
    assert after_resume == (None, None)
    assert any(n.startswith("suspected_complete:") for n in spawned)


# --------------------------------------------------------------------------- #
# Autopilot names the verdict; the fleet routes the dismiss to the owner.      #
# --------------------------------------------------------------------------- #

def test_autopilot_escalation_names_the_verdict():
    from server.automations.autopilot_policy import DecisionContext, pre_decide
    from server.tests.test_autopilot_policy_rules import CONFIG
    ctx = DecisionContext(session_id="s1", status="handed_back", account="a", repo="o/r",
                          branch="b", effective_goal="fix it", goal_source="title",
                          stuck={"kind": "handed_back", "hand_back_reason": STATUS_ONLY,
                                 "suspected_complete": "PR #42 open with auto-merge armed"},
                          transcript_tail=[], ledger={}, pr_facts=None)
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate"
    assert "likely complete: PR #42 open with auto-merge armed" in d.reason


def test_dismiss_is_a_per_session_fleet_method_with_a_remote_proxy():
    from server.sessions.fleet_orchestrator import _PER_SESSION
    from server.sessions.remote import RemoteOrchestrator
    assert "dismiss_suspected_complete" in _PER_SESSION
    assert hasattr(RemoteOrchestrator, "dismiss_suspected_complete")


# --------------------------------------------------------------------------- #
# The route: confirm archives, reject dismisses, both audited; 409 on a card    #
# that carries no verdict.                                                     #
# --------------------------------------------------------------------------- #

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}


class _FakeSessions:
    """The sessions facade the route talks to (daemon-mode shape): only the three
    calls the handler makes."""

    def __init__(self, record):
        self._record = record
        self.archived: list[tuple] = []
        self.dismissed: list[str] = []

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def archive_session(self, sid, *, archived_by="user", context=None):
        self.archived.append((sid, archived_by, context))

    async def dismiss_suspected_complete(self, sid):
        self.dismissed.append(sid)
        self._record.suspected_complete_reason = None
        return True


def _client(record):
    import os
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(record)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def _flagged():
    rec = _rec()
    rec.hand_back_reason = STATUS_ONLY
    rec.suspected_complete_reason = "PR #42 open with auto-merge armed"
    return rec


def test_confirm_archives_through_the_ordinary_path_with_its_own_provenance():
    client, fake = _client(_flagged())
    with client:
        r = client.post("/api/sessions/s1/suspected-complete", json={"decision": "confirm"},
                        headers=_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"decision": "confirm", "sessionId": "s1"}
    assert fake.archived == [("s1", "user", {"reason": "suspected complete — confirmed by user",
                                             "bar": "PR #42 open with auto-merge armed"})]
    assert fake.dismissed == []


def test_reject_dismisses_the_hint_and_archives_nothing():
    client, fake = _client(_flagged())
    with client:
        r = client.post("/api/sessions/s1/suspected-complete", json={"decision": "reject"},
                        headers=_H)
    assert r.status_code == 200, r.text
    assert fake.dismissed == ["s1"] and fake.archived == []


def test_a_card_without_a_verdict_is_a_409_and_an_unknown_session_a_404():
    plain = _rec()
    plain.hand_back_reason = "Need your call on the scope"
    client, fake = _client(plain)
    with client:
        r = client.post("/api/sessions/s1/suspected-complete", json={"decision": "confirm"},
                        headers=_H)
        missing = client.post("/api/sessions/nope/suspected-complete",
                              json={"decision": "confirm"}, headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_suspected"
    assert missing.status_code == 404
    assert fake.archived == [] and fake.dismissed == []


def test_an_unknown_decision_is_rejected_by_validation():
    client, fake = _client(_flagged())
    with client:
        r = client.post("/api/sessions/s1/suspected-complete", json={"decision": "later"},
                        headers=_H)
    assert r.status_code == 422
    assert fake.archived == [] and fake.dismissed == []
