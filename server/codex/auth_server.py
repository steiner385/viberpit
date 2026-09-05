"""The authority's wire surface: JSONL over mutual TLS, with per-route ACLs.

Why not FastAPI, when the rest of ccswitch's RPC is FastAPI
-----------------------------------------------------------
Because the ACL this service needs cannot be built on it. The plan requires that
"edge may administer login; runners may only request envelope/refresh", which
means every request must be attributed to the *specific* client certificate that
made it. Uvicorn does not surface TLS peer information in the ASGI scope (there is
no ``extensions.tls`` in 0.49), and ``ssl.SSLObject`` is a memory-BIO object with
no socket to correlate against — so a FastAPI authority could enforce a handshake
allowlist but could not tell ``codex-edge`` from ``codex-runner:rocky`` inside a
handler. The only remaining option would be a client-asserted identity header,
which any allowlisted runner could forge to call ``login``.

``asyncio.start_server(ssl=...)`` hands the handler the connection's own
``getpeercert()``, so identity is a property of the transport rather than of the
payload. That is the whole reason for the transport choice, and it is why this
module explicitly does **not** import :mod:`server.ipc.daemon` or mount its
generic ``/rpc/{method}`` dispatcher: an authority whose route table can grow a
method by someone else's registration is not a route table.

The framing is one JSON object per line — the same idiom
:mod:`server.codex.protocol` already speaks to the app-server, so there is one
JSONL convention in ``server/codex/``, not two.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from server.codex import redaction
from server.codex.auth_endpoint import EDGE_IDENTITY, RUNNER_IDENTITY_PREFIX
from server.codex.auth_authority import CredentialAuthority
from server.codex.auth_types import (
    AUTHORITY_PROTOCOL_VERSION,
    AuthorityError,
    AuthorityErrorCode,
    AuthorizationSync,
)
from server.ipc.tls import peer_identities
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: Refuse a frame longer than this rather than buffering it. An envelope is a few
#: kilobytes; anything approaching this is a bug or an attack.
MAX_FRAME_BYTES: Final = 256 * 1024
#: Concurrent in-flight requests per connection. One client should not be able to
#: queue unbounded work on the process that holds every ChatGPT credential.
MAX_INFLIGHT_PER_CONNECTION: Final = 8
#: Idle read timeout. A connection that says nothing is dropped, not held open.
CONNECTION_IDLE_S: Final = 300.0


class Role(StrEnum):
    """What a peer certificate is allowed to be. There is no third role."""

    EDGE = "edge"
    RUNNER = "runner"


def resolve_role(identities: set[str]) -> tuple[Role, str] | None:
    """Map certificate identities to a role, or ``None`` for deny.

    Deny-by-default and *unambiguous*: a certificate that somehow presents both
    the edge identity and a runner identity is rejected rather than resolved to
    the more privileged of the two. A cert that claims two roles is a
    misissuance, and the safe reading of a misissuance is "no".
    """
    edge = EDGE_IDENTITY in identities
    runners = sorted(i for i in identities if i.startswith(RUNNER_IDENTITY_PREFIX))
    if edge and runners:
        return None
    if edge:
        return (Role.EDGE, EDGE_IDENTITY)
    if len(runners) == 1:
        return (Role.RUNNER, runners[0])
    return None


@dataclass(frozen=True, slots=True)
class Method:
    """One callable route and the single role permitted to call it."""

    name: str
    role: Role
    handler: Callable[["Session", Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Session:
    """Per-connection context handed to a handler."""

    role: Role
    identity: str
    authority: CredentialAuthority


def _account(params: Mapping[str, Any]) -> str:
    account = params.get("account")
    if not isinstance(account, str) or not account.strip():
        raise AuthorityError(AuthorityErrorCode.BAD_REQUEST, "account is required")
    return account


def _generation(params: Mapping[str, Any], key: str) -> int:
    raw = params.get(key, 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AuthorityError(
            AuthorityErrorCode.BAD_REQUEST, f"{key} must be an integer"
        ) from None
    return max(0, value)


# ---------------------------------------------------------------------------
# Handlers. Each one is small on purpose: the authority owns the behaviour, this
# layer owns only "who may ask, and what shape is the answer".
# ---------------------------------------------------------------------------


async def _health(session: Session, params: Mapping[str, Any]) -> Any:
    return session.authority.health().to_wire()


async def _authorize(session: Session, params: Mapping[str, Any]) -> Any:
    sync = AuthorizationSync.from_wire(params.get("sync"))
    return (await session.authority.authorize(sync)).to_wire()


async def _login_start(session: Session, params: Mapping[str, Any]) -> Any:
    return (await session.authority.login_start(_account(params))).to_wire()


async def _login_status(session: Session, params: Mapping[str, Any]) -> Any:
    return session.authority.login_status(_account(params)).to_wire()


async def _login_cancel(session: Session, params: Mapping[str, Any]) -> Any:
    return (await session.authority.login_cancel(_account(params))).to_wire()


async def _logout(session: Session, params: Mapping[str, Any]) -> Any:
    await session.authority.logout(_account(params))
    return {"ok": True}


async def _revoke(session: Session, params: Mapping[str, Any]) -> Any:
    return {"generation": await session.authority.revoke(_account(params))}


async def _forget(session: Session, params: Mapping[str, Any]) -> Any:
    await session.authority.forget(_account(params))
    return {"ok": True}


async def _envelope(session: Session, params: Mapping[str, Any]) -> Any:
    envelope = await session.authority.get_envelope(
        _account(params),
        minimum_generation=_generation(params, "minimumGeneration"),
    )
    return {"envelope": envelope.to_wire()}


async def _refresh(session: Session, params: Mapping[str, Any]) -> Any:
    envelope = await session.authority.refresh(
        _account(params),
        rejected_generation=_generation(params, "rejectedGeneration"),
    )
    return {"envelope": envelope.to_wire()}


#: THE route table. Adding a method means adding a line here, with a role — there
#: is no registration hook and no wildcard, so "which identities can reach this
#: code path?" is answerable by reading one list.
METHODS: Final[dict[str, Method]] = {
    m.name: m
    for m in (
        # `health` is listed under EDGE for shape only — ROLE_AGNOSTIC below
        # overrides it, so both roles may ask whether the authority is alive and
        # synced. The answer carries no account data beyond a count.
        Method("health", Role.EDGE, _health),
        Method("authorize", Role.EDGE, _authorize),
        Method("login/start", Role.EDGE, _login_start),
        Method("login/status", Role.EDGE, _login_status),
        Method("login/cancel", Role.EDGE, _login_cancel),
        Method("logout", Role.EDGE, _logout),
        Method("revoke", Role.EDGE, _revoke),
        Method("forget", Role.EDGE, _forget),
        # The only two methods a runner may call, and the only two that return a
        # credential. A runner cannot administer, enumerate, or revoke anything.
        Method("envelope", Role.RUNNER, _envelope),
        Method("refresh", Role.RUNNER, _refresh),
    )
}

#: Methods any authenticated role may call, overriding the ``role`` declared
#: above. An explicit exception list rather than a third role, because it is
#: exactly one method and a "both" role would have to be reasoned about at every
#: other route.
ROLE_AGNOSTIC: Final[frozenset[str]] = frozenset({"health"})


class AuthorityServer:
    """Serves :data:`METHODS` over mTLS on one endpoint."""

    def __init__(
        self,
        authority: CredentialAuthority,
        *,
        allowlist: frozenset[str],
        idle_timeout_s: float = CONNECTION_IDLE_S,
    ) -> None:
        self._authority = authority
        self._allowlist = allowlist
        self._idle_timeout_s = idle_timeout_s
        self._server: asyncio.AbstractServer | None = None
        self.rejected_connections = 0
        self.denied_calls = 0

    async def start(
        self, host: str, port: int, ssl_context: ssl.SSLContext
    ) -> asyncio.AbstractServer:
        self._server = await asyncio.start_server(
            self._handle, host=host, port=port, ssl=ssl_context
        )
        log_event(
            logger, logging.INFO, "codex.auth.listening", host=host, port=port
        )
        return self._server

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # -- connection --------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("ssl_object")
        cert = peer.getpeercert() if peer is not None else None
        resolved = resolve_role(peer_identities(cert or {}))
        if cert is None or resolved is None:
            # Verified by the TLS layer but not by us: the chain was good and the
            # CN was not. Counted and logged, then closed without a reply — an
            # unauthorized peer learns nothing about the route table.
            self.rejected_connections += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.auth.connection_rejected",
                identities=sorted(peer_identities(cert or {})),
            )
            await _close(writer)
            return
        role, identity = resolved
        if identity not in self._allowlist:
            self.rejected_connections += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.auth.connection_not_allowlisted",
                identity=identity,
            )
            await _close(writer)
            return

        session = Session(role=role, identity=identity, authority=self._authority)
        inflight: set[asyncio.Task[None]] = set()
        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(), self._idle_timeout_s
                    )
                except TimeoutError:
                    break
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    log_event(
                        logger,
                        logging.WARNING,
                        "codex.auth.frame_too_large",
                        identity=identity,
                        bytes=len(line),
                    )
                    break
                if len(inflight) >= MAX_INFLIGHT_PER_CONNECTION:
                    # Serialize rather than shed: the client is a trusted peer with
                    # a deadline of its own, and answering late is better than
                    # answering "unknown" about a credential.
                    await asyncio.gather(*inflight, return_exceptions=True)
                    inflight.clear()
                task = asyncio.create_task(self._dispatch(session, line, writer))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in list(inflight):
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*inflight, return_exceptions=True)
            await _close(writer)

    async def _dispatch(
        self, session: Session, line: bytes, writer: asyncio.StreamWriter
    ) -> None:
        started = time.monotonic()
        request_id: Any = None
        method_name = ""
        account: str | None = None
        try:
            frame = json.loads(line)
            if not isinstance(frame, Mapping):
                raise ValueError("frame must be an object")
        except ValueError:
            await self._write(writer, {
                "id": None,
                "error": AuthorityError(
                    AuthorityErrorCode.BAD_REQUEST, "frame is not valid JSON"
                ).to_wire(),
            })
            return

        request_id = frame.get("id")
        method_name = str(frame.get("method") or "")
        raw_params = frame.get("params")
        params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
        if isinstance(params.get("account"), str):
            account = params["account"]

        try:
            version = int(frame.get("protocolVersion") or AUTHORITY_PROTOCOL_VERSION)
            if version != AUTHORITY_PROTOCOL_VERSION:
                raise AuthorityError(
                    AuthorityErrorCode.BAD_REQUEST,
                    f"unsupported authority protocol version {version}",
                )
            method = METHODS.get(method_name)
            if method is None:
                raise AuthorityError(
                    AuthorityErrorCode.BAD_REQUEST, "unknown method"
                )
            if method.name not in ROLE_AGNOSTIC and method.role is not session.role:
                self.denied_calls += 1
                raise AuthorityError(
                    AuthorityErrorCode.FORBIDDEN,
                    f"a {session.role.value} certificate may not call this method",
                )
            result = await method.handler(session, params)
        except AuthorityError as exc:
            self._audit(session, method_name, account, exc.code.value, started)
            await self._write(
                writer, {"id": request_id, "error": exc.to_wire()}
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- never leak a traceback to a peer
            log_event(
                logger,
                logging.ERROR,
                "codex.auth.handler_failed",
                method=method_name,
                error=redaction.scrub_exception(exc),
            )
            self._audit(session, method_name, account, "internal", started)
            await self._write(
                writer,
                {
                    "id": request_id,
                    "error": AuthorityError(
                        AuthorityErrorCode.INTERNAL, "authority call failed"
                    ).to_wire(),
                },
            )
            return
        self._audit(
            session, method_name, account, "ok", started,
            generation=_result_generation(result),
        )
        await self._write(writer, {"id": request_id, "result": result})

    def _audit(
        self,
        session: Session,
        method: str,
        account: str | None,
        outcome: str,
        started: float,
        generation: int | None = None,
    ) -> None:
        """One audit line per call: alias, caller, action, outcome, generation, timing.

        Note what is absent: no native ChatGPT account id, no token, no device
        code, no envelope. ``account`` here is ccswitch's own alias, which is
        already public in the manifest, and ``generation`` is a counter — it says
        that a credential was issued or rotated, never which one.
        """
        log_event(
            logger,
            logging.INFO,
            "codex.auth.call",
            method=method,
            identity=session.identity,
            role=session.role.value,
            account=account,
            outcome=outcome,
            generation=generation,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _write(self, writer: asyncio.StreamWriter, frame: Mapping[str, Any]) -> None:
        try:
            writer.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, RuntimeError):
            # The peer hung up mid-answer. Nothing to recover: the envelope it did
            # not receive is still valid and it will ask again.
            pass


def _result_generation(result: Any) -> int | None:
    """The generation a successful call issued or rotated, for the audit line.

    Reads it back off the RESULT rather than threading it out of each handler, so
    a new credential-returning method cannot be added without its generation
    appearing in the audit trail.
    """
    if not isinstance(result, Mapping):
        return None
    envelope = result.get("envelope")
    if isinstance(envelope, Mapping):
        value = envelope.get("generation")
    else:
        value = result.get("generation")
    return value if isinstance(value, int) else None


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
