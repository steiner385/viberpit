# Session daemon — A2.2b-2a: full remote facade parity

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** A2.2b-2a. A2.2b-2 (edge cutover) is split A2.2b-2a (this — complete the remote facade
to full parity + RemoteDriver enrichment, dormant) → A2.2b-2b (the `CCSWITCH_SESSIOND_SOCKET`
flag, lifespan/healthz remote handling, remote-mode suite proof). Builds on A2.1, A2.2a, A2.2b-1.

## Why

After A2.2b-1, `RemoteOrchestrator` implements every facade method except the 4 edge-integration
helpers (`usage`, `pending_permissions`, `subscribe_events`, `unsubscribe_events`), and the
`RemoteDriver` is not yet fully ws.py-compatible. A2.2b-2a closes both gaps so the remote facade
is a drop-in for the in-process one — dormant, proven by integration tests — before A2.2b-2b
flips the flag.

## Design

### 1. RemoteDriver enrichment (ws.py compatibility)

`ws.py` uses the driver three ways: `async for e in driver.events(): … e.to_wire()`, and —
**synchronously, once at attach** — `driver.current_status()` (→str) and `driver.queued()`
(→list[str]). To keep `ws.py` untouched, the remote-ness is confined to the driver:
- The daemon `ensure_driver` RPC returns, when found, `{found, status, queued}` where
  `status = driver.current_status()` and `queued = driver.queued()`.
- `RemoteDriver(client, session_id, status, queued)` returns `current_status()`/`queued()`
  synchronously from that attach-time snapshot (matching ws.py's sync use).
- `RemoteDriver.events()` wraps each subscribed dict in a `_WireEvent` whose `.to_wire()` returns
  the dict — so `ws.py`'s `e.to_wire()` call works unchanged (the daemon pump already serialized).

### 2. `usage` (Usage serialization)

`poller.get(name)` returns `ccswitch.oauth.Usage | None` — a dataclass with two `datetime`
fields, not JSON-safe. Add `Usage.to_dict()/from_dict()` to `src/ccswitch/oauth.py`
(`session_pct`, `weekly_pct` floats, `extra_usage_enabled` bool pass through;
`session_resets_at`/`weekly_resets_at` ISO-encode like `SessionRecord.created_at`). Daemon
handler: `{"usage": u.to_dict() if u else None}`; `RemoteOrchestrator.usage(name)` returns
`Usage.from_dict(r["usage"]) if r["usage"] else None` — so the accounts API receives the same
`Usage` object in both modes.

### 3. `pending_permissions`

Daemon handler returns `{"result": orch._registry.pending(session_id)}` (already `list[dict]`,
JSON-safe — the daemon owns its orchestrator, so reaching `_registry` there is fine); the proxy
reads `call()` directly. Trivial.

### 4. `subscribe_events` / `unsubscribe_events` (global lifecycle bus)

- **Daemon:** one global pump task, started in `register_orchestrator_rpc`: drain
  `orch.subscribe_events()` → `await app.state.publish("_global", ev)` (events are already dicts).
- **Proxy:** `RemoteOrchestrator.subscribe_events()` returns an `asyncio.Queue`, filled by a
  background task that consumes `client.subscribe("_global")` and `put_nowait`s each event; it
  tracks the task keyed by the queue. `unsubscribe_events(q)` cancels that task. ws.py's
  `event_q = await sessions.subscribe_events()` + `await event_q.get()` drain + cleanup work
  unchanged.
- Same at-attach gap as per-session streams (A1 `publish` drops with no subscriber; no replay) —
  benign for lifecycle events, A3 adds replay.

### 5. `active_session_count`

Add as an async method on both facades: `InProcessOrchestratorAdapter.active_session_count()`
delegates to the wrapped orchestrator; `RemoteOrchestrator.active_session_count()` calls a daemon
handler `{"result": orch.active_session_count()}`. Needed so A2.2b-2b's `healthz` (and thus the
Phase-B deploy idle-gate) reflects the daemon's active sessions in remote mode.

## Data flow (remote attach with the enriched driver)

```
ws.py: driver = await sessions.ensure_driver(id)  ──RPC──▶ daemon: {found, status, queued} (+ start pump)
       state = driver.current_status()  (snapshot, sync)
       async for e in driver.events():  ──subscribe(id)──▶ wire dicts ─▶ _WireEvent ─▶ e.to_wire() ─▶ browser
ws.py: event_q = await sessions.subscribe_events()  ──subscribe("_global")──▶ daemon global pump ─▶ queue
```

## Error handling

`DaemonUnavailable` / `DaemonError` propagate from proxy calls as today. The global-bus pump and
the per-queue subscriber tasks survive transient stream drops via A1's reconnect; `aclose()` /
`unsubscribe_events` cancel them cleanly.

## Testing

Integration tests over an in-process daemon-on-UDS (real orchestrator; heavy paths mock-patched):
- `usage` round-trips a `Usage` (datetimes + None); `Usage.to_dict/from_dict` unit tests.
- `pending_permissions` returns the registry dicts.
- `active_session_count` returns the int.
- `subscribe_events`: a `publish("_global")` event is delivered through the returned queue;
  `unsubscribe_events(q)` stops further delivery.
- `ensure_driver` returns a `RemoteDriver` whose `current_status()`/`queued()` equal the daemon
  snapshot and whose `.events()` yields `_WireEvent`s with `.to_wire()` returning the dict.

## Out of scope (A2.2b-2b and later)

- The `app.py` `CCSWITCH_SESSIOND_SOCKET` flag; remote-mode lifespan (skip `rehydrate`/`start`/
  `stop`); async `healthz` busy-count; the remote-mode REST+WS suite run.
- Event replay across the attach/subscribe gap (A3).
- Any change to `server/app.py`, `server/bridge/ws.py`, or the real `SessionOrchestrator`.
