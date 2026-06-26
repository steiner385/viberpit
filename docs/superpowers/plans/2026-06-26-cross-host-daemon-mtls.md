# Cross-host daemon transport (mTLS) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the web edge talk to the session daemon on a different host over TCP with mutual-TLS (mTLS) auth, while same-host Unix-socket deployments keep working byte-for-byte.

**Architecture:** Introduce a scheme-aware endpoint (`unix:///path` default, `https://host:port` opt-in) resolved in `server/ipc/paths.py`. The daemon binds either a UDS or a TCP+mTLS port (uvicorn `ssl_cert_reqs=CERT_REQUIRED`); the edge's `DaemonClient` and the VNC proxy branch their transport on the endpoint scheme, sharing one `daemon_ws_connect` helper and one client `ssl.SSLContext`. Auth is enforced entirely at the TLS handshake — no per-RPC auth code. Spec: `docs/superpowers/specs/2026-06-26-cross-host-daemon-mtls-design.md`.

**Tech Stack:** Python 3.12, FastAPI, uvicorn 0.49 (native mTLS), httpx 0.28 (`verify=SSLContext`), websockets 16 (`connect(..., ssl=ctx)` / `unix_connect`), cryptography 49 (test cert minting), pytest + pytest-asyncio.

## Global Constraints

- **Back-compat is load-bearing.** `unix://` is the default; bare `CCSWITCH_SESSIOND_SOCKET` and the `/run/ccswitch/sessiond.sock` default must keep working unchanged. `DaemonClient(str)` must keep working (≈30 existing call sites pass a bare path string). `sessiond_socket()` and `DEFAULT_SOCKET_PATH` keep their exact current signatures/semantics.
- **No insecure fallback** (constitution Principle IV): an `https` endpoint with a missing/unreadable TLS file must fail loud at startup, never downgrade to plaintext or silently go in-process.
- **mTLS is the only auth.** Enforced at the TLS handshake via uvicorn `ssl_cert_reqs=ssl.CERT_REQUIRED` + `ssl_ca_certs=<ca>`. v1 trust = any client cert signed by the configured CA. **CN/SAN allowlist, cert rotation, ACME, per-RPC authz are OUT of scope (v2).**
- **New env vars** (exact names): `CCSWITCH_SESSIOND_ENDPOINT`; daemon side `CCSWITCH_SESSIOND_TLS_CERT` / `CCSWITCH_SESSIOND_TLS_KEY` / `CCSWITCH_SESSIOND_TLS_CA`; edge side `CCSWITCH_SESSIOND_CLIENT_CERT` / `CCSWITCH_SESSIOND_CLIENT_KEY` / `CCSWITCH_SESSIOND_CLIENT_CA`.
- **Do not touch** the RPC/event/VNC protocol (`server/ipc/protocol.py`, `PROTOCOL_VERSION = 1`) — only the transport.
- **Do not add fields to `server/config.py`** — TLS material lives in the new `server/ipc/tls.py` (avoids the dual pydantic/dataclass Settings duplication).
- **Test environment** (this worktree has no built env). Run server tests against the main checkout's venv with this worktree on `PYTHONPATH`, matching CI env:
  ```bash
  cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/7fad78e8
  env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 \
    PYTHONPATH="$PWD/src" \
    /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>
  ```
  Known-environmental failures on this host (NOT regressions): `test_auth.py` (5), `test_health.py` (2) — live daemon socket makes `create_app()` refuse in-process; `test_x11_integration.py` (1) — real Xvfb.
- **Backend CI lint gates** (run before pushing): `ruff check server/` and `mypy server/` must pass.

---

## File Structure

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `server/tests/tls_certs.py` | Create | Test-only helpers minting a CA + leaf certs with `cryptography`. |
| `server/tests/conftest.py` | Modify | Add the `mtls_certs` pytest fixture (writes CA/server/client/rogue certs to tmp). |
| `server/ipc/paths.py` | Modify | Add `Endpoint`, `parse_endpoint()`, `sessiond_endpoint()`. Keep `sessiond_socket()`/`DEFAULT_SOCKET_PATH` unchanged. |
| `server/ipc/tls.py` | Create | TLS file resolution (`daemon_tls_files`/`client_tls_files`), `client_ssl_context()`, `TLSConfigError`. |
| `server/ipc/serve.py` | Modify | `uvicorn_run_kwargs(endpoint, daemon_tls=None)` + branch `serve()` on scheme. |
| `server/tests/ipc_harness.py` | Modify | Add `serve_daemon_tls(app, certs)` async ctx mgr (reuses `uvicorn_run_kwargs`). |
| `server/ipc/client.py` | Modify | `daemon_ws_connect()` shared helper; `DaemonClient` accepts `Endpoint|str` + mTLS transport. |
| `server/bridge/vnc_ws.py` | Modify | `/vnc` proxy branches transport via `daemon_ws_connect` (endpoint-aware). |
| `server/app.py` | Modify | Endpoint-aware `_daemon_present`/`_daemon_compatible`; mode-select via `sessiond_endpoint()`; wire `daemon_endpoint` + client ssl into the VNC router. |
| `.env.example` | Modify | Document the new env vars. |
| `specs/001-ccswitch-web/quickstart.md` | Modify | Cert-generation recipe + cross-host setup. |

Test files created: `test_ipc_endpoint.py`, `test_ipc_tls.py`, `test_ipc_serve_tls.py`, `test_ipc_client_mtls.py`, `test_vnc_ws_mtls.py`, `test_remote_mode_tls.py`.

---

### Task 1: Test cert-minting helper + `mtls_certs` fixture

**Files:**
- Create: `server/tests/tls_certs.py`
- Modify: `server/tests/conftest.py`
- Test: `server/tests/test_tls_certs_fixture.py` (smoke test, deleted-or-kept — keep, it guards the fixture)

**Interfaces:**
- Produces: `make_ca() -> (key, cert)`; `make_leaf(ca_key, ca_cert, common_name, *, san_dns=None, san_ip=None) -> (key, cert)`; `write_pem_cert(path, cert)`, `write_pem_key(path, key)`. Fixture `mtls_certs` → object with str attrs: `ca, server_cert, server_key, client_cert, client_key, rogue_ca, rogue_client_cert, rogue_client_key`.

- [ ] **Step 1: Write the cert helper**

