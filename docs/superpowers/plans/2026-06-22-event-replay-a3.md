# A3: Event Replay Across the Attach/Reconnect Gap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The daemon buffers per-session events in a bounded ring keyed by a monotonic sequence and replays the tail a (re)subscribing edge missed, closing the at-attach and reconnect gaps without duplicating persisted transcript history.

**Architecture:** `publish` assigns a per-stream seq and appends to a bounded deque; a SUBSCRIBE frame carries `since`; the daemon replays buffered `seq > since` to the new subscriber before live frames. `ensure_driver` returns `head_seq` so the first subscribe anchors at the attach point; the client tracks `last_seq` for reconnect. Seq is stripped before events reach `ws.py`.

**Tech Stack:** Python 3.12 · FastAPI/uvicorn-on-UDS · websockets · pytest/pytest-asyncio.

## Global Constraints

- `seq`/`since` wire fields are ADDITIVE; a missing field means null/auto. Edge+daemon deploy together.
- Events must reach `ws.py` as the SAME bare wire dict as today (seq stripped in the client) — do NOT change `server/bridge/ws.py`, the orchestrator, or the edge `create_app`/lifespan.
- Replay anchored at `since` must NOT re-deliver events `≤ since` (the transcript-duplication trap).
- Buffer is bounded by `CCSWITCH_IPC_REPLAY_BUFFER` (default 256); oldest dropped under flood.
- Run tests from this worktree with the main checkout's venv + `PYTHONPATH=$(pwd)/src`:
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>`
- CI gates: `ruff check .` (full repo), `mypy server/ --ignore-missing-imports`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Daemon ring buffer + seq + replay-on-subscribe

**Files:**
- Modify: `server/ipc/protocol.py` (`event_frame`, `subscribe_frame`)
- Modify: `server/ipc/daemon.py` (`create_daemon_app`: buffers, seqs, publish, `/events` SUBSCRIBE, `head_seq`)
- Test: `server/tests/test_ipc_replay.py` (create)

**Interfaces:**
- Produces: `event_frame(stream, event, seq)` → adds `"seq"`; `subscribe_frame(stream, since=None)` → adds `"since"`. `app.state.head_seq(stream) -> int` (max seq or -1). `app.state.publish(stream, event)` assigns seq + buffers. SUBSCRIBE with `since` replays buffered `seq > since` to the new WS first.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ipc_replay.py
import asyncio
import json
import pytest
import websockets
from server.ipc import protocol as proto
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


async def _collect(ws, n, timeout=2):
    out = []
    for _ in range(n):
        raw = await asyncio.wait_for(ws.recv(), timeout)
        out.append(json.loads(raw))
    return out


@pytest.mark.asyncio
async def test_publish_before_subscribe_is_replayed():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        # Publish two events with NO subscriber yet — must be buffered.
        await app.state.publish("s1", {"type": "a"})
        await app.state.publish("s1", {"type": "b"})
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=-1)))
            frames = await _collect(ws, 2)
    assert [f["event"]["type"] for f in frames] == ["a", "b"]
    assert [f["seq"] for f in frames] == [0, 1]


@pytest.mark.asyncio
async def test_since_anchor_skips_already_seen():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        await app.state.publish("s1", {"type": "a"})   # seq 0
        await app.state.publish("s1", {"type": "b"})   # seq 1
        head = app.state.head_seq("s1")                # 1
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=head)))
            # Nothing replayed (all <= head); a new event after subscribe IS delivered.
            await asyncio.sleep(0.1)
            await app.state.publish("s1", {"type": "c"})  # seq 2
            frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
    assert frame["event"]["type"] == "c"
    assert frame["seq"] == 2


@pytest.mark.asyncio
async def test_buffer_is_bounded(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "4")
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        for i in range(6):
            await app.state.publish("s1", {"type": str(i)})  # seq 0..5
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=-1)))
            frames = await _collect(ws, 4)
    # Only the last 4 survive (seq 2..5).
    assert [f["seq"] for f in frames] == [2, 3, 4, 5]
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py -v`
Expected: FAIL — `subscribe_frame()` takes no `since`; events not buffered/replayed; no `seq`.

