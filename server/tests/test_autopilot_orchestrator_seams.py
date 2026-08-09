"""Autopilot orchestrator seams — the injected dependencies AutopilotLoop calls.

Drives the pure seams (`_autopilot_read_config`, `_autopilot_apply`) against a bare
orchestrator built with ``__new__`` (no full init, no network), mirroring the
backlog-heartbeat test harness.
"""
from __future__ import annotations

import asyncio

import pytest

from server.automations.autopilot_policy import Decision, DecisionContext


def make_decision_context(**kw):
    """Build a minimal DecisionContext; callers override only the fields they care about."""
    base = dict(
        session_id="s1", status="waiting_input", account="acct-a", repo="o/r",
        branch="feat/x", effective_goal="ship it", goal_source="explicit",
        stuck={}, transcript_tail=[], ledger={},
    )
    base.update(kw)
    return DecisionContext(**base)


def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    orch._turn_restart_at = {}
    return orch


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ap.db'}")
    await registry.ensure_rows()


async def _set_row(*, enabled: bool, mode: str) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(sa_update(Automation).where(Automation.id == "autopilot")
                         .values(enabled=enabled, mode=mode))


def test_read_config_defaults_to_off_when_disabled(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        mode, config = await orch._autopilot_read_config()
        assert mode == "off"
        assert config["max_actions_per_session_per_hour"] == 12
    asyncio.run(run())


def test_read_config_off_while_draining(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        await _set_row(enabled=True, mode="auto")
        orch._draining = True
        mode, _ = await orch._autopilot_read_config()
        assert mode == "off"                       # draining forces off
        orch._draining = False
        mode2, _ = await orch._autopilot_read_config()
        assert mode2 == "auto"                     # enabled+auto once drain clears
    asyncio.run(run())


def test_apply_noops_for_terminal_actions(tmp_path):
    async def run():
        orch = _bare_orch()
        # escalate / complete / skip never mutate — return False without a driver.
        assert await orch._autopilot_apply("missing", Decision(action="escalate")) is False
        assert await orch._autopilot_apply("missing", Decision(action="complete")) is False
        assert await orch._autopilot_apply("missing", Decision(action="skip")) is False
    asyncio.run(run())


def test_list_governed_includes_complete_but_not_backlog_runner(tmp_path):
    """A `complete`-status session is governed so the decider can archive a goal-met
    session — EXCEPT a backlog-runner one, which the dedicated auto-archive path owns
    (`_maybe_autoarchive_completed`). Governing both would double-archive the same
    session from two racing paths."""
    async def run():
        from server.sessions.types import SessionRecord
        await _init_db(tmp_path)
        orch = _bare_orch()

        def _rec(sid, status, origin=None):
            return SessionRecord(id=sid, name="n", account="a", claude_session_id="",
                                 cols=80, rows=24, cwd="/tmp", status=status, origin=origin)

        orch._records = {
            "human_complete": _rec("human_complete", "complete"),
            "backlog_complete": _rec("backlog_complete", "complete", origin="backlog-runner"),
            "done": _rec("done", "done"),
            "working": _rec("working", "in_progress"),
        }
        governed = set(await orch._autopilot_list_governed())
        assert "human_complete" in governed        # decider judges it → archives if goal met
        assert "done" in governed                  # unchanged behaviour
        assert "backlog_complete" not in governed  # owned by the backlog auto-archive path
        assert "working" not in governed           # in_progress is never actionable
    asyncio.run(run())


def _perm_ctx(perm_id, command):
    from server.automations.autopilot_policy import DecisionContext
    return DecisionContext(
        session_id="s1", status="waiting_input", account="a", repo=None, branch=None,
        effective_goal="g", goal_source="explicit",
        stuck={"kind": "permission", "perm_id": perm_id,
               "tool_name": "Bash", "tool_input": {"command": command}},
        transcript_tail=[], ledger={})


def test_apply_resolves_decide_time_perm_and_rechecks_denylist(tmp_path):
    """TOCTOU guard: apply resolves exactly the decide-time perm_id, and a
    deny-listed command can never be approved even if it reaches apply."""
    async def run():
        orch = _bare_orch()
        calls: list = []

        async def _ensure(sid, ident=None):
            return object()

        orch.ensure_driver = _ensure
        orch.resolve_permission = (
            lambda sid, pid, decision, ident: calls.append((pid, decision)) or True)

        # Safe perm → approved via the SAME perm_id passed in (no re-fetch).
        assert await orch._autopilot_apply(
            "s1", Decision(action="approve"), _perm_ctx("P1", "pytest")) is True
        assert calls == [("P1", True)]

        # A destructive command reaching apply is blocked by the deny-list re-check;
        # resolve_permission is never called.
        calls.clear()
        assert await orch._autopilot_apply(
            "s1", Decision(action="approve"), _perm_ctx("P2", "git push --force")) is False
        assert calls == []
    asyncio.run(run())


def test_get_context_populates_transcript_tail(tmp_path):
    """The decider is only as good as its input: get_context must carry a compact
    transcript tail (the deployed empty-tail starved the model into prose/escalate)."""
    async def run():
        from server.sessions.types import SessionRecord
        from server.sessions.events import TextDelta, ToolCall, UserMessage, ToolKind
        from server.sessions.replay import ReplayWindow

        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(
            id="s1", name="Implement and ship issue #10282", account="a",
            claude_session_id="", cols=80, rows=24, cwd="/tmp",
            status="needs_attention")}
        orch._registry = None  # no driver → no pending perms
        orch.pending_questions = lambda sid: []

        async def fake_window(session_id, *, before=None, limit=20):
            return ReplayWindow(events=[
                UserMessage(turn_id="t1", text="Please implement issue #10282"),
                ToolCall(turn_id="t1", tool_id="x", name="Bash",
                         kind=ToolKind.BASH, input={"command": "pytest -q"}),
                TextDelta(turn_id="t1", text="Tests failed with 2 errors in auth.py"),
            ], earliest_turn=0, total_turns=1)
        orch.transcript_window = fake_window

        ctx = await orch._autopilot_get_context("s1")
        assert ctx is not None
        tail = ctx.transcript_tail
        assert tail, "transcript_tail must not be empty when a transcript exists"
        text_blob = " ".join(str(e) for e in tail)
        assert "issue #10282" in text_blob          # user intent present
        assert "Tests failed" in text_blob          # last assistant signal present
        assert all(len(str(e)) < 800 for e in tail)  # entries stay compact

    asyncio.run(run())


def test_get_context_tail_empty_on_window_failure(tmp_path):
    """Best-effort: a transcript read failure degrades to an empty tail, never an error."""
    async def run():
        from server.sessions.types import SessionRecord
        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(
            id="s1", name="n", account="a", claude_session_id="",
            cols=80, rows=24, cwd="/tmp", status="done")}
        orch._registry = None
        orch.pending_questions = lambda sid: []

        async def boom(session_id, *, before=None, limit=20):
            raise RuntimeError("transcript unreadable")
        orch.transcript_window = boom

        ctx = await orch._autopilot_get_context("s1")
        assert ctx is not None and ctx.transcript_tail == []

    asyncio.run(run())


