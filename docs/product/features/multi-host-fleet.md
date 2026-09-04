---
slug: multi-host-fleet
title: Multi-Host Session Fleet
state: live
links:
  code:
    - server/sessions/fleet.py
    - server/sessions/fleet_orchestrator.py
    - server/sessions/primary_lease.py
    - server/sessions/host_upgrade.py
    - server/sessions/host_probe.py
    - server/api/fleet.py
    - server/ipc/orchestrator_rpc.py
  prs: [720, 721, 807, 856, 884, 896, 900, 844]
---

# Multi-Host Session Fleet

**Problem** — a single host can only run so many live `claude` sessions, and it's a single point of
failure. Running the session daemon across several hosts (a "fleet" — e.g. `dobby` as edge/primary
plus `rocky` as a session runner) needs new sessions steered to a host with headroom, each session's
owning host recorded so recovery/rotation don't fight across daemons, exactly one host driving
fleet-global automation, and stale cross-host bookkeeping cleaned up — without a two-primary
split-brain silently double-driving everything.

**Target users** — the operator who has added more than one host to their ccswitch deployment and
wants sessions spread across them, plus the dashboard that must show every host's health and every
fleet session in one view.

**Desired outcome** — new sessions are dispatched to the best-scoring healthy host and stamped with
that host permanently; each daemon owns only its own sessions for recovery/rotation; exactly one
primary holds a DB lease and runs the global automation loops; the primary's mirror of a worker's
sessions is evicted when the underlying row is deleted or archived elsewhere; hosts can be probed
for health/capacity and upgraded to a new commit from the app.

**Success metrics** — a session's `host` is stamped at creation and never re-inferred; dispatch
skips hosts marked unhealthy by the probe loop; at most one live `primary` lease exists (a fresh
foreign lease makes a second primary refuse to boot loudly); stale fleet-mirror ghosts are evicted
within two reconcile passes so automation inserts stop failing the session foreign key; a per-host
upgrade lands the target SHA and the daemon's `/info` confirms it.

**Non-goals** — automatic horizontal autoscaling or provisioning of new hosts (hosts are registered
by hand); cross-host live migration of a running session (a session stays on its stamped host);
sharding fleet-global automation across hosts (it is deliberately single-primary); token refresh on
workers (the primary is the sole refresher to avoid racing shared token files).

**Notes** —
- **Host-partitioned ownership**: `Session.host` is stamped at creation by
  `FleetOrchestrator.create_session` (`server/sessions/fleet_orchestrator.py`), which tries hosts in
  ranked order and records the winner. It is a fact about the session, never re-inferred at read
  time. The orchestrator's `_owns_record` gates recovery/rotation: a worker owns only rows stamped
  with its own host; the primary owns unstamped legacy rows, its own, the UDS default, and orphaned
  rows whose stamp has left the registry (PR #856).
- **Host scoring / visibility** (`server/sessions/fleet.py`, `server/sessions/host_probe.py`,
  `server/api/fleet.py`): `probe_loop` (~30s) writes each enabled host's health, SHA, protocol
  version, load, free memory, and active-session count into the registry; failure is recorded
  explicitly (`healthy=False`) rather than left stale. `score_host`/`rank_hosts` weight headroom,
  memory, and load, and a protocol-version mismatch marks a host unhealthy so the edge never
  dispatches into an incompatible daemon. Host registry + health probing + Add Host UI shipped in
  PRs #720/#721; multi-host runner dispatch + onboarding in PR #807.
- **Single-primary lease** (`server/sessions/primary_lease.py`): a single `daemon_leases` row
  (`name='primary'`) makes "exactly one primary" a checked invariant. `acquire()` wins when the row
  is absent, held by the same host (crash-restart re-acquires instantly), or held by a host whose
  heartbeat went stale (fenced takeover); a live foreign lease makes the caller refuse to boot.
  `renew()` is the heartbeat (a failed renew is the runtime split-brain signal); `release()` hands
  the lease back on clean shutdown. TTL default 300s. Introduced with PR #856; the watchdog +
  split-brain guard were made fleet-aware in PR #844.
- **Fleet-mirror eviction**: the primary holds a `_records` mirror of every fleet session for the
  dashboard and global automation. When a session is deleted/archived on its owning worker host, the
  primary's mirror goes stale and drives autopilot to propose against a vanished row (every
  automation insert then fails the session foreign key until restart). `_reconcile_mirror_once`
  evicts foreign records whose DB row has vanished or gone inactive, two-strike so a mid-creation
  worker session is never dropped (PRs #896, #900).
- **Per-host daemon upgrade** (`server/sessions/host_upgrade.py`, `POST
  /api/fleet/hosts/{name}/upgrade`, PR #884): deploys only touch the edge, so a remote runner's
  checkout goes stale (rocky sat on a two-day-old SHA). This runs fetch → ff-only merge → editable
  pip → `sync` → `sudo -n systemctl restart` over SSH per registry host on demand, then verifies the
  daemon's `/info` reports the target SHA. Progress is polled via `GET /api/fleet/hosts`.
- **Worker-role separation**: `CCSWITCH_DAEMON_ROLE` selects `primary` (all background loops:
  poller, rotation monitor, autopilot, deploy watcher, lease heartbeat) vs `worker` (serves
  dispatched sessions only, never refreshes tokens). See KNOWN GAPS.

**Known gaps (open issues)** —
- **#840 — worker role incomplete.** `orchestrator.start()` still launches all fleet-global
  automation loops on every daemon regardless of role, which blocks a clean worker/primary split.
  OPEN.
- **#816 — crash-loop escalation gap.** A crash-looping remote daemon escalates to nobody:
  `Restart=always` spins, the watchdog defers, and production stays down with no alert. OPEN.
- **#831 — edge polling load.** The edge polls the rocky daemon ~40 req/min across 8+ RPC methods;
  it should consolidate onto the events WS or back off. OPEN.

**Evidence** —
- `server/sessions/fleet_orchestrator.py` — ranked dispatch + host stamping (`create_session`, `_stamp_host`).
- `server/sessions/fleet.py` — `score_host`/`rank_hosts` capacity-aware scoring.
- `server/sessions/primary_lease.py` — `daemon_leases` acquire/renew/release, fenced takeover.
- `server/sessions/host_probe.py` — `probe_loop` health/capacity registry writes.
- `server/sessions/host_upgrade.py` + `server/api/fleet.py` — per-host SSH daemon upgrade endpoint.
- `server/ipc/orchestrator_rpc.py` — cross-host RPC surface.
- PRs: #720/#721 (host registry + Add Host UI), #807 (runner dispatch + onboarding), #856 (host
  partitioning + single-primary lease), #884 (per-host upgrade), #896/#900 (mirror eviction), #844
  (fleet-aware watchdog/split-brain guard).
- Open issues: #840 (worker role), #816 (crash-loop escalation), #831 (edge RPC polling).
