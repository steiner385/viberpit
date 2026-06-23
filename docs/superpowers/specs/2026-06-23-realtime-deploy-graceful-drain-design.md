# Near-real-time daemon deploys via graceful drain — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorming) — pending implementation plan
**Topic:** Replace the deploy idle-gate (defer-until-idle) with a server-owned graceful-drain
handshake so daemon-code changes deploy in near-real-time without losing in-flight work.

## Problem

`scripts/auto-deploy.sh` gates daemon-affecting deploys behind an **idle gate**: while any
session is mid-turn (`/healthz` reports `"busy":true`) it defers the whole deploy, retrying each
timer tick, capped by `CCSWITCH_DEPLOY_MAX_DEFER_SEC` (default 1800s). The cap is keyed to the
commit SHA, so **every new commit to master resets the 1800s clock** — on an actively-developed,
busy server a merged daemon-code fix can sit undeployed indefinitely (the starvation bug captured
in memory `deploy-starvation-busy-gate.md`).

The gate is also far more conservative than the real risk. The daemon already **survives a restart**:

- Session metadata + `claude_session_id` live in SQLite (`server/db/models.py`).
- The full transcript is on disk as `.jsonl`.
- On boot, `orchestrator.rehydrate()` (`server/sessions/orchestrator.py:643`) rebuilds session
  records from the DB; on reattach, `ensure_driver()` (`:822`) re-spawns the SDK with
  `resume=<claude_session_id>` and replays the transcript from disk.

What a restart actually costs is narrow:

1. **One in-flight turn's partial output** — a prompt mid-generation is interrupted and its
   un-persisted tokens are lost (the SDK subprocess is SIGTERM'd via the daemon unit's
   `KillMode=control-group`).
2. A **brief WebSocket reconnect blip**.

So the design goal is not "restart without destroying sessions" (already true) but: **let the
currently in-flight turn finish, then swap code — and make the pending update visible and the user's
next prompt safely queued.** This mirrors how Claude Code applies an update on the next launch while
the running session finishes on the old binary.

## Goals

- Daemon-code deploys apply within ~one in-flight turn of being ready, not "whenever the server
  next goes fully idle."
- No lost work for the **common** case (a turn in progress when a deploy lands).
- The user sees an explicit "update pending" state and any prompt they submit during the drain is
  queued and sent automatically after the restart.
- Deploy latency is **bounded** — one pathologically long turn cannot re-create starvation.

## Non-goals

- Zero-downtime in the strict sense (no reconnect blip at all). A brief WS reconnect is acceptable.
- Surviving a closed browser tab with a queued-but-unsent prompt (v1 queue is client-side; see
  Decisions). SQLite-persisted queue is a noted follow-up, not in scope.
- Changing the edge-only fast path. Edge-only changes in remote mode already skip the gate and
  restart non-disruptively (`scripts/auto-deploy.sh:166`); that behavior is unchanged.
- Changing the VNC gate. A human watching a session via VNC is a separate concern from in-flight
  turns and keeps its existing defer behavior.

## Chosen approach

**Server-owned drain handshake + hard cap.** The deploy script stops calling `systemctl restart`
blindly. It asks the server to drain, waits for "ready" (bounded by a hard cap), then restarts.

Rejected alternatives:

- **SIGTERM graceful-shutdown handler only.** Minimal code, but the drain window is a fixed
  `TimeoutStopSec`, the "update pending" signal can only fire *at* SIGTERM (no advance notice), and
  the edge is being SIGTERM'd at the same moment — so the UI learns of the update only as its socket
  drops, which defeats the visible-pending-state requirement.
- **Full-idle drain (wait for the user to stop submitting).** Unbounded worst case — re-creates the
  current starvation in a nicer wrapper.

## Drain boundary (settled)

The daemon waits for the **currently in-flight turn(s)** plus any prompts a session had **already
queued in its driver before the drain began**. New prompts submitted *during* the drain are rejected
by the orchestrator's `send_prompt` guard (the browser buffers and resends them post-restart), so
they do **not** extend the wait — but a driver's pre-existing FIFO queue drains on the old code first,
because those prompts auto-run turn-by-turn inside the driver without re-entering `send_prompt`.

This is a deliberate, reviewed choice (final whole-branch review, finding I1): it finishes work the
user already committed rather than killing it, and it is bounded by `CCSWITCH_DEPLOY_DRAIN_CAP_SEC` so
it cannot re-create starvation — a session with a long pre-drain queue simply hits the cap and gets a
forced restart, with the interrupted turn resuming from transcript. Worst-case drain is therefore
"the in-flight turn + any already-queued prompts," not strictly one turn, capped regardless.

## Components & changes

### 1. Daemon drain state machine — `server/sessions/orchestrator.py` (+ IPC)
- New `DRAINING` state on the orchestrator.
- Entering `DRAINING`:
  - Reject new prompt submissions with a typed `update_pending` result (rather than running them).
  - Broadcast an `update_pending` event on the existing event stream to all connected UIs.
  - Resolve a "drained" future once every session reports not-busy.