- [ ] **Step 3: Protocol — add the fields**

In `server/ipc/protocol.py`, replace `event_frame` and `subscribe_frame`:

```python
def event_frame(stream: str, event: dict, seq: int) -> dict:
    return {"type": EVENT, "stream": stream, "event": event, "seq": seq}


def subscribe_frame(stream: str, since: int | None = None) -> dict:
    return {"type": SUBSCRIBE, "stream": stream, "since": since}
```

- [ ] **Step 4: Daemon — buffer, seq, replay**

In `server/ipc/daemon.py`, add `import os` and `from collections import deque` at the top. Inside `create_daemon_app`, after `app.state.subscribers = {}` add:

```python
    # Per-stream replay ring buffer: (seq, event). Closes the at-attach/reconnect gap —
    # events published before a subscriber connects are kept and replayed on subscribe.
    _maxlen = int(os.environ.get("CCSWITCH_IPC_REPLAY_BUFFER", "256"))
    app.state.buffers = {}   # stream -> deque[(seq, event)]
    app.state.seqs = {}      # stream -> last assigned seq (int)

    def head_seq(stream: str) -> int:
        return app.state.seqs.get(stream, -1)

    app.state.head_seq = head_seq
```

Replace `publish` with a seq-assigning, buffering version:

```python
    async def publish(stream: str, event: dict) -> None:
        seq = app.state.seqs.get(stream, -1) + 1
        app.state.seqs[stream] = seq
        buf = app.state.buffers.get(stream)
        if buf is None:
            buf = app.state.buffers[stream] = deque(maxlen=_maxlen)
        buf.append((seq, event))
        frame = proto.event_frame(stream, event, seq)
        for ws in list(app.state.subscribers.get(stream, ())):
            try:
                await ws.send_text(_dumps(frame))
            except Exception:  # noqa: BLE001 — drop a dead/slow connection, keep publishing
                app.state.subscribers.get(stream, set()).discard(ws)
```

In the `/events` WS handler, replace the SUBSCRIBE branch so it registers the WS and replays the missed tail to *that* WS:

```python
                if frame.get("type") == proto.SUBSCRIBE and stream:
                    app.state.subscribers.setdefault(stream, set()).add(ws)
                    mine.add(stream)
                    since = frame.get("since")
                    buf = app.state.buffers.get(stream)
                    if buf:
                        for seq, event in list(buf):
                            if since is None or seq > since:
                                try:
                                    await ws.send_text(_dumps(proto.event_frame(stream, event, seq)))
                                except Exception:  # noqa: BLE001 — peer gone mid-replay
                                    break
```

(Leave UNSUBSCRIBE and the `finally` cleanup unchanged.)

- [ ] **Step 5: Run, verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Confirm existing IPC/daemon tests still pass (the wire format changed)**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q -k "ipc or daemon or remote"`
Expected: any failures here are callers of `event_frame`/`subscribe_frame` with the old arity — they are fixed in Tasks 2-3. If a test outside Tasks 2-3's files fails purely on the new `seq` field in a delivered event, note it in the report (the client strips seq in Task 2). Do NOT edit Task 2/3 files here.

- [ ] **Step 7: Commit**

```bash
git add server/ipc/protocol.py server/ipc/daemon.py server/tests/test_ipc_replay.py
git commit -m "feat(ipc): per-stream replay ring buffer with seq + since (A3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Client seq tracking + strip + since on (re)subscribe

**Files:**
- Modify: `server/ipc/client.py` (`subscribe`, `_send_subscribe`, `_deliver`, `_reader`, `__init__`)
- Test: `server/tests/test_ipc_replay.py` (extend)

