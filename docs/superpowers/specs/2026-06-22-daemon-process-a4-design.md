# Session daemon — A4: the daemon as a managed process + version handshake

**Date:** 2026-06-22
**Status:** Approved (design; self-driven under an autonomous roadmap directive)
**Phase:** A4. Builds on A3 (replay) and A2.2b-2b (remote facade). Makes the daemon a real,
running, systemd-managed process with a version/protocol handshake — but does NOT yet flip the
edge default to remote (that, plus deploy classification, is A2.3). After A4 the daemon runs
alongside the unchanged in-process edge, proving it starts, serves, and survives.

## Why

The daemon today is only ever built inside tests (`serve_daemon_uds` + `register_orchestrator_rpc`).
`server/ipc/__main__.py` builds a *bare* daemon app with no orchestrator, no lifecycle, no DB. For
the edge to ever run against it (A2.3) the daemon must be a genuine process: own a real
`SessionOrchestrator`, init the shared DB, start the poller/rotation, serve on a stable socket, and
shut down cleanly. And because edge and daemon deploy as separate processes that can briefly run
mismatched code across a deploy, the edge needs a way to detect version/protocol skew. A4 delivers
both, plus the A3 review's buffer-lifecycle follow-up.

## Design

### 1. Well-known socket path (single source of truth)

A module-level constant `server/ipc/paths.py::DEFAULT_SOCKET_PATH = "/run/ccswitch/sessiond.sock"`,
with `sessiond_socket()` returning `os.environ.get("CCSWITCH_SESSIOND_SOCKET", DEFAULT_SOCKET_PATH)`.
Both the daemon entrypoint and (A2.3) the edge resolve the path through this one helper. `/run` is
tmpfs (cleared on boot — no stale socket), and `/run/ccswitch` is created by the systemd unit's
`RuntimeDirectory=ccswitch`. (No `/etc/ccswitch/ccswitch.env` edit needed — the path is in code.)

### 2. Daemon production entrypoint + lifespan (`server/ipc/__main__.py`, `server/ipc/serve.py`)

A `build_daemon(orchestrator=None)` factory (in a new `server/ipc/serve.py`, unit-testable without
serving) that mirrors the edge's construction:
- `app = create_daemon_app()`
- build the orchestrator if not injected: `SessionOrchestrator()`, then the x11 manager
  (`build_x11_manager(get_settings())`) wired via `orchestrator._x11 = x11` — same as
  `server/app.py:199-204`.
- `register_orchestrator_rpc(app, orchestrator)`
- attach a `/info` handshake route (§4) and an `/healthz` (status + sha) for the deploy gate.
- return `(app, orchestrator)`.

A daemon **lifespan** (an `@asynccontextmanager` set on the app, started by uvicorn) that:
- `await init_db(db_url=...)` (the shared `CCSWITCH_DB_URL`, same as the edge).
- `await orchestrator.rehydrate()` then `await orchestrator.start()` (the daemon now OWNS the
  poller + rotation monitor + the global pump — `register_orchestrator_rpc`'s pump already starts
  under the running loop).
- on shutdown: `await orchestrator.stop()`, cancel the global pump, close drivers.

`serve(socket_path)` runs uvicorn on the UDS **with** that lifespan (drop the test-only
`lifespan="off"`; that flag stays in the *test* harness, not production). `__main__` resolves the
socket via `sessiond_socket()` and calls `serve`.

**Restart-storm caution (memory `ccswitch-restart-storm-load`):** rehydrate is lazy (drivers
created on attach), so a daemon start does NOT respawn every session at once. Keep it lazy — do not
eagerly `ensure_driver` all sessions at startup.

### 3. Daemon `/healthz` for the deploy gate

The daemon serves `GET /healthz` → `{"status": "ok", "sha": running_git_sha(), "activeSessions": N}`
(reuse `server.app.running_git_sha`). Lets the deploy verify the daemon answers with the expected
SHA after a restart, exactly as the edge gate does.

### 4. Version / protocol handshake

- `server/ipc/protocol.py::PROTOCOL_VERSION = 1` — an integer bumped only when the edge↔daemon RPC
  or event-wire contract changes incompatibly.
- Daemon `GET /info` → `{"sha": running_git_sha(), "protocol": PROTOCOL_VERSION}`.
- `DaemonClient.info()` fetches it (a plain GET over the UDS httpx transport; raises
  `DaemonUnavailable` if unreachable, like `call`).
- Edge use (consumed in A2.3, but the client method + daemon route land here): the edge can compare
  `daemon.info()["protocol"]` against its own `PROTOCOL_VERSION`; a mismatch is logged loudly and
  surfaced in the edge `/healthz` as `daemon: "version_mismatch"`. A4 only adds the *mechanism* and
  a unit test; wiring it into the edge healthz/startup is A2.3 (when the edge actually connects).

### 5. systemd unit (`deploy/ccswitch-daemon.service`)