Create `server/tests/tls_certs.py`:

```python
"""Test-only TLS cert minting (cryptography). Fixed validity dates → no clock dependency."""
from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_NOT_BEFORE = datetime.datetime(2020, 1, 1)
_NOT_AFTER = datetime.datetime(2099, 1, 1)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_ca():
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ccswitch-test-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_BEFORE).not_valid_after(_NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_leaf(ca_key, ca_cert, common_name, *, san_dns=None, san_ip=None):
    key = _key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_BEFORE).not_valid_after(_NOT_AFTER)
    )
    san = [x509.DNSName(d) for d in (san_dns or [])]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in (san_ip or [])]
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


def write_pem_cert(path: Path, cert) -> None:
    Path(path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def write_pem_key(path: Path, key) -> None:
    Path(path).write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
```

- [ ] **Step 2: Add the fixture to conftest**

Append to `server/tests/conftest.py`:

```python
import dataclasses

import pytest

from server.tests.tls_certs import make_ca, make_leaf, write_pem_cert, write_pem_key


@dataclasses.dataclass(frozen=True)
class MTLSCerts:
    ca: str
    server_cert: str
    server_key: str
    client_cert: str
    client_key: str
    rogue_ca: str
    rogue_client_cert: str
    rogue_client_key: str


@pytest.fixture
def mtls_certs(tmp_path) -> MTLSCerts:
    d = tmp_path / "tls"
    d.mkdir()
    ca_key, ca_cert = make_ca()
    write_pem_cert(d / "ca.pem", ca_cert)
    s_key, s_cert = make_leaf(ca_key, ca_cert, "sessiond",
                              san_dns=["localhost"], san_ip=["127.0.0.1"])
    write_pem_cert(d / "server.pem", s_cert)
    write_pem_key(d / "server.key", s_key)
    c_key, c_cert = make_leaf(ca_key, ca_cert, "edge")
    write_pem_cert(d / "client.pem", c_cert)
    write_pem_key(d / "client.key", c_key)
    rca_key, rca_cert = make_ca()
    write_pem_cert(d / "rogue_ca.pem", rca_cert)
    rc_key, rc_cert = make_leaf(rca_key, rca_cert, "rogue")
    write_pem_cert(d / "rogue_client.pem", rc_cert)
    write_pem_key(d / "rogue_client.key", rc_key)
    return MTLSCerts(
        ca=str(d / "ca.pem"),
        server_cert=str(d / "server.pem"), server_key=str(d / "server.key"),
        client_cert=str(d / "client.pem"), client_key=str(d / "client.key"),
        rogue_ca=str(d / "rogue_ca.pem"),
        rogue_client_cert=str(d / "rogue_client.pem"),
        rogue_client_key=str(d / "rogue_client.key"),
    )
```

- [ ] **Step 3: Write the smoke test**

Create `server/tests/test_tls_certs_fixture.py`:

```python
import ssl


def test_mtls_certs_chain_verifies(mtls_certs):
    # Server context loads its cert/key.
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(mtls_certs.server_cert, mtls_certs.server_key)
    # A client context trusting the CA accepts the client cert chain.
    client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=mtls_certs.ca)
    client_ctx.load_cert_chain(mtls_certs.client_cert, mtls_certs.client_key)
    # Rogue client is signed by a different CA than the trusted one.
    assert mtls_certs.rogue_ca != mtls_certs.ca
```

- [ ] **Step 4: Run tests**

Run: `… -m pytest server/tests/test_tls_certs_fixture.py -v`
Expected: PASS (2 contexts build without error).

- [ ] **Step 5: Commit**

```bash
git add server/tests/tls_certs.py server/tests/conftest.py server/tests/test_tls_certs_fixture.py
git commit -m "test(ipc): mTLS cert-minting helper + mtls_certs fixture (#205)"
```

---

### Task 2: Scheme-aware `Endpoint` in `paths.py`

**Files:**
- Modify: `server/ipc/paths.py`
- Test: `server/tests/test_ipc_endpoint.py`

**Interfaces:**
- Produces: `Endpoint(scheme, path=None, host=None, port=None)` with props `.is_unix`, `.is_tcp`; `parse_endpoint(raw: str) -> Endpoint`; `sessiond_endpoint() -> Endpoint`.
- Unchanged: `DEFAULT_SOCKET_PATH`, `sessiond_socket() -> str`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_endpoint.py`:

```python
import os
from unittest import mock

import pytest

from server.ipc.paths import (
    DEFAULT_SOCKET_PATH, Endpoint, parse_endpoint, sessiond_endpoint, sessiond_socket,
)


def test_parse_unix_scheme():
    ep = parse_endpoint("unix:///run/ccswitch/sessiond.sock")
    assert ep.is_unix and ep.path == "/run/ccswitch/sessiond.sock"


def test_parse_bare_path_is_unix():
    ep = parse_endpoint("/tmp/x.sock")
    assert ep.is_unix and ep.path == "/tmp/x.sock"


def test_parse_https_scheme():
    ep = parse_endpoint("https://daemon.host:9443")
    assert ep.is_tcp and ep.host == "daemon.host" and ep.port == 9443


def test_parse_https_requires_host_and_port():
    with pytest.raises(ValueError):
        parse_endpoint("https://daemon.host")


def test_parse_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        parse_endpoint("ftp://nope")


def test_sessiond_endpoint_precedence():
    # ENDPOINT wins over legacy SOCKET.
    with mock.patch.dict(os.environ, {
        "CCSWITCH_SESSIOND_ENDPOINT": "https://h:1",
        "CCSWITCH_SESSIOND_SOCKET": "/tmp/legacy.sock",
    }):
        assert sessiond_endpoint() == Endpoint("https", host="h", port=1)
    # legacy SOCKET (bare path) maps to unix://.
    env = {k: v for k, v in os.environ.items()
           if k not in ("CCSWITCH_SESSIOND_ENDPOINT", "CCSWITCH_SESSIOND_SOCKET")}
    with mock.patch.dict(os.environ, {**env, "CCSWITCH_SESSIOND_SOCKET": "/tmp/legacy.sock"}, clear=True):
        assert sessiond_endpoint() == Endpoint("unix", path="/tmp/legacy.sock")
    # nothing set → default unix socket.
    with mock.patch.dict(os.environ, env, clear=True):
        assert sessiond_endpoint() == Endpoint("unix", path=DEFAULT_SOCKET_PATH)


