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
    async def send_prompt(self, p, *a, **k): self.sent.append(p)
    async def set_model(self, m): pass


class _OldDriver:
    cwd = "/tmp"; mode = "interactive"; identity = SYSTEM_IDENTITY; engine_session_id = "c1"
    def __init__(self):
        self.closed = False
        self.sent: list[str] = []
    async def interrupt(self): pass
    async def send_prompt(self, p, *a, **k):
        # A closing driver clears its queue in aclose(): anything handed to it here
        # is lost, which is exactly the 2026-10-07 dropped first prompt.
        self.sent.append(p)
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
    orch._draining = False
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


# ── 2026-10-07 (session 1c1a0dfb): the wake-up after a reload ─────────────────────
# The reload API call hit a ReadTimeout while the swap kept running, and the first
# message the operator sent afterwards landed on the CLOSING driver — told "sent",
# never delivered. A swap is now a window prompts wait out, and reload answers
# promptly instead of holding the HTTP call open for the whole rebuild.

def _slow_start(monkeypatch, gate):
    async def start(self):
        await gate.wait()
    monkeypatch.setattr(_NewDriver, "start", start)


async def _until(cond, *, timeout=10.0):
    """Poll on a wall-clock bound, not a tick count: the swap's tail runs real
    threaded awaits, which take arbitrarily many loop ticks under `-n auto` load."""
    import asyncio
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_prompt_sent_during_a_reload_waits_for_the_new_driver(monkeypatch):
    import asyncio
    gate = asyncio.Event()
    _slow_start(monkeypatch, gate)
    rec = _rec("idle")
    old = _OldDriver()
    orch, built = _orch(monkeypatch, rec, old)

    reload = asyncio.create_task(orch.reload_session("s1", "u@x"))
    await _until(lambda: "s1" in orch._swapping)
    send = asyncio.create_task(orch.send_prompt("s1", "are you back?"))
    await asyncio.sleep(0)
    assert not send.done(), "the prompt must be held while the swap is in flight"
    gate.set()
    assert await send == "sent"
    await reload
    assert old.sent == [], "nothing may be handed to the driver being closed"
    assert built[0].sent == ["are you back?"]


@pytest.mark.asyncio
async def test_prompt_held_past_the_bound_is_refused_not_dropped(monkeypatch):
    """A swap that outlasts the hold is refused with "recovering" — the bridge's
    promptRejected re-queue — never silently written to the closing driver."""
    import asyncio
    gate = asyncio.Event()
    _slow_start(monkeypatch, gate)
    monkeypatch.setattr(orch_mod, "_SWAP_PROMPT_HOLD_S", 0.01)
    rec = _rec("idle")
    old = _OldDriver()
    orch, built = _orch(monkeypatch, rec, old)

    reload = asyncio.create_task(orch.reload_session("s1", "u@x"))
    await _until(lambda: "s1" in orch._swapping)
    assert await orch.send_prompt("s1", "hello?") == "recovering"
    assert old.sent == []
    gate.set()
    await reload


def test_prompt_hold_stays_under_the_edge_rpc_read_timeout():
    """The edge reaches the daemon's send_prompt over RPC. A hold at or past the
    RPC read timeout surfaces as "unavailable" (the browser re-queues) while the
    daemon goes on to deliver it — the same prompt twice."""
    from server.ipc import client as ipc_client
    assert orch_mod._SWAP_PROMPT_HOLD_S < ipc_client._TIMEOUT.read


@pytest.mark.asyncio
async def test_slow_reload_answers_reloading_and_finishes_in_the_background(monkeypatch):
    import asyncio
    gate = asyncio.Event()
    _slow_start(monkeypatch, gate)
    monkeypatch.setattr(orch_mod, "_RELOAD_RESPONSE_WAIT_S", 0.01)
    rec = _rec("idle")
    old = _OldDriver()
    orch, built = _orch(monkeypatch, rec, old)

    assert await orch.reload_session("s1", "user@example.test") == "reloading"
    assert "s1" in orch._swapping, "the swap keeps running after the answer"
    gate.set()
    await _until(lambda: orch._drivers.get("s1") is (built[0] if built else None)
                 and "s1" not in orch._swapping)
    await _until(lambda: built[0].identity == "user@example.test")


def test_reload_answer_stays_under_the_assistant_client_timeout():
    """The assistant's reload_session tool posts through a 20s httpx client; the
    2026-10-07 ReadTimeout was that client giving up on a reload that succeeded."""
    import inspect
    from server.assistant import client as assistant_client
    default = inspect.signature(assistant_client.AssistantApi.__init__).parameters["timeout_s"].default
    assert orch_mod._RELOAD_RESPONSE_WAIT_S < default
