import pytest

from server.sessions.orchestrator import SessionOrchestrator, RESUME_NUDGE
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord, RESUME_ELIGIBLE_STATUSES, SYSTEM_IDENTITY


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                cols=80, rows=24, cwd="/tmp", repo=None)
    base.update(kw)
    return SessionRecord(**base)  # type: ignore[arg-type]


def test_eligible_statuses_are_work_states_only():
    assert RESUME_ELIGIBLE_STATUSES == frozenset({"in_progress", "working", "condensing"})


def test_resumable_true_only_for_flagged_work_state():
    r = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    assert r.to_dict()["resumable"] is True


def test_resumable_false_when_from_status_not_work():
    r = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="ready_review")
    assert r.to_dict()["resumable"] is False


def test_resumable_false_without_flag():
    r = _rec(status="in_progress", interrupted_by_restart=False, interrupted_from_status="in_progress")
    assert r.to_dict()["resumable"] is False


def test_interrupted_from_status_round_trips():
    r = _rec(interrupted_by_restart=True, interrupted_from_status="working")
    assert SessionRecord.from_dict(r.to_dict()).interrupted_from_status == "working"


# ---------------------------------------------------------------------------
# Task 2 — resume_interrupted primitive tests
# ---------------------------------------------------------------------------

class _FakeDriver: ...


def _orch_with(rec):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec} if rec else {}
    orch._drivers = {}
    calls = {"ensure": [], "prompt": [], "replayable": []}

    async def ensure_driver(sid, identity=SYSTEM_IDENTITY):
        calls["ensure"].append((sid, identity))
        d = _FakeDriver()
        orch._drivers[sid] = d
        return d

    async def send_prompt(sid, text, attachment_ids=None, *, replayable=None):
        calls["prompt"].append((sid, text))
        calls["replayable"].append(replayable)
        return "sent"

    orch.ensure_driver = ensure_driver          # type: ignore[assignment]
    orch.send_prompt = send_prompt               # type: ignore[assignment]
    return orch, calls


@pytest.mark.asyncio
async def test_resume_interrupted_eligible_nudges():
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, calls = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == "resumed"
    assert calls["ensure"] == [("s1", SYSTEM_IDENTITY)]
    assert calls["prompt"] == [("s1", RESUME_NUDGE)]
    # The nudge is ours, not the user's turn: a later restart must not replay it (#2922).
    assert calls["replayable"] == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_status,reason", [
    ("ready_review", "not-resumable-status"),
    ("waiting_question", "not-resumable-status"),
    ("waiting_input", "not-resumable-status"),
])  # no lost-hold content: nothing to re-ask, so the session stays put
async def test_resume_interrupted_skips_non_work(from_status, reason):
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status=from_status)
    orch, calls = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == f"skipped:{reason}"
    assert calls["ensure"] == [] and calls["prompt"] == []


@pytest.mark.asyncio
async def test_resume_interrupted_skips_unflagged():
    rec = _rec(status="idle", interrupted_by_restart=False, interrupted_from_status=None)
    orch, _ = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == "skipped:not-interrupted"


@pytest.mark.asyncio
async def test_resume_interrupted_unknown_session():
    orch, _ = _orch_with(None)
    assert await orch.resume_interrupted("nope") == "skipped:not-found"


@pytest.mark.asyncio
async def test_resume_interrupted_no_driver_propagates():
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="working")
    orch, _ = _orch_with(rec)
    async def ensure_none(sid, identity=SYSTEM_IDENTITY): return None
    orch.ensure_driver = ensure_none             # type: ignore[assignment]
    assert await orch.resume_interrupted("s1") == "no_driver"


@pytest.mark.asyncio
async def test_resume_interrupted_draining_propagates():
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, _ = _orch_with(rec)

    async def send_draining(sid, text, attachment_ids=None, *, replayable=None):
        return "draining"

    orch.send_prompt = send_draining             # type: ignore[assignment]
    assert await orch.resume_interrupted("s1") == "draining"


