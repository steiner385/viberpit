"""Approve-as: the human may substitute a model, but block still refuses."""
import pytest

from server.sessions.orchestrator import ModelBlockedError, SessionOrchestrator
from server.sessions.types import SessionRecord


class _FakeDriver:
    def __init__(self):
        self.set_model_calls: list[str] = []
        self.turn_active = False

    async def set_model(self, model):
        self.set_model_calls.append(model)


@pytest.fixture
def audit_log(monkeypatch):
    """Capture record_audit calls instead of writing them."""
    entries: list[dict] = []

    async def _record(db, *, type, identity, detail=None, **kw):
        entries.append({"type": type, "identity": identity, "detail": detail or {}})
    monkeypatch.setattr("server.audit.record_audit", _record)
    monkeypatch.setattr("server.sessions.orchestrator.record_audit", _record, raising=False)
    return entries


@pytest.fixture
def orch_with_hold(monkeypatch, audit_log):
    """An orchestrator holding a mid-session, session-initiated fable request."""
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="acct", claude_session_id="c1",
                         cols=80, rows=24, cwd="/tmp", model="sonnet", status="in_progress")
    rec.pending_model = "fable"
    rec.pending_model_reason = "cross-module refactor"
    o._records = {"s1": rec}
    driver = _FakeDriver()
    o._drivers = {"s1": driver}
    o._self_model_state = {}
    o._handled_model_requests = {}
    o._publish_event = lambda payload: None
    o._pending_approvals = {}

    # Both "fable" (the original park) and "opus" (the counter-offer) are gated
    # tiers requiring approval, same as the reason either one was parked at all —
    # a bare "always allow" mock would let a re-park regression through set_model_
    # for_session hide undetected (require_approval never fires, so it never re-
    # parks either way). Only "block" refuses a counter-offer; require_approval is
    # exactly the case where routing through the gated method would wrongly re-ask.
    async def _allow(db, *, model=None, account=None):
        if model in ("fable", "opus"):
            return "require_approval"
        return "allow"
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", _allow)

    # Neutralise DB writes: this test exercises decision logic, not persistence.
    class _NullDb:
        async def execute(self, *a, **k):
            return None

        async def get(self, *a, **k):
            return None

    class _NullCtx:
        async def __aenter__(self):
            return _NullDb()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr("server.sessions.orchestrator.db_session", lambda: _NullCtx())
    return o, driver


@pytest.mark.asyncio
async def test_override_applies_the_humans_model_not_the_requested_one(orch_with_hold):
    o, driver = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    assert driver.set_model_calls == ["opus"]
    assert o._records["s1"].model == "opus"


@pytest.mark.asyncio
async def test_override_clears_the_hold_and_does_not_re_park(orch_with_hold):
    """The infinite-approval-loop regression: approving must END the negotiation."""
    o, _ = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    rec = o._records["s1"]
    assert rec.pending_model is None
    assert rec.pending_model_reason is None


@pytest.mark.asyncio
async def test_no_override_approves_exactly_what_was_requested(orch_with_hold):
    o, driver = orch_with_hold
    await o.resolve_model_approval("s1", "approve", identity="tony")
    assert driver.set_model_calls == ["fable"]


@pytest.mark.asyncio
async def test_blocked_counter_offer_is_refused(orch_with_hold, monkeypatch):
    async def _block(db, *, model=None, account=None):
        return "block" if model == "opus" else "allow"
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", _block)
    o, driver = orch_with_hold
    with pytest.raises(ModelBlockedError):
        await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    assert driver.set_model_calls == []
    assert o._records["s1"].pending_model == "fable"      # hold survives a refusal


@pytest.mark.asyncio
async def test_override_records_requested_and_applied(orch_with_hold, audit_log):
    o, _ = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    entry = [a for a in audit_log if a["type"] == "model_approval.approve"][-1]
    assert entry["detail"]["requested"] == "fable"
    assert entry["detail"]["applied"] == "opus"


@pytest.mark.asyncio
async def test_override_marks_the_tier_settled_so_the_session_stops_asking(orch_with_hold):
    o, _ = orch_with_hold
    await o.resolve_model_approval("s1", "approve", model="opus", identity="tony")
    assert "fable" in o._self_model_state["s1"].amended_away


@pytest.mark.asyncio
async def test_launch_time_hold_spawns_with_the_counter_offer(monkeypatch, audit_log):
    """The other hold kind: a not-yet-spawned session approved at a different model."""
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s2", name="n", account="acct", claude_session_id="",
                         cols=80, rows=24, cwd="/tmp", model=None,
                         status="pending_approval")
    rec.pending_model = "fable"
    o._records = {"s2": rec}
    o._drivers = {}
    o._self_model_state = {}
    o._handled_model_requests = {}
    o._pending_approvals = {"s2": {"cwd": "/tmp"}}
    o._publish_event = lambda payload: None
    spawned = []

    async def _spawn_session(session_id, *, model=None, **kw):
        spawned.append((session_id, model))
        return rec
    o._spawn_session = _spawn_session
    o._active_session_count = lambda: 0

    async def _max_active():
        return 10
    o._max_active_sessions = _max_active

    async def _allow(db, *, model=None, account=None):
        return "allow"
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", _allow)

    class _NullDb:
        async def execute(self, *a, **k):
            return None

        async def get(self, *a, **k):
            return None

        async def delete(self, *a, **k):
            return None

    class _NullCtx:
        async def __aenter__(self):
            return _NullDb()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr("server.sessions.orchestrator.db_session", lambda: _NullCtx())

    await o.resolve_model_approval("s2", "approve", model="opus", identity="tony")
    assert spawned == [("s2", "opus")]
