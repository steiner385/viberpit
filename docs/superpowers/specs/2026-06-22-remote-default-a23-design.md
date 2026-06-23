# Session daemon — A2.3: remote by default + deploy classification + cutover

**Date:** 2026-06-22
**Status:** Approved (design; self-driven under an autonomous roadmap directive)
**Phase:** A2.3 — the capstone. Builds on A4 (the daemon is a runnable, handshake-speaking process)
and A2.2b-2b (remote facade). Flips the edge to use the daemon by default, classifies deploys so
edge-only changes no longer restart the daemon (sessions survive), and performs the live cutover.

## Why

After A4 the daemon can run, but the edge still owns sessions in-process: every deploy restarts the
edge and respawns every session at once (the documented restart-storm — memory
`ccswitch-restart-storm-load`). A2.3 makes the edge run **against the daemon by default**, so the
daemon owns sessions and an edge restart no longer kills them. The deploy then classifies changes:
edge-only changes restart only the freely-restartable edge (no idle gate, no storm); daemon changes
restart the daemon (idle-gated). The whole thing is built **fallback-first** so it is safe and
reversible.

## Design

### 1. Remote-by-default with in-process fallback (`server/app.py` `create_app`)

Today: remote only when `CCSWITCH_SESSIOND_SOCKET` is explicitly set. A2.3 adds **auto-detection of
a daemon at the well-known path**, with a guaranteed in-process fallback:

- An explicit `CCSWITCH_SESSIOND_SOCKET` still forces remote unconditionally (preserves existing
  tests; trusts the operator).
