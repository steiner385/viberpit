# User-Controlled Deploy — design

**Date:** 2026-06-24
**Status:** design approved, pending spec review → implementation plan
**Related:** `2026-06-23-realtime-deploy-graceful-drain-design.md` (drain handshake this reuses),
`2026-06-23-session-historic-scrollback-export-design.md` (scrollback source reused for snapshot).

## Problem

ccswitch is a single-user system, yet deployment is **fully automatic and timer-driven**:
`ccswitch-deploy.timer` fires every 2 minutes → `scripts/auto-deploy.sh` polls `origin/master`,
gates on CI, drains, builds, restarts, health-gates, and rolls back — all without the user in the
loop. A merge to `master` therefore auto-deploys and restarts the daemon, killing background work
and disrupting live sessions on the system's schedule, not the user's (see project memory
`deploy-on-merge-kills-background-loops`).

The user wants to **own the timing**: be *notified* that changes are pending (and see which), then
trigger a controlled deploy that saves sessions, updates, reports status, restarts, and resumes
sessions.

## Goals

- Detect undeployed `origin/master` changes and surface them in the UI (notify-only — never
  auto-deploy).
- List the specific pending commits (message, PR, author, time).
- A one-click **Deploy now** that runs an autonomous, watchable 5-step sequence:
  **save → update → report → restart → resume**.
- Explicit, high-fidelity session snapshot/restore across a daemon restart, including scrollback and
  unsent input — not just the implicit transcript-resume.
- Preserve every incident-proofed invariant currently encoded in `auto-deploy.sh`.

## Non-goals

- Stepped/gated deploy flow (explicitly one-click, watch-it-run).
- Remote/multi-host deploy. This stays the single self-hosted Tailscale host.
- Replacing CI. GitHub Actions remains the test gate; the controller only consumes its verdict.

## Decisions (from brainstorming)

1. **Session save/resume = explicit snapshot + restore**, beyond implicit drain/transcript-resume.
2. **Detection = notify-only**, folded into the new long-lived controller's poll loop.
3. **Flow = one-click, watch it run** (autonomous 5 steps with a live progress checklist).
4. **Snapshot fidelity = full**: re-launch recipe + unsent input + scrollback.
5. **Orchestrator = a new long-lived Python service** (`ccswitch-deploy-controller`).
6. **Port everything and retire `auto-deploy.sh`** — single end-state, no parallel deploy engine.
7. **Scrollback bound = the daemon's existing replay buffer** (no new history policy).
8. **Restore failure = keep the (healthy) deploy, flag failed sessions** for retry.

## Architecture

A new **long-lived systemd service `ccswitch-deploy-controller`** (`python -m server.deploy`). It is
neither the edge nor the daemon, so it survives both restarts and is the stable owner of all deploy
state. This solves "who watches the restart": the edge cannot watch its own restart, and the daemon
cannot watch its own — the controller can watch both.

```
┌─────────────┐   poll origin/master (notify-only, interval)
│ controller  │◄──────────────────────────────────────┐
│  (long-     │                                         │ git fetch + compare SHA
│   lived)    │   owns: pending-state, deploy lifecycle,│
│             │         5-step sequence, status model   │
└──────┬──────┘                                         │
       │ snapshot / restore / drain RPC  ┌──────────────┘
       ▼                                 │
┌─────────────┐  systemctl restart    ┌──┴──────────┐
│   daemon    │◄──────────────────────│  edge (web) │──► browser
│ (sessions)  │                       │  proxies →  │   polls /api/deploy/*
└─────────────┘                       │  controller │
                                       └─────────────┘
```

- **Detection (notify-only):** the controller polls `origin/master` on an interval, compares to the
  live SHA, and holds `pending` state (the commit list). Because the controller is long-lived, this
  **subsumes the systemd timer**.
- **Trigger & status:** the edge exposes `GET /api/deploy/status` (state + pending commits) and
  `POST /api/deploy/start`, both **thin proxies to the controller** over a Unix socket
  (`/run/ccswitch/deploy.sock`). Long-lived controller ⇒ status survives the edge's ~1s restart with
  **no durable status file**; the browser just re-polls and the controller answers.
- **Auth:** deploy endpoints sit behind the existing Tailscale-identity gate.

### Disposition of old machinery (purged in the same change)

Deleted: `scripts/auto-deploy.sh`, `deploy/ccswitch-deploy.service`, `deploy/ccswitch-deploy.timer`.
Their logic moves into the controller. Added: `deploy/ccswitch-deploy-controller.service`.
`deploy/README.md` rewritten. No orphaned units (no-half-dead-code norm).

### Invariants that MUST survive the port

Each traces to a real past fix; the port is incomplete until each has a regression test mirroring the
existing `test_deploy_*.py` seams.

