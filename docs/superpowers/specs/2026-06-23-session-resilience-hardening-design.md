# Session-resilience hardening (post-daemon-cutover)

**Date:** 2026-06-23
**Status:** design — pending review
**Related:** #146 (daemon as managed process), #150 (edge remote-by-default), #152 (daemon `TasksMax` ceiling — prod incident)

## Context

The 2026-06-23 prod incident (no live responses, dropped prompts, dead slash commands)
was rooted in the daemon's `TasksMax=512` cgroup ceiling and is fixed in #152. While
diagnosing it, four robustness gaps surfaced that the remote-by-default cutover exposed
but did **not** cause — they were latent and the cutover made them reachable in prod.
This spec covers those four. They share a theme: **a session whose underlying claude
subprocess dies or wedges is not detected or recovered, and the remote (daemon) path
has thinner coverage than the in-process path it replaced.**

Live evidence gathered during the incident:
- `AttributeError: 'RemoteDriver' object has no attribute 'seed_idle_suggestions'` on
  every attach in remote mode (caught; disables suggestions).
- `DaemonError: ProcessError: exit code -6` / `CLIConnectionError: ... [Errno 11]` —
  crashed subprocesses leaving sessions pinned `in_progress` with a zombie driver.
- `DaemonError: KeyError: 'account'` from `run_command`, surfacing as an uncaught 500.
- Session `d480372e` pinned `in_progress` for hours, subprocess alive but idle, emitting
  only empty-text `thinkingDelta` events with no `turnEnd` — a wedged turn. Manual
  `interrupt` recovered it.

## Non-goals

