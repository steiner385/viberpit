# Daemon mTLS client-cert CN/SAN allowlist — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict the cross-host mTLS daemon to an explicit allowlist of client-cert identities (CN/SAN), enforced at the TLS handshake.

**Architecture:** The client cert is invisible above the transport (uvicorn puts no cert in the ASGI scope; stdlib `ssl` has no verify callback), so enforce at the handshake: uvicorn's `ssl_context_factory` installs a per-context `ssl.SSLObject` subclass whose `do_handshake()` checks the peer CN/SAN against the allowlist and raises on a miss. One check covers RPC, `/events`, and `/vnc` (one TLS transport). Unset allowlist = permissive v1 behavior; `unix://` is untouched.

**Tech Stack:** Python 3.12 · stdlib `ssl` · uvicorn 0.49 (`ssl_context_factory`, `Config.ssl_cert_reqs`) · httpx / websockets clients · `cryptography` (test cert minting) · pytest/pytest-asyncio.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-26-daemon-mtls-cn-allowlist-design.md`. Issue **#325** (v2 of #205).
- Env var name (verbatim): `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST` — comma-separated CN/SAN values.
- **Unset/empty allowlist ⇒ permissive** (any CA-signed cert; identical to v1). Setting it opts into the strict check. No fail-open after misconfig: unset is documented intended behavior, not a fallback.
- Exact CN/SAN match. **No wildcards** (YAGNI).
- `unix://` endpoints never consult the allowlist (UDS trust = socket file perms).
- No new runtime dependency. Tests reuse `server/tests/tls_certs.py` + the `mtls_certs` conftest fixture.
- TDD throughout: failing test first, watch it fail, minimal code, commit per task.
- Run backend tests with: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …` (worktree has no built env; use the main checkout's venv).
- Lint gates before any push: `ruff check` + `mypy` on changed files (mypy's `ccswitch.*` import-untyped notes are pre-existing env noise).

## File Structure

- **Modify `server/ipc/tls.py`** — add the pure helpers (`peer_identities`, `client_allowlist`, `allowed`), the `_AllowlistSSLObject` subclass, the `_bind` per-context subclass factory, and `daemon_ssl_context_factory`. This file already owns all daemon TLS material, so the allowlist belongs here (single source of truth).
- **Modify `server/ipc/serve.py`** — `uvicorn_run_kwargs` gains an `allowlist` kwarg; `serve()` resolves `client_allowlist()` and passes it.
- **Modify `server/tests/conftest.py`** — extend `MTLSCerts` + the `mtls_certs` fixture with a second **same-CA** client cert (`client_other_*`, CN `edge-other`) so rejection can be tested without a CA mismatch.
- **Create `server/tests/test_ipc_cn_allowlist.py`** — unit (helpers) + integration (handshake accept/reject/permissive, WS coverage, unix no-op).
- **Modify `.env.example`** and **`specs/001-ccswitch-web/quickstart.md`** — document the new var + the "rejected client shows only a generic TLS error on the edge; check the daemon log" guidance.

---

### Task 1: Pure helpers — identity extraction, allowlist parse, match

**Files:**
- Modify: `server/ipc/tls.py`
- Test: `server/tests/test_ipc_cn_allowlist.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `peer_identities(cert: dict) -> set[str]` — union of subject commonName + all subjectAltName values from a `ssl.SSLObject.getpeercert()` dict.
  - `client_allowlist() -> frozenset[str] | None` — parse `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST`; `None` when unset/blank (permissive).
  - `allowed(cert: dict, allow: frozenset[str]) -> bool` — `bool(peer_identities(cert) & allow)`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_ipc_cn_allowlist.py`:

```python
import os
from unittest import mock

from server.ipc.tls import peer_identities, client_allowlist, allowed

# getpeercert() shape: subject is a tuple of RDNs, each RDN a tuple of (key, value) pairs;
# subjectAltName is a tuple of (type, value) pairs.
_CERT = {
    "subject": ((("commonName", "edge"),),),
    "subjectAltName": (("DNS", "edge.internal"), ("IP Address", "10.0.0.5")),
}


def test_peer_identities_unions_cn_and_sans():
    assert peer_identities(_CERT) == {"edge", "edge.internal", "10.0.0.5"}


def test_peer_identities_cn_only():
    assert peer_identities({"subject": ((("commonName", "edge"),),)}) == {"edge"}


def test_peer_identities_empty_cert():
    assert peer_identities({}) == set()


def test_client_allowlist_unset_is_none():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert client_allowlist() is None


