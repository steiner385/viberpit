# Daemon mTLS: client-cert CN/SAN allowlist — Design

**Date:** 2026-06-26
**Issue:** #325 (v2 hardening of #205; follow-up to the merged #315)
**Status:** Approved (brainstorm) → pending implementation plan

## Problem

#205 (PR #315) made the edge↔daemon channel cross-host over **mTLS**: the daemon binds
`https://host:port` with `ssl_cert_reqs=CERT_REQUIRED`, so the TLS handshake rejects any
client lacking a CA-signed cert. The trust anchor is a private CA, and v1 deliberately
trusts **any** client cert signed by that CA (`docs/superpowers/specs/2026-06-26-cross-host-daemon-mtls-design.md`,
decision 3: "CA-signed is sufficient for v1 … a CN/SAN allowlist is explicitly deferred to v2").

That leaves a gap this design closes: if the operator's CA also signs certs for other
purposes, or a client key leaks, **any** CA-signed cert can drive the *entire* daemon RPC
surface — `create_session`, `run_command`, `permanent_delete`, `snapshot_deploy`/`restore_deploy`,
and the `/vnc` stream. v2 restricts accepted client **identities** to an explicit allowlist.

Security-sensitive (constitution Principle IV): this tightens the auth boundary on the hop
that exposes every privileged RPC.

## Key facts (verified on this host, 2026-06-26)

- **uvicorn 0.49.0 exposes no client cert to the app.** The ASGI `scope` (HTTP `h11_impl.py:204`,
  WS `websockets_impl.py`) carries only `server`/`client` address tuples — no transport, no
  TLS extension, no peer cert. So an ASGI/Starlette middleware **cannot** see the client cert.
- **stdlib `ssl` has no per-cert verify callback.** `SSLContext` verifies the chain + (optional)
  hostname only; there is no Python hook to reject by client CN during the handshake. `sni_callback`
  fires before the client cert exists (ClientHello), so it can't see it either.
- **uvicorn 0.49 `Config` accepts `ssl_context_factory`** (`config.py:228`): `Callable[[Config, Callable[[], ssl.SSLContext]], ssl.SSLContext]`.
  When set, uvicorn calls it to obtain the server `SSLContext`; the factory receives a builder for
  the default context (which loads cert/key/CA and applies `CERT_REQUIRED`).
- **`ssl.SSLContext.wrap_bio()` constructs the SSLObject via `self.sslobject_class._create(...)`**
  (verified in this Python 3.12.3), and `ctx.sslobject_class` is a settable attribute. asyncio's
  SSL transport uses `wrap_bio`, so a custom `sslobject_class` is honored for uvicorn/asyncio
  connections. `SSLObject` has both `do_handshake()` and `getpeercert()`.
- The repo already has a `cryptography`-based cert fixture (`server/tests/tls_certs.py`,
  `server/tests/test_tls_certs_fixture.py`) that mints an ephemeral CA + server/client certs — the
  allowlist tests reuse it; **no new dependency** is required.

## Decisions (from brainstorm)

1. **Enforce at the TLS handshake (transport layer), not the app layer** — because the client cert
   is invisible above the transport. Mechanism: uvicorn `ssl_context_factory` + a custom
   `ssl.SSLObject` whose `do_handshake()` checks the peer CN/SAN.
2. **Unset allowlist = permissive (v1 behavior).** Existing #205 deployments are unaffected; setting
   `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST` opts into the stricter check. Additive, low-risk — mirrors
   how #205 kept UDS as the zero-config default.
3. **Exact CN/SAN match, no wildcards** (YAGNI). Can revisit if a real wildcard need appears.
4. **One enforcement covers all channels.** RPC (`/rpc/*`), `/events`, and `/vnc` ride the same TLS
   transport, so a single handshake check protects all three — no per-route code.
5. **`unix://` is untouched.** The allowlist is consulted only for an `https` endpoint; the UDS
   trust boundary remains the socket file's permissions.

## Architecture

### Identity extraction + allowlist (`server/ipc/tls.py`)

A pure helper (no I/O) extracts the candidate identities from a `getpeercert()` dict:

```
peer_identities(cert: dict) -> set[str]:
    # subject CN (cert["subject"] is a tuple of RDN tuples) +
    # subjectAltName values (cert["subjectAltName"] is a tuple of (type, value),
    # type in {"DNS", "URI", "IP Address", "email"}). Return the union of values.

client_allowlist() -> frozenset[str] | None:
    # parse CCSWITCH_SESSIOND_CLIENT_ALLOWLIST (comma-separated, trimmed, empties dropped).
    # None when unset/empty -> permissive (no allowlist enforcement).

allowed(cert: dict, allow: frozenset[str]) -> bool:
    # True iff peer_identities(cert) & allow is non-empty.
```

### Custom SSLObject + context factory (`server/ipc/tls.py`)