1. **CI-gate, fail-closed** — deploy only a commit GitHub proved green; pending/red/unavailable → refuse.
2. **Worktree `.git`-is-a-file** — any state path resolved via `git rev-parse --absolute-git-dir`,
   never `$REPO/.git/...` (issue #95).
3. **Daemon-first, then-edge restart ordering** — browser reconnect-flush race (#190).
4. **Health gate asserts `status:ok` AND expected SHA** — the SPA static mount 200s for any path.
5. **Rollback resets to last-good SHA** (so `HEAD != origin/master` re-triggers), rebuilds, restarts
   **both units together** (never split-version daemon=new/edge=old).
6. **Dep-sync only when lockfiles changed** — a new dep crashing boot would otherwise loop rollback.
7. **Working-tree guard** — discard stray tracked edits before pull, else ff aborts permanently.
8. **Edge-only vs daemon-affecting classification** — edge-only skips drain (non-disruptive restart).
9. **Drain handshake before daemon restart**, capped; force restart after cap (turn resumes from
   transcript).
10. **GitHub Deployment publish** — best-effort, never fails the deploy.

> Note: the script's *defer-across-ticks / starvation* logic (`deploy-starvation-busy-gate`) is a
> timer-era concern. Under user-triggered one-click it is **not ported** — the user owns timing, so
> there is no tick to defer across. Drain (invariant 9) still applies within a single deploy.

## The 5-step sequence

Run autonomously by the controller on `POST /api/deploy/start`; each transition emits a phase event
(`pending → running → done/failed`) the browser renders as a live checklist.

| # | Step | Controller action | Skipped when |
|---|------|-------------------|--------------|
| 0 | pre-flight | Re-confirm CI-green for target SHA; refuse if not. Acquire single-deploy lock. | — |
| 1 | **Save** | `snapshot` RPC → daemon writes a snapshot artifact per live session. | never |
| 2 | **Update** | Working-tree guard → `git pull --ff-only` → dep-sync iff lockfiles changed → `npm run build`. | — |
| 3 | **Report** | Classify daemon-affecting vs edge-only; emit the restart plan. | — |
| 4 | **Restart** | daemon-affecting: `drain` RPC (capped) → restart daemon then edge → health-gate on SHA. edge-only: restart edge only. | — |
| 5 | **Resume** | `restore` RPC → daemon eagerly re-launches each snapshotted session, replays scrollback, restores unsent input; controller polls per-session restore status. | edge-only (daemon kept state in memory) |

Step 5 deliberately changes `rehydrate()`'s laziness: today rehydrate rebuilds sessions *detached*
(PTY spun up on first attach) and flattens a pre-restart `working` status to `idle`. "Resume all"
means restore **eagerly re-launches** `claude --resume <id>` for every session that was live.

## Snapshot/restore data model

The re-launch recipe already persists in the `Session` DB row (account, cwd, branch,
`claude_session_id`). The snapshot adds the state that is otherwise lost, written per session to a
durable artifact (`/var/lib/ccswitch/deploy-snapshot/<session_id>.json` + a scrollback blob):

- **liveness** — was the session active/mid-turn at snapshot time (today flattened `working`→`idle`).
- **unsent input** — buffered/half-typed prompt not yet submitted (not persisted today).
- **scrollback** — bounded to the daemon's existing PTY replay buffer; reuse that source, do not
  invent a parallel history policy. Persisted to the blob, replayed on restore.
- **attached flag** — which session a browser was viewing, so restore surfaces it as foreground.

A snapshot is **consumed and cleared on successful restore**; a failed deploy/restore leaves it in
place as the diagnostic + retry artifact.

## Notification surface (always-on)

- Persistent dashboard indicator when `live SHA != origin/master`: "N changes pending deploy."
- Expandable pending-commit list from `git log <live_sha>..origin/master` — message, PR #, author,
  time.
- **Deploy now** button, disabled while a deploy is in flight and while target-SHA CI is not green,
  with the disabled reason shown (never a mysterious greyed-out button).

By construction, merges accumulate in the pending list until the user deploys — eliminating the
deploy-on-merge hazard (`deploy-on-merge-kills-background-loops`).

## Failure handling

- **CI not green / undeterminable** → refused at pre-flight; button disabled with reason.
- **Pull / dep-sync / build / health-gate failure** → ported rollback (reset to last-good SHA,
  rebuild, restart both units together); failed phase + captured error shown; snapshots retained.
- **Drain cap exceeded** → proceed with restart (turn resumes from transcript); reported as a
  warning, not a failure.
- **Restore fails for some sessions** → deploy stands (it is healthy); those sessions flagged in the
  UI with a **retry** action backed by their retained snapshot.
- **Concurrency** → single-deploy lock; a second Deploy-now while one runs is rejected with "deploy
  already in progress."

## Components / boundaries

- `server/deploy/controller.py` — lifecycle, 5-step sequence, status model, single-deploy lock.
- `server/deploy/detect.py` — origin/master poll, pending-commit diff.
- `server/deploy/steps.py` — ported mechanical steps (ci-gate, guard, pull, dep-sync, build, restart,
  health-gate, rollback, publish-deployment), each independently testable.
- `server/deploy/rpc.py` (or extend `server/ipc/`) — controller↔daemon socket protocol.
- daemon: `snapshot` / `restore` RPCs; eager re-launch in/around `rehydrate()`.
- edge: `GET /api/deploy/status`, `POST /api/deploy/start` proxies over `/run/ccswitch/deploy.sock`.
- web: pending-changes indicator + commit list + Deploy-now + live progress checklist + per-session
  restore-retry affordance.
- `deploy/ccswitch-deploy-controller.service`; remove timer/oneshot units; rewrite `deploy/README.md`.

## Testing

- Controller logic unit-tested in Python (now possible — no longer bash).
- Each ported invariant gets a regression test mirroring the existing `test_deploy_*.py` seams.
- snapshot/restore RPCs tested against the daemon (incl. scrollback round-trip, unsent-input
  restore, partial-restore-failure flagging).
- End-to-end: pending-detection → start → phase events → health-gate → restore status.

## Open questions

- Exact poll interval for detection (default: keep the prior 2-min cadence).
- Whether the controller should also expose a CLI (`python -m server.deploy --once`) for
  ops/debugging parity with the old `systemctl start ccswitch-deploy.service`.
