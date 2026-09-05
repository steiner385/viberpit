"""Client for the ChatGPT credential authority. Used by the edge and by runners.

One connection per call, deliberately
-------------------------------------
There is no connection pool here. The plan warns against an outer
``asyncio.wait_for`` stranding a pooled connection — the failure ccswitch already
lived through on the edge→daemon HTTP path (issue #734, see
``server/sessions/facade.py:call_with_deadline``). A pool plus a cancellation is
what turns one slow call into a permanently unusable transport.

Rather than re-derive the careful native-deadline discipline that fix required,
this client removes the precondition: each call dials, sends one frame, reads one
frame, and closes. A timeout closes *its own* socket in ``finally``, so there is
no shared resource left in an unknown state, and a late response arrives on a
socket nobody is reading. The cost is one TLS handshake per call — measured
against a refresh budget of 8 seconds, on a tailnet, for an operation that
happens on a 401 and not per turn.

The same reasoning covers the plan's "a late response must not answer an expired
app-server request": the caller that timed out has already closed the connection
the answer would have arrived on, so there is nothing for a late frame to
resolve.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from collections.abc import Mapping
from typing import Any, Final

from server.codex import redaction
from server.codex.auth_endpoint import (
    AuthorityClientTLSFiles,
    admin_deadline_s,
    authority_client_tls_files,
    client_ssl_context,
    connect_endpoint,
    refresh_deadline_s,
)
from server.codex.auth_types import (
    AUTHORITY_PROTOCOL_VERSION,
    AuthorityError,
    AuthorityErrorCode,
    AuthorityHealth,
    AuthorityUnavailable,
    AuthorizationSync,
    CredentialEnvelope,
    LoginSnapshot,
)
from server.codex.auth_server import MAX_FRAME_BYTES
from server.obs.context import log_event

logger = logging.getLogger(__name__)

_REQUEST_ID: Final = 1


class AuthorityClient:
    """Typed calls against one authority endpoint over mutual TLS."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext,
        deadline_s: float,
        admin_deadline: float | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl_context
        self._deadline_s = deadline_s
        self._admin_deadline_s = admin_deadline or deadline_s

    def __repr__(self) -> str:
        return f"<AuthorityClient {self._host}:{self._port}>"

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(cls, *, deadline_s: float | None = None) -> AuthorityClient:
        """Build from ``CCSWITCH_CODEX_AUTHORITY_*``. Raises if incomplete.

        Raising — rather than returning ``None`` — is the fail-closed half of the
        plan's "do not fall back to managed runner auth". A half-configured
        authority must stop Codex, not silently downgrade it to a mode where the
        runner holds credentials.
        """
        endpoint = connect_endpoint()
        files: AuthorityClientTLSFiles = authority_client_tls_files()
        assert endpoint.host is not None and endpoint.port is not None
        return cls(
            host=endpoint.host,
            port=endpoint.port,
            ssl_context=client_ssl_context(files),
            deadline_s=deadline_s if deadline_s is not None else refresh_deadline_s(),
            admin_deadline=admin_deadline_s(),
        )

    # -- runner-role calls -------------------------------------------------

    async def envelope(
        self, account: str, *, minimum_generation: int = 0
    ) -> CredentialEnvelope:
        result = await self._call(
            "envelope",
            {"account": account, "minimumGeneration": minimum_generation},
            account=account,
        )
        return CredentialEnvelope.from_wire(result.get("envelope"))

    async def refresh(
        self, account: str, *, rejected_generation: int = 0
    ) -> CredentialEnvelope:
        result = await self._call(
            "refresh",
            {"account": account, "rejectedGeneration": rejected_generation},
            account=account,
        )
        return CredentialEnvelope.from_wire(result.get("envelope"))

    # -- edge-role calls ---------------------------------------------------

    async def health(self) -> AuthorityHealth:
        """Never raises for an unreachable authority — that IS the health answer."""
        try:
            return AuthorityHealth.from_wire(
                await self._call("health", {}, deadline_s=self._admin_deadline_s)
            )
        except AuthorityUnavailable as exc:
            return AuthorityHealth.unreachable(str(exc))
        except AuthorityError as exc:
            return AuthorityHealth.unreachable(exc.detail or exc.code.value)

    async def authorize(self, sync: AuthorizationSync) -> AuthorityHealth:
        return AuthorityHealth.from_wire(
            await self._call(
                "authorize", {"sync": sync.to_wire()}, deadline_s=self._admin_deadline_s
            )
        )

    async def login_start(self, account: str) -> LoginSnapshot:
        return LoginSnapshot.from_wire(
            await self._admin("login/start", account)
        )

    async def login_status(self, account: str) -> LoginSnapshot:
        return LoginSnapshot.from_wire(await self._admin("login/status", account))

    async def login_cancel(self, account: str) -> LoginSnapshot:
        return LoginSnapshot.from_wire(await self._admin("login/cancel", account))

    async def logout(self, account: str) -> None:
        await self._admin("logout", account)

    async def revoke(self, account: str) -> int:
        result = await self._admin("revoke", account)
        return int(result.get("generation") or 0)

    async def forget(self, account: str) -> None:
        await self._admin("forget", account)

    async def _admin(self, method: str, account: str) -> Mapping[str, Any]:
        return await self._call(
            method, {"account": account}, account=account,
            deadline_s=self._admin_deadline_s,
        )

    # -- transport ---------------------------------------------------------

    async def _call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        account: str | None = None,
        deadline_s: float | None = None,
    ) -> Mapping[str, Any]:
        budget = deadline_s if deadline_s is not None else self._deadline_s
        frame = json.dumps(
            {
                "id": _REQUEST_ID,
                "method": method,
                "params": dict(params),
                "protocolVersion": AUTHORITY_PROTOCOL_VERSION,
            },
            separators=(",", ":"),
        ).encode() + b"\n"

        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(budget):
                reader, writer = await asyncio.open_connection(
                    self._host, self._port, ssl=self._ssl
                )
                writer.write(frame)
                await writer.drain()
                line = await reader.readline()
        except TimeoutError:
            raise AuthorityUnavailable(
                f"the credential authority did not answer {method} within {budget}s"
            ) from None
        except (ssl.SSLError, OSError) as exc:
            raise AuthorityUnavailable(
                f"cannot reach the credential authority: "
                f"{redaction.scrub_exception(exc)}"
            ) from None
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()

        if not line:
            raise AuthorityUnavailable(
                f"the credential authority closed the connection during {method}"
            )
        if len(line) > MAX_FRAME_BYTES:
            raise AuthorityUnavailable("the credential authority sent an oversized frame")
        try:
            body = json.loads(line)
        except ValueError:
            raise AuthorityUnavailable(
                "the credential authority sent a frame that is not JSON"
            ) from None
        if not isinstance(body, Mapping):
            raise AuthorityUnavailable("the credential authority sent a non-object frame")
        if body.get("error") is not None:
            raise AuthorityError.from_wire(body["error"], account=account)
        result = body.get("result")
        if not isinstance(result, Mapping):
            # A method whose handler returns a scalar would land here; none do, and
            # a silent {} would be indistinguishable from success.
            raise AuthorityError(
                AuthorityErrorCode.INTERNAL,
                f"{method} returned no result object",
                account=account,
            )
        return result


