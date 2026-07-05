import ast
import asyncio
from pathlib import Path

import pytest
from server.sessions.remote import RemoteDriver, RemoteOrchestrator

_SERVER = Path(__file__).resolve().parent.parent
# Every source file that receives a driver and calls methods on it. The confirmed
# #164 AttributeError was in suggestions.kick_idle_suggestions, reached from ws.py —
# both must be scanned so the two driver surfaces can't silently drift again.
_DRIVER_CALLERS = (_SERVER / "bridge" / "ws.py", _SERVER / "sessions" / "suggestions.py")


def _driver_members_used(*paths: Path) -> set[str]:
    """Collect every driver member accessed at RUNTIME in *paths* — real
    ``driver.<name>`` attribute reads and ``getattr(driver, "<name>")`` calls (the
    local param is named ``driver`` in every helper ws.py hands the object to).

    Parsed via ``ast`` rather than regex so docstrings/comments that merely *mention*
    ``driver.identity`` aren't mistaken for call sites. This is the ws-reachable
    driver surface RemoteDriver must cover in daemon mode."""
    names: set[str] = set()
    for p in paths:
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name) and node.value.id == "driver"):
                names.add(node.attr)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "getattr" and len(node.args) >= 2
                  and isinstance(node.args[0], ast.Name) and node.args[0].id == "driver"
                  and isinstance(node.args[1], ast.Constant)
                  and isinstance(node.args[1].value, str)):
                names.add(node.args[1].value)
    return names


class _FakeClient:
    """Minimal client: subscribe() yields preset frames; call() returns found."""
    def __init__(self, frames, found=True, status="idle", queued=None):
        self._frames = frames
        self._found = found
        self._status = status
        self._queued = queued or []
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        if self._found:
            return {"found": self._found, "status": self._status, "queued": self._queued}
        return {"found": False}

    async def _gen(self):
        for f in self._frames:
            yield f

    def subscribe(self, session_id, since=None):
        return self._gen()


def test_remote_driver_covers_every_ws_reachable_driver_member():
    """Lock the daemon-mode driver surface to the in-process one (#164).

    In remote mode ws.py drives a RemoteDriver shim instead of the in-process
    SessionDriver. Any ``driver.*`` call site in ws.py (or a helper it hands the
    driver to) that RemoteDriver doesn't implement explodes with AttributeError
    ONLY in remote mode — the confirmed instance was seed_idle_suggestions, silently
    breaking idle suggestions in prod. This scans the real call sites and fails if a
    new one is added without a matching RemoteDriver method/attr, so the surfaces
    can't drift again."""
    used = _driver_members_used(*_DRIVER_CALLERS)
    # Sanity anchors for the scan itself: ws.py consumes the live stream via
    # driver.open_stream() (driver.events() is no longer a ws-reachable member —
    # the returned stream owns events()), and reads current_status() on attach.
    assert "open_stream" in used and "current_status" in used, (
        "static scan found no driver.* usages — the regex or call sites moved")
    d = RemoteDriver(_FakeClient([]), "s1")
    missing = sorted(name for name in used if not hasattr(d, name))
    assert not missing, (
        f"RemoteDriver is missing driver members ws.py/helpers call: {missing}. "
        "Remote (daemon) mode will AttributeError at those call sites — add the "
        "method (or an RPC proxy) to RemoteDriver in server/sessions/remote.py."
    )


def test_remote_driver_queue_wire_matches_local_shape():
    """ws.py calls driver.queue_wire() on every attach. RemoteDriver must implement
    it (returning [{text, images}]) or remote attaches raise AttributeError and
    queued prompts go invisible. Regression for the daemon-mode WS attach crash."""
    d = RemoteDriver(_FakeClient([]), "s1", queued=["first prompt", "second prompt"])
    assert d.queue_wire() == [
        {"text": "first prompt", "images": []},
        {"text": "second prompt", "images": []},
    ]
    # Empty queue → empty wire (mirrors local SessionDriver.queue_wire).
    assert RemoteDriver(_FakeClient([]), "s2").queue_wire() == []


@pytest.mark.asyncio
async def test_remote_driver_events_wrap_subscribe():
    c = _FakeClient([{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}])
    d = RemoteDriver(c, "s1")
    got = [e async for e in d.events()]
    assert [e.to_wire() for e in got] == [{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}]


@pytest.mark.asyncio
async def test_ensure_driver_returns_remotedriver_when_found():
    c = _FakeClient([{"type": "turnEnd"}], found=True, status="idle", queued=[])
    ro = RemoteOrchestrator(c)
    driver = await ro.ensure_driver("s1")
    assert isinstance(driver, RemoteDriver)
    assert c.calls == [("ensure_driver", {"session_id": "s1", "identity": "system"})]
    first = await asyncio.wait_for(driver.events().__anext__(), 2)
    assert first.to_wire() == {"type": "turnEnd"}


@pytest.mark.asyncio
async def test_ensure_driver_returns_none_when_not_found():
    c = _FakeClient([], found=False)
    ro = RemoteOrchestrator(c)
    assert await ro.ensure_driver("missing") is None


