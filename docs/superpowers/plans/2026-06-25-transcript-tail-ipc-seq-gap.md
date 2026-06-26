# Transcript tail lost across daemon restart (IPC seq-reset gap) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Reconstructed after worktree loss.** The original execution reached Task 4 with an open review finding; this plan bakes both review findings into Task 4 (the `replayComplete` re-assert interaction and the N≥3 duplicate-text dedup) so a fresh run is correct.

**Goal:** Stop the live transcript silently losing its tail across a daemon restart (deploy), and self-heal any residual gap from the complete on-disk transcript.

**Architecture:** Remote mode = web edge ⇄ session daemon over a Unix-socket `/events` WS. The daemon stamps per-process monotonic `seq`s; a restart resets them to 0. The edge resubscribes with a stale `since=_last_seq`, so the daemon's `seq > since` replay filter drops the restart-window events. Fix in three independent layers: (1) edge detects a new daemon epoch and resubscribes from scratch; (2) edge re-replays the on-disk transcript to attached browsers on reconnect; (3) the frontend reducer applies a replay without shrinking or duplicating already-shown turns.

**Tech Stack:** Python (FastAPI/uvicorn, asyncio, websockets, httpx, pytest) backend; React+TS reducer with vitest frontend.

## Global Constraints

- Backend lint gates run first: `ruff`, `mypy`, then `pytest server/tests/`.
- Frontend gates (run in `web/`): `vitest run`, `tsc -b` (NOT `tsc --noEmit`), `eslint` (max-warnings 0).
- Worktree has no built env: run backend tests via the prebuilt env, e.g. `PYTHONPATH="$PWD:$PWD/src" /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest …`. For web, ensure `web/node_modules` (`cd web && rm -f node_modules && npm ci`).
- Wire-format note: `/info` gains an additive `epoch` field; backward-compatible, so `PROTOCOL_VERSION` is NOT bumped.
- The daemon epoch must be **per daemon-app instance** (`PROC_EPOCH` + a uuid suffix captured at `create_daemon_app()`), not the bare module-level `PROC_EPOCH` — two daemon apps in one test process otherwise share an epoch and restart-detection never fires.

---

### Task 1: Failing integration test — daemon-restart seq gap

Promote the verified repro into the suite. It MUST fail on current code (restart-window events lost) and pass after Task 2.

**Files:** Test: `server/tests/test_ipc_daemon_restart_gap.py` (create)

- [ ] **Step 1: Write the failing test**

```python
import asyncio, contextlib, os, tempfile
import pytest, uvicorn
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app


async def _serve(app, sock):
    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


async def _stop(server, task):
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_restart_window_events_survive_daemon_restart():
    tmp = tempfile.mkdtemp(prefix="ccsd-gap-")
    sock = os.path.join(tmp, "d.sock")
    received: list[dict] = []
    client = DaemonClient(sock)
    drain_task = srv_b = task_b = None

    app_a = create_daemon_app()
    srv_a, task_a = await _serve(app_a, sock)
    try:
        stream = client.subscribe("s1")
        await client.events_ready("s1")

        async def drain():
            async for ev in stream:
                if ev.get("type") != "_ipc_reconnect":   # ignore Task-3 sentinel
                    received.append(ev)
        drain_task = asyncio.create_task(drain())

        for i in range(6):
            await app_a.state.publish("s1", {"type": "textDelta", "i": i})
        await asyncio.sleep(0.3)
        assert sorted(e["i"] for e in received) == [0, 1, 2, 3, 4, 5]

        await _stop(srv_a, task_a)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        app_b = create_daemon_app()
        for i in (100, 101, 102):
            await app_b.state.publish("s1", {"type": "textDelta", "i": i})
        srv_b, task_b = await _serve(app_b, sock)
        await asyncio.sleep(2.5)

        got = {e["i"] for e in received}
        assert {100, 101, 102} <= got, f"restart-window events lost: {sorted(got)}"
    finally:
        if drain_task is not None:
            drain_task.cancel()
        if srv_b is not None:
            with contextlib.suppress(Exception):
                await _stop(srv_b, task_b)
        await client.aclose()
        with contextlib.suppress(Exception):
            os.unlink(sock)
            os.rmdir(tmp)
```

