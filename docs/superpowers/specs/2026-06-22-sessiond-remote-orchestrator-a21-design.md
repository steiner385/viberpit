# Session daemon — A2.1: RemoteOrchestrator vertical slice

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** A2.1 of the session-daemon effort. A2 (move the orchestrator into the daemon)
is sliced A2.1 (this — a thin end-to-end vertical) → A2.2 (the remaining orchestrator
methods; full REST+WS suites green in remote mode) → A2.3 (flip default to remote + WAL
cutover; systemd lands in A4). Builds on A1 (`server/ipc/` transport spine).

## Why

A1 proved the edge↔daemon transport with a throwaway handler. A2.1 proves the *real*
model end-to-end through one complete flow — create a session, list/get it, send a prompt,
and stream its live events — with the actual `SessionOrchestrator` running inside the
daemon and the edge driving it through a `RemoteOrchestrator` proxy. Doing a thin vertical
first de-risks the three hard parts together (record serialization, the RPC surface, and
the streaming refactor) before A2.2 fills in breadth.

Everything stays behind a config flag: with `CCSWITCH_SESSIOND_SOCKET` unset the edge uses
today's in-process orchestrator unchanged (zero production change); set, it uses the remote
proxy. The flag flips to default-on only in A2.3.

## Scope (the vertical)

Exactly these orchestrator entry points, end-to-end through the daemon:
`list_sessions`, `get_session`, `create_session`, `send_prompt`, and live event streaming
for an attached session (`ensure_driver` + the per-session event stream). No other methods
(A2.2), no production cutover (A2.3), no event replay (A3).

## Design

### Components

- **`SessionRecord` serialization** (`server/sessions/types.py`): add `to_dict()` /
  `from_dict()` (the only non-JSON field is `created_at: datetime` → ISO-8601 string;
  `None` stays `None`). Round-trippable.
- **Daemon orchestrator host** (`server/ipc/orchestrator_rpc.py`, new): given a real
  `SessionOrchestrator`, registers RPC handlers for the slice's methods on the daemon app
  (via A1's `register_rpc`) and the event-pump wiring (below). Keeps the daemon transport
  (`daemon.py`) free of orchestrator knowledge.
- **`RemoteOrchestrator`** (`server/sessions/remote.py`): implements the slice's method
  names with the **same signatures and return types** the API/WS layers already call,
  backed by `DaemonClient`. `list_sessions()/get_session()` rehydrate `SessionRecord`s via
  `from_dict`; `create_session()` returns a rehydrated `SessionRecord`; `send_prompt()` is
  fire-and-forget over RPC; attaching a session subscribes to its event stream.
- **Edge selection** (`server/app.py`): if `CCSWITCH_SESSIOND_SOCKET` is set, build a
  `RemoteOrchestrator(DaemonClient(socket))` instead of the in-process `SessionOrchestrator`;
  store it on `app.state.orchestrator` exactly as today. Nothing else in the API/WS layer
  changes.

### The streaming refactor (crux)

Today the WS bridge holds a `SessionDriver` and iterates `driver.events()`. A remote edge
cannot hold a driver, so:

- **Daemon side:** when `ensure_driver(session_id)` runs, the daemon starts (idempotently,
  one per session) a pump task: `async for e in driver.events(): await app.state.publish(session_id, e.to_wire())`, feeding A1's event bus keyed by `session_id`. The global
  lifecycle bus (`subscribe_events`) pumps to a reserved stream id (`"_global"`).
- **Edge side:** the WS bridge calls `ensure_driver(session_id)` via RPC (returns an ack/bool,
  not a driver), then consumes `DaemonClient.subscribe(session_id)` for live frames. The same
  `to_wire()` dicts reach the browser as today.

In A2.1 the WS bridge is taught to take this remote path **only when the orchestrator is a
`RemoteOrchestrator`**; the in-process path is untouched. (A2.2 makes the remote WS path
cover permissions/questions/interrupt; A2.1 proves the live text-event stream.)

### Database

Enable SQLite **WAL** + a `busy_timeout` in `server/db._make_engine` (applied for every
connection; benign for the single-process default). The daemon's orchestrator writes the
`sessions` table; the edge keeps writing `audit_events`/`app_settings` directly. WAL gives
concurrent readers + serialized writers so the two processes coexist on the low-write DB.

### Error handling

- Any `RemoteOrchestrator` RPC raising `DaemonUnavailable` → the edge surfaces **HTTP 503**
  (daemon down) rather than a 500. A small dependency/util maps it at the API boundary.
- A dropped event stream resumes via A1's reconnect; a brief gap on edge/daemon restart is
  acceptable until A3 adds replay.
- `DaemonError` (a daemon-side handler exception) propagates as a 500 with the error type —
  same shape the in-process path would produce by raising.

## Data flow

```
browser ◀─WS─ edge WS bridge ──RPC create_session/list/get/send_prompt──▶ daemon ─▶ orchestrator ─▶ DB(sessions, WAL)
                              ──RPC ensure_driver(id)───────────────────▶ daemon ─▶ orch.ensure_driver → SessionDriver
                              ──subscribe(id) [A1] ─────────────────────▶ daemon pump: driver.events() → publish(id)
        ◀──────────────────── event frames [A1] ◀──────────────────────────────────┘
```

## Testing

- **Unit:** `SessionRecord.to_dict()/from_dict()` round-trip, incl. `created_at` ISO encode
  and `None` fields.
- **Integration (the vertical):** spin the daemon in-process on a temp UDS hosting a real
  `SessionOrchestrator` with the fake SDK factory (the test fakes already used across
  `server/tests`); point a `RemoteOrchestrator` at it and assert end-to-end:
  `create_session` returns a record; `list_sessions`/`get_session` reflect it; `send_prompt`
  then `subscribe(session_id)` receives the streamed turn events; a `get_session` on an
  unknown id returns `None`.
- **Error path:** a `RemoteOrchestrator` call against a dead socket raises `DaemonUnavailable`,
  and the API boundary mapping turns it into a 503.
- Existing `server/tests` stay green in the default (in-process) mode — the flag is off, so
  no behavior changes for them.

## Out of scope

- The other ~18 orchestrator methods — archive/unarchive/permanent_delete/adopt/rotation/
  resolve_permission/answer_question/pending_questions/transcript*/cancel_queued/etc. (A2.2).
- Routing audit/settings/healthz through the daemon — they keep direct DB access under WAL.
- Flipping the default to remote, and managing the daemon as a real process (A2.3 / A4).
- Event replay across reconnects (A3).
- Mapping every remote failure mode beyond `DaemonUnavailable`→503 / `DaemonError`→500.
