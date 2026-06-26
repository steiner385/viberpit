# Cross-host daemon transport (TCP over Tailscale) — design

**Status:** design / approved
**Issue:** #205 (follow-up to #204). P4 public-hosting hardening.
**Constitution:** Principle IV (security-first, exposure-conditional auth).

## Problem

The session daemon (`ccswitch-daemon.service`) and the web edge (`ccswitch.service`) talk over a
Unix-domain socket (`server/ipc/serve.py` binds `uds=`; `server/ipc/client.py` connects via
`AsyncHTTPTransport(uds=…)` + `websockets.unix_connect`). The UDS file permissions are the entire
trust boundary, and a UDS is same-host only. To run the daemon on a **different host** from the edge,
the daemon's listener and the edge's client must speak a network transport.

## Decisions (from brainstorming)

The cross-host link runs over a **Tailscale / WireGuard overlay**, which already provides host-to-host
encryption and is the same trust fabric as the existing browser↔edge auth. Given that:

- **Auth = tailnet ACL (network-level).** No app-level token or mutual-auth is added; the daemon has
  no app-level auth today (by design) and keeps none. The trust boundary moves from "UDS file perms"
  to "tailnet ACL + tailnet-IP bind + opt-in TCP."
- **No app-level TLS.** WireGuard already encrypts host-to-host; app-level TLS would be redundant
  cert-management overhead. (If the deployment ever leaves the overlay, TLS becomes a follow-up.)
- **TCP is opt-in; UDS stays the same-host default.** Unset config behaves exactly as today.

These choices are the accepted security model for this feature. Two properties are **non-negotiable
guardrails** that make the network-level trust model safe (see Security posture).

## Goals / non-goals

**Goals:** the daemon can listen on a tailnet TCP address; the edge can point at it by URL; same-host
UDS behaviour is byte-for-byte unchanged when unconfigured; a wildcard TCP bind is refused.

**Non-goals (YAGNI):** TLS, app-level bearer tokens / mutual TLS, certificate generation or rotation,
Tailscale LocalAPI peer-identity lookups, multi-daemon discovery, IPv6-specific features beyond
accepting a bracketed literal.

## Architecture

One new shared address abstraction; `serve.py` (daemon) and `client.py` (edge) consume it. Nothing
else in the edge↔daemon protocol changes — only the connect primitive differs by transport.

### Component 1 — `server/ipc/address.py` (new): the single source of truth

A pure parser (no I/O) that turns one env var into a transport descriptor, plus connect helpers so
neither `serve.py` nor `client.py` re-implements transport branching (CLAUDE.md: no parallel impls).

- **Env var:** `CCSWITCH_SESSIOND_URL`. Scheme selects transport:
  - unset → UDS at the existing default path (`server/ipc/paths.py:DEFAULT_SOCKET_PATH`), i.e.
    identical to today. The existing `CCSWITCH_SESSIOND_SOCKET` continues to override the UDS path
    (back-compat); when both are set, an explicit `CCSWITCH_SESSIOND_URL` wins and a one-line warning
    is logged.
  - `unix:///run/ccswitch/sessiond.sock` → UDS at that path.
  - `tcp://100.64.0.5:8788` → TCP bound to that host:port. IPv6 literal as `tcp://[fd7a::5]:8788`.
- **Descriptor** (frozen dataclass): `DaemonAddress(kind: "unix"|"tcp", uds_path: str|None,
  host: str|None, port: int|None)`.
- **Helpers on the descriptor / module:**
  - `uvicorn_kwargs() -> dict` — `{"uds": path}` or `{"host": host, "port": port}` for `uvicorn.run`.
  - `httpx_base_url() -> str` — `"http://daemon"` (uds) or `f"http://{host}:{port}"` (tcp).
  - `httpx_client_kwargs()` — UDS passes `AsyncHTTPTransport(uds=path)`; TCP passes none (default).
  - `ws_uri() -> str` — `"ws://daemon/events"` (uds) or `f"ws://{host}:{port}/events"` (tcp).
  - `ws_connect()` — `websockets.unix_connect(path, ws_uri())` vs `websockets.connect(ws_uri())`.