- [ ] **Step 2: Run — expect FAIL** on `{100,101,102} <= got` (only `[0..5]` present). Command: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_daemon_restart_gap.py -v`
- [ ] **Step 3: Commit** `test(ipc): failing repro for daemon-restart seq-reset transcript gap`

---

### Task 2: Component 1 — epoch-aware resubscribe (root-cause fix)

**Files:** Modify `server/ipc/daemon.py` (the `/info` handler + a per-instance `_app_epoch` captured in `create_daemon_app`); `server/ipc/client.py` (`__init__` add `_epoch`; new `_reconcile_epoch`; `_reader` calls it before resubscribe). Test: the Task-1 repro (must go GREEN) + a unit test.

- [ ] **Step 1: Add unit test** to `test_ipc_daemon_restart_gap.py`:

```python
@pytest.mark.asyncio
async def test_reconcile_epoch_resets_watermarks_on_change():
    client = DaemonClient("/nonexistent.sock")
    client._last_seq["s1"] = 42
    client._baseline["s1"] = 42

    async def info_same(): return {"epoch": "E1"}
    client.info = info_same                    # type: ignore[assignment]
    await client._reconcile_epoch()            # first sight → record, no reset
    assert client._last_seq.get("s1") == 42 and client._epoch == "E1"

    async def info_new(): return {"epoch": "E2"}
    client.info = info_new                      # type: ignore[assignment]
    await client._reconcile_epoch()            # changed → reset
    assert "s1" not in client._last_seq
    assert client._baseline["s1"] is None and client._epoch == "E2"
    await client.aclose()
```

- [ ] **Step 2: Run — expect FAIL** (`_reconcile_epoch` missing).
- [ ] **Step 3: Daemon — per-instance epoch + `/info`.** In `create_daemon_app()` capture `_app_epoch = f"{PROC_EPOCH}-{uuid.uuid4().hex[:8]}"` (import `uuid`; `from server.obs.context import PROC_EPOCH`). The `/info` handler returns `{"sha": running_git_sha(), "protocol": proto.PROTOCOL_VERSION, "epoch": _app_epoch}`.
- [ ] **Step 4: Client.** Add `self._epoch: str | None = None` to `__init__`. Add:

```python
    async def _reconcile_epoch(self) -> None:
        """Drop stale per-stream watermarks when the daemon process (epoch) changes.
        Best-effort: if /info is unreachable, keep watermarks and retry next loop."""
        try:
            epoch = (await self.info()).get("epoch")
        except Exception:  # noqa: BLE001
            return
        if epoch is None:
            return
        if self._epoch is not None and epoch != self._epoch:
            self._last_seq.clear()
            for stream in self._baseline:
                self._baseline[stream] = None
        self._epoch = epoch