def test_note_user_interaction_stamps(tmp_path):
    async def run():
        from server.sessions.types import SessionRecord
        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                                             cols=80, rows=24, cwd="/tmp", status="idle")}
        assert orch._records["s1"].last_user_interaction_at is None
        orch.note_user_interaction("s1")
        assert orch._records["s1"].last_user_interaction_at is not None
    asyncio.run(run())


def test_autopilot_apply_does_not_stamp_interaction(tmp_path):
    """Anti-self-reset: autopilot's apply path must never stamp the human-interaction time."""
    async def run():
        from server.sessions.types import SessionRecord
        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                                             cols=80, rows=24, cwd="/tmp", status="idle")}
        orch._records["s1"].last_user_interaction_at = None
        # escalate/complete/skip return False without a driver and never touch the field.
        await orch._autopilot_apply("s1", Decision(action="escalate"))
        assert orch._records["s1"].last_user_interaction_at is None
    asyncio.run(run())


def test_get_context_reports_seconds_since_human(tmp_path):
    """seconds_since_human is computed from last_user_interaction_at when set."""
    from datetime import datetime, timedelta

    async def run():
        from server.sessions.types import SessionRecord
        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(
            id="s1", name="n", account="a", claude_session_id="",
            cols=80, rows=24, cwd="/tmp", status="done")}
        orch._registry = None
        orch.pending_questions = lambda sid: []

        async def boom(session_id, *, before=None, limit=20):
            raise RuntimeError("transcript unreadable")
        orch.transcript_window = boom

        orch._records["s1"].last_user_interaction_at = datetime.utcnow() - timedelta(seconds=5)
        ctx = await orch._autopilot_get_context("s1")
        assert ctx is not None
        assert 4.0 <= ctx.seconds_since_human <= 30.0

    asyncio.run(run())


