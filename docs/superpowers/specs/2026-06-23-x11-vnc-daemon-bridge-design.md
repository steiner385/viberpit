# X11 / VNC daemon→edge bridge (Project A)

**Date:** 2026-06-23
**Status:** approved (autonomous execution under `/goal`)
**Bug:** virtual-browser "Browser" tab is stuck on *Reconnecting…* in production (remote/daemon mode).

## Problem

The session-daemon cutover (#146 daemon-as-process, #150 edge-runs-against-daemon-by-default)
moved the `SessionOrchestrator` into a separate daemon process. The orchestrator is the **only**
caller of `X11Manager.start_display`, so in remote mode the per-session Xvfb displays — and the
`X11Manager._specs` that tracks them — live in the **daemon** process.

But the **edge** (web/uvicorn) process serves every x11-touching HTTP/WS endpoint against its **own**
`X11Manager` (`app.state.x11`), which in remote mode nothing ever calls `start_display` on. So its
`_specs` is permanently empty:

- `/ws/vnc` → `x11.display_for(session)` is always `None` → socket closes `CLOSE_NO_DISPLAY (4404)`
  before any relay → noVNC reconnect loop. (No `vnc: relay failed` log because the relay never runs.)
- `/api/x11/metrics` → reports all-zero.
- `/api/sessions/{id}/browser/open` → always `409 no display for session` (human-takeover broken).
- `healthz` `vncAttached` → always `False` (this feeds the deploy busy-gate).

`RemoteOrchestrator` (`server/sessions/remote.py`) has **no** x11 bridge methods at all. Verified
live on `dobby`: 4 daemon-owned Xvfb (`ppid` = the `server.ipc` daemon), 0 `x11vnc`, no `6100–6111`
port bound, metrics `{xvfb_count:0,...}`. Present on `master` (3cf5ad2) too.

This is the same asymmetry class as #186 (`pr_for`): a feature wired only to the in-process
orchestrator silently dies once `RemoteOrchestrator` is the serving path.

## Goal & scope

Make the virtual browser work in remote mode, **cross-host-ready by construction** — the RFB byte
stream must flow through the daemon IPC transport, never assume the edge and daemon share `127.0.0.1`.

**In scope (Project A, this PR):** bridge the full x11 surface the edge uses (VNC relay, metrics,
open-browser, healthz vncAttached) across the daemon→edge boundary, riding the existing
WebSocket-over-IPC transport.

**Out of scope (Project B, separate PR + security review):** making the daemon socket itself
reachable cross-host — a TCP listener, TLS, and authentication to replace the same-host-only Unix
socket. Project A does **not** depend on Project B; it works same-host today and works unchanged the
day the transport becomes TCP. Tracked as a follow-up issue.

Also out of scope (separate small fix, filed as issue): the daemon/edge SIGKILL-on-shutdown
(`TimeoutStopSec` exceeded) that leaks Xvfb/x11vnc/chrome on every restart.

## Key insight

The daemon IPC is already **HTTP + WebSocket over a Unix socket** (uvicorn `uds=`, edge uses `httpx`
UDS transport + `websockets.unix_connect`). It already runs a multiplexed event WebSocket (`/events`).
So the "streaming channel" the RFB relay needs is **not** a new framing — it is one more daemon-side
WebSocket endpoint. Because it rides the IPC transport, it is automatically cross-host the moment
Project B swaps UDS→TCP. No `127.0.0.1` assumption anywhere in the VNC path.

## Architecture

```
browser  ──WS──▶  edge /ws/vnc  ──WS over IPC (UDS today, TCP later)──▶  daemon /vnc?session=ID
                                                                              │
                                                                   x11.attach(ID) → rfbport
                                                                              │
                                                                   TCP 127.0.0.1:rfbport  (daemon-LOCAL)
                                                                              │
                                                                          x11vnc ──▶ Xvfb
```

In-process (non-daemon) mode keeps today's behavior exactly: edge `/ws/vnc` relays straight to its
local `x11vnc`. The mode is selected at request time, not build time.

### Components

1. **Shared relay (`server/bridge/vnc_ws.py`)** — keep the existing `_relay` byte-pump and
   `_StreamAdapter` as the single source of truth. Add a `_WSAdapter` exposing `read()/write()/drain()`
   over a `websockets` client connection so the *same* `_relay` drives both shapes (TCP stream and
   WS). No second relay implementation.

2. **Daemon x11 surface (`server/ipc/x11_rpc.py`, new — mirrors `orchestrator_rpc.py`)** —
   `register_x11(app, x11)` registers:
   - WebSocket `/vnc` (query `session`): the relay-to-local-x11vnc logic that the edge runs today —
     `display_for` guard (close `4404` if absent) → `attach` → `open_connection("127.0.0.1", rfbport)`
     → `_relay` → `detach` in `finally`. This is a **move** of the existing edge logic into the daemon,
     not a copy (the edge's in-process branch keeps using the same shared functions).
   - RPC `x11_metrics(params) -> x11.metrics()`.
   - RPC `x11_open_browser(session_id, url) -> {ok, reason}`: `display_for` guard + `launch_browser`.
   Wired in `build_daemon()` via `app.state.x11 = orchestrator._x11; register_x11(app, app.state.x11)`.

3. **Edge `RemoteOrchestrator` (`server/sessions/remote.py`)** — add async facade methods following
   the #186 pattern: `x11_metrics()`, `x11_open_browser(session_id, url)`. Expose the daemon socket
   path (`self._client._sock`, via a small accessor) so the VNC router can `unix_connect` to it.

4. **Edge wiring (`server/app.py`)**
   - Remote mode: set `app.state.x11 = None` (don't build a misleading empty manager) and **skip**
     the local `sweep_stale` loop (the daemon owns sweeping its own displays).
   - `build_vnc_ws_router(get_x11, get_daemon_sock)`: if `get_daemon_sock()` returns a path →
     WS↔WS proxy to daemon `/vnc?session=…` (propagating the close code, so `4404` still surfaces);
     else → today's local relay.
   - `/api/x11/metrics`, `/api/sessions/{id}/browser/open`, and healthz `vncAttached`: route through
     the facade in remote mode (`await app.state.sessions.x11_metrics()` / `.x11_open_browser(...)`),
     local manager otherwise. Use an await-aware helper like #186's `_resolve_pr` so both modes share
     one call site. Daemon-unavailable degrades gracefully (metrics `{enabled:…}`-shaped, healthz
     `vncAttached=False`).

### Error handling

- No display on the daemon → daemon `/vnc` closes `4404`; edge proxy forwards that close code to the
  browser; noVNC shows its normal "no display" state instead of an infinite reconnect.
- Daemon unreachable (`DaemonUnavailable`) → metrics/open_browser/healthz degrade (no 500); VNC proxy
  closes the browser socket cleanly.
- The startup connect race (memory bug #1: relay connects before `x11vnc` binds) is fixed **here**,
  on the daemon side, with a short bounded readiness retry on `open_connection` after `ensure_vnc`
  (≈1s, 20×50ms) so the first attach doesn't bounce.

### Testing

- Daemon: `test_x11_rpc.py` — `/vnc` closes `4404` when no display; relays bytes to a fake local RFB
  server when a display exists; `x11_metrics`/`x11_open_browser` RPCs return daemon manager state.
- Edge remote proxy: extend `test_remote_mode_edge.py` / a new `test_vnc_ws_remote.py` — `/ws/vnc`
  in remote mode dials the daemon WS (fake daemon), relays both directions, propagates `4404`.
  Metrics + open_browser + healthz route through the facade in remote mode.
- In-process regression: existing `test_vnc_ws*.py` stay green (local path unchanged).
- Connect-race: a test where `x11vnc` binds late and the daemon relay still succeeds via retry.
- Full verification per repo norms: backend `ruff` + `mypy` + `pytest` (with worktree `PYTHONPATH`);
  web is unaffected (no frontend change — the existing `BrowserTab`/`vncClient` already speak `/ws/vnc`).

## Files touched

- `server/bridge/vnc_ws.py` — `_WSAdapter`; router takes `(get_x11, get_daemon_sock)`; remote proxy branch.
- `server/ipc/x11_rpc.py` — **new**: daemon `/vnc` WS + `x11_metrics`/`x11_open_browser` RPCs.
- `server/ipc/serve.py` — `build_daemon()` wires `app.state.x11` + `register_x11`.
- `server/sessions/remote.py` — `RemoteOrchestrator.x11_metrics/x11_open_browser` + socket accessor.
- `server/app.py` — remote: `x11=None`, skip sweep, router gets daemon sock, metrics/open/healthz via facade.
- `server/tests/` — new daemon + edge-remote tests; existing in-process tests unchanged.

## Non-goals / YAGNI

- No new wire framing — reuse the WebSocket transport.
- No TCP/TLS/auth (Project B).
- No frontend changes — the browser already talks to `/ws/vnc`.
- No change to `X11Manager` internals beyond what the daemon endpoint needs (it already does the work).
