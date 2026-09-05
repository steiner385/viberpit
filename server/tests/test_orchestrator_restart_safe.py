"""restart_safe(): True only when no session is mid-turn or awaiting input."""
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _rec(sid: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=sid, name=sid, account="acct", engine=AgentEngine.CLAUDE_CODE, engine_session_id="cs",
        cols=80, rows=24, cwd="/tmp", status=status,
    )


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def test_no_sessions_is_safe():
    o = _orch()
    assert o.active_session_count() == 0
    assert o.restart_safe() is True


def test_idle_and_terminal_statuses_are_safe():
    o = _orch()
    for i, s in enumerate(["idle", "ready_review", "done", "waiting_external", "limited", "ended", "archived"]):
        o._records[f"s{i}"] = _rec(f"s{i}", s)
    assert o.active_session_count() == 0
    assert o.restart_safe() is True


def test_active_statuses_block_restart():
    o = _orch()
    for i, s in enumerate(["in_progress", "working", "waiting_input"]):
        o._records[f"s{i}"] = _rec(f"s{i}", s)
    assert o.active_session_count() == 3
    assert o.restart_safe() is False


def test_mixed_counts_only_active():
    o = _orch()
    o._records["a"] = _rec("a", "idle")
    o._records["b"] = _rec("b", "in_progress")
    o._records["c"] = _rec("c", "done")
    assert o.active_session_count() == 1
    assert o.restart_safe() is False
