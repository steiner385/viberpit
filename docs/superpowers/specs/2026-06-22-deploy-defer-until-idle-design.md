# Deploy without killing live sessions — Phase B: defer-restart-until-idle

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** B of a two-phase effort. Phase A (session daemon for true zero-downtime,
including mid-turn) is deliberately out of scope here — see "Deferred to Phase A".

## Problem

The auto-deployer restarts `ccswitch.service` on every deploy
(`scripts/auto-deploy.sh` → `sudo systemctl restart`). The live `claude`
subprocess for each session is owned by the SDK-client object **inside** the
uvicorn process (orchestrator → `SessionDriver` → `ClaudeSDKClient` →
subprocess), so a restart kills every running session.

Most of that is already recoverable. `claude_session_id` is persisted to the DB;
on startup `orchestrator.rehydrate()` restores detached `SessionRecord`s; on
browser reconnect `ensure_driver(resume=<claude_session_id>)` rebuilds the driver
and the WS bridge replays the transcript. So **conversation continuity and
scrollback already survive a restart.** What does *not* survive is the **live
process and any in-flight turn**: a deploy that lands mid-generation interrupts
that turn, and drops queued prompts and pending permission/question state.

Measured churn (last 60 commits on master): only **2** touched the session engine
(`server/sessions/` or `server/bridge/`); ~96% were web/API/UI only. So the
overwhelming majority of deploys have no reason to disturb a running turn — they
just happen to restart the process at an arbitrary moment.

**Phase B goal:** stop a deploy from restarting the server *while a turn is in
flight*. Land deploys only at a safe (idle) point, where the existing
resume+replay machinery already makes the restart effectively lossless.

## Design

Three small pieces; no process split.

### 1. Orchestrator — `restart_safe()`

Add a read-only, in-memory method to `SessionOrchestrator`:

```
def restart_safe(self) -> bool:
    """True when no session is mid-turn or awaiting user input — i.e. a restart
    now would lose no in-flight work (resume + transcript replay cover the rest)."""
```

It derives the answer from the existing `_records` snapshots: a session counts as
"active" when its status is not in `{idle, done, ended, archived}` (i.e. it is
generating a turn or waiting on a permission/question answer). `restart_safe()`
is `True` iff zero sessions are active. Pure function over current state; no new
fields, no new persistence. A companion `active_session_count()` returns the
count for observability.

Rationale for treating `waiting_input` as "active" (blocking): although
`_handle_attach` re-emits pending permissions/questions on reconnect, the
in-memory permission/question futures are lost on restart and the resumed process
re-derives that state non-deterministically. Blocking on it is the conservative,
lossless choice; the deploy max-defer cap (below) prevents a walked-away user from
blocking deploys forever.

### 2. `/healthz` exposes drain-readiness

Extend the existing `/healthz` body (already `{"status","sha"}`) to:

```
{"status":"ok","sha":"<short>","busy":<bool>,"activeSessions":<int>}
```

`busy` is `not orchestrator.restart_safe()`; `activeSessions` is the count.
Reusing `/healthz` (already curled by the deployer) avoids a second endpoint.
`busy`/`activeSessions` expose only a count — no session content — so the
endpoint stays as unauthenticated as it is today. The schema-error branch keeps
returning `status:schema_error` (a 500 there means the server is unhealthy
regardless of busyness).

### 3. `auto-deploy.sh` — idle gate before the pull

Insert an idle gate immediately **after** the CI gate and **before** the
fast-forward pull, preserving the invariant "HEAD == origin/master ⇒ deployed and
restarted" (gating after the pull would leave new code pulled-but-not-restarted,
which the next tick's `LOCAL == REMOTE` early-exit would treat as done — a wedge).

Per tick, after CI is green:

1. `GET /healthz` on the running server; read `busy`.
2. `busy == false` (or healthz unreachable / field absent) → proceed with the deploy.
3. `busy == true` → consult the defer marker:
   - Marker is a single file at `$REPO/.git/ccswitch-deploy-defer` (inside `.git`,
     never tracked, survives the working-tree `git reset --hard` guard), holding
     `<target-sha> <epoch-first-deferred>`.
   - If the marker's SHA ≠ current `$REMOTE` → write `<REMOTE> <now>` and skip this
     tick (first deferral for this commit; clock starts).
   - Else if `now − epoch > CCSWITCH_DEPLOY_MAX_DEFER_SEC` (default 1800s/30min) →
     **force** the deploy, logging that we deferred for N seconds and are
     proceeding (the in-flight turn will be interrupted; resume covers continuity).
   - Else → skip this tick and retry.
4. On a successful deploy (or a forced one), clear the marker.

`busy == false` and the unreachable/malformed cases proceed: a down server has no
sessions to protect and we *want* the deploy to recover it (fail-open on the drain
check only — the CI gate already enforces correctness).

## Data flow

```
timer (every 2 min)
  → CI green for origin/master?         (skip+retry if not)
  → server idle? (GET /healthz .busy)   (skip+retry if busy, capped at MAX_DEFER → force)
  → working-tree guard + git pull --ff-only
  → dep-sync (if lockfiles changed)
  → npm run build (web/dist)
  → sudo systemctl restart ccswitch.service
  → GET /healthz: status:ok AND sha==expected   (rollback if not)
  → clear defer marker
```

## Error handling

- `/healthz` unreachable or no `busy` field → treat as not busy → proceed (covers
  a crashed server we need to recover; the post-restart health gate still guards
  correctness).
- Max-defer cap (`CCSWITCH_DEPLOY_MAX_DEFER_SEC`, default 1800) guarantees deploys
  never block indefinitely on a perpetually-busy session.
- `restart_safe()` reads only in-memory snapshots and cannot raise on DB issues
  (no DB call); a missing `_records` entry simply isn't counted.

## Testing

- **Orchestrator unit:** `restart_safe()` / `active_session_count()` across status
  combinations (idle-only → safe; one in_progress → unsafe; waiting_input →
  unsafe; done/ended → safe). Construct records directly; no SDK/network.
- **`/healthz` integration:** asserts `busy`/`activeSessions` reflect the
  orchestrator state (monkeypatch `restart_safe`), alongside existing
  `status`/`sha`.
- **`auto-deploy.sh` bats:** the idle check honors a `CCSWITCH_DEPLOY_BUSY` test
  seam (`1`/`0`) that forces the verdict without curling `/healthz`, mirroring the
  existing `CCSWITCH_DEPLOY_CI_OK` seam (production curls `/healthz`). Cases:
  - busy → skip, no fast-forward, marker written;
  - busy past cap (force) → proceeds to the sync stop-hook;
  - idle → proceeds;
  - healthz-unreachable → proceeds.

Mirror the existing test seams (`CCSWITCH_DEPLOY_CI_OK`,
`CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC`) so the gate is exercised without a real
server or `gh`.

## Deferred to Phase A (session daemon)

- Surviving a **mid-turn** deploy (a forced deploy past the cap still interrupts).
- Surviving deploys that change the **engine code itself** (`server/sessions/**`,
  `server/bridge/**`) — those still require a session-affecting restart.
- True "never kill," which requires the `claude` processes to live in a long-lived
  daemon decoupled from the web-server lifecycle.

## Out of scope (YAGNI)

- Notifying connected clients of an imminent restart (Phase A concern).
- Per-session deploy targeting / partial restarts.
- Persisting queued-prompt / pending-question state to disk: unnecessary once
  restarts only happen at idle, where there is nothing in flight to persist.