#: Process-wide cache for the edge's admin client. ``False`` is the negative
#: cache: a configuration error is not transient, so re-deriving it on every
#: request would log the same failure once per page load.
_EDGE_CLIENT: AuthorityClient | None | Any = None


def edge_authority_client() -> AuthorityClient | None:
    """The edge's admin client, or ``None`` when no authority is configured.

    Unlike :meth:`AuthorityClient.from_env`, this **swallows** the configuration
    error and returns ``None``: a deployment with Codex switched off has no
    authority, and the accounts API must still serve Anthropic and Copilot
    accounts on that host. The distinction is the caller's to make — a route that
    genuinely needs the authority turns ``None`` into a 503 with a typed reason.
    """
    global _EDGE_CLIENT
    if _EDGE_CLIENT is False:
        return None
    if _EDGE_CLIENT is not None:
        return _EDGE_CLIENT
    from server.codex.auth_endpoint import AuthorityConfigError, authority_configured

    if not authority_configured():
        _EDGE_CLIENT = False
        return None
    try:
        _EDGE_CLIENT = AuthorityClient.from_env()
    except AuthorityConfigError as exc:
        logger.warning(
            "codex credential authority is configured but unusable: %s", exc
        )
        _EDGE_CLIENT = False
        return None
    return _EDGE_CLIENT


def reset_edge_authority_client() -> None:
    """Drop the cached client. Tests use this; nothing in production does."""
    global _EDGE_CLIENT
    _EDGE_CLIENT = None


async def probe_authority(client: AuthorityClient) -> AuthorityHealth:
    """Health with a log line. Used by the edge's startup and status surfaces."""
    health = await client.health()
    log_event(
        logger,
        logging.INFO if health.reachable else logging.WARNING,
        "codex.auth.health",
        reachable=health.reachable,
        revision=health.sync_revision,
        accounts=health.accounts,
        detail=health.detail,
    )
    return health