```

In `_reader`, immediately after `self._ws = ws` / `backoff = 0.05` and BEFORE the resubscribe loop, add `await self._reconcile_epoch()`.

- [ ] **Step 5: Run** the unit test + Task-1 repro — both PASS.
- [ ] **Step 6: Regression + lint:** `pytest server/tests/ -k "ipc or remote" -q`; `ruff check server/ipc && mypy server/ipc`.
- [ ] **Step 7: Commit** `fix(ipc): reset edge watermarks on daemon epoch change (seq-reset gap)`

---

### Task 3: Component 2 — disk re-replay on reconnect (backfill)

**Files:** Modify `server/ipc/client.py` (`_ever_connected` flag; emit `{"type":"_ipc_reconnect"}` into each non-`_global` session stream queue AFTER resubscribe, only on re-connect; `import contextlib` if needed). Modify `server/bridge/ws.py` (`_forward_live` gains `on_reconnect` param, swallows the sentinel + calls it; `_start_live_stream` passes a `_replay_and_complete` closure). Test: `server/tests/test_ws_reconnect_rereplay.py` + a client sentinel test.

- [ ] **Step 1: ws test** — `_forward_live` with a fake driver yielding `[textDelta, _ipc_reconnect, textDelta]` forwards only the two textDeltas and calls `on_reconnect(session_id)` once. Fail (signature is 3-arg).
- [ ] **Step 2: `_forward_live`** — new signature `_forward_live(driver, out_q, session_id, on_reconnect=None)`; in the loop, `wire = e.to_wire(); if wire.get("type") == "_ipc_reconnect": (await on_reconnect(session_id) if on_reconnect else None); continue; out_q.put_nowait(wire)`.
- [ ] **Step 3: `_start_live_stream`** — define `async def _on_reconnect(sid): await _replay_and_complete(sid)` and pass it as the 4th arg to `_forward_live`.
- [ ] **Step 4: client sentinel** — add `self._ever_connected = False`; in `_reader`, after the resubscribe loop, `if self._ever_connected: for stream in list(self._desired): if stream != "_global": q = self._queues.get(stream); q and (with contextlib.suppress(asyncio.QueueFull): q.put_nowait({"type":"_ipc_reconnect"}))`; then `self._ever_connected = True`.
- [ ] **Step 5: client sentinel test** — first connect emits no sentinel; after a daemon restart on the same socket, a `_ipc_reconnect` arrives. Task-1 repro still GREEN (its drain filters the sentinel).
- [ ] **Step 6: lint** `ruff check server/ipc server/bridge && mypy server/ipc server/bridge`. **Commit** `feat(ipc): re-replay disk transcript to attached browsers on daemon reconnect`.

---

### Task 4: Component 3 — non-shrink replay reconcile (frontend invariant)

**Files:** Modify `web/src/session/reducer.ts` (`TranscriptState` + `initialTranscript` add `replayTouched: Set<string>`; `replayReset` non-destructive; `turnStart` clear-on-first-touch during replay; `user` content-reconcile during replay; **`replayComplete` re-assert dedup**). Test: `web/src/session/reducer.test.ts` + `web/src/session/reducer.reassert.test.ts`.

> **Two review findings baked in (do not skip):**
> 1. **`replayComplete` re-assert interaction.** Because `replayReset` no longer wipes turns, a surviving live-echo user turn can be duplicated by a pending prompt of the same text under a different id. The re-assert loop must re-key a surviving live turn (matched by text, not in `replayTouched`) to the pending id instead of appending; disk-replayed turns (in `replayTouched`) must NOT be replaced.
> 2. **N≥3 duplicate-text dedup.** Each surviving live turn must be consumed by **at most one** pending entry. Track used turn indices across the loop AND add a turn the loop **appends** to that used set, so a later iteration cannot re-key an earlier-appended turn. Regression: 3 same-text pendings + 1 surviving live echo + empty disk replay → 3 distinct turns.

- [ ] **Step 1: Reducer tests** (`reducer.test.ts`): (a) a replay window covering only `win1` over existing `[old1, win1]` keeps `old1` and replaces `win1`'s blocks wholesale (text == `WIN-disk`, not concatenated); (b) a `user` turn under `live-uuid` then a re-replay under `jsonl-uuid` of the same text yields ONE user turn. Plus in `reducer.reassert.test.ts`: (c) live-echo present + disk empty + TWO same-text pendings → two distinct turns; (d) the N≥3 case → three distinct turns. Run — expect FAIL.
- [ ] **Step 2: State** — add `replayTouched: Set<string>` to `TranscriptState` and `new Set<string>()` to `initialTranscript`.
- [ ] **Step 3: `replayReset`** — stop emptying `turns`; set `replaying: true, hadReset: true, replayTouched: new Set<string>(), openBlockId: null, subAgents: {}, earliestTurn: 0, totalTurns: 0, loadingOlder: false`. (Keep clearing `subAgents`; they re-emit fully in replay.)
- [ ] **Step 4: `turnStart`** — at the top, when `state.replaying && !state.replayTouched.has(ev.turnId)`: clone `replayTouched`, add `turnId`; clear that turn's blocks (or append an empty assistant turn if absent) via `replaceTurn`; return with the new `replayTouched`. (So the first replay touch rebuilds the turn wholesale; subsequent deltas refill.)
- [ ] **Step 5: `user`** — during replay, before the generic id path and after `pendingSent` consumption: if no turn matches `turnId` by id, find an existing user turn with matching text not in `replayTouched`; if found, re-key it to `turnId` (mark touched) instead of appending.
- [ ] **Step 6: `replayComplete`** — in the pending re-assert loop, maintain a `usedIndices: Set<number>`; for each pending entry, search for a surviving live user turn (matching text, index not used, id not in `replayTouched`) — if found re-key it and add its index to `usedIndices`; else APPEND a new turn AND add the new turn's index to `usedIndices`. This closes both findings.
- [ ] **Step 7: Gates** — from `web/` (after `npm ci`): full `npx vitest run` (new cases + all existing, incl. follow-up re-assert / `/clear` / compaction), `npx tsc -b --force`, `npx eslint src/session/reducer.ts src/session/reducer.test.ts src/session/reducer.reassert.test.ts`.
- [ ] **Step 8: Commit** `fix(web): reconcile replay over live state without shrinking or duplicating`

---

## Self-Review

Spec coverage: repro → Task 1; Component 1 → Task 2 (per-instance epoch); Component 2 → Task 3; Component 3 → Task 4 (incl. both review findings). Types: `_reconcile_epoch`/`_epoch`/`_ever_connected`/`_ipc_reconnect`/`_forward_live(on_reconnect)`/`replayTouched: Set<string>` defined where introduced and consumed consistently. Risk: Task 4 changes `replayReset` from wipe to reconcile (highest blast radius) — run the FULL vitest before committing.
