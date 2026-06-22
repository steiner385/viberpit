# Session daemon — A2.2b-2b: edge cutover to the daemon

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** A2.2b-2b. Final slice of A2.2b. Builds on A2.2b-2a (full remote facade parity, dormant).
After this slice the edge can run against the daemon behind one env flag; flipping the *default*
to remote is A2.3, daemon process management + systemd is A4.

## Why

A2.2b-2a brought `RemoteOrchestrator`/`RemoteDriver` to full parity with the in-process facade,
but nothing wires it into the live edge. A2.2b-2b is the cutover: a `CCSWITCH_SESSIOND_SOCKET`
flag that makes `create_app` build the remote facade instead of the in-process one, a lifespan
that defers session lifecycle to the daemon, a healthz that reads the busy-count over RPC, and a
`DaemonUnavailable → 503` mapping (deferred here from A2.2b-1). A focused remote-mode integration
test proves the edge runs end-to-end against an in-process daemon-on-UDS. Default (flag unset) is
byte-identical to today.

## Key finding that shapes the design

A coupling audit found that **nothing outside `server/app.py` reads `app.state.orchestrator`** —
every router (`server/api/*`) and the WS bridge (`server/bridge/ws.py`) already consume
`app.state.sessions` (the facade), which has full remote parity. The only seams are five lines in
`app.py`: lifespan's `rehydrate()`/`start()`/`stop()` and healthz's sync `active_session_count()`.

Separately, several routers do **direct DB access unrelated to the orchestrator**: attachments
(`Attachment` read/write), auth logout (`AppSession` update), settings (`AppSettings` singleton),
the WS bridge's audit-log writes, plus healthz's `Session` schema-check SELECT and the lifespan
orphan-attachment sweep. So the edge must keep its own DB access in remote mode; only the
*session/orchestrator lifecycle* moves to the daemon. This is the shared-DB (SQLite WAL +
`busy_timeout`) model the earlier slices established: in remote mode the daemon writes the
`Session` table while the edge writes `Attachment`/`AppSession`/`AppSettings`/audit — disjoint
table sets, no same-row write contention, writers serialized by `busy_timeout`.

## Design

### 1. Flag and facade wiring (`create_app`)

Read `socket = os.environ.get("CCSWITCH_SESSIOND_SOCKET")`.

- **Remote mode** — flag set AND no `orchestrator` argument injected (explicit injection still
  wins, so in-process tests are unaffected):
  - `app.state.sessions = RemoteOrchestrator(DaemonClient(socket))`
    (`from server.ipc.client import DaemonClient`, `from server.sessions.remote import RemoteOrchestrator`)
  - `app.state.orchestrator = None`
  - `app.state.remote_mode = True`
- **In-process** (default, flag unset, or `orchestrator` injected): unchanged — raw
  `SessionOrchestrator` + `InProcessOrchestratorAdapter`, `app.state.remote_mode = False`.

No router or `ws.py` registration changes: they already consume `app.state.sessions`.

### 2. Lifespan branching (`_lifespan`)

Branch on `app.state.remote_mode`.

- **Skip in remote mode** (the daemon owns these — the only three lines guarded by
  `if not app.state.remote_mode:`): `app.state.orchestrator.rehydrate()`,
  `app.state.orchestrator.start()`, and `app.state.orchestrator.stop()`.
- **Keep in both modes:** the F1 security/allowlist check; `init_db` (idempotent — the edge still
  owns the `Attachment`/`AppSession`/`AppSettings`/audit tables and the healthz schema check over
  the shared DB); reauth sweep + SIGTERM/SIGINT handlers + `reauth.shutdown()`; the
  orphan-attachment sweep.
- **Add on remote-mode shutdown:** `await app.state.sessions.aclose()` (closes the `DaemonClient`:
  cancels the WS reader + global-bus feeder tasks and the httpx client). The in-process facade has
  no `aclose`, so this is guarded by `remote_mode` (do NOT use `hasattr` — be explicit).

### 3. healthz unification + daemon-down handling

- Change the active-count read from `app.state.orchestrator.active_session_count()` (sync) to
  `await app.state.sessions.active_session_count()` (async). This works in **both** modes
  (`InProcessOrchestratorAdapter` and `RemoteOrchestrator` both implement it) and removes the last
  direct `app.state.orchestrator` reader outside lifespan.
- In remote mode, wrap that call: on `DaemonUnavailable`, set `activeSessions = 0`, `busy = false`,
  and add `"daemon": "unavailable"`; otherwise `"daemon": "ok"`.
- The `"daemon"` field is added **only in remote mode**, so the in-process healthz JSON
  (`{status, sha, busy, activeSessions}`) stays byte-identical — existing healthz tests untouched.