@pytest.mark.asyncio
async def test_remote_driver_snapshot_and_wire_events():
    from server.sessions.remote import RemoteDriver

    class _Client:
        async def call(self, method, **params):
            return {"found": True, "status": "in_progress", "queued": ["q1", "q2"]}
        async def _gen(self):
            yield {"type": "textDelta", "text": "hi"}
        def subscribe(self, session_id, since=None):
            return self._gen()

    ro = RemoteOrchestrator(_Client())
    d = await ro.ensure_driver("s1")
    assert isinstance(d, RemoteDriver)
    assert d.current_status() == "in_progress"
    assert d.queued() == ["q1", "q2"]
    ev = await d.events().__anext__()
    assert ev.to_wire() == {"type": "textDelta", "text": "hi"}


@pytest.mark.asyncio
async def test_remote_driver_seed_idle_suggestions_issues_rpc():
    # RemoteDriver must mirror the local Driver's seed_idle_suggestions so the WS
    # attach path (kick_idle_suggestions) works in daemon mode instead of raising
    # AttributeError. It is best-effort and fire-and-forget: a sync call that
    # schedules the RPC on the daemon-side driver.
    c = _FakeClient([])
    d = RemoteDriver(c, "s1")
    assert d.seed_idle_suggestions("do the thing", "earlier answer") is None
    # fire-and-forget: let the scheduled task run
    for _ in range(10):
        await asyncio.sleep(0)
        if c.calls:
            break
    assert (
        "seed_idle_suggestions",
        {"session_id": "s1", "user_text": "do the thing", "assistant_text": "earlier answer"},
    ) in c.calls


@pytest.mark.asyncio
async def test_remote_driver_subscribes_with_head_seq_anchor():
    captured = {}

    class _Client:
        async def call(self, method, **params):
            return {"found": True, "status": "idle", "queued": [], "head_seq": 7}

        async def _gen(self):
            # Empty generator: immediately raises StopAsyncIteration
            return
            yield

        def subscribe(self, session_id, since=None):
            captured["since"] = since
            return self._gen()

    ro = RemoteOrchestrator(_Client())
    d = await ro.ensure_driver("s1")
    assert isinstance(d, RemoteDriver)
    # Kick the generator so events() calls subscribe().
    agen = d.events()
    try:
        await agen.__anext__()
    except StopAsyncIteration:
        pass
    assert captured["since"] == 7


def test_remote_driver_subscribes_from_resume_since_and_exposes_turn_active():
    import asyncio
    from server.sessions.remote import RemoteDriver

    captured = {}
    class FakeClient:
        def subscribe(self, session_id, since=None):
            captured["since"] = since
            async def gen():
                if False:
                    yield None
            return gen()

    async def run():
        d = RemoteDriver(FakeClient(), "s", status="in_progress", queued=[],
                         head_seq=10, resume_since=3, turn_active=True)
        assert d.turn_active is True
        async for _ in d.events():
            pass
    asyncio.run(run())
    assert captured["since"] == 3   # resume_since, not head_seq


def test_remote_orchestrator_threads_resume_fields():
    import asyncio
    from server.sessions.remote import RemoteOrchestrator

    class FakeClient:
        async def call(self, method, **kw):
            assert method == "ensure_driver"
            return {"found": True, "status": "in_progress", "queued": [],
                    "head_seq": 10, "resume_since": 3, "turn_active": True,
                    "turn_started_at": 1751710000.5}
        def subscribe(self, *a, **k):
            async def gen():
                if False:
                    yield None
            return gen()

    async def run():
        orch = RemoteOrchestrator(FakeClient())
        d = await orch.ensure_driver("s", "ident")
        assert d.turn_active is True
        assert d._resume_since == 3
        # turn_started_at must ride the same snapshot: ws.py hands it to
        # replay_window so the incomplete-turn drop can distinguish the in-progress
        # turn's disk user entry from a PREVIOUS turn's (resume-lag amputation bug).
        assert d.turn_started_at == 1751710000.5
    asyncio.run(run())


def test_remote_driver_turn_started_at_defaults_none():
    """A snapshot without the field (idle session / legacy daemon) must read None —
    ws.py falls back to the legacy positional drop then."""
    d = RemoteDriver(_FakeClient([]), "s1")
    assert d.turn_started_at is None


@pytest.mark.asyncio
async def test_remote_driver_open_stream_shim():
    """RemoteDriver.open_stream() must present the SAME stream interface as the
    local DriverStream (turn_active / turn_started_at / events() / close()) so
    ws.py never hasattr-branches on the driver flavor. The flags come from the
    ensure_driver RPC snapshot; events() delegates to the subscribe-based
    generator; close() is a best-effort no-op (async-generator teardown closes
    the daemon subscription)."""
    c = _FakeClient([{"type": "textDelta", "text": "hi"}])
    d = RemoteDriver(c, "s1", turn_active=True, turn_started_at=1751710123.5)
    stream = d.open_stream()
    assert stream.turn_active is True
    assert stream.turn_started_at == 1751710123.5
    got = [e.to_wire() async for e in stream.events()]
    assert got == [{"type": "textDelta", "text": "hi"}]
    assert stream.close() is None   # nothing to release before events() is iterated
