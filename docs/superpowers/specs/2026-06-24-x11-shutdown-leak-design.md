# Design — Bound & sweep X11 display teardown (issue #206)

**Issue:** Daemon/edge SIGKILL on shutdown leaks Xvfb/x11vnc/chrome (TimeoutStopSec exceeded).

## Status — what already landed vs. this PR

Two PRs already merged the **shutdown** half of this design:
- **#253** — edge shutdown bounds + parallelizes display teardown (`X11Manager.stop_all()`,
  `asyncio.wait_for(_SHUTDOWN_SWEEP_TIMEOUT_S=15s)` in `app.py`).
- **#255** — daemon shutdown calls `x11.stop_all()` (15s cap) in `daemon_lifespan` `finally`.

So **layers 1 (bounded concurrent shutdown) and the daemon-shutdown fix are DONE.**
`stop_all()` already gathers `stop_display` concurrently with `return_exceptions=True`.

**This PR delivers the remaining layer 3 — the startup orphan-process sweep** (the
issue's third fix option, "a startup sweep adopts/cleans orphans"). It is the
self-healing keystone: today `sweep_stale()` only unlinks stale lock/socket *files* and
never reaps leaked Xvfb/x11vnc/chromium, and **the daemon startup sweeps nothing at all**
(neither files nor processes). So any prior SIGKILLed shutdown still accumulates orphans
until manually cleaned. This PR fixes that.

## Problem

On a clean stop, per-session virtual displays (Xvfb + x11vnc + chromium) must be torn
down. Today that teardown is slow, unbounded, and in one mode entirely absent, so
`ccswitch.service` is SIGKILLed by systemd (`TimeoutStopSec=60`) and the display
processes leak. Orphaned `ccswitch-*` Xvfb accumulate across restarts.

Three concrete defects, all confirmed in the code:

1. **Edge in-process shutdown is sequential and unbounded.**
   `server/app.py` `_lifespan` shutdown (non-remote branch) does
   `for sid in orchestrator._drivers: await close_session(sid)`. Each
   `close_session` awaits `x11.stop_display`, which calls `_terminate(x11vnc, 5s)` +
   `_terminate(xvfb, 5s)` + `terminate_process_tree(browser, 1.5s)` — **serialized**.
   A handful of live sessions exceeds the 60s `TimeoutStopSec`, so systemd SIGKILLs the
   process before teardown finishes → leaked children.

2. **Daemon shutdown tears down nothing.**
   `server/ipc/serve.py` `daemon_lifespan` `finally` calls only
   `orchestrator.stop()` (poller/monitor). It never calls `stop_display`, so in
   daemon mode **every** display leaks on every stop. The daemon startup also never
   sweeps stale files.

3. **Startup sweep never kills processes.**
   `X11Manager.sweep_stale` (`x11manager.py`) only unlinks stale lock/socket *files*;
   it never reaps orphaned Xvfb/x11vnc/chromium left by a prior SIGKILL. So a leak,
   once it happens, persists.

## Approach — full defense-in-depth (code only; systemd units unchanged)

Three layers. The bounded teardown makes SIGKILL rare; the force-kill makes shutdown
finite; the startup process sweep makes accumulation impossible regardless of cause
(self-healing). The ~15s teardown budget finishes far under the existing 60s
`TimeoutStopSec`, so no unit-file change is needed.

Reuse existing code (constitution VI — no parallel implementations):
- `/proc` enumeration: `server/sessions/adopt.py` `_proc_lister()` → `ProcInfo`
  (`pid`, `cmdline`, `environ`, `cwd`).
- Process-tree kill: `adopt.terminate_process_tree(pid, grace=…)` (SIGTERM, grace,
  then SIGKILL; no-op if already gone).

### New `X11Manager` methods (`server/sessions/x11manager.py`)

**`async shutdown_all(deadline_s: float = 15.0) -> None`**
- Snapshot `list(self._specs.values())` under `self._lock` (snapshot is needed
  because `stop_display` pops the spec immediately, so PIDs must be captured up front
  for the force-kill fallback).
- `await asyncio.wait_for(asyncio.gather(*(stop_display(sid) … ), return_exceptions=True), timeout=deadline_s)`
  — concurrent, so wall-clock ≈ slowest single teardown, not the sum.
- On `asyncio.TimeoutError`: log a warning and force-kill every PID in the snapshot
  (`xvfb`, `x11vnc`, each `browsers[*]`) via `terminate_process_tree`. Idempotent —
  PIDs that already exited are no-ops.
- No live sessions → return immediately.

**`sweep_orphan_processes(lister=_proc_lister) -> int`**
- Compute `managed = range(display_start, display_start + display_count)` and
  `live = {spec.lease.display for spec in self._specs.values()}`.
- For each `ProcInfo`, derive its display number:
  - `Xvfb`: argv token matching `:<N>` (e.g. `Xvfb :100`).
  - `x11vnc`: token after `-display` (e.g. `-display :100`).
  - `chromium`: `environ["DISPLAY"]` (e.g. `:100`).
- Kill it (`terminate_process_tree`) when its display is in `managed` and **not** in
  `live`. Secondary safety match: `XAUTHORITY` env basename starts with `ccswitch-`.
  Return the count killed.
- At process startup `_specs` is empty, so every in-range display is an orphan and
  gets reaped — exactly right for a fresh boot. Only one component owns displays per
  host (edge in-process mode has `x11`; daemon mode the daemon does), so there is no
  risk of one reaping another's live displays.

`lister` is injected so tests pass a fake `/proc` with no real processes.

### Wiring

- **Edge startup** (`server/app.py`): in the existing X-sweep block, also call
  `x11.sweep_orphan_processes()` and log the count.
- **Edge shutdown** (`server/app.py`, non-remote branch): before/in place of relying
  on the per-session loop for display teardown, `await x11.shutdown_all()`. The
  session-ending loop and `orchestrator.stop()` are unchanged in intent; displays are
  now reaped fast and concurrently rather than one-at-a-time.
- **Daemon startup** (`server/ipc/serve.py` `daemon_lifespan`): add the same
  `sweep_stale()` + `sweep_orphan_processes()` block (currently absent).
- **Daemon shutdown** (`daemon_lifespan` `finally`): `await x11.shutdown_all()` before
  `orchestrator.stop()`, guarded on `x11 is not None`.

### systemd

No change. The bounded teardown finishes well under the existing `TimeoutStopSec=60`.
(Committed unit files may not match the live host anyway, so a bump would require a
manual reinstall to take effect — out of scope here.)

## Testing (`server/tests/`)

- **Concurrent + bounded:** `shutdown_all` with N fake specs whose `_terminate`
  sleeps; total wall-clock ≈ one teardown, not N; completes under the deadline.
- **Force-kill on timeout:** a fake proc whose `wait()` never returns triggers the
  `TimeoutError` branch and `terminate_process_tree` is invoked for its PID.
- **Orphan sweep matches range:** a fake `/proc` Xvfb on an in-range display is killed;
  an out-of-range Xvfb and an in-range display that maps to a live spec are spared;
  x11vnc (`-display :N`) and chromium (`DISPLAY` env) are each matched.
- **Daemon lifespan:** entering then exiting `daemon_lifespan` calls `shutdown_all`;
  startup calls the sweep. (Patch `build_x11_manager`/inject a fake x11.)
- Existing `test_x11_manager.py`, `test_orchestrator_display.py`,
  `test_app_x11_lifespan.py` stay green.

## Out of scope

- Changing whether shutdown *ends* sessions (close_session semantics) — unrelated to
  the leak.
- systemd unit edits.
- The `#204` work this was split out from.