def test_get_context_none_when_never_touched(tmp_path):
    """seconds_since_human is None when last_user_interaction_at has never been set."""
    async def run():
        from server.sessions.types import SessionRecord
        orch = _bare_orch()
        orch._records = {"s1": SessionRecord(
            id="s1", name="n", account="a", claude_session_id="",
            cols=80, rows=24, cwd="/tmp", status="done")}
        orch._registry = None
        orch.pending_questions = lambda sid: []

        async def boom(session_id, *, before=None, limit=20):
            raise RuntimeError("transcript unreadable")
        orch.transcript_window = boom

        orch._records["s1"].last_user_interaction_at = None
        ctx = await orch._autopilot_get_context("s1")
        assert ctx is not None
        assert ctx.seconds_since_human is None

    asyncio.run(run())


# --- fleet context (cross-session awareness) --------------------------------------

def _fleet_rec(sid, *, repo=None, branch=None, status="idle", name=None):
    from server.sessions.types import SessionRecord
    return SessionRecord(id=sid, name=name or sid, account="a", claude_session_id="",
                         cols=80, rows=24, cwd="/tmp", status=status,
                         repo=repo, branch=branch)


def test_get_context_populates_fleet_context(tmp_path):
    """get_context carries the sibling picture: same-repo detail + other one-liners."""
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        orch._clock = lambda: 100.0
        orch.pr_for = lambda repo, br: None
        orch._records = {
            "me": _fleet_rec("me", repo="o/r", branch="feat/me", status="done"),
            "sib": _fleet_rec("sib", repo="o/r", branch="feat/sib",
                              status="in_progress", name="fix parser"),
            "other": _fleet_rec("other", repo="x/y", status="idle", name="docs pass"),
        }
        orch._registry = None
        orch.pending_questions = lambda sid: []

        async def no_window(session_id, *, before=None, limit=20):
            return None
        orch.transcript_window = no_window

        ctx = await orch._autopilot_get_context("me")
        assert ctx is not None and ctx.fleet_context is not None
        sibs = ctx.fleet_context["same_repo_sessions"]
        assert [s["session"] for s in sibs] == ["fix parser"]
        assert sibs[0]["branch"] == "feat/sib"
        assert any("docs pass" in line
                   for line in ctx.fleet_context["other_active_sessions"])
    asyncio.run(run())


def test_get_context_fleet_degrades_to_none(tmp_path):
    """A broken fleet snapshot (here: no _clock on the bare orch) must degrade to
    fleet_context=None, never break get_context (mirrors the transcript-tail contract)."""
    async def run():
        orch = _bare_orch()   # no _clock, no DB → snapshot raises internally
        orch._records = {"s1": _fleet_rec("s1", status="done")}
        orch._registry = None
        orch.pending_questions = lambda sid: []

        async def no_window(session_id, *, before=None, limit=20):
            return None
        orch.transcript_window = no_window

        ctx = await orch._autopilot_get_context("s1")
        assert ctx is not None and ctx.fleet_context is None
    asyncio.run(run())


def test_fleet_snapshot_cached_within_ttl(tmp_path):
    """One snapshot serves the whole tick: the archived-session query runs once
    within the TTL and re-runs after it lapses."""
    async def run():
        orch = _bare_orch()
        now = [100.0]
        orch._clock = lambda: now[0]
        orch.pr_for = lambda repo, br: None
        orch._records = {}
        calls = []

        async def fake_archived(**kw):
            calls.append(1)
            return []
        orch._autopilot_recent_archived = fake_archived

        await orch._autopilot_fleet_snapshot()
        await orch._autopilot_fleet_snapshot()
        assert len(calls) == 1                 # second call hits the cache
        now[0] += 16.0                          # TTL (15s) lapses
        await orch._autopilot_fleet_snapshot()
        assert len(calls) == 2
    asyncio.run(run())


