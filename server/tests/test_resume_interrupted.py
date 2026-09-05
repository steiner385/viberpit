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
    calls = {"ensure": [], "prompt": []}

    async def ensure_driver(sid, identity=SYSTEM_IDENTITY):
        calls["ensure"].append((sid, identity))
        d = _FakeDriver()
        orch._drivers[sid] = d
        return d

    async def send_prompt(sid, text, attachment_ids=None):
        calls["prompt"].append((sid, text))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("from_status,reason", [
    ("ready_review", "not-resumable-status"),
    ("waiting_question", "not-resumable-status"),
    ("waiting_input", "not-resumable-status"),
])
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

    async def send_draining(sid, text, attachment_ids=None):
        return "draining"

    orch.send_prompt = send_draining             # type: ignore[assignment]
    assert await orch.resume_interrupted("s1") == "draining"


# ---------------------------------------------------------------------------
# Task 4 — auto_resume_interrupted tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_resume_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_AUTO_RESUME_ON_RESTART", raising=False)
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 0
    assert calls["prompt"] == []


@pytest.mark.asyncio
async def test_auto_resume_resumes_eligible_when_enabled(monkeypatch):
    monkeypatch.setenv("CCSWITCH_AUTO_RESUME_ON_RESTART", "true")
    eligible = _rec(id="s1", status="idle", interrupted_by_restart=True,
                    interrupted_from_status="working")
    parked = _rec(id="s2", status="idle", interrupted_by_restart=True,
                  interrupted_from_status="ready_review")
    orch, calls = _orch_with(eligible)
    orch._records["s2"] = parked
    assert await orch.auto_resume_interrupted() == 1
    assert [sid for sid, _ in calls["prompt"]] == ["s1"]   # s2 skipped