def test_client_allowlist_blank_is_none():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_CLIENT_ALLOWLIST": "  ,, "}, clear=True):
        assert client_allowlist() is None


def test_client_allowlist_parses_trims_and_drops_empties():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_CLIENT_ALLOWLIST": " edge, edge.internal ,"}, clear=True):
        assert client_allowlist() == frozenset({"edge", "edge.internal"})


def test_allowed_cn_hit():
    assert allowed(_CERT, frozenset({"edge"})) is True


def test_allowed_san_hit():
    assert allowed(_CERT, frozenset({"edge.internal"})) is True


def test_allowed_miss():
    assert allowed(_CERT, frozenset({"someone-else"})) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_cn_allowlist.py -q`
Expected: FAIL — `ImportError: cannot import name 'peer_identities'`.

- [ ] **Step 3: Implement the helpers in `server/ipc/tls.py`**

Add after the imports (add `import logging`; keep existing content):

```python
log = logging.getLogger("server.ipc.tls")


def peer_identities(cert: dict) -> set[str]:
    """CN + SAN values from a getpeercert() dict (the candidate client identities)."""
    ids: set[str] = set()
    for rdn in cert.get("subject", ()):          # rdn: tuple of (key, value) pairs
        for key, val in rdn:
            if key == "commonName":
                ids.add(val)
    for _typ, val in cert.get("subjectAltName", ()):
        ids.add(val)
    return ids


def client_allowlist() -> frozenset[str] | None:
    """Parse CCSWITCH_SESSIOND_CLIENT_ALLOWLIST. None when unset/blank (permissive)."""
    raw = os.environ.get("CCSWITCH_SESSIOND_CLIENT_ALLOWLIST")
    if not raw:
        return None
    items = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(items) or None


def allowed(cert: dict, allow: frozenset[str]) -> bool:
    return bool(peer_identities(cert) & allow)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_cn_allowlist.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Lint + commit**

```bash
PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/ipc/tls.py server/tests/test_ipc_cn_allowlist.py
git add server/ipc/tls.py server/tests/test_ipc_cn_allowlist.py
git commit -m "feat(ipc): CN/SAN identity helpers for daemon mTLS allowlist (#325)"
```

---

### Task 2: Handshake enforcement — custom SSLObject + context factory + serve wiring

This task carries the highest-risk assumption (asyncio propagates a `do_handshake` rejection as a clean drop). Its integration tests are the spike that confirms it. If the reject test does NOT fail the client connection, STOP and consult the spec's fallback (post-accept ASGI check / pyOpenSSL) before proceeding.

**Files:**
- Modify: `server/ipc/tls.py`
- Modify: `server/ipc/serve.py:28-45` (`uvicorn_run_kwargs`) and `serve()`
- Modify: `server/tests/conftest.py:197-234` (extend `MTLSCerts` + `mtls_certs`)
- Test: `server/tests/test_ipc_cn_allowlist.py` (append)

**Interfaces:**
- Consumes: `peer_identities`, `allowed` (Task 1); `Endpoint`, `DaemonTLSFiles`, `daemon_tls_files` (existing); `serve_daemon_tls`, `mtls_certs` (test harness).
- Produces:
  - `daemon_ssl_context_factory(allow: frozenset[str]) -> Callable[[Any, Callable[[], ssl.SSLContext]], ssl.SSLContext]` — a uvicorn `ssl_context_factory`.
  - `uvicorn_run_kwargs(endpoint, *, daemon_tls=None, allowlist: frozenset[str] | None = None) -> dict` — adds `ssl_context_factory` when `allowlist` is truthy and endpoint is https.
  - conftest `MTLSCerts.client_other_cert` / `.client_other_key` — a **same-CA** client cert, CN `edge-other`.

- [ ] **Step 1: Extend the cert fixture (same-CA second client)**

In `server/tests/conftest.py`, add two fields to `MTLSCerts` (after `client_key`):

```python
    client_other_cert: str
    client_other_key: str
```

In the `mtls_certs` fixture, after the `client.pem`/`client.key` block (signed by `ca_key`/`ca_cert`) and before the rogue block, add:

```python
    co_key, co_cert = make_leaf(ca_key, ca_cert, "edge-other")   # SAME CA, different identity
    write_pem_cert(d / "client_other.pem", co_cert)
    write_pem_key(d / "client_other.key", co_key)
```

And in the returned `MTLSCerts(...)`, add:

```python
        client_other_cert=str(d / "client_other.pem"),
        client_other_key=str(d / "client_other.key"),
```

- [ ] **Step 2: Write the failing integration tests**

Append to `server/tests/test_ipc_cn_allowlist.py`:

```python
import ssl
from unittest import mock as _mock

import httpx
import pytest

from server.ipc.paths import Endpoint
from server.ipc.serve import build_daemon, uvicorn_run_kwargs
from server.ipc.tls import ClientTLSFiles, client_ssl_context
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.ipc_harness import serve_daemon_tls


def _orch():
    return SessionOrchestrator(sdk_factory=_mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)


def _client_ctx(certs, cert, key):
    return client_ssl_context(ClientTLSFiles(cert=cert, key=key, ca=certs.ca))


def _serve(app, certs, allowlist):
    # serve_daemon_tls passes allowlist through to uvicorn_run_kwargs (see Step 4).
    # Plain def returning the async CM → call sites use `async with _serve(...)`.
    return serve_daemon_tls(app, certs, allowlist=allowlist)


def test_uvicorn_kwargs_adds_factory_only_when_allowlist_set(mtls_certs):
    from server.ipc.tls import DaemonTLSFiles
    tls = DaemonTLSFiles(cert=mtls_certs.server_cert, key=mtls_certs.server_key, ca=mtls_certs.ca)
    ep = Endpoint("https", host="127.0.0.1", port=9443)
    assert "ssl_context_factory" not in uvicorn_run_kwargs(ep, daemon_tls=tls, allowlist=None)
    kw = uvicorn_run_kwargs(ep, daemon_tls=tls, allowlist=frozenset({"edge"}))
    assert callable(kw["ssl_context_factory"])
    assert kw["ssl_cert_reqs"] == ssl.CERT_REQUIRED  # still mutual


@pytest.mark.asyncio
async def test_allowlisted_client_connects(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, frozenset({"edge"})) as ep:
        ctx = _client_ctx(mtls_certs, mtls_certs.client_cert, mtls_certs.client_key)
        async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}", verify=ctx, timeout=5.0) as c:
            assert (await c.get("/info")).json()["protocol"] == 1


@pytest.mark.asyncio
async def test_same_ca_client_not_in_allowlist_is_rejected(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, frozenset({"edge"})) as ep:
        # CA-signed but CN 'edge-other' ∉ allowlist → handshake must fail.
        ctx = _client_ctx(mtls_certs, mtls_certs.client_other_cert, mtls_certs.client_other_key)
        with pytest.raises((ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError,
                            httpx.ReadError, OSError)):
            async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}", verify=ctx, timeout=5.0) as c:
                await c.get("/info")


@pytest.mark.asyncio
async def test_unset_allowlist_is_permissive(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, None) as ep:
        ctx = _client_ctx(mtls_certs, mtls_certs.client_other_cert, mtls_certs.client_other_key)
        async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}", verify=ctx, timeout=5.0) as c:
            assert (await c.get("/info")).status_code == 200
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_cn_allowlist.py -q`
Expected: FAIL — `serve_daemon_tls() got an unexpected keyword argument 'allowlist'` and `uvicorn_run_kwargs() got an unexpected keyword argument 'allowlist'`.

- [ ] **Step 4: Implement the factory + SSLObject (`server/ipc/tls.py`)**

Append to `server/ipc/tls.py`:

```python
def _bind(base: type, allow: frozenset[str]) -> type:
    # Per-context subclass: wrap_bio()._create() gives no opening to pass an instance
    # attribute, so the allowlist rides on the class. A fresh subclass per context keeps
    # allowlists from bleeding across contexts.
    return type("_AllowlistSSLObject", (base,), {"_allow": allow})


class _AllowlistSSLObject(ssl.SSLObject):
    _allow: frozenset[str] = frozenset()

    def do_handshake(self, *args, **kwargs):
        super().do_handshake(*args, **kwargs)        # chain + CERT_REQUIRED already enforced
        cert = self.getpeercert() or {}
        if not allowed(cert, self._allow):
            ids = sorted(peer_identities(cert))
            log.warning("daemon mTLS: rejected client identities %s (not in allowlist)", ids)
            raise ssl.SSLCertVerificationError("client certificate identity not in allowlist")


def daemon_ssl_context_factory(allow: frozenset[str]):
    """uvicorn ssl_context_factory that enforces a client CN/SAN allowlist at the handshake."""
    def factory(config, default_factory):
        ctx = default_factory()                       # loads cert/key/CA + CERT_REQUIRED
        ctx.sslobject_class = _bind(_AllowlistSSLObject, allow)
        return ctx
    return factory
```

