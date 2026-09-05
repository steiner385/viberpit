"""reload_session — the "Reload session" menu action rebuilds the driver in place.

Harness mirrors test_reassign_reapplies_model.py: a bare SessionOrchestrator with the
collaborators _reassign reaches for stubbed, so the assertions are about WHICH driver
is built, with what identity, and whether the interrupted prompt is replayed.
"""
import time

import pytest

from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, *a, **k): return None


class _NewDriver:
    def __init__(self, *a, **k):
        self.account = a[1] if len(a) > 1 else k.get("account")
        self.cwd = "/tmp"; self.mode = "interactive"
        self.identity = k.get("identity")
        self.engine_session_id = "c1"
        self.sent: list[str] = []
    async def start(self): pass
    async def send_prompt(self, p): self.sent.append(p)
    async def set_model(self, m): pass


class _OldDriver:
    cwd = "/tmp"; mode = "interactive"; identity = SYSTEM_IDENTITY; engine_session_id = "c1"
    def __init__(self):
        self.closed = False
    async def interrupt(self): pass
    async def aclose(self): self.closed = True


def _orch(monkeypatch, rec, old, *, last_prompt=None):
    built: list[_NewDriver] = []

    class _Factory(_NewDriver):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            built.append(self)

    monkeypatch.setattr(
        orch_mod.SessionOrchestrator,
        "_create_driver",
        lambda self, **kw: _Factory(
            account=kw.get("account"), resume=kw.get("resume"), identity=kw.get("identity")),
    )
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch._drivers = {rec.id: old} if old is not None else {}
    orch._last_prompt = {rec.id: last_prompt} if last_prompt else {}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = time.time
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None
    return orch, built


def _rec(status: str) -> SessionRecord:
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                         cols=80, rows=24, cwd="/tmp", status=status)


@pytest.mark.asyncio
async def test_reload_rebuilds_driver_on_same_account_and_rethreads_identity(monkeypatch):
    """The scenario from 2026-09-04: a system-built driver parked on a question the
    user cannot answer. Reload closes it, resumes the transcript on the SAME account,
    and the new driver belongs to the user who asked for the reload."""
    rec = _rec("waiting_question")
    old = _OldDriver()
    orch, built = _orch(monkeypatch, rec, old, last_prompt="implement #15144")

    result = await orch.reload_session("s1", "user@example.test")

    assert result == "reloaded"
    assert old.closed, "the wedged driver must be closed"
    assert len(built) == 1 and orch._drivers["s1"] is built[0]
    assert built[0].account == "a" and rec.account == "a", "reload never switches account"
    assert built[0].identity == "user@example.test", (
        "the rebuilt driver must be re-threaded to the reloading user so its next "
        "question/permission is answerable by them")


@pytest.mark.asyncio
async def test_reload_replays_the_interrupted_prompt_only_when_mid_turn(monkeypatch):
    """#864 rule: continue a turn the rebuild killed, never redo an awaiting-user one."""
    rec = _rec("in_progress")
    orch, built = _orch(monkeypatch, rec, _OldDriver(), last_prompt="keep going")
    assert await orch.reload_session("s1", "u@x") == "reloaded"
    assert built[0].sent == ["keep going"]

    rec = _rec("waiting_question")
    orch, built = _orch(monkeypatch, rec, _OldDriver(), last_prompt="implement #15144")
    assert await orch.reload_session("s1", "u@x") == "reloaded"
    assert built[0].sent == [], (
        "a session parked on a question must resume and WAIT — re-sending the prompt "
        "is the 'I paused since it was rejected' confusion reload recovers from")


@pytest.mark.asyncio
async def test_reload_works_for_a_driverless_session(monkeypatch):
    """A session whose driver the daemon lost (idle, no live driver) is rebuilt from
    record state — the driverless branch of _reassign."""
    rec = _rec("idle")
    orch, built = _orch(monkeypatch, rec, None)
    assert await orch.reload_session("s1", "u@x") == "reloaded"
    assert len(built) == 1 and orch._drivers["s1"] is built[0]
    assert built[0].identity == "u@x"


@pytest.mark.asyncio
async def test_reload_refuses_unknown_and_parked_sessions(monkeypatch):
    rec = _rec("archived")
    orch, built = _orch(monkeypatch, rec, _OldDriver())
    assert await orch.reload_session("nope", "u@x") == "skipped:not-found"
    assert await orch.reload_session("s1", "u@x") == "skipped:archived"
    assert built == [], "a parked session must not get a driver built"
    for parked in ("queued", "pending_approval", "ended"):
        rec.status = parked
        assert await orch.reload_session("s1", "u@x") == f"skipped:{parked}"
    assert built == []