def test_sessiond_socket_unchanged():
    env = {k: v for k, v in os.environ.items() if k != "CCSWITCH_SESSIOND_SOCKET"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert sessiond_socket() == DEFAULT_SOCKET_PATH
```

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_ipc_endpoint.py -v`
Expected: FAIL (`ImportError: cannot import name 'Endpoint'`).

- [ ] **Step 3: Implement**

Replace `server/ipc/paths.py` with:

```python
"""Well-known endpoint for the session daemon — single source of truth for edge + daemon.

Supports two transports:
  * unix:///path   — same-host Unix socket (default; trust = file permissions).
  * https://host:port — cross-host TCP with mutual TLS (see server/ipc/tls.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_SOCKET_PATH = "/run/ccswitch/sessiond.sock"


@dataclass(frozen=True)
class Endpoint:
    scheme: str                 # "unix" | "https"
    path: str | None = None     # unix only
    host: str | None = None     # https only
    port: int | None = None     # https only

    @property
    def is_unix(self) -> bool:
        return self.scheme == "unix"

    @property
    def is_tcp(self) -> bool:
        return self.scheme == "https"


def parse_endpoint(raw: str) -> Endpoint:
    """Parse a daemon endpoint string. A bare path is treated as a unix socket."""
    raw = raw.strip()
    if raw.startswith("unix://"):
        path = raw[len("unix://"):]
        if not path:
            raise ValueError(f"unix endpoint requires a path: {raw!r}")
        return Endpoint("unix", path=path)
    if raw.startswith("https://"):
        u = urlparse(raw)
        if not u.hostname or not u.port:
            raise ValueError(f"https endpoint requires host and port: {raw!r}")
        return Endpoint("https", host=u.hostname, port=u.port)
    if raw.startswith("/"):
        return Endpoint("unix", path=raw)
    raise ValueError(f"unsupported daemon endpoint: {raw!r}")


def sessiond_endpoint() -> Endpoint:
    """Resolve the daemon endpoint: CCSWITCH_SESSIOND_ENDPOINT, else the legacy
    CCSWITCH_SESSIOND_SOCKET (bare path → unix://), else the well-known default."""
    raw = os.environ.get("CCSWITCH_SESSIOND_ENDPOINT")
    if raw:
        return parse_endpoint(raw)
    sock = os.environ.get("CCSWITCH_SESSIOND_SOCKET")
    if sock:
        return parse_endpoint(sock if "://" in sock else f"unix://{sock}")
    return Endpoint("unix", path=DEFAULT_SOCKET_PATH)


def sessiond_socket() -> str:
    """The daemon UDS path: CCSWITCH_SESSIOND_SOCKET if set, else the well-known default.

    Unchanged legacy accessor (same-host callers, e.g. the deploy controller)."""
    return os.environ.get("CCSWITCH_SESSIOND_SOCKET", DEFAULT_SOCKET_PATH)
```

- [ ] **Step 4: Run tests**

Run: `… -m pytest server/tests/test_ipc_endpoint.py server/tests/test_ipc_handshake.py -v`
Expected: PASS (new endpoint tests + the unchanged handshake tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/paths.py server/tests/test_ipc_endpoint.py
git commit -m "feat(ipc): scheme-aware daemon Endpoint (unix:// default, https://) (#205)"
```

---

### Task 3: TLS material module `server/ipc/tls.py`

**Files:**
- Create: `server/ipc/tls.py`
- Test: `server/tests/test_ipc_tls.py`

**Interfaces:**
- Produces: `TLSConfigError`; `DaemonTLSFiles(cert, key, ca)`; `ClientTLSFiles(cert, key, ca)`; `daemon_tls_files() -> DaemonTLSFiles`; `client_tls_files() -> ClientTLSFiles`; `client_ssl_context(files: ClientTLSFiles) -> ssl.SSLContext`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_tls.py`:

```python
import os
import ssl
from unittest import mock

import pytest

from server.ipc.tls import (
    ClientTLSFiles, TLSConfigError, client_ssl_context, client_tls_files, daemon_tls_files,
)


def test_daemon_tls_files_missing_raises():
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(TLSConfigError):
            daemon_tls_files()


def test_client_tls_files_unreadable_raises(tmp_path):
    env = {
        "CCSWITCH_SESSIOND_CLIENT_CERT": str(tmp_path / "nope.pem"),
        "CCSWITCH_SESSIOND_CLIENT_KEY": str(tmp_path / "nope.key"),
        "CCSWITCH_SESSIOND_CLIENT_CA": str(tmp_path / "nope.ca"),
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(TLSConfigError):
            client_tls_files()


def test_daemon_tls_files_reads_env(mtls_certs):
    env = {
        "CCSWITCH_SESSIOND_TLS_CERT": mtls_certs.server_cert,
        "CCSWITCH_SESSIOND_TLS_KEY": mtls_certs.server_key,
        "CCSWITCH_SESSIOND_TLS_CA": mtls_certs.ca,
    }
    with mock.patch.dict(os.environ, env, clear=True):
        files = daemon_tls_files()
    assert files.cert == mtls_certs.server_cert and files.ca == mtls_certs.ca


def test_client_ssl_context_loads(mtls_certs):
    ctx = client_ssl_context(ClientTLSFiles(
        cert=mtls_certs.client_cert, key=mtls_certs.client_key, ca=mtls_certs.ca))
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED  # the client verifies the server
```

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_ipc_tls.py -v`
Expected: FAIL (`ModuleNotFoundError: server.ipc.tls`).

- [ ] **Step 3: Implement**

Create `server/ipc/tls.py`:

```python
"""TLS material + SSL contexts for the cross-host (mTLS) daemon transport.

Only consulted when the daemon endpoint scheme is `https`. For `unix://` the trust
boundary is the socket file's permissions and none of this is used.
"""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass


class TLSConfigError(RuntimeError):
    """A required TLS file is missing/unreadable for an https endpoint."""


@dataclass(frozen=True)
class DaemonTLSFiles:
    cert: str   # server cert the daemon presents
    key: str    # server private key
    ca: str     # CA used to verify CLIENT certs


@dataclass(frozen=True)
class ClientTLSFiles:
    cert: str   # client cert the edge presents
    key: str    # client private key
    ca: str     # CA used to verify the SERVER cert


def _require(path: str | None, what: str) -> str:
    if not path:
        raise TLSConfigError(f"https daemon endpoint requires {what} (unset)")
    if not os.access(path, os.R_OK):
        raise TLSConfigError(f"{what} not readable: {path}")
    return path


def daemon_tls_files() -> DaemonTLSFiles:
    return DaemonTLSFiles(
        cert=_require(os.environ.get("CCSWITCH_SESSIOND_TLS_CERT"), "CCSWITCH_SESSIOND_TLS_CERT"),
        key=_require(os.environ.get("CCSWITCH_SESSIOND_TLS_KEY"), "CCSWITCH_SESSIOND_TLS_KEY"),
        ca=_require(os.environ.get("CCSWITCH_SESSIOND_TLS_CA"), "CCSWITCH_SESSIOND_TLS_CA"),
    )


def client_tls_files() -> ClientTLSFiles:
    return ClientTLSFiles(
        cert=_require(os.environ.get("CCSWITCH_SESSIOND_CLIENT_CERT"), "CCSWITCH_SESSIOND_CLIENT_CERT"),
        key=_require(os.environ.get("CCSWITCH_SESSIOND_CLIENT_KEY"), "CCSWITCH_SESSIOND_CLIENT_KEY"),
        ca=_require(os.environ.get("CCSWITCH_SESSIOND_CLIENT_CA"), "CCSWITCH_SESSIOND_CLIENT_CA"),
    )


def client_ssl_context(files: ClientTLSFiles) -> ssl.SSLContext:
    """SSL context the edge uses for httpx (verify=) AND websockets (ssl=).

    Trusts the configured CA for the server cert and presents the client cert chain.
    check_hostname stays True — the client verifies the daemon's cert SAN.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=files.ca)
    ctx.load_cert_chain(certfile=files.cert, keyfile=files.key)
    return ctx
```

- [ ] **Step 4: Run tests**

Run: `… -m pytest server/tests/test_ipc_tls.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/tls.py server/tests/test_ipc_tls.py
git commit -m "feat(ipc): TLS material resolution + client SSL context (#205)"
```

---

### Task 4: mTLS daemon bind in `serve.py` + TLS test harness

**Files:**
- Modify: `server/ipc/serve.py` (imports at top; add `uvicorn_run_kwargs`; rewrite `serve()` lines 89-94)
- Modify: `server/tests/ipc_harness.py`
- Test: `server/tests/test_ipc_serve_tls.py`

**Interfaces:**
- Consumes: `Endpoint`, `sessiond_endpoint` (Task 2); `DaemonTLSFiles`, `daemon_tls_files` (Task 3).
- Produces: `uvicorn_run_kwargs(endpoint: Endpoint, *, daemon_tls: DaemonTLSFiles | None = None) -> dict`; harness `serve_daemon_tls(app, certs)` async ctx mgr yielding an `Endpoint`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_serve_tls.py`:

```python
import ssl
from unittest import mock

import httpx
import pytest

from server.ipc.paths import Endpoint
from server.ipc.serve import build_daemon, uvicorn_run_kwargs
from server.ipc.tls import DaemonTLSFiles, ClientTLSFiles, client_ssl_context
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.ipc_harness import serve_daemon_tls


def test_uvicorn_kwargs_unix():
    kw = uvicorn_run_kwargs(Endpoint("unix", path="/tmp/x.sock"))
    assert kw == {"uds": "/tmp/x.sock"}


def test_uvicorn_kwargs_https_enforces_client_certs(mtls_certs):
    tls = DaemonTLSFiles(cert=mtls_certs.server_cert, key=mtls_certs.server_key, ca=mtls_certs.ca)
    kw = uvicorn_run_kwargs(Endpoint("https", host="127.0.0.1", port=9443), daemon_tls=tls)
    assert kw["host"] == "127.0.0.1" and kw["port"] == 9443
    assert kw["ssl_certfile"] == mtls_certs.server_cert
    assert kw["ssl_ca_certs"] == mtls_certs.ca
    assert kw["ssl_cert_reqs"] == ssl.CERT_REQUIRED  # this is what makes it mutual


@pytest.mark.asyncio
async def test_daemon_serves_info_over_mtls(mtls_certs):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_tls(app, mtls_certs) as ep:
        ctx = client_ssl_context(ClientTLSFiles(
            cert=mtls_certs.client_cert, key=mtls_certs.client_key, ca=mtls_certs.ca))
        async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}",
                                     verify=ctx, timeout=5.0) as c:
            r = await c.get("/info")
            assert r.json()["protocol"] == 1


@pytest.mark.asyncio
async def test_daemon_rejects_client_without_cert(mtls_certs):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_tls(app, mtls_certs) as ep:
        # Trust the server CA but present NO client cert → handshake must fail.
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=mtls_certs.ca)
        with pytest.raises((ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError, OSError)):
            async with httpx.AsyncClient(base_url=f"https://{ep.host}:{ep.port}",
                                         verify=ctx, timeout=5.0) as c:
                await c.get("/info")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_ipc_serve_tls.py -v`
Expected: FAIL (`ImportError: cannot import name 'uvicorn_run_kwargs'`).

- [ ] **Step 3: Implement `uvicorn_run_kwargs` + branch `serve()`**

In `server/ipc/serve.py`, change the import line `from server.ipc.paths import sessiond_socket` to:

```python
from server.ipc.paths import Endpoint, sessiond_endpoint, sessiond_socket
```

Add near the top (after imports):

```python
import ssl

from server.ipc.tls import DaemonTLSFiles, daemon_tls_files


def uvicorn_run_kwargs(endpoint: Endpoint, *, daemon_tls: DaemonTLSFiles | None = None) -> dict:
    """Build uvicorn bind kwargs for a daemon endpoint.

    unix  -> {uds}.
    https -> {host, port, ssl_*}. ssl_cert_reqs=CERT_REQUIRED makes it MUTUAL TLS:
             a client without a CA-signed cert fails the handshake.
    """
    if endpoint.is_unix:
        return {"uds": endpoint.path}
    files = daemon_tls or daemon_tls_files()
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "ssl_certfile": files.cert,
        "ssl_keyfile": files.key,
        "ssl_ca_certs": files.ca,
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
```

Replace `serve()` (lines 89-94) with:

```python
def serve(socket_path: str | None = None) -> None:
    # Back-compat: an explicit socket_path arg forces a unix bind at that path.
    endpoint = Endpoint("unix", path=socket_path) if socket_path else sessiond_endpoint()
    app, orchestrator = build_daemon()
    app.router.lifespan_context = lambda _app: daemon_lifespan(_app, orchestrator)
    where = endpoint.path if endpoint.is_unix else f"{endpoint.host}:{endpoint.port} (mTLS)"
    log.info("session daemon serving on %s", where)
    uvicorn.run(app, log_level="info", **uvicorn_run_kwargs(endpoint))
```

(`sessiond_socket` stays imported for any existing reference; remove only if unused after this edit — check with `ruff`.)

- [ ] **Step 4: Add the TLS harness**

Append to `server/tests/ipc_harness.py`:

```python
import socket as _socket
import ssl  # noqa: F401  (kept for parity / future use)

from server.ipc.paths import Endpoint
from server.ipc.serve import uvicorn_run_kwargs
from server.ipc.tls import DaemonTLSFiles


def _free_port() -> int:
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.asynccontextmanager
async def serve_daemon_tls(app, certs):
    """Serve `app` over mTLS on an ephemeral 127.0.0.1 port; yield its Endpoint."""
    endpoint = Endpoint("https", host="127.0.0.1", port=_free_port())
    tls = DaemonTLSFiles(cert=certs.server_cert, key=certs.server_key, ca=certs.ca)
    kwargs = uvicorn_run_kwargs(endpoint, daemon_tls=tls)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", **kwargs))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield endpoint
    finally:
        server.should_exit = True
        await task
```

- [ ] **Step 5: Run tests**

Run: `… -m pytest server/tests/test_ipc_serve_tls.py server/tests/test_ipc_serve.py -v`
Expected: PASS (new TLS serve tests + the unchanged UDS serve tests).

- [ ] **Step 6: Commit**

```bash
git add server/ipc/serve.py server/tests/ipc_harness.py server/tests/test_ipc_serve_tls.py
git commit -m "feat(ipc): mTLS daemon bind (uvicorn CERT_REQUIRED) + TLS test harness (#205)"
```

---

### Task 5: mTLS `DaemonClient` + shared `daemon_ws_connect`

**Files:**
- Modify: `server/ipc/client.py` (`__init__` lines 35-49; `_reader` connect line 127; add module-level helper)
- Test: `server/tests/test_ipc_client_mtls.py`

**Interfaces:**
- Consumes: `Endpoint`, `parse_endpoint` (Task 2); `client_ssl_context`, `client_tls_files`, `ClientTLSFiles` (Task 3); `serve_daemon_tls` (Task 4).
- Produces: `daemon_ws_connect(endpoint: Endpoint, path: str, ssl_ctx=None)` (returns a websockets connect ctx mgr); `DaemonClient(endpoint: Endpoint | str, *, client_tls: ClientTLSFiles | None = None)`; new attribute `DaemonClient.endpoint`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_client_mtls.py`:

```python
from unittest import mock

import pytest

from server.ipc.client import DaemonClient, DaemonUnavailable
from server.ipc.serve import build_daemon
from server.ipc.tls import ClientTLSFiles
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_tls


def _orch():
    o = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                            usage_fn=lambda n: None)
    o._records["s1"] = SessionRecord(id="s1", name="S1", account="acct",
                                     claude_session_id="cs", cols=80, rows=24, cwd="/tmp")
    return o


def _good_tls(c):
    return ClientTLSFiles(cert=c.client_cert, key=c.client_key, ca=c.ca)


@pytest.mark.asyncio
async def test_rpc_and_events_over_mtls(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with serve_daemon_tls(app, mtls_certs) as ep:
        client = DaemonClient(ep, client_tls=_good_tls(mtls_certs))
        try:
            info = await client.info()
            assert info["protocol"] == 1
            listed = await client.call("list_sessions")
            assert [r["id"] for r in listed["sessions"]] == ["s1"]
            # event stream round-trips over wss
            gen = client.subscribe("demo")
            await client.events_ready("demo")
            await client.call("echo", stream="demo", text="hi")
            evt = await gen.__anext__()
            assert evt["text"] == "hi"
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_rpc_rejected_without_client_cert(mtls_certs):
    app, _ = build_daemon(orchestrator=_orch())
    async with serve_daemon_tls(app, mtls_certs) as ep:
        # Trust server CA but present no client cert (use a CA-only context via a bogus pair).
        bad = ClientTLSFiles(cert=mtls_certs.rogue_client_cert,
                             key=mtls_certs.rogue_client_key, ca=mtls_certs.ca)
        client = DaemonClient(ep, client_tls=bad)
        try:
            with pytest.raises((DaemonUnavailable, Exception)):
                await client.info()
        finally:
            await client.aclose()


def test_str_path_back_compat_is_unix():
    c = DaemonClient("/tmp/x.sock")
    assert c.endpoint.is_unix and c.endpoint.path == "/tmp/x.sock"
```

(Note: the rogue client cert is signed by `rogue_ca`, which the daemon's `ssl_ca_certs=ca` does NOT trust → handshake rejected; that is the "untrusted client" case, equivalent in effect to "no valid cert".)

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_ipc_client_mtls.py -v`
Expected: FAIL (`DaemonClient(ep, client_tls=...)` rejects the `Endpoint`/kwarg).

- [ ] **Step 3: Implement**

In `server/ipc/client.py`, add imports at top:

```python
from server.ipc.paths import Endpoint, parse_endpoint
```

Add a module-level helper (after the exception classes):

```python
def daemon_ws_connect(endpoint: Endpoint, path: str, ssl_ctx=None):
    """Return a websockets connect context manager for a daemon WS path.

    Single source of truth for opening edge→daemon WebSockets (events + vnc).
    `path` is like "/events" or "/vnc?session=abc".
    """
    if endpoint.is_unix:
        return websockets.unix_connect(endpoint.path, f"ws://daemon{path}")
    return websockets.connect(f"wss://{endpoint.host}:{endpoint.port}{path}", ssl=ssl_ctx)
```

Replace `DaemonClient.__init__` (lines 35-49) with:

```python
    def __init__(self, endpoint, *, client_tls=None):
        if isinstance(endpoint, str):
            endpoint = parse_endpoint(endpoint if "://" in endpoint else f"unix://{endpoint}")
        self.endpoint = endpoint
        if endpoint.is_unix:
            self._sock = endpoint.path
            self._ssl = None
            self._http = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=endpoint.path),
                base_url="http://daemon",
                timeout=30.0,
            )
        else:
            from server.ipc.tls import client_ssl_context, client_tls_files
            self._ssl = client_ssl_context(client_tls or client_tls_files())
            self._sock = None
            self._http = httpx.AsyncClient(
                base_url=f"https://{endpoint.host}:{endpoint.port}",
                verify=self._ssl,
                timeout=30.0,
            )
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._desired: set[str] = set()
        self._last_seq: dict[str, int] = {}
        self._baseline: dict[str, int | None] = {}
        self._started = False
```

In `_reader` (line 122-136), replace the connect line. Change:

```python
        uri = "ws://daemon/events"
        backoff = 0.05
        while True:
            try:
                async with websockets.unix_connect(self._sock, uri) as ws:
```

to:

```python
        backoff = 0.05
        while True:
            try:
                async with daemon_ws_connect(self.endpoint, "/events", self._ssl) as ws:
```

(The rest of `_reader` is unchanged.)

- [ ] **Step 4: Run tests**

Run: `… -m pytest server/tests/test_ipc_client_mtls.py server/tests/test_ipc_client_rpc.py server/tests/test_ipc_client_events.py server/tests/test_ipc_client_reconnect.py -v`
Expected: PASS (new mTLS tests + all existing UDS client tests, proving back-compat).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/client.py server/tests/test_ipc_client_mtls.py
git commit -m "feat(ipc): mTLS DaemonClient + shared daemon_ws_connect helper (#205)"
```

---

### Task 6: Endpoint-aware `/vnc` proxy in `vnc_ws.py`

**Files:**
- Modify: `server/bridge/vnc_ws.py` (`build_vnc_ws_router` signature line 104; the remote branch lines 118-140)
- Test: `server/tests/test_vnc_ws_mtls.py`

**Interfaces:**
- Consumes: `daemon_ws_connect` (Task 5); `Endpoint` (Task 2).
- Produces: `build_vnc_ws_router(get_x11, get_daemon_endpoint=lambda: None, get_daemon_ssl=lambda: None)` — the remote branch fires when `get_daemon_endpoint()` returns a truthy `Endpoint`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_vnc_ws_mtls.py`:

```python
"""The /vnc proxy must open the daemon WS via daemon_ws_connect (endpoint-aware)."""
from unittest import mock

from server.ipc.paths import Endpoint


def test_router_uses_daemon_ws_connect_for_remote(monkeypatch):
    import server.bridge.vnc_ws as mod
    captured = {}

    class _FakeWS:
        close_code = 1000
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    def _fake_connect(endpoint, path, ssl_ctx=None):
        captured["endpoint"] = endpoint
        captured["path"] = path
        captured["ssl"] = ssl_ctx
        return _FakeWS()

    monkeypatch.setattr(mod, "daemon_ws_connect", _fake_connect, raising=False)
    ep = Endpoint("https", host="127.0.0.1", port=9443)
    router = mod.build_vnc_ws_router(
        get_x11=lambda: None,
        get_daemon_endpoint=lambda: ep,
        get_daemon_ssl=lambda: "SSLCTX",
    )
    assert router is not None  # smoke: builds with the new signature
```

(Full end-to-end relay is covered by the existing `test_vnc_ws_remote.py`, retargeted in Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_vnc_ws_mtls.py -v`
Expected: FAIL (`build_vnc_ws_router` has no `get_daemon_endpoint` kwarg).

- [ ] **Step 3: Implement**

In `server/bridge/vnc_ws.py`, add at top (with the other imports):

```python
from server.ipc.client import daemon_ws_connect
```

Change the signature (line 104):

```python
def build_vnc_ws_router(get_x11, get_daemon_endpoint=lambda: None, get_daemon_ssl=lambda: None) -> APIRouter:
```

Replace the remote branch (lines 117-140) with:

```python
        session_id = websocket.query_params.get("session", "")
        endpoint = get_daemon_endpoint()
        if endpoint:
            from websockets.exceptions import ConnectionClosed
            await websocket.accept()
            close_code = 1000
            try:
                path = f"/vnc?session={session_id}"
                async with daemon_ws_connect(endpoint, path, get_daemon_ssl()) as dws:
                    await _relay(websocket, _WSAdapter(dws))
                    close_code = dws.close_code or 1000
            except ConnectionClosed as exc:
                close_code = exc.code or 1000
            except Exception:
                log.exception("vnc: remote proxy failed session=%s", session_id)
            if 4000 <= close_code < 5000:
                try:
                    await websocket.close(close_code)
                except Exception:
                    pass
            return
```

- [ ] **Step 4: Update the existing remote test to the new signature**

In `server/tests/test_vnc_ws_remote.py`, update calls to `build_vnc_ws_router(...)`: the daemon-socket getter is now `get_daemon_endpoint` returning an `Endpoint("unix", path=sock)` (import `Endpoint` from `server.ipc.paths`). Add `get_daemon_ssl=lambda: None`. The relay behavior is unchanged, so the assertions stand.

- [ ] **Step 5: Run tests**

Run: `… -m pytest server/tests/test_vnc_ws_mtls.py server/tests/test_vnc_ws_remote.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/bridge/vnc_ws.py server/tests/test_vnc_ws_mtls.py server/tests/test_vnc_ws_remote.py
git commit -m "feat(vnc): endpoint-aware /vnc proxy via daemon_ws_connect (#205)"
```

---

### Task 7: Endpoint-aware probe + mode-select in `app.py`

**Files:**
- Modify: `server/app.py` (`_daemon_socket_present`/`_daemon_compatible` lines 40-95; mode-select lines 275-312; VNC router wiring lines 415-419)
- Test: `server/tests/test_remote_mode_tls.py`

**Interfaces:**
- Consumes: `Endpoint`, `sessiond_endpoint`, `DEFAULT_SOCKET_PATH` (Task 2); `client_ssl_context`, `client_tls_files` (Task 3); `DaemonClient` (Task 5).
- Produces: `_daemon_present(endpoint: Endpoint) -> bool`; `_daemon_compatible(endpoint: Endpoint, *, attempts=5, interval=0.5) -> bool`; sets `app.state.daemon_endpoint` (Endpoint | None) and `app.state.daemon_client_ssl` (ssl.SSLContext | None).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_remote_mode_tls.py`:

```python
import os
from unittest import mock

import pytest

from server.ipc.paths import Endpoint


def test_daemon_present_tcp_false_when_unreachable():
    from server.app import _daemon_present
    # Nothing listening on this ephemeral port.
    assert _daemon_present(Endpoint("https", host="127.0.0.1", port=9)) is False


def test_create_app_remote_mode_with_https_endpoint(mtls_certs, monkeypatch):
    """An explicit https endpoint → remote mode + DaemonClient pointed at it."""
    env = {
        "CCSWITCH_SESSIOND_ENDPOINT": "https://127.0.0.1:9443",
        "CCSWITCH_SESSIOND_CLIENT_CERT": mtls_certs.client_cert,
        "CCSWITCH_SESSIOND_CLIENT_KEY": mtls_certs.client_key,
        "CCSWITCH_SESSIOND_CLIENT_CA": mtls_certs.ca,
        "CCSWITCH_DEV": "1",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        from server.app import create_app
        app = create_app()
        assert app.state.remote_mode is True
        assert app.state.daemon_endpoint == Endpoint("https", host="127.0.0.1", port=9443)
        assert app.state.daemon_client_ssl is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `… -m pytest server/tests/test_remote_mode_tls.py -v`
Expected: FAIL (`_daemon_present` missing / `daemon_endpoint` not set).

- [ ] **Step 3: Implement the probes**

In `server/app.py`, replace `_daemon_socket_present` (lines 40-50) and `_daemon_compatible` (lines 53-95) with endpoint-aware versions. Keep the live-event-loop guard.

```python
def _daemon_present(endpoint) -> bool:
    """True if something is listening at the endpoint (socket file exists / TCP connects)."""
    if endpoint.is_unix:
        import os
        import stat
        try:
            return stat.S_ISSOCK(os.stat(endpoint.path).st_mode)
        except OSError:
            return False
    import socket
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=1.0):
            return True
    except OSError:
        return False


def _daemon_compatible(endpoint, *, attempts: int = 5, interval: float = 0.5) -> bool:
    """True iff a protocol-compatible daemon answers /info at the endpoint. Synchronous."""
    import time

    import httpx

    from server.ipc.protocol import PROTOCOL_VERSION
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        logging.getLogger("server").warning(
            "skipping synchronous daemon probe at %s: create_app() ran under a live "
            "event loop where blocking I/O would freeze (or self-deadlock) it", endpoint)
        return False
    if not _daemon_present(endpoint):
        return False

    def _client() -> httpx.Client:
        if endpoint.is_unix:
            return httpx.Client(transport=httpx.HTTPTransport(uds=endpoint.path),
                                base_url="http://daemon", timeout=1.0)
        from server.ipc.tls import client_ssl_context, client_tls_files
        ctx = client_ssl_context(client_tls_files())
        return httpx.Client(base_url=f"https://{endpoint.host}:{endpoint.port}",
                            verify=ctx, timeout=1.0)

    for i in range(max(1, attempts)):
        try:
            with _client() as c:
                info = c.get("/info").json()
            if info.get("protocol") == PROTOCOL_VERSION:
                return True
        except Exception:  # noqa: BLE001 — unreachable / bad response → retry
            pass
        if i < attempts - 1:
            time.sleep(interval)
    return False
```

- [ ] **Step 4: Implement the mode-select block**

Replace lines 275-312 (the `socket_path = os.environ.get(...)` block through the in-process `else`) with:

```python
    from server.ipc.paths import DEFAULT_SOCKET_PATH, Endpoint, sessiond_endpoint

    explicit = bool(os.environ.get("CCSWITCH_SESSIOND_ENDPOINT")
                    or os.environ.get("CCSWITCH_SESSIOND_SOCKET"))
    endpoint = None
    app.state.daemon_protocol = None
    if orchestrator is None:
        if explicit:
            endpoint = sessiond_endpoint()
        else:
            default_ep = Endpoint("unix", path=DEFAULT_SOCKET_PATH)
            if _daemon_compatible(default_ep):
                endpoint = default_ep
                from server.ipc.protocol import PROTOCOL_VERSION
                app.state.daemon_protocol = PROTOCOL_VERSION
                logging.getLogger("server").info(
                    "session daemon detected at %s — running in remote mode", DEFAULT_SOCKET_PATH)
            elif _daemon_present(default_ep):
                raise RuntimeError(
                    f"session daemon socket {DEFAULT_SOCKET_PATH} exists but no compatible "
                    "daemon answered /info after retries; refusing in-process fallback to "
                    "avoid a split-brain over the shared database")
            # else: no daemon on this host → in-process (dev/CI default).

    if orchestrator is None and endpoint is not None:
        from server.ipc.client import DaemonClient
        from server.sessions.remote import RemoteOrchestrator
        client_ssl = None
        if endpoint.is_tcp:
            from server.ipc.tls import client_ssl_context, client_tls_files
            client_ssl = client_ssl_context(client_tls_files())
        app.state.remote_mode = True
        app.state.daemon_endpoint = endpoint
        app.state.daemon_socket = endpoint.path if endpoint.is_unix else None
        app.state.daemon_client_ssl = client_ssl
        app.state.orchestrator = None
        app.state.sessions = RemoteOrchestrator(DaemonClient(endpoint))
    else:
        if orchestrator is None:
            orchestrator = SessionOrchestrator()
        app.state.remote_mode = False
        app.state.daemon_endpoint = None
        app.state.daemon_socket = None
        app.state.daemon_client_ssl = None
        app.state.orchestrator = orchestrator
        app.state.sessions = InProcessOrchestratorAdapter(orchestrator)
```

- [ ] **Step 5: Update the VNC router wiring**

Replace the `build_vnc_ws_router(...)` call (lines 416-419) with:

```python
    app.include_router(build_vnc_ws_router(
        lambda: getattr(app.state, "x11", None),
        lambda: getattr(app.state, "daemon_endpoint", None),
        lambda: getattr(app.state, "daemon_client_ssl", None),
    ))
```

(Keep the first arg exactly as it is today — copy the existing `get_x11` lambda verbatim; only add the two new getters.)

- [ ] **Step 6: Run tests**

Run: `… -m pytest server/tests/test_remote_mode_tls.py server/tests/test_remote_mode_edge.py server/tests/test_remote_mode_wiring.py server/tests/test_remote_default.py -v`
Expected: PASS (new TLS mode test + existing UDS mode tests unchanged).

- [ ] **Step 7: Commit**

```bash
git add server/app.py server/tests/test_remote_mode_tls.py
git commit -m "feat(app): endpoint-aware daemon probe + mTLS remote mode wiring (#205)"
```

---

### Task 8: Docs — env vars + cert-generation quickstart

**Files:**
- Modify: `.env.example`
- Modify: `specs/001-ccswitch-web/quickstart.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Document env vars in `.env.example`**

Append:

```bash
# --- Cross-host session daemon (mTLS) — optional, P4 public hosting (#205) ---
# Default is same-host over a Unix socket; leave these unset for that.
# CCSWITCH_SESSIOND_ENDPOINT=https://daemon.example.ts.net:9443
# Daemon (server) side — presents server cert, verifies client certs against the CA:
# CCSWITCH_SESSIOND_TLS_CERT=/etc/ccswitch/tls/server.pem
# CCSWITCH_SESSIOND_TLS_KEY=/etc/ccswitch/tls/server.key
# CCSWITCH_SESSIOND_TLS_CA=/etc/ccswitch/tls/ca.pem
# Edge (client) side — presents client cert, verifies server cert against the CA:
# CCSWITCH_SESSIOND_CLIENT_CERT=/etc/ccswitch/tls/client.pem
# CCSWITCH_SESSIOND_CLIENT_KEY=/etc/ccswitch/tls/client.key
# CCSWITCH_SESSIOND_CLIENT_CA=/etc/ccswitch/tls/ca.pem
```

- [ ] **Step 2: Add a cert-generation + cross-host section to `quickstart.md`**

Add a "Cross-host daemon (mTLS)" section documenting: generate a CA; issue a server cert **with the daemon's reachable hostname/IP in the SAN** (call out hostname verification as the common footgun); issue a client cert; the env vars on each host; that `https` with missing TLS files fails loud; and that CN allowlist / rotation are deferred to v2. Include a worked `openssl` recipe:

```bash
# CA
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem \
  -subj "/CN=ccswitch-ca" -days 3650
# Server cert (SAN MUST match how the edge reaches the daemon)
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=sessiond"
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem \
  -days 825 -extfile <(printf "subjectAltName=DNS:daemon.example.ts.net,IP:100.x.y.z")
# Client cert
openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj "/CN=edge"
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out client.pem -days 825
```

- [ ] **Step 3: Commit**

```bash
git add .env.example specs/001-ccswitch-web/quickstart.md
git commit -m "docs(ipc): cross-host mTLS env vars + cert-generation quickstart (#205)"
```

---

### Task 9: Full-suite verification + security self-review

**Files:** none (verification gate).

- [ ] **Step 1: Run the full server suite**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 \
  PYTHONPATH="$PWD/src" \
  /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q
```
Expected: only the 8 known-environmental failures (auth×5, health×2, x11×1); everything else PASS. Compare the count to the baseline (1318 passed pre-change + the new tests).

- [ ] **Step 2: Lint gates**

Run: `… -m ruff check server/` and `… -m mypy server/`
Expected: clean (fix any new findings in the files this plan touched).

- [ ] **Step 3: Security self-review (capturing-fix-patterns mindset)**

Confirm, with evidence: (a) an `https` endpoint with a missing TLS file raises at startup (no plaintext/in-process fallback); (b) the daemon rejects a no-cert / wrong-CA client at the handshake (Task 4/5 tests prove it); (c) `unix://` deployments are byte-for-byte unchanged (all pre-existing UDS tests green); (d) no secret (key path contents) is logged. Note any residual risk for the PR's security-review section.

- [ ] **Step 4: Push + PR**

Open a non-draft PR off `origin/master`, body ending with the Claude Code trailer and `Refs #205` (partial — full closure when v2 hardening lands; use `Fixes #205` instead only if the team considers cross-host transport complete). Mark ready + arm auto-merge:
```bash
gh pr merge <#> --squash --auto
```

---

## Self-Review (against the spec)

- **Endpoint abstraction (spec §"The seam"):** Task 2. ✓ `unix://` default, `https://` opt-in, legacy var honored.
- **TLS settings (spec §"TLS settings"):** Task 3 (in `ipc/tls.py`, not `config.py` — deviation noted & justified: avoids dual-Settings duplication; all 6 env vars named per spec). ✓
- **Daemon bind (spec §"Daemon bind"):** Task 4. ✓ `CERT_REQUIRED` mutual TLS.
- **Edge client (spec §"Edge client"):** Task 5. ✓ httpx `verify=ctx`, websockets `ssl=ctx`, str back-compat.
- **VNC second connect point:** Task 6 — discovered during planning (`vnc_ws.py` bypasses DaemonClient); folded in via the shared `daemon_ws_connect`. ✓ (additive to spec; same trust model.)
- **Probe + mode-select (spec §"Edge mode selection + probe"):** Task 7. ✓ TCP present-check, mTLS `/info`, split-brain guard preserved, live-loop guard preserved.
- **Testing (spec §"Testing"):** Tasks 1,4,5,7 cover: parsing, happy-path mTLS, no-cert rejected, wrong-CA rejected, server-untrusted (client CA verification via `client_ssl_context`), TCP probe, UDS regression. ✓
- **Docs (spec §"Config / docs"):** Task 8. ✓ incl. the hostname-SAN footgun.
- **Scope/YAGNI (spec §"Scope"):** CN allowlist, rotation, ACME, Tailscale-coupling, per-RPC authz all excluded. ✓
- **Type consistency:** `Endpoint`, `parse_endpoint`, `sessiond_endpoint`, `uvicorn_run_kwargs`, `daemon_ws_connect`, `client_ssl_context`, `DaemonTLSFiles`/`ClientTLSFiles`, `_daemon_present`/`_daemon_compatible`, `app.state.daemon_endpoint`/`daemon_client_ssl` used consistently across tasks. ✓
- **Placeholder scan:** none. ✓
