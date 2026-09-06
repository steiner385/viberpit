"""Tests for _autopilot_resolve_pr_intent: gating, classifier invocation, persistence.

Drives the helper via a bare orchestrator (same __new__ pattern as test_autopilot_orchestrator_seams).
"""
from __future__ import annotations

import asyncio

from server.automations.completion_bar import PrFact, PrFacts
from server.sessions.engine import AgentEngine


def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    return orch


def _rec(pr_resolution=None, pr_resolution_source=None):
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    r = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                      cols=80, rows=24, cwd="/tmp", status="complete")
    r.pr_resolution = pr_resolution
    r.pr_resolution_source = pr_resolution_source
    return r


def _no_pr_facts():
    """Repo-attached session, zero PRs — the only branch where intent matters."""
    return PrFacts(has_repo=True, prs=())


def _open_pr_facts():
    """Repo-attached session with a real open PR."""
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, True, "u"),))


# ---------------------------------------------------------------------------
# (a) Already stored: returns existing value without calling classifier
# ---------------------------------------------------------------------------

def test_returns_stored_resolution_without_classifier(monkeypatch):
    """If rec.pr_resolution is already VERIFIED (classifier-sourced), return it
    immediately — no model call."""
    calls = []

    async def fake_classify(tail, goal, **kw):
        calls.append((tail, goal))
        from server.automations.pr_intent import PrIntentVerdict
        return PrIntentVerdict(resolved=True, reason="not-needed", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)

    orch = _bare_orch()
    rec = _rec(pr_resolution="not-needed", pr_resolution_source="classifier")

    async def run():
        result = await orch._autopilot_resolve_pr_intent(
            rec, "fix ci", "explicit", _no_pr_facts(), [])
        return result

    result = asyncio.run(run())
    assert result == "not-needed"
    assert calls == [], "classifier must NOT be called when resolution is already stored"


def test_marker_cancelled_is_returned_without_classifier(monkeypatch):
    """A marker-sourced 'cancelled' is a decision, not a completion claim — trusted."""
    calls = []

    async def fake_classify(tail, goal, **kw):
        calls.append(goal)
        from server.automations.pr_intent import PrIntentVerdict
        return PrIntentVerdict(resolved=True, reason="cancelled", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)
    orch = _bare_orch()
    rec = _rec(pr_resolution="cancelled", pr_resolution_source="marker")
    result = asyncio.run(orch._autopilot_resolve_pr_intent(
        rec, "fix ci", "explicit", _no_pr_facts(), []))
    assert result == "cancelled"
    assert calls == []


# ---------------------------------------------------------------------------
# (a2) Self-certified: a marker-sourced 'not-needed' on a code goal is a CLAIM
# ---------------------------------------------------------------------------
#
# Prod 2026-09-06 (session fdc6b27a): the agent's own `<!-- ccswitch:no-pr -->` on
# "Ship PR adding persistent color field …" — zero code, a recommended spec, an
# offer to draft it — satisfied the bar as a stored resolution. The classifier is
# the verification of that claim, so it MUST run here and judge against the goal.

def test_marker_not_needed_on_code_goal_is_verified_by_classifier(monkeypatch):
    """Marker-sourced not-needed + code goal + no PR → classifier runs; a high-
    confidence confirmation upgrades the source to 'classifier' and is returned."""
    from server.automations.pr_intent import PrIntentVerdict
    calls = []

    async def fake_classify(tail, goal, **kw):
        calls.append(goal)
        return PrIntentVerdict(resolved=True, reason="not-needed", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)
    orch = _bare_orch()
    rec = _rec(pr_resolution="not-needed", pr_resolution_source="marker")
    result = asyncio.run(orch._autopilot_resolve_pr_intent(
        rec, "ship the color field PR", "inferred", _no_pr_facts(), []))
    assert calls == ["ship the color field PR"]
    assert result == "not-needed"
    assert rec.pr_resolution_source == "classifier"


def test_marker_not_needed_unverified_returns_none_and_keeps_marker(monkeypatch):
    """When the classifier does NOT confirm the claim, the resolution is unusable
    (None → the bar's code-goal branch nudges) but the marker token is kept for audit."""
    from server.automations.pr_intent import PrIntentVerdict

    async def fake_classify(tail, goal, **kw):
        return PrIntentVerdict(resolved=False, reason="", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)
    orch = _bare_orch()
    rec = _rec(pr_resolution="not-needed", pr_resolution_source="marker")
    result = asyncio.run(orch._autopilot_resolve_pr_intent(
        rec, "ship the color field PR", "inferred", _no_pr_facts(), []))
    assert result is None
    assert rec.pr_resolution == "not-needed" and rec.pr_resolution_source == "marker"


def test_marker_not_needed_on_research_goal_is_trusted(monkeypatch):
    """No verification needed where the bar would accept a bare no-PR anyway."""
    calls = []

    async def fake_classify(tail, goal, **kw):
        calls.append(goal)
        from server.automations.pr_intent import PrIntentVerdict
        return PrIntentVerdict(resolved=False, reason="", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)
    orch = _bare_orch()
    rec = _rec(pr_resolution="not-needed", pr_resolution_source="marker")
    result = asyncio.run(orch._autopilot_resolve_pr_intent(
        rec, "investigate the flaky job", "explicit", _no_pr_facts(), []))
    assert result == "not-needed"
    assert calls == []