- [ ] **Step 5: Wire it into `uvicorn_run_kwargs` + `serve()` (`server/ipc/serve.py`)**

Update the import line:

```python
from server.ipc.tls import DaemonTLSFiles, daemon_tls_files, daemon_ssl_context_factory, client_allowlist
```

Change `uvicorn_run_kwargs` signature + https branch:

```python
def uvicorn_run_kwargs(endpoint: Endpoint, *, daemon_tls: DaemonTLSFiles | None = None,
                       allowlist: frozenset[str] | None = None) -> dict:
    if endpoint.is_unix:
        return {"uds": endpoint.path}
    files = daemon_tls or daemon_tls_files()
    kwargs = {
        "host": endpoint.host,
        "port": endpoint.port,
        "ssl_certfile": files.cert,
        "ssl_keyfile": files.key,
        "ssl_ca_certs": files.ca,
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
    if allowlist:
        kwargs["ssl_context_factory"] = daemon_ssl_context_factory(allowlist)
    return kwargs
```

In `serve()`, resolve the allowlist from env and pass it:

```python
    uvicorn.run(app, log_level="info", **uvicorn_run_kwargs(endpoint, allowlist=client_allowlist()))
```

- [ ] **Step 6: Teach the harness to pass the allowlist (`server/tests/ipc_harness.py`)**

Change `serve_daemon_tls` signature + the kwargs build:

```python
async def serve_daemon_tls(app, certs, allowlist=None):
    """Serve `app` over mTLS on an ephemeral 127.0.0.1 port; yield its Endpoint."""
    endpoint = Endpoint("https", host="127.0.0.1", port=_free_port())
    tls = DaemonTLSFiles(cert=certs.server_cert, key=certs.server_key, ca=certs.ca)
    kwargs = uvicorn_run_kwargs(endpoint, daemon_tls=tls, allowlist=allowlist)
```

