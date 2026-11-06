"""One-tap MCP sign-in on the edge (MCP auth overhaul phase 4).

:func:`start` discovers the server, ensures a registered client (inside one overall
:data:`START_DEADLINE_S`) and returns the provider's authorize URL, remembering
``{state: pending}`` in memory. :func:`complete` takes the provider's callback, consumes
that state (single use, 10 min TTL, bound to the tailnet identity that started the flow),
checks the response's ``iss`` against the authorization server the flow started at
(RFC 9207), exchanges the code, writes the bundle to the shared store at the server's CLI
key and schedules the post-login follow-through.

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

from viberpit import accounts, creds, fleet_role
from viberpit.mcp_keys import store_name
from viberpit.mcp_oauth_client import (Client, Discovery, DiscoveryFailed, ExchangeFailed,
                                       RegistrationFailed, RegistrationUnsupported,
                                       authorize_url, choose_scope, discover, ensure_client,
                                       exchange_code, forget_client, new_state, pkce_pair)
from viberpit.mcp_oauth_refresh import url_href

from server.aio import cancel_and_reap
from server.config import get_settings
from server.library import scan
from server.library.names import valid_mcp_name
from server.mcp_health import grants
from server.mcp_health.after_login import after_login

log = logging.getLogger(__name__)

CALLBACK_PATH = "/api/mcp/oauth/callback"
STATE_TTL_S = 600.0
MAX_PENDING = 100
# Overall deadline for a start's discovery + registration. Each leg has its own HTTP
# timeout, but a slow provider could chain probe + up to six metadata fetches + DCR to
# ~2 min while the browser waits on the redirect; past this the tap falls back to
# paste-back (the thread finishes on its own timeouts; its result is simply unused).
START_DEADLINE_S = 30.0
SIGN_IN_ERRORS = frozenset({"state_invalid", "denied", "exchange_failed",
                            "registration_failed", "not_configured"})

_clock: Callable[[], float] = time.monotonic


class PasteBack(Exception):
    """One-tap cannot sign in to *server*; the paste-back flow should take over."""

    def __init__(self, server: str) -> None:
        super().__init__(server)
        self.server = server


class SignInFailed(Exception):
    """The sign-in failed with *code*, one of :data:`SIGN_IN_ERRORS` (anything else raises
    ``ValueError``). ``registration_failed`` is reserved: by ruling a refused registration
    falls back to paste-back (:class:`PasteBack`) instead."""

    def __init__(self, code: str) -> None:
        if code not in SIGN_IN_ERRORS:
            raise ValueError("sign-in error code outside the closed set")
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


def _oauth_code(exc: Exception) -> str:
    """*exc*'s OAuth ``error`` code, lower-cased: providers differ in case (AWS answers
    ``INVALID_GRANT``), the standard codes are lower-case."""
    return (getattr(exc, "code", None) or "").lower()


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
    ``SignInFailed("not_configured")`` for a name that is no single http/sse server, or
    on a worker host (it cannot write the shared store)."""
    if fleet_role.is_worker():
        raise SignInFailed("not_configured")
    # Validate the name first, so an arbitrary string is never echoed into a redirect.
    server = await asyncio.to_thread(_server, server_name)
    origin = get_settings().public_origin
    if not origin:
        raise PasteBack(server_name)
    key = grants.key_for(server)
    if key is None:  # no knowable CLI key (no type/url, or a ${…} placeholder)
        raise PasteBack(server_name)
    try:
        async with asyncio.timeout(START_DEADLINE_S):
            discovery = await asyncio.to_thread(discover, server["config"]["url"])
            client = await asyncio.to_thread(ensure_client, discovery,
                                             f"{origin}{CALLBACK_PATH}")
    except (DiscoveryFailed, RegistrationUnsupported, RegistrationFailed, TimeoutError) as exc:
        # No DCR (e.g. KinDash) and a refused redirect URI (AWS answers
        # ``invalid_redirect_uri`` to the tailnet callback) are provider policy, not a
        # fault: INFO, so a Sign-in tap does not auto-file an issue. Any other refused or
        # broken registration is WARNING, as is a provider too slow to finish discovery
        # inside START_DEADLINE_S.
        code = _oauth_code(exc)
        expected = isinstance(exc, RegistrationUnsupported) or code == "invalid_redirect_uri"
        level = logging.INFO if expected else logging.WARNING
        log.log(level, "mcp one-tap: falling back to paste-back server=%s reason=%s code=%s",
                    server_name, type(exc).__name__, code or None)
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


def _check_issuer(pending: _Pending, iss: str | None) -> None:
    """RFC 9207: a callback carrying ``iss`` must name the authorization server the flow
    started at, and one from a server advertising
    ``authorization_response_iss_parameter_supported`` must carry it. Either failure is
    an authorization response that is not ours (mix-up), read as ``state_invalid``."""
    if iss is None:
        supported = pending.discovery.as_metadata.get(
            "authorization_response_iss_parameter_supported")
        if supported is True:
            raise SignInFailed("state_invalid")
        return
    if url_href(iss) != pending.discovery.authorization_server:
        raise SignInFailed("state_invalid")


async def complete(identity: str, *, state: str | None, code: str | None, error: str | None,
                   orch_getter: Callable[[], Any], iss: str | None = None) -> str:
    """Finish the sign-in the provider redirected back with; return the server name.

    The state is consumed before anything else, so a replay or a foreign identity's
    callback is refused and writes nothing. The issuer is checked next (RFC 9207), on
    error responses too, so a response from another authorization server never reads as
    the user's refusal."""
    pending = await _take(identity, state)
    _check_issuer(pending, iss)
    if error is not None:
        # Only the user's refusal reads as "denied"; any other provider error is a failed
        # sign-in. The provider's text is never reflected.
        raise SignInFailed("denied" if error.lower() == "access_denied" else "exchange_failed")
    if not code:
        raise SignInFailed("denied")
    try:
        bundle = await asyncio.to_thread(
            exchange_code, pending.discovery, pending.client, code=code,
            verifier=pending.verifier, server_name=pending.store_name,
            now_ms=int(time.time() * 1000))
    except ExchangeFailed as exc:
        # invalid_grant is usually a code that outlived its lifetime (the user lingered on
        # the provider page): expected, so INFO. Anything else is WARNING.
        level = logging.INFO if _oauth_code(exc) == "invalid_grant" else logging.WARNING
        log.log(level, "mcp one-tap: code exchange failed server=%s reason=%s code=%s",
                    pending.server, type(exc).__name__, exc.code)
        if _oauth_code(exc) == "invalid_client":
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


async def shutdown() -> None:
    """Cancel and reap the in-flight post-login follow-throughs (edge lifespan shutdown)."""
    for task in list(_background):
        await cancel_and_reap(task, phase="shutdown:mcp_one_tap")
    _background.clear()


def _follow_through_done(task: asyncio.Task[Any]) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # The grant is stored; a failed follow-through only delays the account-dir publish
        # and reconnects to the next refresher tick.
        log.warning("mcp one-tap: post-login follow-through failed reason=%s",
                    type(task.exception()).__name__)
