# Cross-host daemon transport (TCP + TLS + mTLS auth) — Design

**Date:** 2026-06-26
**Issue:** #205 (follow-up to #204; deferred P4 public-hosting hardening)
**Status:** Approved (brainstorm) → pending implementation plan

## Problem

The session daemon (`server/ipc/`) and the web edge (`server/app.py`) run as two processes that share one SQLite DB. Today they only talk **same-host** over a Unix domain socket:

- `server/ipc/serve.py:94` — `uvicorn.run(app, uds=sock)`.
- `server/ipc/client.py:34-41` — `DaemonClient` is hardwired to `httpx.AsyncHTTPTransport(uds=...)` for RPC and `websockets.unix_connect(...)` for the `/events` and `/vnc` WebSockets.
- Trust boundary = **filesystem permissions on the socket file**. `server/ipc/daemon.py:1-4` documents: *"No TCP. No app-level auth — the socket's filesystem permissions are the trust boundary."* Identity is passed as an RPC *parameter* (`orchestrator_rpc.py`) but the daemon never re-validates it.

#204 made the VNC/x11 path transport-agnostic (it rides the existing WebSocket-over-IPC channel, no `127.0.0.1` assumption). To run the daemon on a **different host** from the edge, the daemon channel itself must become reachable cross-host **and** gain an auth model that does not depend on local filesystem permissions.

This is security-sensitive (constitution Principle IV): the change exposes **every** edge↔daemon RPC — `create_session`, `run_command`, `permanent_delete`, `snapshot_deploy`/`restore_deploy`, etc. — over the network, not just VNC.

## Key facts (verified on this host, 2026-06-26)

- The whole edge↔daemon channel is already **HTTP/1.1 + JSON over a socket** (uvicorn server; httpx + `websockets` client). Swapping the *transport* does not touch the RPC/stream protocol (`PROTOCOL_VERSION = 1`, `server/ipc/protocol.py`).
- **uvicorn 0.49.0** natively supports mTLS: `Config` accepts `ssl_certfile`, `ssl_keyfile`, `ssl_ca_certs`, `ssl_cert_reqs`, `ssl_ciphers`, `ssl_version`, `ssl_context_factory`. With `ssl_cert_reqs=ssl.CERT_REQUIRED` + `ssl_ca_certs=<ca>`, **the TLS handshake rejects any client lacking a CA-signed cert** — auth is enforced at the transport layer, before any ASGI handler runs.
- **httpx 0.28.1** supports `cert=(certfile, keyfile)` (client cert) and `verify=<ca-bundle|ssl.SSLContext>`.
- **websockets 16.0** supports `websockets.connect("wss://…", ssl=<ssl.SSLContext>)`.
- **cryptography 49.0.0** is installed → tests can mint an ephemeral CA + server/client certs in a fixture. No new dependency (`trustme`) required.
- The edge's mode selector (`server/app.py:275-312`) keys entirely off `CCSWITCH_SESSIOND_SOCKET` being a path; `_daemon_socket_present` (`app.py:40-50`) is a `stat.S_ISSOCK` check; `_daemon_compatible` (`app.py:53-95`) does a synchronous `GET /info` over the UDS and matches `PROTOCOL_VERSION`. The split-brain guard (`app.py:286-296`) refuses an in-process fallback when a socket is present but unreachable.
- The edge's own **Tailscale identity auth** (`auth/middleware.py`, allowlist `CCSWITCH_TAILNET_ALLOWLIST`) sits in front of the *edge* and is unchanged by this work — this hardens only the edge→daemon hop.

## Decisions (from brainstorm)

1. **mTLS** is the auth + encryption mechanism (one mechanism for both). The TLS handshake is the enforcement point; no per-RPC auth code.
2. **Generalize the endpoint config; UDS stays the default.** A scheme-aware endpoint accepts `unix:///path` (default, zero-overhead same-host — every existing deployment unchanged) or `https://host:port` (opt-in cross-host).
3. **Trust anchor = a private CA. CA-signed is sufficient for v1** (any client cert signed by the configured CA is trusted). A **CN/SAN allowlist is explicitly deferred to v2** (YAGNI — the CA is operator-controlled).
4. Certs/keys are **operator-provided file paths** via env/config. Cert generation is documented (quickstart), not automated.
5. **Full delivery this session:** spec → implementation plan → subagent-driven implementation with a security review.