def test_recent_archived_window_and_outcome(tmp_path):
    """Archived rows inside the 24h window surface with their PR outcome; rows
    outside the window (and never-archived rows) stay invisible."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    async def run():
        await _init_db(tmp_path)
        from server.db import db_session
        from server.db.models import Session as DbSession
        now = datetime.utcnow()
        async with db_session() as db:
            db.add(DbSession(id="fresh", name="shipped parser fix", account_name="a",
                             cwd="/tmp", status="ended", repo="o/r", branch="feat/p",
                             active_goal="fix the parser",
                             archived_at=now - timedelta(hours=3)))
            db.add(DbSession(id="stale", name="old work", account_name="a",
                             cwd="/tmp", status="ended", repo="o/r",
                             archived_at=now - timedelta(hours=48)))
            db.add(DbSession(id="live", name="not archived", account_name="a",
                             cwd="/tmp", status="idle", repo="o/r"))

        orch = _bare_orch()
        orch.pr_for = lambda repo, br: SimpleNamespace(
            number=591, state="merged", is_draft=False, auto_merge_enabled=False,
            in_merge_queue=False, checks="passing", url="u") if br == "feat/p" else None

        rows = await orch._autopilot_recent_archived()
        assert [r.name for r in rows] == ["shipped parser fix"]
        assert rows[0].outcome == "PR #591 merged"
        assert rows[0].goal == "fix the parser"
        assert 2.5 <= rows[0].hours_ago <= 3.5
    asyncio.run(run())


# --- archive-context provenance ---------------------------------------------------


@pytest.mark.asyncio
async def test_autopilot_archive_context_records_resolution(tmp_path):
    """A session with pr_resolution set → archive context reason names the resolution
    and the pr_resolution key carries the raw token."""
    from server.sessions.types import SessionRecord
    from server.automations.completion_bar import PrFacts

    orch = _bare_orch()
    orch._records = {"s1": SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                                         cols=80, rows=24, cwd=str(tmp_path), repo="o/r")}
    orch._records["s1"].pr_resolution = "cancelled"
    # Stub _autopilot_pr_facts so the bare orch doesn't need pr_for / _session_pr_branches.
    orch._autopilot_pr_facts = lambda rec: PrFacts(has_repo=bool(rec.repo), prs=())

    captured: dict = {}

    async def _spy(session_id, *, archived_by="user", context=None):
        captured["by"] = archived_by
        captured["context"] = context

    orch.archive_session = _spy
    ok = await orch._autopilot_archive("s1")
    assert ok is True
    assert captured["by"] == "autopilot"
    assert "cancelled" in captured["context"]["reason"]
    assert captured["context"]["pr_resolution"] == "cancelled"


# --- PR-facts refresh + compose wrapper seams ------------------------------------


class _FakePoller:
    """Minimal PrStatusPoller stand-in: configurable cooldown, stubbed refresh."""

    def __init__(self):
        self._cooldown_until = 0.0
        self._clock = __import__("time").monotonic
        self.refreshed = []

    @property
    def in_cooldown(self) -> bool:
        return self._clock() < self._cooldown_until

    async def refresh_once(self, pairs):
        self.refreshed.extend(pairs)
        return True

    def cached(self, repo, branch):
        return None


class _FakePopulation:
    """Minimal population poller: returns an injected list per repo."""

    def __init__(self, prs_by_repo=None):
        self._prs = prs_by_repo or {}

    def cached(self, repo):
        return tuple(self._prs.get(repo, []))


def _make_orch_with_session(repo="o/r", branch="feat/x"):
    """Build a bare orchestrator wired with a session record and fake pollers."""
    from server.sessions.types import SessionRecord
    orch = _bare_orch()
    orch._records = {"s1": SessionRecord(
        id="s1", name="n", account="acct-a", claude_session_id="",
        cols=80, rows=24, cwd="/tmp", status="in_progress",
        repo=repo, branch=branch,
    )}
    orch._pr_poller = _FakePoller()
    orch._pr_population = _FakePopulation()
    # refresh_authored_branches does DB I/O in production; stub it out.
    async def _noop_refresh():
        pass
    orch.refresh_authored_branches = _noop_refresh
    return orch


@pytest.fixture
def orch():
    return _make_orch_with_session()


@pytest.mark.asyncio
async def test_refresh_pr_facts_returns_none_without_repo(orch):
    """No repo → None immediately (nothing to query)."""
    orch._records["s1"].repo = None
    assert await orch._autopilot_refresh_pr_facts("s1") is None


@pytest.mark.asyncio
async def test_refresh_pr_facts_skips_in_cooldown(orch):
    """Poller in backoff cooldown → caller gets None; don't pile on."""
    orch._pr_poller._cooldown_until = orch._pr_poller._clock() + 999
    assert await orch._autopilot_refresh_pr_facts("s1") is None