```
[Unit]
Description=ccswitch session daemon — owns live claude sessions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tony
RuntimeDirectory=ccswitch          # creates /run/ccswitch (0755), removed on stop
WorkingDirectory=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher
EnvironmentFile=/etc/ccswitch/ccswitch.env   # shared DB url, keys, PATH/HOME (read-only use)
ExecStart=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m server.ipc
Restart=on-failure
RestartSec=3
TimeoutStopSec=60
KillMode=control-group
MemoryMax=12G
TasksMax=512
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

The unit lives in `deploy/` (deploys via git). **A4 does NOT leave the daemon permanently enabled.**
Running the daemon *and* the in-process edge at the same time means two live `SessionOrchestrator`s
(two pollers, two rotation monitors) against the same accounts and DB — double-polling and competing
rotation. So A4's deploy-verify is a **smoke test only**: install the unit, `systemctl start` it,
confirm it answers `/info` + `/healthz` over the socket with the expected SHA, then `systemctl stop`
it (leaving it installed-but-inactive). **Permanent `enable --now` happens in A2.3**, atomically with
switching the edge to remote (so exactly one orchestrator — the daemon's — is ever active). The edge
unit is NOT modified in A4.

### 6. Deploy-script integration (`scripts/auto-deploy.sh`)

Minimal in A4: after the frontend build and before/with the edge restart, **also restart the daemon
if it is installed** — `systemctl is-enabled ccswitch-daemon.service` gate, then
`sudo systemctl restart ccswitch-daemon.service`, and verify the daemon `/healthz` reports the
expected SHA (same pattern as the edge gate, curling the UDS via `curl --unix-socket`). Because the
daemon holds no sessions until A2.3, an A4 daemon restart is harmless (no idle gate needed yet). The
busy/idle-gating *classification* — restart the edge without touching the daemon when only edge code
changed — is A2.3. Guard all of it behind `is-enabled` so a host without the daemon unit is
unaffected (and CI/tests, which never install it, see a no-op).

### 7. A3 follow-up (buffer lifecycle)

- `publish` logs a one-line `warning` when the ring buffer is at capacity and drops the oldest
  (the spec'd-but-missing truncation warning).
- On `permanent_delete` (and `archive`?) of a session, evict `app.state.buffers/seqs[session_id]`
  so the per-stream buffers don't accumulate for the daemon's lifetime. Hook it in the daemon RPC
  handler for `permanent_delete` (a small `app.state.evict(stream)` helper on the daemon app).

## Error handling

- Daemon fails to init DB / start orchestrator → uvicorn exits non-zero → systemd `Restart=on-failure`
  retries; the deploy `/healthz` gate catches a daemon that never comes healthy and (A2.3) the edge
  falls back to in-process.
- `/info` / `/healthz` unreachable → `DaemonUnavailable` (handshake) / deploy gate skip.
- Protocol mismatch → logged + surfaced (mechanism here, enforcement A2.3).

## Testing

- `build_daemon()` wires a real orchestrator + registers RPC + the `/info`/`/healthz` routes
  (construct it, assert `app.state.rpc` has the expected methods, `/info` returns
  `{sha, protocol}`, `/healthz` returns status ok) — no real serving needed; use Starlette TestClient
  on the daemon app (the daemon app has no auth).
- Daemon lifespan: over the in-process UDS harness (now WITH the lifespan), the orchestrator is
  started (poller running) and stopped cleanly; rehydrate is lazy (no driver spawned at startup).
- `DaemonClient.info()` round-trips `{sha, protocol}` over the UDS; raises `DaemonUnavailable` on a
  dead socket.
- `PROTOCOL_VERSION` mismatch helper: a small pure function `protocol_compatible(daemon_proto)`
  returns False when versions differ (unit test).
- Buffer eviction: publish to a stream, `permanent_delete` it via RPC → the buffer/seq entries are
  gone; truncation warning fires when the deque overflows (caplog).
- `deploy/ccswitch-daemon.service` parses (`systemd-analyze verify` if available, else a shellcheck-
  style key presence test) and `scripts/auto-deploy.sh`'s new daemon-restart block is guarded by
  `is-enabled` (exercise via the existing `CCSWITCH_DEPLOY_*` test seams + a new seam for the daemon
  restart, asserting it's a no-op when the unit isn't enabled).
- Regression: full ipc/remote/daemon/replay suites + the edge suites stay green; the edge is
  untouched in A4.

## Out of scope (A2.3)

- Flipping the edge default to remote (remote-with-in-process-fallback) and wiring the protocol
  handshake into the edge startup/healthz enforcement.
- Deploy classification (edge-only vs daemon change; idle-gating only daemon restarts since sessions
  then live in the daemon).
- The edge systemd unit ordering (`After=ccswitch-daemon.service`).
- Any change to `server/bridge/ws.py` or `server/app.py` (the edge) in A4.
