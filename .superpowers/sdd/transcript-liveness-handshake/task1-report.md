# Task 1 report — server: per-socket attached-set + lifecycle-triggered auto re-attach

**Files changed**
- `server/bridge/ws.py` (modified)
- `server/tests/test_ws_auto_reattach.py` (new)

Nothing else. The web files that moved in this worktree during the task belong to the
concurrent Task 2/3 agent (commits `30d54e9b`, `7a5110cd`), not to this change.

## What landed (plan steps 3a–3f, re-applied onto current master's `ws.py`)

| Step | Site in today's file |
|---|---|
| 3a | `_AUTO_REATTACH_MIN_INTERVAL_S = 2.0` next to `_BATCH_INTERVAL`; `import time` already present |
| 3b | `attached` / `attach_lock` / `auto_reattach_pending` / `auto_reattach_last` right after `live_tasks` |
| 3c | `attached.add(...)` first statement of `_handle_attach`; `attached.discard(...)` first statement of `_handle_detach` (today's body is a single `await _teardown_live_task(...)`, nothing dropped) |
| 3d | `_auto_reattach()` immediately after `_handle_detach` |
| 3e | trigger block in `_forward_events` after `await _send(redact_event(event))` |
| 3f | lock wrapped around the four `live_tasks` mutator call sites (below) |

WHY-comments are the rescued author's wording verbatim, with one factual adaptation: the
`attach_lock` comment says the two callers are "the per-session work chains and the
lifecycle-triggered auto re-attach task" — since #1180 the receive loop no longer calls these
handlers itself, it defers them to `_defer(...)`.

## Which receive-loop call sites were wrapped, and why

Today's file has two `if msg_type == "attach":` occurrences and two `_ensure_live` calls. Only
one of each pair is a real mutator:

- **`_refuse_saturated` (~line 1278, `if msg_type == "attach"`)** — NOT wrapped. This is the
  chain-saturated refusal path; it only emits an error frame + `_error_complete`. It never
  touches `live_tasks`.
- **The receive loop's `if msg_type == "prompt"` (~line 1512)** — NOT wrapped. It only sends the
  `promptAck` receipt frame and reads `session_id in live_tasks`; taking a lock there would put
  the "I heard you" ack behind whatever else holds it, which is the one thing that ack may never
  wait for.
- **`_run_session_frame` (~line 1421 `attach` / `elif detach`)** — WRAPPED. This is the real
  dispatcher, running on the session's work chain; it is what actually calls `_handle_attach` /
  `_handle_detach`.
- **`_handle_prompt`'s first `_ensure_live` (~line 1299)** — WRAPPED, lock released before
  `send_prompt` exactly as the plan requires.
- **`_handle_prompt`'s `no_driver` rebuild (~line 1366-67: `_teardown_live_task` + `_ensure_live`)**
  — WRAPPED as one locked region. The plan predates this path, but it is the same mutation pair
  (drop the stale forwarding task, rebuild it) and must not interleave with an auto re-attach.
  The retried `send_prompt` stays outside the lock.
- **`finally:` teardown loop** — WRAPPED, with a deliberate deviation from the plan's text (below).

## Deadlock analysis

`attach_lock` is a non-reentrant `asyncio.Lock`, so the question is whether any path holding it
can await something that needs it.

Holders: `_run_session_frame` (attach/detach), `_handle_prompt` (two regions), the `finally`
teardown, and `_auto_reattach`. Callees under the lock: `_handle_attach`, `_handle_detach`,
`_ensure_live`, `_teardown_live_task`.

- None of those four acquires `attach_lock` itself — `_auto_reattach` is the only nested caller
  and it takes the lock *around* `_handle_attach`, never inside it. No self-deadlock.
- `_teardown_live_task` → `cancel_and_reap(live_task)` awaits `_forward_live`, which never touches
  the lock; its completion callback `_clear_when_finished` is synchronous.
- `_handle_attach` / `_ensure_live` await orchestrator work (`get_session`, `ensure_driver`,
  `ring_covers`/`ring_tail`, disk replay, `_emit_command_catalog`,
  `_send_deferred_initial_prompt` → `orchestrator.send_prompt`) and `out_q.put_nowait`. None
  re-enters the WS handler, and the `out_q` batcher is an independent task that takes no lock, so
  a locked path enqueuing frames never depends on a lock holder.
- Nothing under the lock awaits a **work-chain** item. That is the important one: a chain blocked
  on the lock can therefore never be something a holder is waiting for. `_drain_work_chains()`,
  which *does* await the chains, runs earlier in `finally` and holds no lock.
- Cancellation-safe: `async with` releases in `__aexit__` when `CancelledError` propagates, so
  `cancel_and_reap(work_task, …)` in the teardown cannot strand the lock.

**Conclusion: no cycle, no deadlock.** The residual cost is liveness, not safety — the lock is
socket-global, so a slow attach for session A can delay an attach/`_ensure_live` for session B on
the same socket (worst case `_RESUME_TIMEOUT`, 90 s). `send_prompt`, the actual long pole, is
deliberately outside it. The 74-test WS sweep shows no timeouts.

**One real bug found and fixed, in the teardown.** The plan's `for sid in …: async with
attach_lock: await _handle_detach(sid)` locks per iteration, but `event_task` is still running
during `finally` and can still spawn an auto re-attach. One that acquires the lock in a gap
between iterations (or after the loop) would run `_handle_attach` and register a fresh forwarding
task the already-taken `live_tasks` snapshot no longer covers — a leaked driver subscriber
buffering forever on a closed socket. Fixed by hoisting the lock over the whole loop and clearing
`attached` inside it first: an in-flight re-attach finishes before the teardown (so its task IS
torn down), and a later one finds an empty attached-set and returns.

## Test adaptation

The rescued test is byte-identical to the plan's listing and was copied verbatim; the harness
symbols (`_Harness`, `_IDENTITY_HEADERS`, `_drain_until`, `_release_finished`, `_wait_until`) all
still exist in `server/tests/test_ws_prompt_after_release.py` with those names, and
`ensure_driver(session_id, identity=SYSTEM_IDENTITY)` takes no extra required args, so
`client.portal.call(orch.ensure_driver, sid)` is correct as written.

**One adaptation was required** (`_with_history(orch)`, added to all three tests): `replayReset`
— the frame all three tests assert on — is only emitted when the replay has history to send or a
live in-progress turn to re-supply (`if events or drop_incomplete:` in `_replay_and_complete`'s
helper). The `_Harness` session writes nothing to disk, so every replay in it produced a bare
`replayComplete` and **zero** `replayReset` frames, making a successful auto re-attach
indistinguishable from no re-attach: with the feature implemented but no fixture, all three tests
still failed (test 3 saw `0 == 1` even for its own terminator attach). Pointing
`orch.transcript_path` at `server/tests/fixtures/transcript.jsonl` — the same mechanism
`test_ws.py` uses for its replay assertions — restores the observable. No assertion was weakened;
the fixture makes them meaningful rather than vacuous.

## Verification

Red (implementation reverted, fixture adaptation in place) — `/tmp/ws-auto-reattach-red.log`:

```
FAILED server/tests/test_ws_auto_reattach.py::test_snapshot_viewer_converges_on_external_resume
FAILED server/tests/test_ws_auto_reattach.py::test_released_mid_view_then_external_resume_converges
2 failed, 1 passed, 61 warnings in 195.78s (0:03:15)
```

Exactly the plan's prediction: 1 and 2 fail because no `replayReset` ever arrives (the drain never
terminates and pytest-timeout kills it at 90 s), 3 already passes. Note the failure mode is a
*hang*, not an assertion — `_drain_until` has no per-`receive_text` timeout, so an unmatched
predicate blocks (a known trap in this repo). Test 3 bounds itself with a terminator attach; 1 and
2 rely on the pytest timeout, as the plan's author intended.

Green:

```
$ pytest server/tests/test_ws_auto_reattach.py -q --timeout=120
3 passed, 61 warnings in 27.83s
```

Regression sweep (`/tmp/ws-neighbors.log`, re-run after the teardown hardening) — no failures, no
timeouts:

```
$ pytest test_ws.py test_ws_prompt_after_release.py test_ws_gap_healing.py test_ws_frame_order.py \
         test_ws_multiclient_seams.py test_ws_cursor_catchup.py test_ws_inprogress_replay.py \
         test_ws_reattach_turn_resupply.py test_ws_auto_reattach.py -q --timeout=120
74 passed, 489 warnings in 227.65s (0:03:47)
```

Lint / types:

```
$ ruff check src/ server/
All checks passed!
$ python -m mypy server/bridge/ws.py --ignore-missing-imports
Success: no issues found in 1 source file
```

All runs used a fully stripped `CCSWITCH_*` environment plus `CCSWITCH_DEV=1` (`/tmp/runpytest.sh`):
this shell exports `CCSWITCH_SESSIOND_ENDPOINT`/`_TLS_*`, and unsetting only the vars the plan
names still left the module-level `create_app()` demanding client certs at import.

## Unresolved concerns

1. **Tests 1 and 2 hang rather than fail when the feature regresses.** They are left as the author
   wrote them (the plan's contract), so a regression costs a 300 s CI shard timeout instead of a
   fast assertion. A follow-up could give them a terminator like test 3's, but a terminator attach
   emits its own `replayReset` and would make the "an unsolicited replay arrived" assertion
   vacuous — it needs a different observable (e.g. counting resets around a sentinel), which is
   more than a test-harness tweak.
2. **`attach_lock` is socket-global, not per-session.** Correct and deadlock-free, but it
   serializes attach/`_ensure_live` across every session on one socket, partially undoing #1180's
   per-session concurrency for those specific operations. A per-session lock map would be
   strictly better; it was out of scope for a recovered implementation and no test regressed.
3. **Auto re-attach tasks are supervised but untracked.** Teardown now fences them via
   `attached.clear()` under the lock rather than cancelling them, which is sufficient (a fenced
   task returns immediately) but means there is no set to inspect if one ever wedges inside
   `_handle_attach`.
4. **Remote/fleet mode is untested here.** `_handle_attach` on a fleet orchestrator does RPCs,
   so an auto re-attach storm across many attached sockets would multiply daemon load; the 2 s
   per-session floor bounds it per session but not across sessions.