**Interfaces:**
- Consumes: daemon frames now carry `seq`; SUBSCRIBE accepts `since` (Task 1).
- Produces: `DaemonClient.subscribe(stream, since=None)` records a baseline; the client sends `since=self._last_seq.get(stream, baseline)` on every (re)subscribe; `_deliver` strips `seq`, tracks `_last_seq[stream]`, and enqueues the bare event dict.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_ipc_replay.py
from server.ipc.client import DaemonClient


@pytest.mark.asyncio
async def test_client_strips_seq_and_replays_gap():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        # Two events published BEFORE the client subscribes.
        await app.state.publish("s1", {"type": "a"})
        await app.state.publish("s1", {"type": "b"})
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1", since=-1)  # anchor before the first event
            got = []
            for _ in range(2):
                got.append(await asyncio.wait_for(stream.__anext__(), 2))
        finally:
            await client.aclose()
    # Bare event dicts (no seq leaked), in order — the pre-subscribe gap is replayed.
    assert got == [{"type": "a"}, {"type": "b"}]


@pytest.mark.asyncio
async def test_client_resubscribe_uses_last_seq():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1", since=-1)
            await client.events_ready("s1")
            await app.state.publish("s1", {"type": "a"})
            assert await asyncio.wait_for(stream.__anext__(), 2) == {"type": "a"}
            # last_seq now tracks seq 0.
            assert client._last_seq.get("s1") == 0
        finally:
            await client.aclose()
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py -k client -v`
Expected: FAIL — `subscribe` takes no `since`; events arrive with a `seq` key / `_last_seq` doesn't exist.

- [ ] **Step 3: Implement client changes**

In `server/ipc/client.py` `__init__`, add after `self._desired: set[str] = set()`:

```python
        self._last_seq: dict[str, int] = {}      # stream -> highest delivered seq
        self._baseline: dict[str, int | None] = {}  # stream -> initial `since` for first subscribe
```

Change `subscribe` to accept and store the baseline:

```python
    def subscribe(self, stream: str, since: int | None = None, maxsize: int = 1000):
        self._queues[stream] = asyncio.Queue(maxsize=maxsize)
        self._ready[stream] = asyncio.Event()
        self._desired.add(stream)
        self._baseline[stream] = since
        if not self._started:
            self._started = True
            self._reader_task = asyncio.create_task(self._reader())
        elif self._ws is not None:
            asyncio.create_task(self._send_subscribe(stream))
        return self._iter(stream)

    def _since_for(self, stream: str) -> int | None:
        # Reconnect → resume after the last delivered seq; first subscribe → the baseline.
        if stream in self._last_seq:
            return self._last_seq[stream]
        return self._baseline.get(stream)
```

Change `_send_subscribe` and the `_reader` resubscribe loop to send `since=self._since_for(stream)`:

```python
    async def _send_subscribe(self, stream: str) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
            self._ready[stream].set()
        except Exception:  # noqa: BLE001 — reader re-subscribes on its next connect
            pass
```

In `_reader`, replace the per-stream subscribe send:

```python
                    for stream in list(self._desired):
                        await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
                        self._ready[stream].set()
```

Change `_reader`'s EVENT dispatch and `_deliver` to carry seq. In `_reader`:

```python
                        if frame.get("type") == proto.EVENT:
                            self._deliver(frame["stream"], frame["event"], frame.get("seq"))
```

And `_deliver`:

```python
    def _deliver(self, stream: str, event: dict, seq: int | None = None) -> None:
        if seq is not None:
            self._last_seq[stream] = seq
        q = self._queues.get(stream)
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass
            log.warning("ipc: subscriber queue for %s full — dropped oldest", stream)
        q.put_nowait(event)
```

- [ ] **Step 4: Run, verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py -v`
Expected: PASS (all replay tests).

