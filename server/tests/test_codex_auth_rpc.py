"""The authority's wire surface, over REAL mutual TLS on 127.0.0.1.

The per-route ACL is the reason this service is not a FastAPI app: the role has to
come from the *transport's* peer certificate, not from anything in the payload. A
test that faked ``getpeercert()`` would therefore be testing the wrong half. So
this suite mints a CA, a server cert and four client certs with ``cryptography``,
binds a real ``asyncio.start_server(ssl=...)`` on an ephemeral loopback port, and
speaks the real JSONL framing to it.

The headline test is the negative one: a *runner* certificate must be refused for
every administrative method, because a runner that could call ``login/start`` or
``revoke`` would make the whole authority split pointless.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from server.codex.auth_authority import CredentialAuthority
from server.codex.auth_client import AuthorityClient
from server.codex.auth_endpoint import (
    EDGE_IDENTITY,
    RUNNER_IDENTITY_PREFIX,
    AuthorityClientTLSFiles,
    AuthorityTLSFiles,
    client_ssl_context,
    server_ssl_context,
)
from server.codex.auth_server import AuthorityServer, Role, resolve_role
from server.codex.auth_store import CredentialStore
from server.codex.auth_types import (
    AUTHORITY_PROTOCOL_VERSION,
    LOGIN_AUTHORIZED,
    AuthorityError,
    AuthorityErrorCode,
    AuthorityUnavailable,
    AuthorizationRecord,
    AuthorizationSync,
)
from server.tests.test_codex_auth_authority import (
    ACCOUNT,
    CHATGPT_ACCOUNT_ID,
    make_access_token,
    write_auth_json,
)
from server.tests.tls_certs import make_ca, make_leaf, write_pem_cert, write_pem_key

RUNNER_IDENTITY = f"{RUNNER_IDENTITY_PREFIX}testhost"
UNLISTED_RUNNER_IDENTITY = f"{RUNNER_IDENTITY_PREFIX}rogue-host"

#: Every method only the edge may call. This list IS the security claim.
ADMIN_METHODS = (
    "authorize",
    "login/start",
    "login/status",
    "login/cancel",
    "logout",
    "revoke",
    "forget",
)
#: Every method only a runner may call — the only two that return a credential.
CREDENTIAL_METHODS = ("envelope", "refresh")


# ---------------------------------------------------------------------------
# Real mTLS material
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientCert:
    cert: str
    key: str


def _leaf(directory: Path, ca_key, ca_cert, name: str, slug: str, **kw) -> ClientCert:
    key, cert = make_leaf(ca_key, ca_cert, name, **kw)
    cert_path = directory / f"{slug}.pem"
    key_path = directory / f"{slug}.key"
    write_pem_cert(cert_path, cert)
    write_pem_key(key_path, key)
    return ClientCert(cert=str(cert_path), key=str(key_path))


@dataclass(frozen=True)
class AuthorityTLS:
    ca: str
    server: ClientCert
    edge: ClientCert
    runner: ClientCert
    unlisted_runner: ClientCert
    stranger: ClientCert
    rogue_ca: str
    rogue: ClientCert

    def client_context(self, who: ClientCert) -> ssl.SSLContext:
        return client_ssl_context(
            AuthorityClientTLSFiles(cert=who.cert, key=who.key, ca=self.ca)
        )

    def server_context(self) -> ssl.SSLContext:
        return server_ssl_context(
            AuthorityTLSFiles(
                cert=self.server.cert, key=self.server.key, client_ca=self.ca
            )
        )


@pytest.fixture(scope="module")
def authority_tls(tmp_path_factory) -> AuthorityTLS:
    # Module-scoped: six RSA-2048 keypairs is the slowest thing in this file, and
    # the material is read-only — every test gets its own listener, not its own CA.
    directory = tmp_path_factory.mktemp("authority-tls")
    ca_key, ca_cert = make_ca()
    ca_path = directory / "ca.pem"
    write_pem_cert(ca_path, ca_cert)

    rogue_ca_key, rogue_ca_cert = make_ca()
    rogue_ca_path = directory / "rogue-ca.pem"
    write_pem_cert(rogue_ca_path, rogue_ca_cert)

    return AuthorityTLS(
        ca=str(ca_path),
        server=_leaf(
            directory, ca_key, ca_cert, "codex-authority", "server",
            san_dns=["localhost"], san_ip=["127.0.0.1"],
        ),
        edge=_leaf(directory, ca_key, ca_cert, EDGE_IDENTITY, "edge"),
        runner=_leaf(directory, ca_key, ca_cert, RUNNER_IDENTITY, "runner"),
        unlisted_runner=_leaf(
            directory, ca_key, ca_cert, UNLISTED_RUNNER_IDENTITY, "runner-unlisted"
        ),
        # Same CA, an identity that is neither an edge nor a runner.
        stranger=_leaf(directory, ca_key, ca_cert, "ccswitch-edge", "stranger"),
        rogue_ca=str(rogue_ca_path),
        rogue=_leaf(directory, rogue_ca_key, rogue_ca_cert, EDGE_IDENTITY, "rogue"),
    )


# ---------------------------------------------------------------------------
# A listening authority
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    tls: AuthorityTLS
    server: AuthorityServer
    authority: CredentialAuthority
    store: CredentialStore
    host: str
    port: int

    def context(self, who: ClientCert) -> ssl.SSLContext:
        return self.tls.client_context(who)

    def client(self, who: ClientCert, *, deadline_s: float = 10.0) -> AuthorityClient:
        return AuthorityClient(
            host=self.host,
            port=self.port,
            ssl_context=self.context(who),
            deadline_s=deadline_s,
        )


@pytest_asyncio.fixture
async def harness(tmp_path, authority_tls):
    store = CredentialStore(tmp_path / "authority-state")
    tokens = iter(f"{n}" for n in range(1, 100))

    async def login_runner(account: str, home: Path) -> None:
        write_auth_json(home, make_access_token(nonce=next(tokens)))

    async def refresh_runner(account: str, home: Path) -> None:
        write_auth_json(home, make_access_token(nonce=next(tokens)))

    authority = CredentialAuthority(
        store=store, login_runner=login_runner, refresh_runner=refresh_runner
    )
    server = AuthorityServer(
        authority, allowlist=frozenset({EDGE_IDENTITY, RUNNER_IDENTITY})
    )
    listening = await server.start("127.0.0.1", 0, authority_tls.server_context())
    port = listening.sockets[0].getsockname()[1]
    try:
        yield Harness(
            tls=authority_tls,
            server=server,
            authority=authority,
            store=store,
            host="127.0.0.1",
            port=port,
        )
    finally:
        await server.aclose()
        await authority.aclose()


# ---------------------------------------------------------------------------
# Raw wire helpers — used wherever the test is ABOUT the frame, not the client
# ---------------------------------------------------------------------------


async def send_raw(harness: Harness, who: ClientCert, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(
        harness.host, harness.port, ssl=harness.context(who)
    )
    try:
        writer.write(payload)
        await writer.drain()
        return await asyncio.wait_for(reader.readline(), 10.0)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def rpc(
    harness: Harness,
    who: ClientCert,
    method: str,
    params: dict | None = None,
    *,
    request_id: object = 1,
    protocol_version: int = AUTHORITY_PROTOCOL_VERSION,
) -> dict:
    frame = {
        "id": request_id,
        "method": method,
        "params": params or {},
        "protocolVersion": protocol_version,
    }
    line = await send_raw(harness, who, json.dumps(frame).encode() + b"\n")
    assert line, f"the authority answered nothing for {method!r}"
    return json.loads(line)


def sync_wire(*, enabled: bool = True, revision: int = 1) -> dict:
    return AuthorizationSync(
        revision=revision,
        records=(
            AuthorizationRecord(
                name=ACCOUNT, provider="openai", engine="codex", enabled=enabled
            ),
        ),
    ).to_wire()


# ---------------------------------------------------------------------------
# Happy path: the two roles, doing exactly their own jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_administers_and_the_runner_collects(harness):
    edge = harness.client(harness.tls.edge)
    runner = harness.client(harness.tls.runner)

    health = await edge.authorize(
        AuthorizationSync.from_wire(sync_wire(revision=3))
    )
    assert health.reachable and health.sync_revision == 3 and health.accounts == 1

    assert (await edge.login_start(ACCOUNT)).state in {"pending", "awaiting_authorization"}
    for _ in range(200):
        snapshot = await edge.login_status(ACCOUNT)
        if snapshot.terminal:
            break
        await asyncio.sleep(0.005)
    assert snapshot.state == LOGIN_AUTHORIZED
    assert snapshot.generation == 1

    envelope = await runner.envelope(ACCOUNT)
    assert envelope.account == ACCOUNT
    assert envelope.generation == 1
    assert envelope.chatgpt_account_id.reveal() == CHATGPT_ACCOUNT_ID
    assert envelope.chatgpt_plan_type == "pro"

    rotated = await runner.refresh(ACCOUNT, rejected_generation=1)
    assert rotated.generation == 2
    assert rotated.access_token.reveal() != envelope.access_token.reveal()

    assert harness.server.denied_calls == 0
    assert harness.server.rejected_connections == 0


@pytest.mark.asyncio
async def test_health_is_callable_by_both_roles(harness):
    for who in (harness.tls.edge, harness.tls.runner):
        answer = await rpc(harness, who, "health", {})
        assert "error" not in answer
        assert answer["result"]["reachable"] is True
        assert answer["result"]["protocolVersion"] == AUTHORITY_PROTOCOL_VERSION
    assert harness.server.denied_calls == 0


# ---------------------------------------------------------------------------
# The ACL. This is the headline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ADMIN_METHODS)
@pytest.mark.asyncio
async def test_a_runner_certificate_may_not_administer_anything(harness, method):
    answer = await rpc(harness, harness.tls.runner, method, {"account": ACCOUNT})

    assert answer["error"]["code"] == AuthorityErrorCode.FORBIDDEN.value
    assert "runner certificate" in answer["error"]["detail"]
    assert "result" not in answer
    assert harness.server.denied_calls == 1
    # The refusal happened before the handler, so nothing was mutated.
    assert harness.authority.health().sync_revision is None


@pytest.mark.parametrize("method", CREDENTIAL_METHODS)
@pytest.mark.asyncio
async def test_an_edge_certificate_may_not_collect_a_credential(harness, method):
    answer = await rpc(harness, harness.tls.edge, method, {"account": ACCOUNT})

    assert answer["error"]["code"] == AuthorityErrorCode.FORBIDDEN.value
    assert harness.server.denied_calls == 1


@pytest.mark.asyncio
async def test_the_typed_client_surfaces_forbidden_as_a_typed_refusal(harness):
    edge = harness.client(harness.tls.edge)
    with pytest.raises(AuthorityError) as excinfo:
        await edge.envelope(ACCOUNT)
    assert excinfo.value.code is AuthorityErrorCode.FORBIDDEN
    # A settled "no" — a runner that retried this forever would be a DoS.
    assert not excinfo.value.retryable


@pytest.mark.parametrize(
    "method",
    ["create_session", "../rpc/foo", "deploy", "", "login", "envelope/", "__init__"],
)
@pytest.mark.asyncio
async def test_there_is_no_generic_dispatcher_behind_the_route_table(harness, method):
    answer = await rpc(harness, harness.tls.edge, method, {"account": ACCOUNT})

    assert answer["error"]["code"] == AuthorityErrorCode.BAD_REQUEST.value
    assert answer["error"]["detail"] == "unknown method"


# ---------------------------------------------------------------------------
# Connection-level refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who_attr", ["unlisted_runner", "stranger"])
@pytest.mark.asyncio
async def test_a_non_allowlisted_certificate_is_closed_without_a_reply(
    harness, who_attr
):
    who = getattr(harness.tls, who_attr)
    frame = json.dumps(
        {"id": 1, "method": "health", "params": {}, "protocolVersion": 1}
    ).encode() + b"\n"

    line = await send_raw(harness, who, frame)

    # No reply at all: an unauthorized peer learns nothing about the route table.
    assert line == b""
    assert harness.server.rejected_connections == 1
    assert harness.server.denied_calls == 0


@pytest.mark.asyncio
async def test_a_certificate_from_an_untrusted_ca_never_completes_the_handshake(
    harness,
):
    context = client_ssl_context(
        AuthorityClientTLSFiles(
            cert=harness.tls.rogue.cert,
            key=harness.tls.rogue.key,
            ca=harness.tls.ca,
        )
    )
    # Pinned to TLS 1.2 so the server's rejection of the client certificate lands
    # inside the handshake rather than on the first read; the point of the test is
    # that the connection never becomes usable, not which frame carries the alert.
    context.maximum_version = ssl.TLSVersion.TLSv1_2

    # Which side reports the alert depends on the OpenSSL build, so accept either
    # shape of transport failure — what must never happen is a usable connection.
    with pytest.raises((ssl.SSLError, ConnectionResetError)):
        reader, writer = await asyncio.open_connection(
            harness.host, harness.port, ssl=context
        )
        writer.close()
        await writer.wait_closed()

    # And the typed client maps it to "we do not know" rather than to a refusal:
    # a handshake failure must never be read as "the account was revoked".
    client = AuthorityClient(
        host=harness.host, port=harness.port, ssl_context=context, deadline_s=5.0
    )
    with pytest.raises(AuthorityUnavailable):
        await client.envelope(ACCOUNT)

    # It never reached the handler, so it is not even counted as a rejection.
    assert harness.server.rejected_connections == 0


# ---------------------------------------------------------------------------
# Frame validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mismatched_protocol_version_is_refused_not_guessed(harness):
    answer = await rpc(
        harness, harness.tls.edge, "health", {}, protocol_version=99
    )

    assert answer["id"] == 1
    assert answer["error"]["code"] == AuthorityErrorCode.BAD_REQUEST.value
    assert "99" in answer["error"]["detail"]


@pytest.mark.parametrize(
    "payload", [b"not json at all\n", b"[1,2,3]\n", b'"a string"\n', b"\n"]
)
@pytest.mark.asyncio
async def test_a_frame_that_is_not_a_json_object_is_a_bad_request_with_a_null_id(
    harness, payload
):
    line = await send_raw(harness, harness.tls.edge, payload)
    answer = json.loads(line)

    assert answer["id"] is None
    assert answer["error"]["code"] == AuthorityErrorCode.BAD_REQUEST.value
    assert answer["error"]["detail"] == "frame is not valid JSON"


@pytest.mark.asyncio
async def test_a_call_without_an_account_is_a_bad_request(harness):
    answer = await rpc(harness, harness.tls.runner, "envelope", {})
    assert answer["error"]["code"] == AuthorityErrorCode.BAD_REQUEST.value
    assert answer["error"]["detail"] == "account is required"


@pytest.mark.asyncio
async def test_a_non_integer_generation_is_a_bad_request(harness):
    answer = await rpc(
        harness,
        harness.tls.runner,
        "envelope",
        {"account": ACCOUNT, "minimumGeneration": "soon"},
    )
    assert answer["error"]["code"] == AuthorityErrorCode.BAD_REQUEST.value
    assert "minimumGeneration" in answer["error"]["detail"]


# ---------------------------------------------------------------------------
# AuthorityClient: one connection per call, and it closes its own socket
# ---------------------------------------------------------------------------


@dataclass
class FlakyServer:
    """Accepts, ignores the first caller, answers the second."""

    connections: int = 0
    first_reader: asyncio.StreamReader | None = None


@pytest_asyncio.fixture
async def flaky(authority_tls):
    state = FlakyServer()
    reply = {
        "id": 1,
        "result": {
            "envelope": {
                "account": ACCOUNT,
                "generation": 4,
                "accessToken": make_access_token(nonce="flaky"),
                "chatgptAccountId": CHATGPT_ACCOUNT_ID,
                "chatgptPlanType": "pro",
                "expiresAt": None,
                "issuedAt": 1.0,
            }
        },
    }

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state.connections += 1
        if state.connections == 1:
            # Accept and say nothing at all: the client's own deadline is the
            # only thing that can end this call.
            state.first_reader = reader
            with contextlib.suppress(Exception):
                await reader.read()
            return
        with contextlib.suppress(Exception):
            await reader.readline()
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        handle, host="127.0.0.1", port=0, ssl=authority_tls.server_context()
    )
    port = server.sockets[0].getsockname()[1]
    try:
        yield state, authority_tls, port
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@pytest.mark.asyncio
async def test_a_silent_authority_becomes_unavailable_and_the_socket_is_closed(flaky):
    state, tls, port = flaky
    client = AuthorityClient(
        host="127.0.0.1",
        port=port,
        ssl_context=tls.client_context(tls.runner),
        deadline_s=0.2,
    )

    with pytest.raises(AuthorityUnavailable) as excinfo:
        await client.envelope(ACCOUNT)
    assert "did not answer envelope" in str(excinfo.value)

    # The timeout closed ITS OWN socket in `finally`, so the server sees EOF and
    # there is no half-open connection left for a late frame to arrive on.
    assert state.first_reader is not None
    assert await asyncio.wait_for(state.first_reader.read(1), 2.0) == b""

    # And the client itself is still perfectly usable — there is no pool to poison.
    envelope = await client.envelope(ACCOUNT)
    assert envelope.generation == 4
    assert state.connections == 2


@pytest.mark.asyncio
async def test_an_unreachable_authority_is_unavailable_not_a_refusal(authority_tls):
    # Bind and immediately release a port so the connect is refused deterministically.
    probe = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    client = AuthorityClient(
        host="127.0.0.1",
        port=port,
        ssl_context=authority_tls.client_context(authority_tls.runner),
        deadline_s=2.0,
    )
    with pytest.raises(AuthorityUnavailable):
        await client.envelope(ACCOUNT)


@pytest.mark.asyncio
async def test_health_reports_an_unreachable_authority_instead_of_raising(
    authority_tls,
):
    probe = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    client = AuthorityClient(
        host="127.0.0.1",
        port=port,
        ssl_context=authority_tls.client_context(authority_tls.edge),
        deadline_s=2.0,
    )
    health = await client.health()
    assert health.reachable is False
    assert health.detail


# ---------------------------------------------------------------------------
# resolve_role — deny by default, and never resolve an ambiguity upward
# ---------------------------------------------------------------------------


def test_resolve_role_maps_the_two_legitimate_identities():
    assert resolve_role({EDGE_IDENTITY}) == (Role.EDGE, EDGE_IDENTITY)
    assert resolve_role({RUNNER_IDENTITY}) == (Role.RUNNER, RUNNER_IDENTITY)
    # An unrelated SAN alongside a single valid identity is still unambiguous.
    assert resolve_role({RUNNER_IDENTITY, "localhost"}) == (Role.RUNNER, RUNNER_IDENTITY)


def test_a_certificate_claiming_both_roles_is_denied_not_promoted():
    # The dangerous reading would be "edge wins". A cert claiming two roles is a
    # misissuance, and the safe reading of a misissuance is "no".
    assert resolve_role({EDGE_IDENTITY, RUNNER_IDENTITY}) is None


def test_two_runner_identities_are_ambiguous_and_denied():
    assert resolve_role({RUNNER_IDENTITY, UNLISTED_RUNNER_IDENTITY}) is None


@pytest.mark.parametrize(
    "identities",
    [set(), {"ccswitch-edge"}, {"codex-runner"}, {"edge"}, {"CODEX-EDGE"}],
)
def test_an_unknown_identity_resolves_to_nothing(identities):
    assert resolve_role(identities) is None
