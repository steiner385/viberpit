# Session daemon — A1: IPC spine + event streaming

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** A1 of the session-daemon effort (A1 spine → A2 move orchestrator + DB ownership →
A3 reconnect/replay → A4 deploy/systemd). This spec covers **A1 only**.

## Why

To let live `claude` sessions survive a web-server restart, the session-owning code must
live in a long-lived process (`ccswitch-sessiond`) separate from the frequently-deployed
web edge. A1 builds and proves the **transport** between the two — unary RPC plus
bidirectional event streaming over a Unix-domain socket — in isolation, with a throwaway
demo handler. No orchestrator logic, no DB, no real sessions move yet (that is A2). The
spine is the deliverable; the demo handler is scaffolding that later tasks delete.

De-risking the transport first is deliberate: streaming, backpressure, and reconnect are
the hard, novel parts. Proving them against a trivial handler avoids entangling transport
bugs with the orchestrator move.

## Transport decision

Reuse the existing stack rather than add gRPC/protobuf or hand-roll a socket protocol:

- The daemon is a **uvicorn app bound to a Unix-domain socket** (`--uds`), never a TCP port.
- **Unary RPC** = FastAPI routes; the edge calls them with `httpx.AsyncClient` over the
  socket (`httpx.AsyncHTTPTransport(uds=<path>)`).
- **Event streaming** = a single **WebSocket** (`/events`); the edge connects with
  `websockets` over the socket (`websockets.unix_connect`).
- Events stay the existing `to_wire()` JSON dicts — nothing to re-encode.

All three libraries (uvicorn, httpx, websockets) are already dependencies.

## Components

New package `server/ipc/`, one responsibility per file:

### `protocol.py`
Envelope shapes and (de)serialization helpers — no I/O.
- RPC request: `{"id": <str>, "method": <str>, "params": <dict>}`.
- RPC response: `{"id": <str>, "result": <json>}` or `{"id": <str>, "error": {"type": <str>, "message": <str>}}`.
- Event frame (daemon → edge): `{"type": "event", "stream": <stream_id>, "event": <dict>}`.
- Control frame (edge → daemon): `{"type": "subscribe", "stream": <stream_id>}` /
  `{"type": "unsubscribe", "stream": <stream_id>}`.
- A `stream_id` is an opaque string the subscriber chooses (in A2 it becomes the session id;
  in A1 the demo uses a fixed id).

### `daemon.py`
Builds the daemon FastAPI app (`create_daemon_app()`), independent of `server/app.py`.
- A **handler registry**: `register_rpc(name, fn)`; `POST /rpc/{method}` looks up the
  handler, awaits it with the JSON body as params, returns `{"result": …}` or
  `{"error": …}` (500 with the error envelope on handler exception).
- One **`GET /events` WebSocket**: accepts `subscribe`/`unsubscribe` control frames and
  fans matching event frames to that connection. Multiple subscriptions multiplex over the
  one socket, tagged by `stream`.
- An in-daemon **publish API** `publish(stream_id, event: dict)` that the (demo, later real)
  producers call; it fans to every WS subscribed to that `stream_id`.
- A1 demo handlers (deleted in A2): `ping() -> {"pong": true}`; `start_demo(stream, n)` —
  spawns a background task that publishes `n` `{"type":"tick","i":k}` events to `stream`;
  `echo(stream, text)` — publishes `{"type":"echo","text":text}` to `stream`.

### `client.py`
Edge-side `DaemonClient(socket_path)`:
- `async call(method, **params) -> dict` — unary RPC via httpx-over-UDS; raises
  `DaemonUnavailable` on connection failure, `DaemonError(type, message)` on an error envelope.
- `subscribe(stream_id) -> async iterator[dict]` — yields event dicts for that stream from a
  **single shared** `/events` WebSocket (lazily opened, shared across subscriptions).
  Each subscription has a bounded `asyncio.Queue` (default maxsize 1000); on overflow the
  oldest frame is dropped and a warning logged (A3 adds replay).
- A background reader task demuxes incoming frames by `stream` into subscriber queues.
- **Auto-reconnect:** if the WS drops, the reader reconnects with capped exponential backoff
  and re-sends `subscribe` for every active `stream_id`; a sentinel is not surfaced to
  consumers in A1 (they simply see the stream resume).

### entrypoint
`server/ipc/__main__.py` exposes `serve(socket_path: str)` (and a `python -m server.ipc`
CLI) that runs `create_daemon_app()` via `uvicorn` bound to `--uds socket_path`. Tests call
a helper that runs an in-process `uvicorn.Server` on a temp socket path rather than a
subprocess, for speed and clean teardown.

## Data flow

```
edge                                   ccswitch-sessiond (uvicorn --uds <sock>)
 DaemonClient.call("ping")  ─httpx──▶  POST /rpc/ping ──▶ registry["ping"]() ──▶ {"result":…}
 DaemonClient.subscribe(s)  ─ws─────▶  WS /events  ◀── subscribe{stream:s}
 (demo) call("start_demo")  ─httpx──▶  POST /rpc/start_demo ──▶ publish(s, tick)×n
        async for ev ◀──────ws frames─ {"type":"event","stream":s,"event":{…}} ◀── publish
```

## Trust / security

The socket is created with `0600` permissions, owned by the service user; the local
filesystem permission is the entire trust boundary. The daemon binds **only** the UDS and
never a TCP port, so no app-level auth is added between edge and daemon. (When A4 wires
systemd, the socket lives under a service-owned runtime dir.)

## Error handling

- **Daemon down** on a unary call → `DaemonClient.call` raises `DaemonUnavailable` (A2 maps
  this to a 503 at the API edge; A1 just defines the exception).
- **Event-WS drop** → reader reconnects (capped backoff) and re-subscribes; the stream
  resumes. Gap *replay* (no lost events across the drop) is explicitly A3, not A1.
- **Backpressure** → bounded per-subscription queue, drop-oldest + a logged warning.
- **Handler exception** in the daemon → `POST /rpc/{method}` returns HTTP 500 with the
  `{"error": {...}}` envelope; the client raises `DaemonError`.

## Testing

`server/tests/test_ipc_spine.py`, against the real stack on a temp UDS (in-process uvicorn):
- `ping` RPC round-trips (`result == {"pong": true}`).
- `subscribe` + `start_demo` → the consumer receives all `n` tick events in order.
- `echo` command from the edge is published back into the subscribed stream.
- Forced WS drop (close the daemon-side connection) → the client reconnects and a subsequent
  `echo` is still received (stream resumed).
- `DaemonUnavailable` raised when calling with no daemon listening.
- Handler-exception path → `DaemonError` surfaced with the daemon's error type/message.

Backpressure (drop-oldest) is covered by a unit test on the subscriber queue in isolation.

## Out of scope (later A-phases / YAGNI)

- The orchestrator, real `SessionDriver`s, SDK clients, any DB access (A2).
- Mapping `DaemonUnavailable` to API responses; the `RemoteOrchestrator` proxy (A2).
- Gap **replay** across reconnects / per-session event buffering (A3).
- systemd units, socket runtime dir, deploy classification, version handshake (A4).
- TCP transport, multi-host, authentication beyond filesystem permissions.
