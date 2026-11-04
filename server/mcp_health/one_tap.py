"""One-tap MCP sign-in on the edge (MCP auth overhaul phase 4).

:func:`start` discovers the server, ensures a registered client and returns the provider's
authorize URL, remembering ``{state: pending}`` in memory. :func:`complete` takes the
provider's callback, consumes that state (single use, 10 min TTL, bound to the tailnet
identity that started the flow), exchanges the code, writes the bundle to the shared store
at the server's CLI key and schedules the post-login follow-through.

Every failure maps to a redirect the routes build: :class:`PasteBack` (one-tap cannot sign
in to this server; the existing paste-back flow can) or :class:`SignInFailed` with a code
from the closed :data:`SIGN_IN_ERRORS` set. Provider error text is never surfaced, and
codes, verifiers and tokens are never logged.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from typing import Any

from viberpit import accounts, creds
from viberpit.mcp_keys import store_name
from viberpit.mcp_oauth_client import (Client, Discovery, DiscoveryFailed, ExchangeFailed,
                                       RegistrationFailed, RegistrationUnsupported,
                                       authorize_url, choose_scope, discover, ensure_client,
                                       exchange_code, forget_client, new_state, pkce_pair)

from server.config import get_settings
from server.library import scan
from server.library.names import valid_mcp_name
from server.mcp_health import grants
from server.mcp_health.after_login import after_login

log = logging.getLogger(__name__)

CALLBACK_PATH = "/api/mcp/oauth/callback"
STATE_TTL_S = 600.0
MAX_PENDING = 100
SIGN_IN_ERRORS = frozenset({"state_invalid", "denied", "exchange_failed",
                            "registration_failed", "not_configured"})

_clock: Callable[[], float] = time.monotonic


class PasteBack(Exception):
    """One-tap cannot sign in to *server*; the paste-back flow should take over."""

    def __init__(self, server: str) -> None:
        super().__init__(server)
        self.server = server


class SignInFailed(Exception):
    """The sign-in failed with *code*, one of :data:`SIGN_IN_ERRORS`."""

    def __init__(self, code: str) -> None:
        assert code in SIGN_IN_ERRORS, code
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class _Pending:
    identity: str
    server: str
    key: str
    store_name: str
    verifier: str
    discovery: Discovery
    client: Client
    created_at: float


_pending: dict[str, _Pending] = {}
_lock = asyncio.Lock()
# Strong references to the scheduled follow-throughs, so none is garbage-collected mid-run.
_background: set[asyncio.Task[Any]] = set()


def reset_pending() -> None:
    """Drop every pending sign-in (tests)."""
    _pending.clear()


def pending_count() -> int:
    return len(_pending)


def _prune(now: float) -> None:
    for state in [s for s, p in _pending.items() if now - p.created_at >= STATE_TTL_S]:
        del _pending[state]


def _account_names() -> list[str]:
    return list(accounts._anthropic_account_names())


def _server(name: str) -> dict:
    if not valid_mcp_name(name):
        raise SignInFailed("not_configured")
    try:
        return scan.sign_in_server(scan.list_mcp_servers()["servers"], name)
    except scan.SignInServerError:
        raise SignInFailed("not_configured") from None


async def start(identity: str, server_name: str) -> str:
    """The provider authorize URL for *server_name*. Raises :class:`PasteBack` when one-tap
    is off or the provider cannot take ViberPit as a client, and
    ``SignInFailed("not_configured")`` for a name that is no single http/sse server."""
    origin = get_settings().public_origin
    if not origin:
        raise PasteBack(server_name)
    server = await asyncio.to_thread(_server, server_name)
    key = grants.key_for(server)
    if key is None:  # no knowable CLI key (no type/url, or a ${…} placeholder)
        raise PasteBack(server_name)
    try:
        discovery = await asyncio.to_thread(discover, server["config"]["url"])
        client = await asyncio.to_thread(ensure_client, discovery, f"{origin}{CALLBACK_PATH}")
    except (DiscoveryFailed, RegistrationUnsupported, RegistrationFailed) as exc:
        log.warning("mcp one-tap: falling back to paste-back server=%s reason=%s",
                    server_name, type(exc).__name__)
        raise PasteBack(server_name) from None
    verifier, challenge = pkce_pair()
    state = new_state()
    async with _lock:
        now = _clock()
        _prune(now)
        while len(_pending) >= MAX_PENDING:
            del _pending[min(_pending, key=lambda s: _pending[s].created_at)]
        _pending[state] = _Pending(identity, server_name, key,
                                   store_name(server_name, server.get("pluginName")),
                                   verifier, discovery, client, now)
    return authorize_url(discovery, client, state=state, code_challenge=challenge,
                         scope=choose_scope(discovery))


async def _take(identity: str, state: str | None) -> _Pending:
    async with _lock:
        _prune(_clock())
        pending = _pending.pop(state, None) if state else None
    if pending is None or pending.identity != identity:
        raise SignInFailed("state_invalid")
    return pending


async def complete(identity: str, *, state: str | None, code: str | None, error: str | None,
                   orch_getter: Callable[[], Any]) -> str:
    """Finish the sign-in the provider redirected back with; return the server name.

    The state is consumed before anything else, so a replay or a foreign identity's
    callback is refused and writes nothing."""
    pending = await _take(identity, state)
    if error is not None or not code:
        raise SignInFailed("denied")
    try:
        bundle = await asyncio.to_thread(
            exchange_code, pending.discovery, pending.client, code=code,
            verifier=pending.verifier, server_name=pending.store_name,
            now_ms=int(time.time() * 1000))
    except ExchangeFailed as exc:
        log.warning("mcp one-tap: code exchange failed server=%s reason=%s code=%s",
                    pending.server, type(exc).__name__, exc.code)
        if exc.code == "invalid_client":
            await asyncio.to_thread(forget_client, pending.discovery.authorization_server)
            raise PasteBack(pending.server) from None
        raise SignInFailed("exchange_failed") from None
    try:
        await asyncio.to_thread(creds.merge_shared_mcp, {pending.key: bundle},
                                authenticated_keys={pending.key})
    except Exception as exc:
        log.warning("mcp one-tap: storing the grant failed server=%s reason=%s",
                    pending.server, type(exc).__name__)
        raise SignInFailed("exchange_failed") from None
    names = await asyncio.to_thread(_account_names)
    task = asyncio.create_task(after_login(orch_getter(), pending.server, names, signed_in=True))
    _background.add(task)
    task.add_done_callback(_follow_through_done)
    return pending.server


def _follow_through_done(task: asyncio.Task[Any]) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # The grant is stored; a failed follow-through only delays the account-dir publish
        # and reconnects to the next refresher tick.
        log.warning("mcp one-tap: post-login follow-through failed reason=%s",
                    type(task.exception()).__name__)
