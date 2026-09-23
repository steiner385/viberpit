# A recovery that worked is not a warning

## Symptom

An auto-error issue whose body describes the system working as designed, with an
occurrence count that tracks how often the condition arises rather than how broken
anything is — and nothing to fix. #1718 is the type specimen: 45 occurrences of
`stall watchdog: interrupting wedged turn session=<id>`, filed against a watchdog that
did exactly its job. Of the 17 sessions it interrupted on rocky between 2026-09-14 and
2026-09-23, 11 were interrupted once and settled.

The issue body has a second tell: `Sessions | —` even though the session id is sitting
in plain sight inside the quoted message.

## Anti-pattern

**A log line that reports the *success* of an automatic recovery, emitted at WARNING.**
`server/obs/errsink.py` is a `logging.Handler` with `level=WARNING`, so the log level
is the filing decision — WARNING is not "notable", it is "file an issue about this".
A self-healing action is therefore either INFO or an issue per occurrence; there is no
third option.

The escalation ladder usually already draws the line correctly and the first rung is
the one that drifts: `_check_stalls` logged the recompose (WARNING) and the pause
(WARNING) *and* the plain interrupt (WARNING), flattening "we recovered" into the same
band as "recovery is failing". The rest of that module had it right —
`disabled_account_sweep: rotating`, `rotation.restart_driverless`, `reassign.complete`
are all INFO.

**A corollary trap:** `extra={"session_id": sid}` does not attach a session id.
`ContextFilter` (`server/obs/jsonlog.py`) *assigns* `record.session_id` from the
contextvar, clobbering anything a caller passed. It is a handler filter that mutates
the shared record, so the error sink sees the ids only because the stream handler runs
first. The only mechanism that works is `bind_session()` around the emitting code.

## Fix shape

Level by what a human must do, not by how unusual the event is.

- **The recovery action** → INFO, carrying the fields an investigation needs. For the
  watchdog: elapsed silence against the timeout in force (300 s vs the 900 s throttled
  timeout distinguishes a hang from a backoff), `throttled`, and the consecutive
  `streak` — the one field that says whether the *previous* interrupt landed.
  `RecoveryState.stall_streak()` exposes it, and `stall_cap_reached()` is defined in
  terms of it so the count and the cap cannot drift.
- **The rung where recovery failed** → WARNING, unchanged. A recompose loses the turn's
  in-flight work; the pause parks the session in `needs_attention`. Those are the
  conditions worth an issue, and they are reached only after the streak proves the
  interrupt is not clearing the turn.
- **The subject** → `with bind_session(sid):` around the whole per-driver body, so the
  pre-timeout diagnostic snapshot, the interrupt and both escalations are all queryable
  by session and the filed issues name their sessions.

Downgrading loses nothing: journald keeps INFO (that is how the #2226 diagnostic
snapshot is read), and the unrecovered case still files.

## Where to look

- Fixed: `SessionOrchestrator._check_stalls` in `server/sessions/orchestrator.py`;
  `RecoveryState.stall_streak` in `server/sessions/recovery.py`. Guarded by
  `server/tests/test_stall_watchdog_log_levels.py`, which asserts the level of every
  rung, not just the message.
- Same shape, still open (each has its own auto-error issue; swept under #2275):
  - `rehydrate: recovered %d undelivered initial prompt(s)` and the
    `session.initial_prompt_recovered` event, `server/sessions/orchestrator.py` —
    "recovered" at WARNING (#1979, #1980, #2172).
  - `ws.prompt: no_driver with a live stream entry — rebuilding driver and retrying
    once`, `server/bridge/ws.py` (#1976).
  - `codex.manager.runner_restarted`, `server/codex/manager.py` — the restart that
    succeeded, alongside `runner_exited` which is the failure (#2149, #2150).
  - `daemon restart observed`, `server/sessions/daemon_health.py` — an observation of a
    deploy, not a fault (#2190).
- Companion: `2026-09-20-a-refusal-is-a-state-not-an-event.md` covers volume (one line
  per distinct fact). This note covers band (which facts belong above the filing
  threshold at all). A line can need both.
