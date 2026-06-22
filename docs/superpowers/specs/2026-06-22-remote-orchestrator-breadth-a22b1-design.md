# Session daemon — A2.2b-1: complete RemoteOrchestrator + daemon handlers

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** A2.2b-1. A2.2b (remote breadth + edge cutover) is split A2.2b-1 (this — complete the
remote method surface + the RemoteDriver streaming shim, dormant) → A2.2b-2 (app.py flag,
lifespan/healthz remote handling, global lifecycle-bus pump + subscribe_events, remote-mode
suite proof). Builds on A2.1 (RemoteOrchestrator vertical) and A2.2a (the async facade).

## Why

A2.1 gave `RemoteOrchestrator` 5 of ~27 facade methods. For the edge to run against the daemon
(A2.2b-2) the proxy must implement the same async interface the in-process facade exposes.
A2.2b-1 fills in the **18 session-lifecycle / control methods** + their daemon RPC handlers,
and resolves the streaming integration with a **`RemoteDriver` shim** so the WS bridge needs no
changes. The 4 remaining facade methods are **edge-integration helpers** — `usage`,
`pending_permissions`, `subscribe_events`, `unsubscribe_events` — consumed only when the edge
runs in remote mode, so they land in A2.2b-2 alongside the wiring that uses them. A2.2b-1 is
dormant (nothing wired into the live edge) and is proven by an integration test, de-risking the
breadth before A2.2b-2's cutover.

## Design

### RemoteDriver shim (the streaming integration)

The WS bridge does `driver = await sessions.ensure_driver(id)` then iterates `driver.events()`.
In-process, `ensure_driver` returns the real `SessionDriver`. Remote, `RemoteOrchestrator.ensure_driver` returns a **`RemoteDriver`** (`server/sessions/remote.py`):

```
class RemoteDriver:
    def __init__(self, client, session_id): ...
    async def events(self):
        async for wire in self._client.subscribe(self._session_id):
            yield wire
```

`ensure_driver` calls the daemon `ensure_driver` RPC (which starts the per-session event
pump, as in A1) and returns `RemoteDriver(client, session_id)` if found, else `None` — exactly
the `.events()`-plus-non-`None` shape `ws.py` already consumes. **`ws.py` is untouched.**

### Daemon RPC handlers (`server/ipc/orchestrator_rpc.py`)

Add a handler per remaining method, each calling `orch.<m>(**params)` and returning a
method-appropriate JSON shape (consistent with A2.1's keyed envelopes):
- **Simple returns** → `{"result": <value>}` where value is JSON-safe (bool / null / str / list
  of dicts): `archive_session`, `permanent_delete`, `rename_session`, `interrupt`,
  `set_permission_mode`, `cancel_queued`, `resolve_permission`, `answer_question`,
  `transcript_path`, `pending_questions`, `pop_pending_initial_prompt`,
  `pop_pending_initial_attachment_ids`, `command_catalog`, `run_command`, `list_archived`.
- **SessionRecord return** → `{"session": rec.to_dict() | None}`: `unarchive`.
- **Quirks:** `adopt_session` → `{"record": rec.to_dict(), "metadata": {...}}` (unpacks the
  `(SessionRecord, dict)` tuple); `transcript_events` → `{"events": [e.to_wire() for e in …]}`
  (the orchestrator returns `Event` objects).

Positional vs keyword: handlers pass `params` through by keyword (`orch.<m>(**params)`); the
proxy sends named params matching each method's signature.

### RemoteOrchestrator methods (`server/sessions/remote.py`)

Add the 22 matching async methods: each `await self._client.call("<m>", **params)` and
deserializes — `SessionRecord.from_dict` for `unarchive`; `(SessionRecord.from_dict(record),
metadata)` for `adopt_session`; the raw `events` list for `transcript_events`; `r["result"]`
for the simple ones. Signatures mirror the orchestrator/facade methods.

### Deferred to A2.2b-2 (the 4 edge-integration helpers)

These are consumed only when the edge runs in remote mode, so they land with that wiring:
- `subscribe_events` / `unsubscribe_events` — the global lifecycle bus; needs the daemon to pump
  `orch.subscribe_events()` → `publish("_global")` and the proxy to adapt that stream to a queue.
- `pending_permissions` — `orch._registry.pending(session_id)` already returns `list[dict]`
  (JSON-safe), so its handler is trivial, but it's only used by `ws.py`'s attach path under
  remote mode (A2.2b-2).
- `usage` — `orch.poller.get(name)`; consumed by the accounts API only under remote mode. (Its
  payload's exact wire form is pinned in A2.2b-2.)

After A2.2b-1, `RemoteOrchestrator` implements every facade method except those four.

## Known limitation (A3 resolves)

The daemon pump starts on `ensure_driver`; the edge subscribes a beat later via
`RemoteDriver.events()`. A1's `publish` drops frames with no current subscriber, so the brief
gap has no replay. Benign in practice — a session is idle at attach, and events arrive on the
next prompt, after the subscription is established. A3 adds replay/subscription-driven pumping.
The integration test orders subscribe-before-events to stay deterministic.

## Data flow (remote attach + stream)

```
edge ws.py: driver = await sessions.ensure_driver(id)  ──RPC ensure_driver──▶ daemon: orch.ensure_driver + start pump(driver.events()→publish(id))
            (driver is a RemoteDriver)
            async for e in driver.events():  ──subscribe(id) [A1]──▶ daemon publish(id) ─▶ wire frames ─▶ RemoteDriver yields ─▶ out_q ─▶ browser
```

## Error handling

- `DaemonError` (a daemon-side handler raised) propagates from the proxy as today (A2.1) — the
  RPC route wraps the handler exception into the error envelope; the proxy raises `DaemonError`.
- `DaemonUnavailable` propagates from any proxy call (A1). Mapping it to API responses is
  A2.2b-2 (edge wiring).
- No new error handling in `ws.py` (untouched).

## Testing

A remote-facade integration test over an in-process daemon-on-UDS (real `SessionOrchestrator`;
the heavy `create_session`/`adopt_session`/`ensure_driver` paths mock-patched, as A2.1 did),
asserting each method group round-trips:
- bool/None/str/list returns (`archive_session`/`permanent_delete`/`rename_session`/`interrupt`/
  `set_permission_mode`/`cancel_queued`/`resolve_permission`/`answer_question`/`transcript_path`/
  `pending_questions`/`pop_*`/`command_catalog`/`run_command`/`list_archived`);
- `unarchive` → `SessionRecord` (or None);
- `adopt_session` → `(SessionRecord, metadata-dict)`;
- `transcript_events` → list of wire dicts;
- `ensure_driver` → a `RemoteDriver` whose `.events()` streams the pumped frames (subscribe
  before triggering, to avoid the at-attach gap in the test).
- A `RemoteDriver.events()` unit test (wraps `client.subscribe`, yields wire dicts).

## Out of scope (A2.2b-2 and later)

- The `app.py` `CCSWITCH_SESSIOND_SOCKET` flag; remote-mode lifespan (skip `start`/`stop`/
  `rehydrate`) and healthz (active-count over RPC).
- The global lifecycle-bus pump + `subscribe_events`/`unsubscribe_events` over RPC.
- Running the real REST+WS suites in remote mode; mapping `DaemonUnavailable`→503.
- Event replay across the attach gap / reconnect (A3).
- Any change to `server/app.py`, `server/bridge/ws.py`, or the real `SessionOrchestrator`.