- Changing the daemon/edge process topology or the IPC wire protocol.
- The `TasksMax` fix (#152, separate).
- A general distributed-systems retry framework. Each gap reuses existing machinery.
- Replay-buffer sizing/backpressure (the overflow we saw was a *symptom* of the wedged
  turn, not an independent defect). Out of scope; revisit only if it recurs without a
  wedged producer.

## Gap 1 — Transparent crash recovery (primary)

**Root cause.** `server/sessions/failure.py` classifies recoverable turn failures as
`_RECOVERABLE = {"rate_limit", "auth_401"}`; everything else is `"other"`.
`SessionDriver._maybe_signal_recovery` only fires the orchestrator's
`on_recoverable_failure` callback for recoverable kinds. A hard subprocess crash
(`ProcessError` with a negative/signal exit, `CLIConnectionError`, "terminated process")
classifies as `"other"` → no callback → the dead driver stays registered in
`orchestrator._drivers`. `ensure_driver`'s fast path (`orchestrator.py:741`) returns any
existing driver without a liveness check, so re-attach hands back the zombie and
`send_prompt` writes to a terminated process.

**Design (chosen: transparent auto-respawn + bounded-retry/cooldown guard).**
1. `failure.classify(...)`: recognize hard-crash signatures and return a new kind
   **`process_crash`**, added to `_RECOVERABLE`. Scope tightly so ordinary turn errors
   stay `"other"` (match on `ProcessError` exit `< 0`, `CLIConnectionError`,
   "terminated process", "failed to start claude"). Unit-tested against real SDK error
   strings.
2. No new driver wiring: because `process_crash` is now recoverable,
   `_maybe_signal_recovery` already routes it to `_handle_recoverable_failure` →
   `_recover_session`.
3. `_recover_session` gains a `process_crash` branch:
   - **Guard first.** New `RecoveryPolicy.crash_cap_reached(session_id, now)` mirroring
     the existing `rotation_cap_reached` — allow up to `CRASH_CAP` (default 3) respawns
     within `CRASH_WINDOW` (default 120s). Exceeded → do **not** respawn; emit a visible
     `error` event ("session repeatedly crashed — paused; retry shortly") and leave it
     evicted/detached. Reset after a cooldown of no crashes.
   - **Otherwise rebuild in place** on the same account, resuming from
     `claude_session_id`, via the existing `_reassign(record, record.account,
     reason="process_crash")` path (same mechanism rate-limit recovery uses). Record the
     crash timestamp for the guard.
4. Respawn is **proactive** (on crash, not lazily on next access) — consistent with how
   rate-limit recovery already behaves; the guard makes it storm-safe. Re-attach and the
   next prompt then transparently use the fresh driver.

**Tests.** Fake SDK factory whose client raises `ProcessError(-6)` mid-turn. Assert:
(a) the session auto-rebuilds and `ensure_driver` returns a live driver; (b) a prompt
after a crash succeeds; (c) the guard trips after `CRASH_CAP` crashes in the window and
emits the paused error instead of respawning; (d) `rate_limit`/`auth_401` paths
unchanged.

## Gap 2 — `RemoteDriver` method parity

**Root cause.** `RemoteDriver` (`server/sessions/remote.py`) implements only
`current_status()`, `queued()`, `events()`. Any other `driver.*` call ws.py or its
helpers make explodes only in remote mode. Confirmed: `seed_idle_suggestions`.

**Design.**
- Add a daemon RPC `seed_idle_suggestions(session_id, user_text, assistant_text)` that
  calls the real driver's method on the daemon (where the suggester + LLM access live);
  the resulting `Suggestions` event flows back over the existing session stream.
- Add `RemoteDriver.seed_idle_suggestions(...)` forwarding to that RPC.
- **Audit:** grep every `driver.*` attribute access in `server/bridge/ws.py` and any
  module it hands the driver to (`server/sessions/suggestions.py`, etc.); ensure each is
  present on `RemoteDriver` (forwarded via RPC) or explicitly a documented no-op. Add a
  test that asserts the `RemoteDriver` public surface covers the set ws.py calls, so this
  can't silently regress.

**Tests.** Remote-mode attach no longer raises `AttributeError`; seeding triggers a
daemon-side suggestion and the event arrives over the stream.

## Gap 3 — Graceful `run_command` / `DaemonError` handling

**Root cause.** `orchestrator.run_command` raises bare `KeyError(name)` when the
session's driver isn't loaded or the command is unknown; over IPC this becomes an
uncaught `DaemonError` → 500 (seen on both the WS command path and a REST path emitting
`Exception in ASGI application`).

**Design.**
- `orchestrator.run_command`: for an unknown command or a not-live session, return a
  clean notice (`{"render": "notice", "data": {"text": ...}}`) instead of raising
  `KeyError`.
- ws.py command handler: `_ensure_live(session_id)` before dispatch (parity with the
  prompt path) so a not-live session is resumed; on any residual failure, emit a clean
  `commandResult`/`error` frame rather than letting it propagate.
- Map `DaemonError` to a proper HTTP response (e.g. 409/503 with a typed body) on the
  REST path(s) that currently 500, via the existing error-handling layer. The plan's
  first task is to identify the exact REST endpoint(s) (the incident trace showed the
  failure passing through `server/auth/middleware.py` dispatch) and add a single
  `DaemonError` exception handler rather than per-route try/except.

**Tests.** `run_command` on a not-live session returns a notice (no raise); WS command on
a detached session resumes then runs; a daemon RPC error maps to a typed HTTP status, not
a 500.

## Gap 4 — Stall watchdog for wedged turns

**Root cause.** A turn can pin a session `in_progress` indefinitely while the subprocess
is alive but idle, emitting only empty-text `thinkingDelta`s and never a `turnEnd`. No
component detects "in_progress but making no progress," so prompts queue behind a turn
that never completes (or don't land at all).

**Design (conservative).**
- Per live driver, track the timestamp of the last *meaningful* event — a non-empty
  `TextDelta`/`ThinkingDelta`, any `ToolCall`/`ToolResult`, or a state transition. Empty
  deltas do **not** count.
- A watchdog (single periodic task on the orchestrator, not per-driver timers) checks live
  `in_progress` sessions: if `now - last_meaningful > STALL_TIMEOUT` (default generous,
  e.g. 5 min) **and** no `turnEnd` since the turn started, classify as a stall.
- On stall: emit a visible `error`/status event ("turn stalled — interrupting") and call
  the existing `interrupt()` (flips `_turn_active`, clears queue, returns to idle). Do
  **not** silently auto-retry the prompt; the user re-prompts. Respect the same
  `crash_cap`-style guard so a session that repeatedly stalls isn't interrupted in a loop.
- Tunable via env (`CCSWITCH_STALL_TIMEOUT_SEC`), defaulting conservative to avoid
  killing slow-but-real turns.

**Tests.** A fake driver that emits only empty `thinkingDelta`s and never `turnEnd` is
interrupted after `STALL_TIMEOUT` (using an injectable clock); a driver emitting
non-empty deltas or tool calls within the window is left alone.

## Shared concerns

- **Single source of truth.** All caps/cooldowns live in the existing `RecoveryPolicy`
  helper alongside `rotation_cap`/`QUARANTINE_*`; no parallel retry bookkeeping.
- **Constants** (`CRASH_CAP`, `CRASH_WINDOW`, `STALL_TIMEOUT`) defined once, env-overridable.
- **Clock injection** for deterministic tests (the orchestrator already takes `clock`).
- **Both paths.** Recovery logic lives in the in-process orchestrator/driver (which the
  daemon runs); the edge sees the results over the existing event stream. Remote-mode
  tests assert parity.

## Delivery

The four gaps are independent and may land as separate PRs (Gap 1 is the largest; Gaps
2–3 are small). The implementation plan sequences them; each PR is independently
shippable and tested.

## Files touched

- `server/sessions/failure.py` — `process_crash` classification.
- `server/sessions/orchestrator.py` — `_recover_session` crash branch, crash guard, stall
  watchdog task, `run_command` graceful returns.
- `server/sessions/recovery.py` — `crash_cap_reached` + stall guard bookkeeping.
- `server/sessions/driver.py` — last-meaningful-event tracking; crash classification hook.
- `server/sessions/remote.py` — `RemoteDriver.seed_idle_suggestions` + any audited methods.
- `server/ipc/orchestrator_rpc.py` — `seed_idle_suggestions` RPC.
- `server/bridge/ws.py` — command handler `_ensure_live` + clean error frames.
- REST error mapping for `DaemonError` (location TBD in plan).
- Tests under `server/tests/`.
