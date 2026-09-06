# Transcript Liveness Handshake Implementation Plan

> **Provenance (added on rescue, 2026-09-05 — #1300).** Written 2026-08-29; the worktree it
> lived in was evicted by the janitor before anything was committed, and it survived only in a
> janitor stash on rocky (`da92affb`, pinned there as `refs/rescue/issue-1300`). Rescued and
> executed under #1300. The "Global Constraints" below name that evicted worktree and are kept
> verbatim as the historical record — **use the current repo's paths and the four-step gate in
> `.agents/instructions/core.md`, not the paths in this file.**
>
> Before executing, the three root causes were re-verified against `origin/master` at `f700d96b`:
> H1 is still live (`ws.py`'s `_handle_attach` early-returns for a detached record), H2 is still
> live (`_clear_when_finished` drops the `live_tasks` entry with no re-subscribe), and H4 is
> still live (`client.ts` heals only on seq gaps). #954 (host-heartbeat liveness *pills*) and
> #1181 (frame *ordering* within a reattach) are orthogonal — neither makes a reattach happen.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An attached transcript viewer always converges to the true transcript — externally-triggered session activity (autopilot, queue drain, another device) reaches the viewer without navigating away/back, archiving, or restarting ccswitch.

**Architecture:** Two independent convergence mechanisms. (1) **Server push**: the WS handler tracks which sessions each socket is *attached* to (independent of whether a live forwarding task exists) and, when a lifecycle `status` event arrives for an attached session that has no live forwarding task on this socket, automatically re-runs the attach path (full replay + live stream) — this closes the "detached-session viewer subscribes to nothing" hole (`_handle_attach`'s early return for `rec.detached`) and the "forward task ended silently after completion-driver-release" hole. (2) **Client watermark probe**: `SessionSocket` tracks the session-list `revAt` it was last demonstrably synced to; when the cached row's `revAt` (kept fresh by the 5s poll and lifecycle patches — channels independent of the per-session stream) advances while no transcript frames have arrived, it self-heals via the existing detach+attach heal. This catches wedged edge↔daemon event readers that starve the stream entirely.

**Tech Stack:** Python FastAPI WS handler (`server/bridge/ws.py`), pytest with the existing `_Harness`/FakeSDK scaffolding; React/TS `SessionSocket` (`web/src/ws/client.ts`), vitest.

**Spec:** This plan's root-cause analysis (conversation, 2026-08-29). Symptoms: transcript (including the user's own prompts sent from other devices/paths) freezes until navigate-away-and-back / archive-unarchive / restart. Root causes: (H1) attach to a detached session replays and returns with no subscription — every session is detached after task completion (`orchestrator.py:1578` completion-driver-release) and after every daemon restart (rehydrate); (H2) live forward task ends silently when the driver is released mid-view; (H4) a wedged daemon event reader starves the stream and total silence never triggers the client's gap-heal (heal only fires on seq gaps inside frames that *arrive*, `client.ts:491`).

## Global Constraints

- Run everything from the worktree: `/home/tony/GitHub/.ccswitch/worktrees/ccswitch/4f7e6e9b`.
- Python env: `/home/tony/GitHub/ccswitch/.venv/bin/python` (worktree has no venv; network blocks `uv sync`). All touched Python is under `server/` — the editable-install trap (deploy-worktree `ccswitch.*`) does NOT apply, no `PYTHONPATH` needed.
- Server tests: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_URL -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/<file> -x -q` (unset ALL `CCSWITCH_SESSIOND_*` vars — module-level `create_app()` at import demands client certs otherwise).
- Web tests: first `ln -sfn /home/tony/GitHub/ccswitch/web/node_modules web/node_modules` if missing, then run in `web/`: `npx vitest run <file>`, and for the gate `npx tsc --noEmit` + `npx eslint src`.
- Full gate before PR (four server steps + web trio): `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/` — a green `server/tests/` alone is NOT the gate.
- Branch `ccswitch/transcript-liveness` off `origin/master` (`git fetch` first; NEVER off local `master`). Commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Match `ws.py` house style: heavily-commented WHY-comments on subtle concurrency, `log_event` journal entries for decisions.

---

### Task 1: Server — per-socket attached-set + lifecycle-triggered auto re-attach

**Files:**
- Modify: `server/bridge/ws.py` (socket-local state near line 268; `_handle_attach` ~line 720; `_handle_detach` ~line 887; `_forward_events` ~line 340; receive loop dispatch ~lines 925–964; `finally` cleanup ~line 1040)
- Test: `server/tests/test_ws_auto_reattach.py` (new)

**Interfaces:**
- Consumes: existing `_handle_attach(session_id, since_src_seq=None, since_epoch=None)`, `_handle_detach(session_id)`, `_ensure_live(session_id)`, `live_tasks: dict[str, asyncio.Task]`, `supervise(task, name=, on_failure=)` (from `server.obs.tasks`), `_Harness`/`_drain_until`/`_wait_until`/`_release_finished`/`_IDENTITY_HEADERS` (import from `server.tests.test_ws_prompt_after_release`).
- Produces: socket-local `attached: set[str]`, `attach_lock: asyncio.Lock`, module constant `_AUTO_REATTACH_MIN_INTERVAL_S = 2.0`, socket-local `_auto_reattach(session_id)` coroutine. No wire-protocol change (clients see an ordinary unsolicited replayReset→replay→replayComplete, which the reducer already double-buffers).

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_ws_auto_reattach.py`:

```python
# server/tests/test_ws_auto_reattach.py
"""An attached viewer must converge without client action when its session is driven
from elsewhere (autopilot, queue drain, another device).

Two holes closed here:
  H1 — attaching to a DETACHED session replays history and subscribes to nothing
       (_handle_attach early-returns on rec.detached), so external activity was
       invisible until the user navigated away and back;
  H2 — when completion releases the driver mid-view, this socket's forwarding task
       ends silently and a later external resume was equally invisible.

The fix: the socket tracks which sessions it is *attached* to (independent of
live_tasks) and re-runs the attach path when a lifecycle status event arrives for an
attached session with no live forwarding task on this socket.
"""
import json
import time

from fastapi.testclient import TestClient

from server.tests.test_ws_prompt_after_release import (
    _Harness, _IDENTITY_HEADERS, _drain_until, _release_finished, _wait_until,
)


def test_snapshot_viewer_converges_on_external_resume(tmp_path):
    """H1: attach AFTER the driver was released (a detached session — the everyday
    state after any completed task or daemon restart). Then resume + prompt the
    session purely server-side, as autopilot would. The viewer must receive a fresh
    replay WITHOUT sending a single frame."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-detached")
            sid = session.id
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            # Complete → completion-driver-release → the session is detached.
            client.portal.call(orch._persist_status, sid, "complete")
            assert sid not in orch._drivers
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")
                # Detached attach = snapshot only: no live forwarding task exists.

                # EXTERNAL drive: resume the driver and deliver a prompt with no
                # frame from this socket (the autopilot shape).
                client.portal.call(orch.ensure_driver, sid)
                client.portal.call(orch.send_prompt, sid, "external work")
                assert _wait_until(
                    lambda: orch._last_prompt.get(sid) == "external work")

                # The resume's status transition must auto re-attach this socket:
                # a fresh unsolicited replayReset→…→replayComplete arrives.
                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                resets = [e for e in events if e.get("type") == "replayReset"]
                assert resets, (
                    "an attached snapshot viewer must be re-attached when its "
                    f"session is driven externally; got {[e.get('type') for e in events]}")

            client.portal.call(orch.close_session, sid)


def test_released_mid_view_then_external_resume_converges(tmp_path):
    """H2: the viewer attached while LIVE; completion releases the driver (ending
    this socket's forwarding task silently); a later external resume must still
    reach the viewer via auto re-attach."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-live")
            sid = session.id
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")
                assert sid in orch._drivers

                client.portal.call(orch._persist_status, sid, "complete")
                assert _wait_until(lambda: _release_finished(orch))
                time.sleep(0.3)   # let the forwarding task observe _STREAM_DONE and end
                # Drain the frames the completion itself produced so the next drain
                # unambiguously observes the post-resume replay.
                _drain_until(ws, until_type="status", timeout=2.0)

                client.portal.call(orch.ensure_driver, sid)
                client.portal.call(orch.send_prompt, sid, "second wind")
                assert _wait_until(
                    lambda: orch._last_prompt.get(sid) == "second wind")

                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                assert any(e.get("type") == "replayReset" for e in events), (
                    "external resume after mid-view release must re-attach the viewer")

            client.portal.call(orch.close_session, sid)


def test_detach_stops_auto_reattach(tmp_path):
    """After the client detaches, external activity must NOT trigger a re-attach —
    detach removes the session from the socket's attached-set. Observed via a
    terminator attach: the only replayReset in the drain is the terminator's own."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-detach")
            sid = session.id
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            client.portal.call(orch._persist_status, sid, "complete")
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")
                ws.send_text(json.dumps({"type": "detach", "sessionId": sid}))

                client.portal.call(orch.ensure_driver, sid)
                client.portal.call(orch.send_prompt, sid, "unwatched work")
                assert _wait_until(
                    lambda: orch._last_prompt.get(sid) == "unwatched work")
                time.sleep(0.5)   # window in which a buggy auto re-attach would fire

                # Terminator: frames are handled in order, so this attach bounds the
                # drain (a regression that emits nothing must FAIL, not hang).
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                resets = [e for e in events if e.get("type") == "replayReset"]
                assert len(resets) == 1, (
                    f"detached socket must not auto re-attach; saw {len(resets)} "
                    "replayReset frames (expected only the terminator's)")

            client.portal.call(orch.close_session, sid)
```

Before running: check `ensure_driver`'s exact signature at `server/sessions/orchestrator.py:5175` (`grep -n "async def ensure_driver" server/sessions/orchestrator.py`) and adjust the `client.portal.call(orch.ensure_driver, sid)` calls if it takes extra required args (pass keyword defaults explicitly if needed). Also confirm `_last_prompt` exists on the orchestrator (it is used by `test_ws_prompt_after_release.py`); if the FakeSDK harness records prompts differently, mirror what that file asserts.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_URL -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_ws_auto_reattach.py -x -q 2>&1 | tee /tmp/ws-auto-reattach-red.log`

Expected: tests 1 and 2 FAIL (drain times out with no `replayReset` → assertion on `resets`); test 3 PASSES already (nothing auto re-attaches yet — it exists to pin the negative behavior once the feature lands). If tests 1/2 fail for a *different* reason (signature mismatch, harness issue), fix the test until the failure is the missing feature.

- [ ] **Step 3: Implement in `server/bridge/ws.py`**

3a. Module level (near `_BATCH_INTERVAL = 0.016`, ~line 61) add:

```python
# Minimum spacing between lifecycle-triggered auto re-attaches per session. Status
# transitions cluster (idle → connecting → in_progress inside one resume); one replay
# converges the viewer, the rest inside this window are redundant load.
_AUTO_REATTACH_MIN_INTERVAL_S = 2.0
```

Ensure `import time` exists at the top of the file (add if absent).

3b. Socket-local state, right after `live_tasks` (~line 270):

```python
        # Sessions this socket is VIEWING (attach seen, no detach yet) — independent
        # of live_tasks, which only holds sessions with a live forwarding task. The
        # difference is exactly the "snapshot viewer" set: a detached-session attach
        # replays history and starts no task, and a forwarding task ends on its own
        # when completion releases the driver (_clear_when_finished). Both leave the
        # client rendering a frozen transcript with no way to learn it went stale —
        # the nav-away-and-back bug. _forward_events watches lifecycle status events
        # against this set and re-runs the attach path to converge the viewer.
        attached: set[str] = set()
        # Serialises attach/detach/ensure-live across their two callers: the receive
        # loop and the lifecycle-triggered auto re-attach task. Without it, an auto
        # re-attach can interleave with a user attach/prompt and register two
        # forwarding tasks for one session (duplicate events, leaked subscriber).
        attach_lock = asyncio.Lock()
        # Auto re-attach bookkeeping: per-session in-flight guard + last-fire time.
        auto_reattach_pending: set[str] = set()
        auto_reattach_last: dict[str, float] = {}
```

3c. In `_handle_attach`, first line of the function body (before the `rebind =` line): `attached.add(session_id)`. In `_handle_detach`, before `await _teardown_live_task(session_id)`: `attached.discard(session_id)`.

3d. Add the auto re-attach coroutine next to `_handle_detach`:

```python
        async def _auto_reattach(session_id: str) -> None:
            """Re-run the attach path because a lifecycle status event showed this
            attached session changed while the socket had no live forwarding task
            for it (snapshot viewer). Re-checks its preconditions under attach_lock:
            the user may have detached, or a user attach/prompt may have raced ahead
            and already made the session live on this socket. No cursor is passed —
            the full-replay path is the safe converger, and the client's reducer
            double-buffers replayReset so the view doesn't flash."""
            try:
                async with attach_lock:
                    if session_id not in attached or session_id in live_tasks:
                        return
                    log_event(log, logging.INFO, "ws.auto_reattach",
                              session_id=session_id)
                    await _handle_attach(session_id)
            finally:
                auto_reattach_pending.discard(session_id)
```

(`log_event` and `logging` are already imported/used in this file — see the `ws.attach.decision` call sites.)

3e. In `_forward_events` (~line 340), after `await _send(redact_event(event))` add:

```python
                    # Liveness handshake: a status event for a session this socket is
                    # viewing WITHOUT a live forwarding task means the viewer is a
                    # snapshot (detached-session attach, or the task ended when the
                    # driver was released) and just went stale. Re-run the attach path
                    # to converge it. Spawned, not awaited: _handle_attach does disk
                    # IO / RPCs and this forwarder must keep lifecycle frames flowing.
                    sid = event.get("sessionId")
                    if (event.get("type") == "status" and sid
                            and sid in attached
                            and sid not in live_tasks
                            and sid not in auto_reattach_pending):
                        now = time.monotonic()
                        if (now - auto_reattach_last.get(sid, 0.0)
                                >= _AUTO_REATTACH_MIN_INTERVAL_S):
                            auto_reattach_last[sid] = now
                            auto_reattach_pending.add(sid)
                            supervise(
                                asyncio.create_task(
                                    _auto_reattach(sid),
                                    name=f"ws-auto-reattach-{sid}"),
                                name=f"ws-auto-reattach-{sid}")
                            # No on_failure: a failed auto re-attach leaves the viewer
                            # a snapshot (status quo ante), which the next status event
                            # or the client's own liveness probe retries — killing the
                            # socket over it would be strictly worse.
```

Note `_forward_events` is defined BEFORE `_handle_attach` in the file; it only *calls* `_auto_reattach` at runtime (closure late binding), so definition order is fine — do NOT reorder the file.

3f. Serialize the competing mutators of `live_tasks` under `attach_lock` in the receive loop and cleanup:
- `elif msg_type == "attach":` branch: wrap the `await _handle_attach(...)` call: `async with attach_lock: await _handle_attach(session_id, since_src_seq, since_epoch)`.
- `elif msg_type == "detach":` branch: `async with attach_lock: await _handle_detach(session_id)`.
- `elif msg_type == "prompt":` branch: wrap ONLY the `_ensure_live` call (the mutator): `async with attach_lock: live_ok = await _ensure_live(session_id)` then `if live_ok:` for the rest (do not hold the lock across `send_prompt` — it can block on a resuming turn and would starve auto re-attaches for other sessions).
- `finally:` cleanup loop: `for sid in list(live_tasks.keys()): async with attach_lock: await _handle_detach(sid)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_URL -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_ws_auto_reattach.py -x -q`
Expected: 3 passed.

- [ ] **Step 5: Run the neighboring WS suites (regression sweep)**

Run: same env, `-m pytest server/tests/test_ws.py server/tests/test_ws_prompt_after_release.py server/tests/test_ws_gap_healing.py server/tests/test_ws_frame_order.py server/tests/test_ws_multiclient_seams.py server/tests/test_ws_cursor_catchup.py server/tests/test_ws_inprogress_replay.py server/tests/test_ws_reattach_turn_resupply.py -q`
Expected: all pass. The attach_lock must not deadlock any existing flow (watch for timeouts).

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws_auto_reattach.py
git commit -m "$(cat <<'EOF'
fix(ws): auto re-attach snapshot viewers when their session is driven externally

An attach to a detached session replays history and subscribes to nothing, and a
live forwarding task ends silently when completion releases the driver — either
way the viewer renders a frozen transcript while autopilot / the queue / another
device drives the session, until a manual navigate-away-and-back forced a
re-attach. Track the socket's attached-set independently of live_tasks and re-run
the attach path when a lifecycle status event lands for an attached session with
no live forwarding task.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Web — rev-watermark liveness probe in SessionSocket

**Files:**
- Modify: `web/src/ws/client.ts`
- Test: `web/src/ws/client.liveness.test.ts` (new)

**Interfaces:**
- Consumes: `revIsNewer(candidate, existing)` from `@/session/reconcile`; existing `_healActiveSession()` (cooldown + loop detection stay authoritative); `QK.sessions` cache rows (`Session { id, status, revAt }`); `clientLogger`.
- Produces: `SessionSocket` privates `_syncedRevAt: string | null`, `_lastSessionFrameAt: number`, `_livenessTimer`, method `_livenessCheck(): void` (tests call it directly via cast, like `_handleInbound`); constants `LIVENESS_CHECK_MS = 5_000`, `LIVENESS_STALE_MS = 15_000` (module-level, exported for tests).

- [ ] **Step 1: Write the failing test**

Create `web/src/ws/client.liveness.test.ts`:

```typescript
// web/src/ws/client.liveness.test.ts
// The rev-watermark liveness probe: when the cached session row's revAt (kept fresh
// by the 5s poll / lifecycle patches — channels independent of the transcript
// stream) advances while NO transcript frames are arriving, the transcript stream
// is demonstrably stale (wedged reader, dead forwarding task) and the socket must
// self-heal via the existing detach+attach heal. Total silence produces no seq
// gaps, so the gap-heal alone can never catch this.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

const T1 = '2026-08-29T12:00:00+00:00'
const T2 = '2026-08-29T12:00:30+00:00'

type Probe = {
  setQueryClient: (q: QueryClient) => void
  _livenessCheck: () => void
  _activeSessionId: string | null
  _connectionState: string
  _syncedRevAt: string | null
  _lastSessionFrameAt: number
  _send: (m: object) => boolean
}

function makeSocket(row: Partial<Session>) {
  const qc = new QueryClient()
  qc.setQueryData<Session[]>(QK.sessions, [
    { id: 's1', status: 'in_progress', accountName: 'a', ...row } as Session,
  ])
  const s = new SessionSocket('/ws') as unknown as Probe
  s.setQueryClient(qc)
  s._activeSessionId = 's1'
  s._connectionState = 'connected'
  const sent: object[] = []
  s._send = (m: object) => { sent.push(m); return true }
  return { s, sent }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('SessionSocket rev-watermark liveness probe', () => {
  it('heals when revAt advanced and the stream has been silent past the threshold', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent.map((m) => (m as { type: string }).type)).toEqual(['detach', 'attach'])
  })

  it('does not heal when revAt has not advanced (silent-but-static session)', () => {
    const { s, sent } = makeSocket({ revAt: T1 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent).toEqual([])
  })

  it('does not heal while frames are arriving, even with a newer revAt', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 1_000
    s._livenessCheck()
    expect(sent).toEqual([])
  })

  it('advances the watermark on heal so a static rev cannot re-trigger a loop', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._livenessCheck()
    expect(sent.length).toBe(2)          // one heal fired
    expect(s._syncedRevAt).toBe(T2)      // watermark advanced
    s._livenessCheck()                   // same rev again → no second heal
    expect(sent.length).toBe(2)
  })

  it('is inert with no active session or a disconnected socket', () => {
    const { s, sent } = makeSocket({ revAt: T2 })
    s._syncedRevAt = T1
    s._lastSessionFrameAt = Date.now() - 60_000
    s._activeSessionId = null
    s._livenessCheck()
    s._activeSessionId = 's1'
    s._connectionState = 'reconnecting'
    s._livenessCheck()
    expect(sent).toEqual([])
  })
})
```

Note: `_healActiveSession` has a 2s `HEAL_COOLDOWN_MS` keyed on `Date.now()`; with fake timers each test starts at the same epoch — set `_lastSessionFrameAt` relative to `Date.now()` as above and, if the cooldown swallows the first heal in a test, advance timers with `vi.advanceTimersByTime(3000)` before calling `_livenessCheck()`.

- [ ] **Step 2: Run to verify failure**

Run (in `web/`): `npx vitest run src/ws/client.liveness.test.ts`
Expected: FAIL — `_livenessCheck` is not a function.

- [ ] **Step 3: Implement in `web/src/ws/client.ts`**

3a. Module constants next to `CONNECTION_STABLE_MS`:

```typescript
// Rev-watermark liveness probe. The 5s sessions poll and lifecycle status patches
// keep the cached row's revAt fresh over channels independent of the per-session
// transcript stream; if that rev advances while no transcript frames arrive, the
// stream is stale (wedged daemon reader, dead forwarding task) — a state the seq
// gap-heal can never see, because total silence contains no gaps.
export const LIVENESS_CHECK_MS = 5_000
export const LIVENESS_STALE_MS = 15_000
```

3b. Instance state next to `_gap`:

```typescript
  // revAt the transcript stream was last demonstrably synced to: whenever session
  // frames arrive we adopt the cached row's revAt as "seen". Compared against the
  // cache by _livenessCheck.
  private _syncedRevAt: string | null = null
  private _lastSessionFrameAt = 0
  private _livenessTimer: ReturnType<typeof setInterval> | null = null
```

3c. Start/stop the timer: in `connect()`, after `this.ws = ws`, add (idempotent):

```typescript
    if (this._livenessTimer === null) {
      this._livenessTimer = setInterval(() => this._livenessCheck(), LIVENESS_CHECK_MS)
    }
```

In `destroy()`, alongside clearing `_reconnectTimer`:

```typescript
    if (this._livenessTimer !== null) {
      clearInterval(this._livenessTimer)
      this._livenessTimer = null
    }
```

3d. Watermark updates. In `_handleInbound`'s `case 'batch':`, after the `for (const e of dispatch) this._emit('sessionEvent', e)` loop:

```typescript
        if (dispatch.length > 0) {
          // Frames for the active session arrived → the stream is live; adopt the
          // cache's current rev as synced so the liveness probe stays quiet.
          this._lastSessionFrameAt = Date.now()
          this._syncedRevAt = this._cachedRevAt(this._activeSessionId) ?? this._syncedRevAt
        }
```

In the public `attach(sessionId)`, after `this._activeSessionId = sessionId`:

```typescript
    // Fresh view: baseline the probe so pre-attach staleness can't instantly fire.
    this._lastSessionFrameAt = Date.now()
    this._syncedRevAt = this._cachedRevAt(sessionId)
```

3e. The probe + cache reader (private methods, next to `_healActiveSession`):

```typescript
  private _cachedRevAt(sessionId: string | null): string | null {
    if (!sessionId || !this._queryClient) return null
    const rows = this._queryClient.getQueryData<Session[]>(QK.sessions)
    return rows?.find((s) => s.id === sessionId)?.revAt ?? null
  }

  /** Rev-watermark liveness probe (see LIVENESS_CHECK_MS above). Runs on an
   *  interval; also directly callable by tests. Heals at most once per rev
   *  advance: the watermark is bumped BEFORE the heal, so a heal whose replay
   *  produces no frames cannot re-trigger on the same rev. */
  private _livenessCheck(): void {
    const sid = this._activeSessionId
    if (!sid || this._connectionState !== 'connected') return
    const revAt = this._cachedRevAt(sid)
    if (!revIsNewer(revAt, this._syncedRevAt)) return
    if (Date.now() - this._lastSessionFrameAt < LIVENESS_STALE_MS) return
    clientLogger.warn('transcript.liveness.stale — rev advanced with a silent stream, healing', {
      sessionId: sid,
    })
    this._syncedRevAt = revAt
    this._healActiveSession()
  }
```

Add `revIsNewer` to the existing import from `@/session/reconcile` (which already provides `shouldApplyRev`), and `import type { Session } from '@/api/types'` is already present.

- [ ] **Step 4: Run to verify pass**

Run (in `web/`): `npx vitest run src/ws/client.liveness.test.ts`
Expected: 5 passed. Then the neighbors: `npx vitest run src/ws` — all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.liveness.test.ts
git commit -m "$(cat <<'EOF'
fix(web): heal the transcript when its rev advances over a silent stream

The gap-heal only fires on seq holes inside frames that arrive; a wedged daemon
reader or dead forwarding task delivers total silence, which contains no gaps.
Probe the cached session row's revAt (kept fresh by the 5s poll and lifecycle
patches, channels independent of the transcript stream) against the rev the
stream last demonstrably delivered, and self-heal via detach+attach when it
advances while the stream is silent.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Web hygiene — session-scoped error frames + observable malformed frames

**Files:**
- Modify: `web/src/session/useSessionRuntime.ts` (~lines 137–139, the `onError` callback)
- Modify: `web/src/ws/client.ts` (the `ws.onmessage` handler, ~lines 152–159)
- Test: extend `web/src/session/useSessionRuntime.test.ts`; new assertions in `web/src/ws/client.liveness.test.ts` are NOT needed — add a small `_onRawMessage` test to `web/src/ws/client.test.ts`

**Interfaces:**
- Consumes: `WsErrorMsg { sessionId?: string }`; existing reducer `error` action; `clientLogger`.
- Produces: `SessionSocket._onRawMessage(data: string): void` — the extracted body of `ws.onmessage`, directly testable.

- [ ] **Step 1: Write the failing tests**

In `web/src/session/useSessionRuntime.test.ts`, find how existing tests drive the hook (they render it and emit socket events; mirror the file's own harness exactly), and add:

```typescript
  it("ignores an error frame addressed to a different session", () => {
    // Using this file's existing harness: mount the runtime for session 's1',
    // then emit an error event tagged for 's2' and assert state.error is null;
    // then emit one tagged 's1' (and one untagged) and assert each IS applied.
  })
```

Replace the comment body with real code following the file's established pattern (the file already emits `error` events in at least one test — copy that emission mechanism). The three assertions that matter: mismatched `sessionId` → `state.error` unchanged; matching `sessionId` → applied; missing `sessionId` (legacy frames) → applied.

In `web/src/ws/client.test.ts` add:

```typescript
  it('logs malformed inbound frames instead of dropping them silently', () => {
    const s = new SessionSocket('/ws') as unknown as { _onRawMessage: (d: string) => void }
    const warn = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    s._onRawMessage('{not json')
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('ws.message.parse_error'),
      expect.objectContaining({ dataLen: 9 }),
    )
    warn.mockRestore()
  })
```

(Import `clientLogger` from `@/obs/logger` and `vi` from vitest in that file if not already imported.)

- [ ] **Step 2: Run to verify failures**

Run (in `web/`): `npx vitest run src/session/useSessionRuntime.test.ts src/ws/client.test.ts`
Expected: the new tests FAIL (`_onRawMessage` missing; cross-session error currently applied).

- [ ] **Step 3: Implement**

`web/src/ws/client.ts` — replace the `ws.onmessage` body with a call to the extracted method, and add the method:

```typescript
    ws.onmessage = (event) => this._onRawMessage(event.data as string)
```

```typescript
  /** Parse + route one raw inbound frame. Extracted from ws.onmessage so tests can
   *  drive it directly. A malformed frame is dropped (nothing sane to do with it)
   *  but LOGGED — a server encoding bug or truncated frame that vanished silently
   *  here surfaced days later as an unexplained "chat got stuck" report. */
  private _onRawMessage(data: string): void {
    try {
      const msg = JSON.parse(data) as WsInboundMsg
      this._handleInbound(msg)
    } catch (e) {
      clientLogger.warn('ws.message.parse_error', { dataLen: data.length, error: String(e) })
    }
  }
```

`web/src/session/useSessionRuntime.ts` — in the `onError` callback, before the dispatch:

```typescript
    const onError = (msg: WsErrorMsg) => {
      // Session-scoped errors must not land on whichever session happens to be
      // open: an async failure from another session (its frame carries that
      // session's id) would otherwise paint this view's error banner. Untagged
      // frames (connection-level, legacy) still apply.
      if (msg.sessionId && msg.sessionId !== sessionId) return
      dispatch({ type: 'error', code: msg.code, message: msg.message, resetsAt: msg.resetsAt ?? null })
    }
```

- [ ] **Step 4: Run to verify pass**

Run (in `web/`): `npx vitest run src/session/useSessionRuntime.test.ts src/ws/client.test.ts src/ws/client.liveness.test.ts`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/ws/client.ts web/src/ws/client.test.ts web/src/session/useSessionRuntime.ts web/src/session/useSessionRuntime.test.ts
git commit -m "$(cat <<'EOF'
fix(web): scope error frames to their session; log malformed inbound frames

An error frame tagged with another session's id painted the open session's error
banner, and malformed frames disappeared in a bare catch — both invisible in
client_logs. Filter error dispatch by sessionId (untagged frames still apply)
and log parse failures with the frame length.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Full quality gate

**Files:** none modified (verification only; fix anything red).

- [ ] **Step 1: Server-side gate (all four steps)**

From the worktree root, with the Task-1 env prefix (`env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_URL -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1`), venv = `/home/tony/GitHub/ccswitch/.venv/bin`:

```bash
<venv>/ruff check src/ server/
<venv>/python -m mypy server/ --ignore-missing-imports
<venv>/python -m pytest tests/ -q          | tee /tmp/gate-core.log
<venv>/python -m pytest server/tests/ -q   | tee /tmp/gate-server.log
```

Expected: all green. mypy on `server/bridge/ws.py` is the step most likely to catch a typing slip in the new socket-local state.

- [ ] **Step 2: Web gate**

In `web/`: `npx vitest run 2>&1 | tail -5`, `npx tsc --noEmit`, `npx eslint src`.
Expected: all green.

- [ ] **Step 3: Commit any gate fixes**

Only if steps 1–2 required changes; message `fix(gate): <what>` with the standard trailer.