# ---------------------------------------------------------------------------
# Task 4 — auto_resume_interrupted tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_resume_on_by_default(monkeypatch):
    monkeypatch.delenv("VIBERPIT_AUTO_RESUME_ON_RESTART", raising=False)
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 1
    assert [p for _, p in calls["prompt"]] == [RESUME_NUDGE]


@pytest.mark.asyncio
async def test_auto_resume_kill_switch(monkeypatch):
    monkeypatch.setenv("VIBERPIT_AUTO_RESUME_ON_RESTART", "false")
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 0
    assert calls["prompt"] == []


@pytest.mark.asyncio
async def test_auto_resume_resumes_eligible_when_enabled(monkeypatch):
    monkeypatch.setenv("VIBERPIT_AUTO_RESUME_ON_RESTART", "true")
    eligible = _rec(id="s1", status="idle", interrupted_by_restart=True,
                    interrupted_from_status="working")
    parked = _rec(id="s2", status="idle", interrupted_by_restart=True,
                  interrupted_from_status="ready_review")
    orch, calls = _orch_with(eligible)
    orch._records["s2"] = parked
    assert await orch.auto_resume_interrupted() == 1
    assert [sid for sid, _ in calls["prompt"]] == ["s1"]   # s2 skipped


@pytest.mark.asyncio
async def test_resume_interrupted_never_sends_to_an_assistant():
    """D-12: an assistant's driver would come up with no side-chat scope; the nudge is
    never auto-sent to it (as `auto_resume_interrupted` already skips it)."""
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress",
               kind="assistant")
    orch, calls = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == "skipped:assistant"
    assert calls["ensure"] == [] and calls["prompt"] == []


# ---------------------------------------------------------------------------
# Phase 3 (FR-306) — hold-aware eligibility, per-session veto, AUTO_RESUMED events
# ---------------------------------------------------------------------------

_HOLD = {"kind": "question", "holdId": "h1", "createdAt": "2026-11-27T00:00:00Z",
         "content": {"questions": [{"question": "Ship it to prod?"}]}}


@pytest.fixture
def events(monkeypatch):
    from server.sessions import provenance
    got: list = []

    async def fake_record(sid, kind, detail=None):
        got.append((sid, kind, detail))

    monkeypatch.setattr(provenance, "record", fake_record)
    return got


@pytest.mark.asyncio
async def test_waiting_session_with_lost_hold_auto_resumes_and_reasks(events):
    rec = _rec(status="idle", interrupted_by_restart=True,
               interrupted_from_status="waiting_question", lost_hold=_HOLD)
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 1
    (_, nudge), = calls["prompt"]
    assert nudge.startswith(RESUME_NUDGE)
    assert "Ship it to prod?" in nudge
    assert "do not decide it yourself" in nudge
    assert events == [("s1", "auto_resumed", {
        "fromStatus": "waiting_question", "holdAware": True, "holdKind": "question"})]


@pytest.mark.asyncio
async def test_waiting_session_without_hold_stays_put(events):
    rec = _rec(status="idle", interrupted_by_restart=True,
               interrupted_from_status="waiting_question", lost_hold=None)
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 0
    assert calls["prompt"] == [] and events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("from_status,hold", [("working", None),
                                              ("waiting_question", _HOLD)])
async def test_opted_out_session_is_never_auto_resumed(events, from_status, hold):
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status=from_status,
               lost_hold=hold, auto_resume_opt_out=True)
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 0
    assert calls["prompt"] == [] and events == []


@pytest.mark.asyncio
async def test_work_status_resume_event_is_not_hold_aware(events):
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="working")
    orch, _ = _orch_with(rec)
    await orch.auto_resume_interrupted()
    assert events == [("s1", "auto_resumed", {
        "fromStatus": "working", "holdAware": False, "holdKind": None})]


def test_resumable_flag_true_for_waiting_status_with_hold():
    r = _rec(status="idle", interrupted_by_restart=True,
             interrupted_from_status="waiting_question", lost_hold=_HOLD)
    assert r.to_dict()["resumable"] is True


def test_auto_resume_opt_out_round_trips():
    r = _rec(auto_resume_opt_out=True)
    assert r.to_dict()["autoResumeOptOut"] is True
    assert SessionRecord.from_dict(r.to_dict()).auto_resume_opt_out is True