@pytest.mark.asyncio
async def test_refresh_pr_facts_merges_population_cache(orch):
    """Per-branch cache empty but population cache has a PR on the same branch —
    the linkage-gap PR must land in the returned facts.prs (spec §2)."""
    from server.github.pr_status import PullRequestInfo
    pop_pr = PullRequestInfo(
        number=42, url="https://github.com/o/r/pull/42",
        state="open", checks="passing", title="feat",
        is_draft=False, auto_merge_enabled=True, in_merge_queue=False,
        head_ref="feat/x",
    )
    orch._pr_population = _FakePopulation({"o/r": [pop_pr]})
    # pr_for returns None (per-branch cache misses) — population must fill the gap.
    orch.pr_for = lambda repo, branch: None
    facts = await orch._autopilot_refresh_pr_facts("s1")
    assert facts is not None
    numbers = [p.number for p in facts.prs]
    assert 42 in numbers, f"population-cache PR #42 missing from facts.prs={numbers}"


@pytest.mark.asyncio
async def test_compose_wrapper_passes_rich_tail_and_history(orch, monkeypatch):
    """_autopilot_compose calls compose_nudge with intent_hint, recent_nudges, and
    verdict fields from the Decision; returns whatever compose_nudge returns."""
    captured = {}

    async def fake_compose(ctx, **kw):
        captured.update(kw)
        return "do the thing"

    monkeypatch.setattr("server.automations.nudge_composer.compose_nudge", fake_compose)
    # Stub the rich tail so no transcript I/O runs.
    async def _fake_tail(sid, **kw):
        return [{"role": "assistant", "text": "work done"}]
    orch._autopilot_transcript_tail = _fake_tail

    ctx = make_decision_context(ledger={"recent_nudges": [{"ts": 1.0, "text": "old"}]})
    d = Decision(action="nudge", prompt="hint", downgraded_from="complete",
                 verdict_reason="r", verdict_remedy="m", needs_composition=True)
    out = await orch._autopilot_compose(ctx, d)
    assert out == "do the thing"
    assert captured["intent_hint"] == "hint"
    assert captured["recent_nudges"] == ["old"]
    assert captured["verdict_reason"] == "r"


@pytest.mark.asyncio
async def test_autopilot_archive_context_default_reason_when_unresolved(tmp_path):
    """A session without pr_resolution → context reason is the plain 'autopilot: goal met'
    and pr_resolution is None."""
    from server.sessions.types import SessionRecord
    from server.automations.completion_bar import PrFacts

    orch = _bare_orch()
    orch._records = {"s1": SessionRecord(id="s1", name="n", account="a", claude_session_id="",
                                         cols=80, rows=24, cwd=str(tmp_path), repo="o/r")}
    orch._autopilot_pr_facts = lambda rec: PrFacts(has_repo=bool(rec.repo), prs=())

    captured: dict = {}

    async def _spy(session_id, *, archived_by="user", context=None):
        captured["context"] = context

    orch.archive_session = _spy
    await orch._autopilot_archive("s1")
    assert captured["context"]["reason"] == "autopilot: goal met"
    assert captured["context"].get("pr_resolution") is None