- The DB schema-check SELECT is unchanged and independent: still its own `try/except → 500` on
  schema drift, regardless of daemon state. (A daemon-down edge with a healthy DB is `200` +
  `daemon:"unavailable"`; a drifted DB is still `500`.)

### 4. `DaemonUnavailable → 503` (the deferred A2.2b-1 contract)

- **REST:** one `@app.exception_handler(DaemonUnavailable)` registered in `create_app`, returning
  `JSONResponse(status_code=503, {"detail": "session daemon unavailable"})`. Registered in both
  modes; harmless in-process (never raised there). `DaemonUnavailable` is imported from
  `server.ipc.client`.
- **WS:** the single minimal change to the otherwise-untouched `ws.py` — wrap the attach-boundary
  acquisition (the initial `ensure_driver` / `subscribe_events` calls) in
  `try/except DaemonUnavailable:` → `await websocket.close(code=1013)` ("try again later") and
  return. Only the attach handshake is guarded; mid-stream daemon drops remain A1's reconnect /
  A3's replay territory.

### 5. Healthz/deploy-gate semantics note

The Phase-B deploy idle-gate reads healthz `busy`. The 503-+-degraded choice means a transient
daemon blip reports `busy:false` (edge restart not blocked) rather than `500` (edge looks
unhealthy). Faithful "edge up, daemon down" signal. Reworking the gate to distinguish edge vs
daemon restarts is A4, not this slice.

## Data flow (remote-mode request)

```
browser ─▶ edge REST/WS route ─▶ app.state.sessions (RemoteOrchestrator)
                                   └─RPC over UDS─▶ daemon orchestrator ─▶ Session table (writes)
edge also ─▶ db_session() ─▶ Attachment / AppSession / AppSettings / audit (writes, shared WAL DB)
healthz   ─▶ await sessions.active_session_count()  ──RPC──▶ daemon  (DaemonUnavailable → degraded)
          └▶ db SELECT Session LIMIT 1              (schema drift → 500, independent)
DaemonUnavailable ─▶ REST 503 handler / WS close(1013)
```

## Error handling

- `DaemonUnavailable` from any facade RPC → REST `503` (exception handler) or WS `close(1013)` at
  attach; healthz degrades to `200` + `daemon:"unavailable"`.
- `DaemonError` (a daemon-side handler raised) propagates as today (A2.1) — not remapped here; it
  surfaces as a `500` unless a route already handles it. (Out of scope to reshape per-route error
  contracts in this slice.)
- DB schema drift → healthz `500`, unchanged.

## Testing

A focused remote-mode integration test (`server/tests/test_remote_mode_edge.py`). It does NOT
re-run the full in-process REST/WS suites in remote mode (those cover business logic already);
it proves the *wiring and the seams that changed*.

Harness: stand up an in-process daemon (`create_daemon_app` + `register_orchestrator_rpc(orch)`)
on a UDS via the existing `serve_daemon_uds`; share one `CCSWITCH_DB_URL` between the daemon's
orchestrator and the edge; set `CCSWITCH_SESSIOND_SOCKET` to that socket; build the edge via
`create_app()` (no injected orchestrator → remote mode). Because a UDS daemon needs a running loop
shared with the edge, drive the edge with `httpx` over `ASGITransport` (async test) rather than the
sync `TestClient`; the WS attach test uses the ASGI app's websocket route over the same loop. Heavy
`create_session`/PTY/SDK paths are mock-patched as the existing remote tests do (fake `sdk_factory`).

Cases:
- **healthz busy-count over RPC:** with N active sessions on the daemon, edge `/healthz` reports
  `activeSessions == N`, `busy == (N>0)`.
- **healthz daemon-down:** with the daemon stopped/unreachable, `/healthz` is `200` with
  `daemon:"unavailable"`, `busy:false` (DB still reachable).
- **REST round-trip:** create a session (daemon side), then through the edge: `GET /api/sessions`
  lists it, rename, archive — each a facade RPC round-trip.
- **WS attach + stream:** attach to a session over WS, receive an event pumped from the daemon.
- **DaemonUnavailable → 503:** point the edge at a dead socket (or stop the daemon), a REST call
  returns `503`.

Plus an `app.py`-level unit assertion that the flag selects `RemoteOrchestrator` and sets
`remote_mode`/`orchestrator=None`, and that the in-process path is unchanged when the flag is unset.

## Out of scope (A2.3 and later)

- Flipping the *default* to remote (A2.3).
- Daemon process management, systemd units, deploy classification (edge vs daemon restart), and a
  daemon/edge version handshake (A4).
- Event replay across the attach/subscribe gap and reconnect (A3).
- Re-running the full REST/WS suites parametrized over both modes (explicitly rejected — marginal
  signal for large, brittle effort).
- Reshaping per-route `DaemonError`/business-error contracts.