- **Self-clearing timeout** (`CCSWITCH_DRAIN_SELFCLEAR_SEC`, default 600s — comfortably above the
  deploy-side drain cap + restart time): if no restart arrives within this bound, exit `DRAINING`,
  resume normal prompt acceptance, and broadcast `update_cancelled` so a missed/failed restart
  cannot wedge the daemon in `DRAINING`.

### 2. Drain control endpoints — `server/api/`
- `POST /api/deploy/drain` — localhost-only (mirrors the existing localhost trust used for the
  deploy curl against `/healthz`); forwards over the Unix-socket IPC to the daemon.
- `GET /api/deploy/drain-status` — returns `{state, busySessions, elapsed}` for the script to poll.

### 3. IPC method — `server/ipc/orchestrator_rpc.py`
- `begin_drain` / `drain_status` RPCs so the edge can drive the daemon over the existing
  `/run/ccswitch/sessiond.sock` transport.

### 4. Deploy script — `scripts/auto-deploy.sh`
- Replace the busy idle-gate block (roughly lines 99–168, the `busy_verdict` defer logic) with:
  `POST /drain` → poll `drain-status` until `ready` **or** `CCSWITCH_DEPLOY_DRAIN_CAP_SEC`
  (default 180s) → restart edge + daemon.
- On cap-exceeded: force the restart anyway (rare; auto-resume + transcript replay recover the one
  interrupted turn). Log the forced cutover.
- Keep the existing edge-only/remote-mode fast path and the VNC gate unchanged.
- The DEFER_FILE / `CCSWITCH_DEPLOY_MAX_DEFER_SEC` machinery for the busy case is removed (the drain
  handshake replaces it); VNC defer, if retained, keeps its own state.

### 5. Frontend — `web/src/`
- Handle the `update_pending` event: show a banner — "Update pending — finishing current turn, your
  next message will send after a brief restart."
- When the user submits during drain, hold the prompt **client-side**, show it as `queued`, and
  auto-resend on WS reconnect after the restart.
- On reconnect with no pending update, clear the banner.

## Data flow (happy path)

```
deploy script
  → POST /api/deploy/drain
    → IPC begin_drain
      → daemon: enter DRAINING, broadcast update_pending
        → UIs show banner; in-flight turns finish; new prompts rejected-and-held by browsers
      → daemon: all sessions not-busy → resolve "drained"
  → poll drain-status == ready (or hit cap)
  → systemctl restart edge + daemon
    → daemon rehydrate() → sessions resume from transcript
  → UIs reconnect → clear banner → auto-resend queued prompts
```

## Failure modes

| Scenario | Handling |
| --- | --- |
| Restart never happens (script dies post-drain) | Daemon self-clearing timeout exits `DRAINING`, resumes prompt acceptance, broadcasts `update_cancelled`. |
| Turn longer than the cap | Forced restart interrupts it; existing auto-resume + transcript replay recover the conversation. One partial turn lost — the explicitly-accepted rare backstop. |
| Tab closed mid-queue | Client-side queued prompt is lost. Accepted v1 trade-off; SQLite persistence is the upgrade path. |
| Drain requested while already draining | Idempotent — return current drain-status. |
| Edge restarts but daemon doesn't (edge-only change) | Not a drain case — fast path unchanged; no `update_pending`. |

## Decisions (resolved during brainstorming)

- **Drain waits for the current turn only**; prompts submitted during drain are queued for after the
  restart. (Bounds worst-case latency.)
- **Approach 1 (server-owned handshake) + hard cap** from the hybrid. Cap is insurance against one
  endless turn re-creating starvation.
- **Queue lives client-side for v1.** The browser tab is the only layer that does not restart during
  a deploy (both edge and daemon bounce), so a client-side hold + resend-on-reconnect survives both
  restarts with no server-side queue table. SQLite-persisted queue is a clean follow-up if losing a
  queued prompt on tab-close ever matters.

## Testing

- **Bats** (`scripts/tests/`): the drain handshake replaces the idle-gate tests. Cover: ready-path
  (drain → ready → restart), cap-exceeded force restart, and self-clear when no restart arrives.
- **Pytest** (`server/tests/`): daemon `DRAINING` transitions, prompt rejection while draining,
  drained-future resolution, self-clearing timeout, idempotent re-drain.
- **Vitest** (`web/src/`): `update_pending` banner render, queue-and-resend-on-reconnect.

## Open follow-ups (out of scope)

- Server-persisted (SQLite) prompt queue for durability across a closed tab.
- Only restart the edge when edge code actually changed (today the script restarts the edge
  unconditionally), so a daemon-only change keeps browser WS connections up and makes the drain UX
  even smoother.