```
class _AllowlistSSLObject(ssl.SSLObject):
    _allow: frozenset[str]            # set on the class/instance via the factory
    def do_handshake(self, *a, **k):
        super().do_handshake(*a, **k)             # chain + CERT_REQUIRED already enforced here
        cert = self.getpeercert() or {}
        if not allowed(cert, self._allow):
            log.warning("daemon mTLS: rejected client identities %s (not in allowlist)",
                        sorted(peer_identities(cert)))
            raise ssl.SSLCertVerificationError("client certificate identity not in allowlist")

daemon_ssl_context_factory(allow: frozenset[str]):
    def factory(config, default_factory):
        ctx = default_factory()                   # loads cert/key/CA + CERT_REQUIRED
        # bind the allowlist to a subclass so each context carries its own set
        ctx.sslobject_class = _bind(_AllowlistSSLObject, allow)
        return ctx
    return factory
```

`_bind(base, allow)` returns a **per-context subclass** — `type("_AllowlistSSLObject", (base,), {"_allow": allow})`
— because `wrap_bio()._create()` constructs the SSLObject with no opening to pass an instance
attribute; the allowlist therefore rides on the class object, and a fresh subclass per context keeps
allowlists from bleeding across contexts. When `allow` is empty/None, the factory is **not used** at
all (see serve wiring) — v1 behavior is byte-for-byte preserved.

### Daemon bind wiring (`server/ipc/serve.py`)

`uvicorn_run_kwargs(endpoint, ...)` already returns the `ssl_*` kwargs for `https`. Extend it:

- Resolve `allow = client_allowlist()`.
- If `allow` is None → return the existing kwargs unchanged (permissive v1).
- If `allow` is set → ALSO pass `ssl_context_factory=daemon_ssl_context_factory(allow)`. uvicorn still
  receives `ssl_certfile/keyfile/ca_certs/cert_reqs` so the default factory loads them; our factory
  wraps that context to install the CN check.

No change to `unix://` kwargs. No change to any RPC/event/VNC handler.

### Config / docs

- New env var `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST` documented alongside the other
  `CCSWITCH_SESSIOND_*` TLS vars (config docstring, `.env.example`, and the #205 quickstart in
  `specs/001-ccswitch-web/quickstart.md`).
- Quickstart note: a rejected client shows only a generic TLS error on the **edge**; the **daemon**
  log line (above) is the diagnostic. Issue client certs whose CN/SAN match the allowlist exactly.

## Security model

- **Closes:** "any CA-signed cert is fully trusted." With the allowlist set, a CA-signed cert whose
  identity is not listed fails the handshake before any RPC/stream byte is processed.
- **Defense in depth, not a replacement:** mTLS chain verification (v1) still runs first; the
  allowlist is an additional gate on top.
- **No insecure fallback / no silent loosening:** unset allowlist is *documented* permissive v1
  behavior, not a fail-open after a misconfig. Required TLS files still fail loud (#205, unchanged).
- **Deferred (documented, not built):** wildcard/regex identity matching, per-RPC authorization,
  cert rotation/ACME, Tailscale-transport coupling. Same v2+ list as #205.

## Testing (TDD)

Pure unit (no I/O):
1. `peer_identities` — CN only, SAN only, CN+SAN union, empty cert → empty set.
2. `client_allowlist` parsing — unset → None, `"a, b ,"` → `{"a","b"}`, whitespace/empties dropped.
3. `allowed` — CN hit, SAN hit, miss, empty-allow semantics.

Integration (reuse `tls_certs.py`; mint CA + server cert + two client certs `edge-ok` / `edge-evil`,
**both CA-signed**), daemon served via the real uvicorn path on an ephemeral `127.0.0.1` port:
4. **Spike/foundational:** allowlist=`{edge-ok}`, `edge-ok` client completes an RPC `ping`. This also
   confirms asyncio propagates a `do_handshake` rejection as a clean drop (see Risks).
5. `edge-evil` (same CA, identity not listed) → handshake rejected; `DaemonClient` RPC raises
   `DaemonUnavailable`/SSL error, not a 200.
6. Allowlist **unset** → both clients succeed (back-compat / permissive).
7. One WS channel (`/events` or `/vnc`) with `edge-evil` → rejected, proving transport-wide coverage.
8. Same-host `unix://` regression: allowlist env set but endpoint is unix → no effect, existing UDS
   tests stay green.

## Scope

**In:** identity extraction + allowlist + custom SSLObject + context factory (`tls.py`), serve wiring
(`serve.py`), config var, tests, docs.

**Out (v2+):** wildcard/regex matching, per-RPC authorization, cert rotation/ACME, Tailscale-transport
coupling, any RPC/stream protocol change.

## Risks / notes

- **asyncio error propagation (highest risk):** the design relies on an exception raised inside a
  custom `SSLObject.do_handshake()` aborting the connection cleanly under asyncio/uvicorn. The
  `sslobject_class` seam is verified; end-to-end propagation is confirmed by test 4 as the FIRST
  implementation step. **Fallback if it doesn't hold:** a post-accept check (capture the verified
  peer cert in the custom SSLObject into a per-connection store and reject in a thin ASGI middleware),
  or pyOpenSSL's `set_verify` callback behind a hand-built context — both heavier; only if test 4 fails.
- **Context state isolation:** the allowlist must be bound per-context (not a module global) so tests
  and multi-endpoint setups don't bleed state — asserted by running the permissive and strict
  integration tests in the same process.
- **CN vs SAN guidance:** modern certs put identity in SANs; the quickstart must tell operators which
  field they're matching so they issue certs correctly (a common mTLS footgun).