- **Validation:** malformed URL, unknown scheme, missing/zero port, or a **wildcard host**
  (`0.0.0.0`, `::`, empty) raises a `ValueError` with a clear message. The wildcard check lives here
  so both the daemon (at bind) and any tooling get the same guarantee.
- **Resolver:** `daemon_address() -> DaemonAddress` reads the env and returns the descriptor (the
  analogue of today's `sessiond_socket()`; `paths.py` stays the source of the default UDS path).

### Component 2 — daemon listener (`server/ipc/serve.py`)

`serve()` calls `daemon_address()` and feeds `addr.uvicorn_kwargs()` to `uvicorn.run(app, **kwargs,
log_level="info")`. UDS path is unchanged. For TCP, the wildcard guardrail has already rejected a
wildcard host during parsing, so a misconfigured `0.0.0.0` bind fails fast at startup with a clear
error rather than silently exposing the port. The log line reports the resolved transport + address.

### Component 3 — edge client (`server/ipc/client.py`)

`DaemonClient.__init__` accepts a `DaemonAddress` (a bare path string is still accepted and treated as
a UDS path, for back-compat). It builds its `httpx.AsyncClient` from `addr.httpx_client_kwargs()`, and
the `_reader` reconnect loop uses `addr.ws_connect()` instead of the hard-coded
`websockets.unix_connect`. All higher-level behaviour — RPC envelopes, `DaemonUnavailable`, the
`/events` subscribe loop, reconnect backoff — is unchanged.

> **Caller audit:** every construction site of `DaemonClient` and every connection use of
> `sessiond_socket()` must route through `daemon_address()`. The plan enumerates them
> (`grep -rn "DaemonClient(\|sessiond_socket(" server/`) so no path keeps a UDS-only assumption.

## Security posture

The model is "network-level trust on the tailnet." It is safe **only** with both guardrails:

1. **Tailnet-IP bind, never wildcard.** The daemon must bind the specific tailnet IP. A wildcard bind
   is refused at parse/startup (Component 1 validation) — this is the linchpin: it prevents the
   no-auth daemon port from landing on a public or LAN NIC.
2. **TCP is opt-in.** With `CCSWITCH_SESSIOND_URL` unset, the daemon is UDS-only exactly as today; no
   existing deployment opens a port by upgrading.

Operationally, the tailnet ACL must restrict the daemon port to the edge host. This is documented in
the spec/quickstart, not enforced in code. The plan's final review is **security-focused** on these
two guardrails and the opt-in default.

## Error handling

- Parse-time: malformed URL / unknown scheme / bad port / wildcard host → `ValueError`, surfaced at
  daemon startup (fail fast) and at client construction.
- Runtime: a TCP connect refusal raises the same `DaemonUnavailable` / triggers the same reconnect
  backoff as a missing UDS today — no new client-facing error path.

## Testing

- **Address parser unit tests** (`server/tests/test_ipc_address.py`): unset→UDS default; explicit
  `unix://`; `tcp://host:port` (v4 and bracketed v6); `CCSWITCH_SESSIOND_SOCKET` back-compat and the
  URL-wins-with-warning precedence; malformed URL; unknown scheme; missing/zero port; and
  **wildcard-host rejection** (`0.0.0.0`, `::`, empty) → `ValueError`.
- **DaemonClient-over-TCP integration test**: serve a daemon app on `127.0.0.1:<ephemeral>` (a
  specific IP — passes the guardrail) and run the existing RPC + `/events` subscribe/echo flow over
  TCP, mirroring the UDS reconnect test. Reuse `ipc_harness` patterns.
- **Wildcard-bind rejection test** for `serve()`/parse path.
- **Regression:** existing UDS tests (`test_ipc_*`, `test_remote_*`) stay green — the default path is
  untouched. Backend lint gates `ruff` + `mypy` first.

## Rollout

Additive and opt-in: no schema change, no protocol-version bump (the wire contract is unchanged;
only the transport carrying it differs). Same-host deployments are unaffected until an operator sets
`CCSWITCH_SESSIOND_URL=tcp://<tailnet-ip>:<port>` on both services and adds the tailnet ACL.