- Otherwise (no env, no injected orchestrator) the edge **probes** `DEFAULT_SOCKET_PATH`
  (`/run/ccswitch/sessiond.sock`) synchronously: a short-timeout sync `GET /info` over the UDS.
  - If a daemon answers AND `info["protocol"] == PROTOCOL_VERSION` → remote mode at that path.
  - On ANY failure (no socket, connect refused, timeout, version mismatch) → **in-process**
    (today's behavior), with a one-line log at INFO (auto-detect miss) or WARNING (version mismatch).
- An injected orchestrator still forces in-process (tests).

`_daemon_compatible(path) -> bool` (a new module-level helper in `create_app`'s module): uses a
short-lived **sync** `httpx.Client(transport=httpx.HTTPTransport(uds=path), timeout=1.0)` to
`GET /info`; returns True iff reachable and protocol-compatible; swallows all errors to False. Sync
is required because `create_app` is synchronous; the probe is a single fast local UDS call.

**This is the safety property:** deploying A2.3 changes NOTHING until a compatible daemon is actually
listening at the well-known path. With no daemon enabled, every edge build auto-detects nothing and
runs in-process — byte-identical to today. Activation (enabling the daemon) is what flips it, and
stopping the daemon reverts it on the next edge restart.

### 2. Version-handshake enforcement

The probe IS the enforcement on the default path: an incompatible daemon → fallback to in-process +
a loud warning (the edge never drives a daemon it can't speak to). The handshake mechanism
(`/info`, `protocol_compatible`) shipped in A4; A2.3 wires it into the default-path decision.
`app.state.daemon_protocol` records the detected protocol (or None) for `/healthz` visibility.

### 3. healthz remains honest

`/healthz` already reports `daemon: ok|unavailable` in remote mode (A2.2b-2b). No new field is
required — a version-incompatible daemon is never selected (we fell back to in-process, so healthz
shows no `daemon` field, correctly indicating in-process). The active-count + `daemon:ok` already
prove the daemon link.

### 4. Deploy classification (`scripts/auto-deploy.sh`)

The point of the cutover: edge restarts are cheap (sessions survive in the daemon), daemon restarts
are expensive (kill sessions → lazy respawn). So classify the change in the deploy range
`$LOCAL..$REMOTE`:

- `daemon_changed` ⇐ `git diff --name-only $LOCAL $REMOTE` matches `^(server/ipc/|server/sessions/|src/ccswitch/)`
  (the orchestrator, session logic, IPC, and the agent-SDK glue the daemon runs). Anything else
  (web/, server/api/, server/app.py, server/bridge/, server/auth/, …) is edge-only.
- `remote_active` ⇐ the running edge `/healthz` includes `"daemon":"ok"` (it is in remote mode).

Gate change (minimal, guarded):
- The existing busy/idle/VNC defer gate now applies **only when a session-disrupting restart will
  happen**: `daemon_changed` is true OR `remote_active` is false (in-process edge — a restart still
  kills sessions). When `remote_active` AND NOT `daemon_changed`, the edge restart is
  non-disruptive → **skip the idle gate** (deploy immediately even if busy — no storm).
- After fast-forward + build: always restart the edge (+ existing health gate). Then, **only if
  `daemon_changed` and the daemon unit is enabled**, restart the daemon (the A4
  `is-enabled`-guarded block, now additionally gated on `daemon_changed`).
- All new logic is guarded so a host where the daemon is NOT enabled (or the edge is in-process)
  behaves exactly as today. Test seams (`CCSWITCH_DEPLOY_*`) extended for `daemon_changed` and
  `remote_active`.

### 5. The cutover (operational, performed in deploy-verify — reversible)

Once the code is merged + deployed (dormant, in-process), activate:
1. Install the daemon unit (`cp deploy/ccswitch-daemon.service /etc/systemd/system/`, customized to
   the **deploy worktree** path — matching how the edge unit runs — `daemon-reload`).
2. `systemctl enable --now ccswitch-daemon.service`; verify `/info`+`/healthz` over the socket report
   the deployed SHA + `protocol:1`.
3. Restart the edge once (`systemctl restart ccswitch.service`). On startup it probes the daemon →
   finds it → runs **remote**. Its in-process orchestrator is gone; the daemon owns sessions. This is
   a **one-time** session disruption (the edge's current PTYs die; the daemon starts fresh, sessions
   respawn lazily on next attach). Verify edge `/healthz` shows `daemon:"ok"` and `activeSessions`
   reflects the daemon.
4. Confirm a session round-trips through the daemon (list/attach) and that a subsequent edge restart
   does NOT drop sessions (the payoff).

**Reversibility:** `systemctl disable --now ccswitch-daemon.service` + restart the edge → the probe
misses → the edge reverts to in-process. The well-known socket on tmpfs is gone on daemon stop.

## Error handling

- Daemon unreachable/incompatible at probe → in-process fallback (logged). No user-facing failure.
- Daemon dies while the edge runs remote → existing `DaemonUnavailable`→503 / WS 1013 (A2.2b-2b) +
  healthz `daemon:"unavailable"`; the daemon's `Restart=on-failure` brings it back; the edge's
  reconnecting IPC client (A1) + replay (A3) resume streams.
- Deploy classification failure (can't determine) → fail safe: treat as `daemon_changed` (idle-gate +
  restart both) so we never skip a needed gate.

## Testing

- `_daemon_compatible`: a live daemon-on-UDS → True; a dead path → False; a daemon whose `/info`
  returns an incompatible protocol → False (mock the probe response).
- `create_app` default path: with a live compatible daemon at the probed path → `remote_mode` True;
  with none → in-process; explicit `CCSWITCH_SESSIOND_SOCKET` still → remote unconditionally (no
  probe); injected orchestrator → in-process. (Use a real daemon-on-UDS or patch `_daemon_compatible`.)
- The in-process default (no daemon) is byte-identical: existing app/healthz/security tests stay green.
- `auto-deploy.sh`: classification picks daemon-vs-edge correctly (seed `CCSWITCH_DEPLOY_*` seams);
  edge-only + remote_active skips the idle gate; daemon_changed keeps it; daemon restart only on
  daemon_changed + is-enabled; a host without the daemon is unaffected. Shellcheck-clean.
- Regression: the remote-mode edge suite (A2.2b-2b) still passes with the explicit-flag path.

## Out of scope

- Multi-version rolling protocol negotiation (we use a single int + fail-closed fallback).
- Running two daemons / HA. The daemon is a singleton on the host.
- Any change to `server/bridge/ws.py` or the orchestrator internals.
