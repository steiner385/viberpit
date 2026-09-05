"""Runner-side external-token auth: fetch envelopes, answer refresh callbacks.

This is the half of Phase 3 that runs on a *runner* — dobby's own daemon and
rocky's worker daemon alike — and its defining property is what it does not have.
A runner never holds a ChatGPT refresh token, never writes ``auth.json``, and
never keeps a credential between calls. It holds one integer per account: the
generation it last logged in with.

Two seams into ``server/codex/manager.py``:

* :meth:`ExternalAuthProvider.login_params` is the manager's ``CredentialSupplier``.
  It fetches an envelope over mTLS, converts it to
  ``account/login/start`` parameters, and drops every other reference — the token
  survives exactly as long as the JSON-RPC frame being written to the
  app-server's stdin.
* :meth:`ExternalAuthProvider.refresh_handler_for` builds the app-server's
  ``account/chatgptAuthTokens/refresh`` callback, bound to one account and one
  provider epoch.

Why the epoch. The app-server can ask for a refresh at any time, including after
the manager decided this runner's credentials were revoked and invalidated them.
A handler created before that invalidation must refuse rather than fetch a fresh
credential for an account the fleet has just stopped: the manager's own
invalidation would otherwise be undone by the very callback it was trying to
outrun. So a handler captures the account's epoch when it is built, and refuses
if the epoch has moved.

Failing closed. Every path here raises rather than degrading. There is no
fallback to managed runner auth: the whole point of the design is that a runner
*cannot* authenticate on its own, and a fallback would quietly restore the
property we removed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from server.codex import redaction, schema
from server.codex.auth_client import AuthorityClient
from server.codex.auth_endpoint import (
    AuthorityConfigError,
    authority_configured,
)
from server.codex.auth_store import AUTH_FILENAME
from server.codex.auth_types import (
    AuthorityError,
    AuthorityErrorCode,
    AuthorityUnavailable,
    CredentialEnvelope,
)
from server.codex.protocol import RefreshHandler
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: Consecutive refusals before an account is marked degraded and further callbacks
#: are refused locally without touching the authority. Bounded on purpose: an
#: app-server that keeps 401-ing against a revoked account would otherwise turn
#: one revocation into an unbounded request loop against the credential service.
DEFAULT_MAX_CONSECUTIVE_FAILURES: Final = 3


class ExternalAuthUnavailable(RuntimeError):
    """External-token auth cannot be used on this runner. Always fatal for Codex."""


class ExternalAuthProvider:
    """Supplies login params and refresh answers from the credential authority."""

    def __init__(
        self,
        client: AuthorityClient,
        *,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self._client = client
        self._max_failures = max(1, max_consecutive_failures)
        #: account -> generation we last logged in (or refreshed) with.
        self._generations: dict[str, int] = {}
        #: Native identity fence retained only as a one-way digest. This lets a
        #: refresh callback prove continuity without parking the identifier itself.
        self._account_ids: dict[str, str] = {}
        #: account -> monotonic epoch, bumped by invalidate(). Handlers capture it.
        self._epochs: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._degraded: dict[str, str] = {}
        #: account -> the single in-flight refresh, so three threads on one
        #: app-server produce one authority call, not three.
        self._refreshes: dict[str, asyncio.Task[CredentialEnvelope]] = {}

    def __repr__(self) -> str:
        return f"<ExternalAuthProvider accounts={sorted(self._generations)}>"

    # -- introspection -----------------------------------------------------

    def generation_for(self, account: str) -> int:
        return self._generations.get(account, 0)

    def degraded_reason(self, account: str) -> str | None:
        return self._degraded.get(account)

    def invalidate(self, account: str) -> None:
        """Drop everything we know about *account* and orphan its handlers.

        Called on revocation fan-out and on manager teardown. Bumping the epoch is
        what makes an already-issued refresh handler refuse: the manager cannot
        reach into an app-server to unregister a callback, so the callback has to
        be able to notice it is stale.
        """
        self._epochs[account] = self._epochs.get(account, 0) + 1
        self._generations.pop(account, None)
        self._account_ids.pop(account, None)
        self._failures.pop(account, None)
        self._degraded.pop(account, None)
        pending = self._refreshes.pop(account, None)
        if pending is not None and not pending.done():
            # `cancel()` WITHOUT awaiting, deliberately: this method is sync (the
            # manager calls it from a fan-out that must not block on a wedged
            # authority), so there is no `await` here to swallow an outer
            # cancellation — the hazard `cancel_and_reap` exists for. The task's own
            # exception is retrieved by the done-callback below so it is not
            # reported as never-retrieved.
            pending.cancel()
            pending.add_done_callback(lambda t: t.cancelled() or t.exception())
        log_event(logger, logging.INFO, "codex.external_auth.invalidated", account=account)

    # -- the manager's CredentialSupplier ----------------------------------

    async def login_params(self, account: str) -> schema.ExternalAuthLoginParams:
        """Fetch an envelope and convert it to login parameters.

        The envelope is local: nothing here stores it. Only its generation and a
        one-way digest of its native account identifier outlive this frame. A
        provider that cached envelopes would be a second credential store on the
        exact host that is not supposed to have one.
        """
        degraded = self._degraded.get(account)
        if degraded is not None:
            raise ExternalAuthUnavailable(
                f"codex auth for {account} is degraded: {degraded}"
            )
        minimum_generation = self._generations.get(account, 0)
        try:
            envelope = await self._client.envelope(
                account, minimum_generation=minimum_generation
            )
        except AuthorityError as exc:
            self._note_failure(account, exc)
            raise ExternalAuthUnavailable(
                f"the credential authority refused {account}: {exc.code.value}"
            ) from None
        except AuthorityUnavailable as exc:
            raise ExternalAuthUnavailable(
                f"the credential authority is unreachable: {exc}"
            ) from None
        if envelope.generation < minimum_generation:
            raise ExternalAuthUnavailable(
                f"credential authority returned an older generation for {account}"
            )
        self._accept(account, envelope)
        return envelope.to_login_params()

    # -- the app-server's refresh callback ---------------------------------

    def refresh_handler_for(self, account: str) -> RefreshHandler:
        """Build the ``account/chatgptAuthTokens/refresh`` handler for one account."""
        epoch = self._epochs.get(account, 0)

        async def handler(
            params: schema.ChatgptAuthTokensRefreshParams,
        ) -> schema.ChatgptAuthTokensRefreshResponse:
            if self._epochs.get(account, 0) != epoch:
                # The manager instance that installed this handler has been
                # invalidated. Refusing is the point: answering would re-credential
                # an app-server the fleet has already decided to stop.
                raise ExternalAuthUnavailable(
                    f"refresh handler for {account} belongs to a superseded manager"
                )
            expected_id = self._account_ids.get(account)
            if (
                expected_id is None
                or params.previous_account_id is None
                or _identity_digest(params.previous_account_id) != expected_id
            ):
                raise ExternalAuthUnavailable(
                    f"refresh handler for {account} received a mismatched native account"
                )
            rejected_generation = self._generations.get(account, 0)
            if rejected_generation < 1:
                raise ExternalAuthUnavailable(
                    f"refresh handler for {account} has no accepted generation"
                )
            envelope = await self._refresh(
                account,
                reason=params.reason,
                expected_epoch=epoch,
                rejected_generation=rejected_generation,
            )
            return envelope.to_refresh_response()

        return handler

    async def _refresh(
        self,
        account: str,
        *,
        reason: str,
        expected_epoch: int | None = None,
        rejected_generation: int | None = None,
    ) -> CredentialEnvelope:
        degraded = self._degraded.get(account)
        if degraded is not None:
            raise ExternalAuthUnavailable(
                f"codex auth for {account} is degraded: {degraded}"
            )
        existing = self._refreshes.get(account)
        if existing is not None and not existing.done():
            # Shield, so a caller that hits its own deadline detaches without
            # cancelling the rotation every other caller is waiting on.
            return await asyncio.shield(existing)
        task = asyncio.create_task(
            self._do_refresh(
                account,
                reason,
                expected_epoch=(
                    self._epochs.get(account, 0)
                    if expected_epoch is None
                    else expected_epoch
                ),
                rejected_generation=(
                    self._generations.get(account, 0)
                    if rejected_generation is None
                    else rejected_generation
                ),
            ),
            name=f"codex-auth-refresh-{account}",
        )
        self._refreshes[account] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._refreshes.get(account) is task and task.done():
                self._refreshes.pop(account, None)

    async def _do_refresh(
        self,
        account: str,
        reason: str,
        *,
        expected_epoch: int,
        rejected_generation: int,
    ) -> CredentialEnvelope:
        if self._epochs.get(account, 0) != expected_epoch:
            raise ExternalAuthUnavailable(
                f"refresh handler for {account} belongs to a superseded manager"
            )
        if self._generations.get(account, 0) != rejected_generation:
            raise ExternalAuthUnavailable(
                f"refresh handler for {account} no longer matches the rejected generation"
            )
        try:
            envelope = await self._client.refresh(
                account, rejected_generation=rejected_generation
            )
        except AuthorityError as exc:
            self._note_failure(account, exc)
            raise ExternalAuthUnavailable(
                f"credential refresh for {account} failed: {exc.code.value}"
            ) from None
        except AuthorityUnavailable as exc:
            self._note_failure(account, exc)
            raise ExternalAuthUnavailable(
                f"credential refresh for {account} could not reach the authority: {exc}"
            ) from None
        if self._epochs.get(account, 0) != expected_epoch:
            raise ExternalAuthUnavailable(
                f"refresh handler for {account} belongs to a superseded manager"
            )
        if self._generations.get(account, 0) != rejected_generation:
            raise ExternalAuthUnavailable(
                f"refresh handler for {account} no longer matches the rejected generation"
            )
        if envelope.generation <= rejected_generation:
            # The authority answered with a credential no newer than the one the
            # app-server just rejected. Retrying would produce the same answer, so
            # this counts as a failure rather than a success.
            self._note_failure(
                account,
                AuthorityError(
                    AuthorityErrorCode.REFRESH_FAILED,
                    "refresh returned a generation that was already rejected",
                    account=account,
                ),
            )
            raise ExternalAuthUnavailable(
                f"credential refresh for {account} returned generation "
                f"{envelope.generation}, which was already rejected"
            )
        self._accept(account, envelope)
        log_event(
            logger,
            logging.INFO,
            "codex.external_auth.refreshed",
            account=account,
            generation=envelope.generation,
            reason=reason,
        )
        return envelope

    # -- bookkeeping -------------------------------------------------------

    def _accept(self, account: str, envelope: CredentialEnvelope) -> None:
        if envelope.account != account:
            raise ExternalAuthUnavailable(
                "credential authority returned an envelope for another account alias"
            )
        account_id_digest = _identity_digest(envelope.chatgpt_account_id)
        previous_id = self._account_ids.get(account)
        if previous_id is not None and account_id_digest != previous_id:
            raise ExternalAuthUnavailable(
                f"credential authority changed the native account identity for {account}"
            )
        self._generations[account] = envelope.generation
        self._account_ids[account] = account_id_digest
        self._failures.pop(account, None)
        self._degraded.pop(account, None)
    def _note_failure(self, account: str, exc: Exception) -> None:
        """Count a refusal and, past the cap, stop asking.

        A *non-retryable* refusal degrades immediately: ``account_disabled`` and
        ``revoked`` will not become true again by asking a fourth time, and the
        remedy is an operator action, not a retry.
        """
        settled = isinstance(exc, AuthorityError) and not exc.retryable
        count = self._failures.get(account, 0) + 1
        self._failures[account] = count
        if settled or count >= self._max_failures:
            reason = (
                exc.code.value
                if isinstance(exc, AuthorityError)
                else redaction.scrub_exception(exc)
            )
            self._degraded[account] = reason
            log_event(
                logger,
                logging.ERROR,
                "codex.external_auth.degraded",
                account=account,
                reason=reason,
                failures=count,
            )


def _identity_digest(value: redaction.Redacted) -> str:
    """Return a non-reversible continuity key for a native account identifier."""

    return hashlib.sha256(value.reveal().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------


def assert_no_runner_credentials(home: str | Path) -> None:
    """Fail loudly if a runner's ``CODEX_HOME`` has acquired an ``auth.json``.

    The external-token design's central claim is that a runner holds no
    credential. That claim is only worth anything if something checks it, so the
    manager checks it after every login and the test suite checks it after a full
    two-runner cycle.
    """
    path = Path(home) / AUTH_FILENAME
    if path.exists():
        raise ExternalAuthUnavailable(
            f"{path} exists: a runner using external-token auth must never hold a "
            f"credential cache. Refusing to continue rather than leaving a "
            f"refresh token on a session host."
        )


def build_external_auth_provider(settings: Any) -> ExternalAuthProvider:
    """Construct the provider, or raise. Never returns a degraded object.

    Three separate refusals, in the order they can be detected:

    1. ``CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED`` is off — the experimental
       app-server surface this depends on has not been opted into.
    2. No authority endpoint is configured on this host.
    3. The mTLS material is missing, unreadable, or shares a key with the session
       daemon (:mod:`server.codex.auth_endpoint` decides that).
    """
    if not bool(getattr(settings, "codex_external_auth_enabled", False)):
        raise ExternalAuthUnavailable(
            "CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED is false; native Codex requires "
            "the experimental external-token surface and will not fall back to "
            "managed runner credentials"
        )
    if not authority_configured():
        raise ExternalAuthUnavailable(
            "CCSWITCH_CODEX_AUTHORITY_ENDPOINT is unset; a runner cannot "
            "authenticate without a credential authority"
        )
    try:
        client = AuthorityClient.from_env()
    except AuthorityConfigError as exc:
        raise ExternalAuthUnavailable(str(exc)) from None
    return ExternalAuthProvider(client)


#: Signature the manager uses to build a per-account refresh handler.
RefreshHandlerFactory = Callable[[str], RefreshHandler]