- [ ] **Step 5: Confirm IPC reconnect + remote suites pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_reconnect.py server/tests/test_remote_driver.py server/tests/test_remote_orchestrator.py server/tests/test_remote_global_bus.py -q`
Expected: PASS — events still arrive as bare dicts; seq is internal.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/client.py server/tests/test_ipc_replay.py
git commit -m "feat(ipc): client tracks last_seq, strips seq, resubscribes with since (A3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: RPC `head_seq` + remote facade anchors the first subscribe

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (`ensure_driver` response)
- Modify: `server/sessions/remote.py` (`RemoteDriver`, `RemoteOrchestrator.ensure_driver`)
- Test: `server/tests/test_remote_driver.py` (extend)

**Interfaces:**
- Consumes: `app.state.head_seq(stream)` (Task 1); `DaemonClient.subscribe(stream, since=...)` (Task 2).
- Produces: `ensure_driver` RPC returns `head_seq`; `RemoteDriver(..., head_seq=H)` subscribes with `since=H`; `RemoteOrchestrator.ensure_driver` threads `r["head_seq"]`.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_remote_driver.py
@pytest.mark.asyncio
async def test_remote_driver_subscribes_with_head_seq_anchor():
    from server.sessions.remote import RemoteOrchestrator, RemoteDriver

    captured = {}

    class _Client:
        async def call(self, method, **params):
            return {"found": True, "status": "idle", "queued": [], "head_seq": 7}

        async def _gen(self):
            if False:
                yield {}

        def subscribe(self, session_id, since=None):
            captured["since"] = since
            return self._gen()

    ro = RemoteOrchestrator(_Client())
    d = await ro.ensure_driver("s1")
    assert isinstance(d, RemoteDriver)
    # Kick the generator so events() calls subscribe().
    await d.events().__anext__() if False else None
    agen = d.events()
    try:
        await agen.__anext__()
    except StopAsyncIteration:
        pass
    assert captured["since"] == 7
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py::test_remote_driver_subscribes_with_head_seq_anchor -v`
Expected: FAIL — `ensure_driver` doesn't pass `head_seq`; `subscribe` called with `since=None` (or `RemoteDriver` has no head_seq).

- [ ] **Step 3: RPC — return head_seq**

In `server/ipc/orchestrator_rpc.py`, change the `ensure_driver` handler's found-return:

```python
        return {"found": True, "status": driver.current_status(),
                "queued": driver.queued(), "head_seq": app.state.head_seq(session_id)}
```

- [ ] **Step 4: Remote facade — thread the anchor**

In `server/sessions/remote.py`, give `RemoteDriver` a `head_seq` and use it in `events()`:

```python
    def __init__(self, client, session_id: str, status="idle", queued=None, head_seq=None):
        self._client = client
        self._session_id = session_id
        self._status = status
        self._queued = queued or []
        self._head_seq = head_seq

    def current_status(self) -> str:
        return self._status

    def queued(self) -> list:
        return self._queued

    async def events(self):
        async for wire in self._client.subscribe(self._session_id, since=self._head_seq):
            yield _WireEvent(wire)
```

And thread it in `RemoteOrchestrator.ensure_driver`:

```python
    async def ensure_driver(self, session_id: str, identity: str = "system") -> "RemoteDriver | None":
        r = await self._client.call("ensure_driver", session_id=session_id, identity=identity)
        if not r["found"]:
            return None
        return RemoteDriver(self._client, session_id, status=r["status"],
                            queued=r["queued"], head_seq=r.get("head_seq"))
```

- [ ] **Step 5: Run, verify pass + regression**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py server/tests/test_remote_orchestrator.py server/tests/test_orchestrator_rpc_events.py -q
```
Expected: PASS. (Fakes that return `ensure_driver` dicts without `head_seq` still work because `RemoteOrchestrator` uses `r.get("head_seq")` → None → first subscribe anchors at None = replay-all, which is the safe pre-A3 behavior for those unit fakes.)

- [ ] **Step 6: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/sessions/remote.py server/tests/test_remote_driver.py
git commit -m "feat(ipc): ensure_driver returns head_seq; RemoteDriver anchors first subscribe (A3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: End-to-end replay integration (gap closed, no dup, reconnect tail)

**Files:**
- Test: `server/tests/test_ipc_replay.py` (extend) — full edge↔daemon path over UDS.

**Interfaces:**
- Consumes: everything from Tasks 1-3. No production code changes.

- [ ] **Step 1: Write the end-to-end test**

```python
# append to server/tests/test_ipc_replay.py
from unittest import mock
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator


class _ManualDriver:
    """Driver whose events() yields whatever is pushed onto its queue."""
    def __init__(self):
        self.q = asyncio.Queue()

    def current_status(self): return "idle"
    def queued(self): return []

    async def events(self):
        while True:
            yield (await self.q.get())


class _Ev:
    def __init__(self, payload): self._p = payload
    def to_wire(self): return self._p


@pytest.mark.asyncio
async def test_end_to_end_gap_closed_and_anchored():
    from server.sessions.remote import RemoteOrchestrator
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    driver = _ManualDriver()
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        with mock.patch.object(orch, "ensure_driver",
                               new=mock.AsyncMock(return_value=driver)):
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                # Emit an event BEFORE the edge attaches → must be buffered, but it is
                # PRE-attach so the anchor (head_seq) means the edge does NOT see it
                # (it would be in the transcript). Push one pre-attach event:
                await driver.q.put(_Ev({"type": "pre"}))
                await asyncio.sleep(0.1)   # daemon pump buffers it (seq 0)
                d = await ro.ensure_driver("s1")   # head_seq captured = 0
                stream = d.events()
                # A post-attach event must arrive; the pre-attach one must NOT.
                await asyncio.sleep(0.1)
                await driver.q.put(_Ev({"type": "post"}))
                first = await asyncio.wait_for(stream.__anext__(), 2)
            finally:
                await ro.aclose()
    assert first.to_wire() == {"type": "post"}
```

NOTE: the daemon pump starts on `ensure_driver`, so the "pre" event is only buffered if the pump is already running — verify the ordering matches reality when implementing. If the pump cannot emit before `ensure_driver`, adapt the test to push the pre-event AFTER ensure_driver returns but BEFORE the subscribe completes (the genuine gap window) using a small controlled delay; the INVARIANT to assert is: an event in the ensure→subscribe window is delivered (gap closed), and the anchor prevents re-delivery of events at/below head_seq. Keep the assertion meaningful, not tautological.

- [ ] **Step 2: Run, verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py -v`
Expected: PASS.

- [ ] **Step 3: Full gate**

```bash
cd /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.claude/worktrees/feature+api-retry-backoff
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
"$PY" -m ruff check .
PYTHONPATH=$(pwd)/src "$PY" -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src "$PY" -m pytest server/tests/ -q -k "ipc or remote or daemon or replay"
```
Expected: ruff clean, mypy Success, tests pass.

- [ ] **Step 4: Commit**

```bash
git add server/tests/test_ipc_replay.py
git commit -m "test(ipc): end-to-end replay — gap closed, anchored at attach (A3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Plan Self-Review

**Spec coverage:** ring buffer + seq + replay-on-subscribe → Task 1; client seq tracking/strip/since → Task 2; `head_seq` + anchor threading → Task 3; end-to-end gap/anchor/reconnect → Tasks 1,2,4. ✓
**Placeholder scan:** complete code in every code step. The Task 4 NOTE is a verify-against-reality guard for the pump-timing of the integration test, with the invariant stated explicitly — not a logic placeholder. ✓
**Type/name consistency:** `head_seq` (daemon helper + RPC field + RemoteDriver kwarg) consistent; `since`/`seq` consistent across protocol/daemon/client; `_last_seq`/`_baseline`/`_since_for` consistent within client. Events reach `ws.py` as bare dicts (seq stripped in `_deliver`). ✓