(Existing callers pass no `allowlist` → `None` → unchanged behavior.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_cn_allowlist.py -q`
Expected: PASS (all). **If `test_same_ca_client_not_in_allowlist_is_rejected` fails (client connects anyway), the mechanism is unsound — STOP and switch to the spec's fallback.**

- [ ] **Step 8: Regression — existing mTLS tests still green**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_serve_tls.py server/tests/test_ipc_client_mtls.py server/tests/test_vnc_ws_mtls.py -q`
Expected: PASS (the `serve_daemon_tls`/`uvicorn_run_kwargs` signature changes are backward-compatible).

- [ ] **Step 9: Lint + commit**

```bash
PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/ipc/tls.py server/ipc/serve.py server/tests/ipc_harness.py server/tests/conftest.py server/tests/test_ipc_cn_allowlist.py
PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ipc/tls.py server/ipc/serve.py
git add server/ipc/tls.py server/ipc/serve.py server/tests/ipc_harness.py server/tests/conftest.py server/tests/test_ipc_cn_allowlist.py
git commit -m "feat(ipc): enforce client-cert CN/SAN allowlist at the mTLS handshake (#325)"
```

---

### Task 3: Transport-wide coverage — WS channel + unix no-op

Proves the single handshake check also gates the event/VNC WebSocket, and that `unix://` ignores the allowlist entirely (no regression for same-host).

**Files:**
- Test: `server/tests/test_ipc_cn_allowlist.py` (append)

**Interfaces:**
- Consumes: `serve_daemon_tls` (now allowlist-aware), `mtls_certs`, `client_ssl_context`, `uvicorn_run_kwargs`.
- Produces: nothing (tests only).

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_ipc_cn_allowlist.py`:

```python
import websockets


@pytest.mark.asyncio
async def test_events_ws_rejected_for_non_allowlisted_client(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, frozenset({"edge"})) as ep:
        ctx = _client_ctx(mtls_certs, mtls_certs.client_other_cert, mtls_certs.client_other_key)
        with pytest.raises((ssl.SSLError, OSError, websockets.exceptions.WebSocketException)):
            async with websockets.connect(f"wss://{ep.host}:{ep.port}/events", ssl=ctx) as ws:
                await ws.send("{}")
                await ws.recv()


@pytest.mark.asyncio
async def test_events_ws_ok_for_allowlisted_client(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with _serve(app, mtls_certs, frozenset({"edge"})) as ep:
        ctx = _client_ctx(mtls_certs, mtls_certs.client_cert, mtls_certs.client_key)
        # Entering the context manager == the TLS+WS handshake completed (no exception),
        # which is the proof the allowlisted client was accepted. (websockets 16 has no
        # reliable `.open`; a subscribe round-trip would also work but isn't needed here.)
        async with websockets.connect(f"wss://{ep.host}:{ep.port}/events", ssl=ctx):
            pass


def test_unix_endpoint_ignores_allowlist():
    # A unix endpoint never carries ssl kwargs, even with an allowlist set.
    kw = uvicorn_run_kwargs(Endpoint("unix", path="/tmp/x.sock"), allowlist=frozenset({"edge"}))
    assert kw == {"uds": "/tmp/x.sock"}
```

- [ ] **Step 2: Run to verify they fail (then pass)**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_cn_allowlist.py -q`
Expected: the two WS tests + unix test PASS on first run **if** Task 2 is correct (they exercise already-built behavior). The `test_unix_endpoint_ignores_allowlist` guards against a future regression where the allowlist leaks into the unix branch. If any fail, fix per the assertion before committing.

> Note: these assert behavior produced in Task 2, so they may pass immediately — that is acceptable here because they cover *additional channels/paths* (WS, unix) not asserted in Task 2, closing real coverage gaps rather than re-testing the same call.

- [ ] **Step 3: Commit**

```bash
git add server/tests/test_ipc_cn_allowlist.py
git commit -m "test(ipc): allowlist gates the event WS and never touches unix (#325)"
```

---

### Task 4: Documentation — env var + operator guidance

**Files:**
- Modify: `.env.example`
- Modify: `specs/001-ccswitch-web/quickstart.md`

**Interfaces:**
- Consumes/Produces: nothing (docs only).

- [ ] **Step 1: Add the var to `.env.example`**

Near the other `CCSWITCH_SESSIOND_*` TLS vars, add:

```bash
# Optional client-cert identity allowlist for the cross-host mTLS daemon.
# Comma-separated CN/SAN values. UNSET = any CA-signed client cert is accepted (v1).
# When set, a CA-signed client whose CN/SAN is not listed fails the TLS handshake.
# CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge.internal
```

- [ ] **Step 2: Document it in the quickstart**

In `specs/001-ccswitch-web/quickstart.md`, in the mTLS section, add a subsection:

```markdown
### Restricting which client certs are accepted (optional)

By default any client cert signed by `CCSWITCH_SESSIOND_TLS_CA` is accepted. To restrict to
specific client identities, set on the **daemon**:

    CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge.internal,edge2.internal

Values are matched **exactly** against the client cert's Common Name (CN) and Subject
Alternative Names (SAN). Modern certs carry identity in the SAN — issue client certs whose
SAN matches a listed value.

Troubleshooting: a rejected client sees only a generic TLS handshake error on the **edge**
side. The authoritative diagnostic is the **daemon** log line
`daemon mTLS: rejected client identities [...] (not in allowlist)`.
```

- [ ] **Step 3: Commit**

```bash
git add .env.example specs/001-ccswitch-web/quickstart.md
git commit -m "docs(ipc): document CCSWITCH_SESSIOND_CLIENT_ALLOWLIST (#325)"
```

---

## Final verification (after all tasks)

- [ ] Full new-test file green: `pytest server/tests/test_ipc_cn_allowlist.py -q`.
- [ ] mTLS regression suite green: `pytest server/tests/test_ipc_serve_tls.py server/tests/test_ipc_client_mtls.py server/tests/test_vnc_ws_mtls.py server/tests/test_remote_mode_tls.py -q`.
- [ ] `ruff check` + `mypy` clean on all changed `server/` files (modulo pre-existing `ccswitch.*` import-untyped notes).
- [ ] Open PR per CLAUDE.md completion workflow (non-draft, `--squash --auto`), body `Fixes #325`, security-sensitive note for reviewers (Principle IV).

## Self-Review notes (author)

- **Spec coverage:** enforcement mechanism → Task 2; helpers/parse/match → Task 1; transport-wide (WS) + unix no-op → Tasks 2–3; permissive default → Task 2; docs/env → Task 4. All spec sections mapped.
- **Highest risk (asyncio propagation)** is gated by Task 2 Step 7 with an explicit STOP + fallback.
- **Type consistency:** `daemon_ssl_context_factory(allow)`, `uvicorn_run_kwargs(..., allowlist=)`, `serve_daemon_tls(app, certs, allowlist=)`, `peer_identities/allowed/client_allowlist` names are used identically across tasks.