# ---------------------------------------------------------------------------
# (b) PR exists: returns None without calling classifier (gating)
# ---------------------------------------------------------------------------

def test_returns_none_when_pr_exists(monkeypatch):
    """When pr_facts contains a real PR, gating fires: no classifier, returns None."""
    calls = []

    async def fake_classify(tail, goal, **kw):
        calls.append((tail, goal))
        from server.automations.pr_intent import PrIntentVerdict
        return PrIntentVerdict(resolved=True, reason="not-needed", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)

    orch = _bare_orch()
    rec = _rec()

    async def run():
        return await orch._autopilot_resolve_pr_intent(
            rec, "ship the refactor", "explicit", _open_pr_facts(), [])

    result = asyncio.run(run())
    assert result is None
    assert calls == [], "classifier must NOT fire when a PR already exists"


# ---------------------------------------------------------------------------
# (c) No PR + no stored resolution: calls classifier, persists on high-confidence
# ---------------------------------------------------------------------------

def test_calls_classifier_and_persists_on_no_pr_branch(monkeypatch, tmp_path):
    """On the no-PR met_bar branch, the classifier is called; a high-confidence
    resolved verdict is persisted and its reason returned."""
    from server.automations.pr_intent import PrIntentVerdict

    captured = []

    async def fake_classify(tail, goal, **kw):
        captured.append(goal)
        return PrIntentVerdict(resolved=True, reason="cancelled", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)

    async def run():
        # Init a real DB so the persistence path works
        from server.db import init_db, db_session
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ctx.db'}"
        await init_db(db_url=db_url)

        # Insert a Session row so the UPDATE has a target
        from server.db.models import Session as DbSession
        async with db_session() as db:
            db.add(DbSession(
                id="s1", name="n", account_name="a",
                cwd="/tmp", status="complete"))

        orch = _bare_orch()
        rec = _rec()  # no stored resolution

        result = await orch._autopilot_resolve_pr_intent(
            rec, "fix ci", "explicit", _no_pr_facts(), [{"role": "user", "text": "done"}])

        return result, rec

    result, rec = asyncio.run(run())

    assert result == "cancelled", "should return the classifier's reason"
    assert captured == ["fix ci"], "classifier must be called with the session's goal"
    assert rec.pr_resolution == "cancelled", "in-memory record must be updated"
    assert rec.pr_resolution_source == "classifier"


def test_classifier_low_confidence_returns_none(monkeypatch):
    """When classifier returns high confidence=False, returns None (no persist)."""
    from server.automations.pr_intent import PrIntentVerdict

    async def fake_classify(tail, goal, **kw):
        return PrIntentVerdict(resolved=True, reason="not-needed", confidence="low")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)

    orch = _bare_orch()
    rec = _rec()

    async def run():
        return await orch._autopilot_resolve_pr_intent(
            rec, "fix ci", "explicit", _no_pr_facts(), [])

    result = asyncio.run(run())
    assert result is None
    assert rec.pr_resolution is None, "low-confidence result must not be persisted"


def test_classifier_unresolved_returns_none(monkeypatch):
    """When classifier says resolved=False, returns None (no persist)."""
    from server.automations.pr_intent import PrIntentVerdict

    async def fake_classify(tail, goal, **kw):
        return PrIntentVerdict(resolved=False, reason="", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)

    orch = _bare_orch()
    rec = _rec()

    async def run():
        return await orch._autopilot_resolve_pr_intent(
            rec, "fix ci", "explicit", _no_pr_facts(), [])

    result = asyncio.run(run())
    assert result is None
    assert rec.pr_resolution is None


# ---------------------------------------------------------------------------
# (f) Non-complete status: classifier must NOT fire (per-tick cost gate)
# ---------------------------------------------------------------------------

def test_classifier_not_invoked_for_non_complete_status(monkeypatch):
    """Regression: when rec.status is a non-complete actionable status (e.g. 'done'),
    the classifier must NOT be invoked even on the no-PR code-goal branch. This guards
    against the per-tick model-call waste discovered in the final review."""
    from server.sessions.types import SessionRecord

    invocations = []

    async def fake_classify(tail, goal, **kw):
        invocations.append(goal)
        from server.automations.pr_intent import PrIntentVerdict
        return PrIntentVerdict(resolved=True, reason="cancelled", confidence="high")

    monkeypatch.setattr("server.automations.pr_intent.classify_pr_intent", fake_classify)

    orch = _bare_orch()
    # Build a record with a non-complete status — still has a repo, no PR, no stored resolution
    rec = SessionRecord(id="s2", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                        cols=80, rows=24, cwd="/tmp", status="done")
    rec.pr_resolution = None
    rec.pr_resolution_source = None

    async def run():
        return await orch._autopilot_resolve_pr_intent(
            rec, "fix ci", "explicit", _no_pr_facts(), [{"role": "user", "text": "progress"}])

    result = asyncio.run(run())
    assert result is None, "should return None for non-complete session"
    assert invocations == [], "classifier must NOT be called for non-complete status"