## Architecture

### The seam: a scheme-aware endpoint (`server/ipc/paths.py`)

Add an `Endpoint` value object and a resolver, keeping `sessiond_socket()` as a back-compat shim.

```
Endpoint:
  scheme: "unix" | "https"
  path:   str | None     # unix only
  host:   str | None     # https only
  port:   int | None     # https only

sessiond_endpoint() -> Endpoint:
  raw = env CCSWITCH_SESSIOND_ENDPOINT
        or ("unix://" + env CCSWITCH_SESSIOND_SOCKET)   # legacy var still honored
        or ("unix://" + DEFAULT_SOCKET_PATH)            # /run/ccswitch/sessiond.sock
  parse raw; validate scheme in {unix, https}; https requires host+port.

sessiond_socket() -> str:   # unchanged signature; returns the unix path (raises if https)
```

Precedence: explicit `CCSWITCH_SESSIOND_ENDPOINT` > legacy `CCSWITCH_SESSIOND_SOCKET` > well-known default. A bare path in the legacy var maps to `unix://<path>` so nothing downstream changes for same-host.

### TLS settings (`server/config.py`)

New settings, all optional, only required when the endpoint scheme is `https`:

- Daemon (server) side: `CCSWITCH_SESSIOND_TLS_CERT`, `CCSWITCH_SESSIOND_TLS_KEY`, `CCSWITCH_SESSIOND_TLS_CA` (CA used to verify *client* certs).
- Edge (client) side: `CCSWITCH_SESSIOND_CLIENT_CERT`, `CCSWITCH_SESSIOND_CLIENT_KEY`, `CCSWITCH_SESSIOND_CLIENT_CA` (CA used to verify the *server* cert).

A single helper validates that, for an `https` endpoint, the required files exist and are readable, failing loud at startup otherwise (Principle IV: no silent insecure fallback).

### Daemon bind (`server/ipc/serve.py`)

`serve()` resolves the endpoint and branches:

- `unix://` → `uvicorn.run(app, uds=endpoint.path, ...)` (**unchanged**).
- `https://` → `uvicorn.run(app, host=endpoint.host, port=endpoint.port, ssl_certfile=CERT, ssl_keyfile=KEY, ssl_ca_certs=CA, ssl_cert_reqs=ssl.CERT_REQUIRED, ...)`.

`ssl_cert_reqs=CERT_REQUIRED` is what makes it **mutual** TLS — clients without a CA-signed cert fail the handshake.

### Edge client (`server/ipc/client.py`)

`DaemonClient.__init__` accepts an `Endpoint` (and TLS material for `https`). Branch the two transports:

- `unix://` → today's `httpx.AsyncHTTPTransport(uds=...)` + `websockets.unix_connect(self._path, "ws://daemon/...")` (**unchanged**).
- `https://` →
  - RPC: `httpx.AsyncClient(base_url="https://host:port", cert=(client_cert, client_key), verify=<client_ca>, timeout=30.0)`.
  - Events/VNC: build one `ssl.SSLContext` (load client cert/key, load CA as verify locations, `check_hostname` per cert SANs) and `websockets.connect("wss://host:port/events", ssl=ctx)`.

The reader/reconnect/backoff/replay logic (`client.py:103-143`) is transport-independent and stays as-is — only the connect call differs. Factor the "open a WS to `<path>` / `<url>`" step behind a small internal method so the reader loop is unchanged.

### Edge mode selection + probe (`server/app.py`)

Replace the path-centric checks with endpoint-aware ones:

- `_daemon_present(endpoint)`:
  - `unix` → `stat.S_ISSOCK` (today's `_daemon_socket_present`).
  - `https` → a TCP `connect()` to host:port succeeds (socket-level reachability; cheap, no TLS).
- `_daemon_compatible(endpoint)`:
  - `unix` → today's synchronous `GET /info` over UDS.
  - `https` → synchronous `httpx.Client(verify=<ca>, cert=(...))` `GET https://host:port/info`, match `PROTOCOL_VERSION`.
- Mode-select block (`app.py:275-312`): drive off `sessiond_endpoint()`; pass the `Endpoint` to `DaemonClient`. The split-brain guard is preserved verbatim in meaning: present-but-incompatible → `RuntimeError`, refusing in-process fallback.

The existing "blocking I/O must not run under a live loop" guard (`app.py:71-79`) is retained for the TCP probe (it too does sync I/O).

## Data flow (unchanged above the transport)

`POST /rpc/{method}` (JSON) for unary RPC; `/events` WS (multiplexed, per-stream replay) for events; `/vnc` WS for RFB bytes. #204 already removed `127.0.0.1` assumptions from these, so **no RPC, event, or VNC handler changes** — they ride the new transport transparently.

## Security model

- **Threat the change introduces:** the daemon's RPC surface becomes network-reachable. Mitigation: mTLS `CERT_REQUIRED` means an attacker who can reach the port still cannot complete a handshake without a CA-signed client cert; all traffic is encrypted.
- **Trust anchor:** the operator's private CA. Compromise of the CA private key compromises the channel — documented as the key operational secret to protect; keep it off the daemon/edge hosts where practical.
- **No insecure fallback:** an `https` endpoint with missing/unreadable TLS files fails loud at startup; the edge never downgrades to plaintext or in-process when a daemon endpoint is configured.
- **Unchanged:** edge-facing Tailscale identity + allowlist remain the user-facing auth; this is strictly the second hop.
- **Deferred to v2 (documented, not built):** CN/SAN client allowlist, cert rotation/ACME automation, Tailscale-transport coupling, per-RPC authorization.

## Testing (TDD)

New `server/tests/` coverage, using a `cryptography`-based fixture that mints an ephemeral CA + server cert (SAN `127.0.0.1`/`localhost`) + client cert into a tmp dir:

1. **Endpoint parsing** (pure, no I/O): `unix://path`, `https://host:port`, legacy bare `CCSWITCH_SESSIOND_SOCKET`, precedence of `CCSWITCH_SESSIOND_ENDPOINT`, invalid scheme rejected, `https` without host/port rejected.
2. **Happy path mTLS round-trip:** daemon serving mTLS on `127.0.0.1:<ephemeral port>`; a `DaemonClient` with a valid client cert performs an RPC (`ping`) and receives a published event over `/events`.
3. **No client cert → rejected:** a client without a cert cannot complete the handshake (RPC raises `DaemonUnavailable`/connection error; not a 200).
4. **Wrong-CA client cert → rejected.**
5. **Server cert not trusted by client CA → client refuses** (server-identity verification works both ways).
6. **Probe over TCP:** `_daemon_present`/`_daemon_compatible` for `https` (reachable+compatible → True; unreachable → False; reachable but wrong protocol → False, exercising the split-brain branch).
7. **Same-host UDS regression:** existing UDS tests remain green unchanged.

TLS tests bind an ephemeral localhost port and run the daemon app via uvicorn (or an equivalent `ssl.SSLContext` server) within the test; they are independent of the live host daemon.

## Config / docs deliverables

- `specs/001-ccswitch-web/quickstart.md` (or a new ops note): how to generate a CA + server/client certs with `openssl`, the env vars to set on each host, and a worked `https://` example.
- Enumerate all new env vars in one place (config docstring + quickstart).

## Scope

**In:** endpoint abstraction (`paths.py`), TLS config (`config.py`), mTLS daemon bind (`serve.py`), mTLS `DaemonClient` (`client.py`), endpoint-aware probe + mode-select (`app.py`), tests, cert-generation docs.

**Out (v2):** CN/SAN allowlist, automated cert rotation / ACME, Tailscale-transport coupling, per-RPC authorization, changing the RPC/stream protocol.

## Risks / notes

- **Hostname verification:** the client must verify the server cert against its SANs. Cross-host deployments must issue the server cert for the daemon's reachable hostname/IP; the quickstart must call this out (a common mTLS footgun).
- **`websockets.connect` vs `unix_connect` API parity:** confirm frame iteration / `send`/recv semantics are identical so the reader loop is genuinely transport-agnostic (they are in 16.0; verify in the happy-path test).
- **uvicorn passes the SSL context to the event loop's `start_tls`:** the in-test server must use the same uvicorn path (not a hand-rolled server) so the test actually exercises production behavior.
- **Back-compat is load-bearing:** the legacy `CCSWITCH_SESSIOND_SOCKET` var and the bare-path default must keep working byte-for-byte; this is asserted by the UDS regression tests.
