"""The ChatGPT credential authority: the one process that holds refresh tokens.

Everything about this module follows from a single decision in the plan: a runner
must never hold a ChatGPT refresh token. It gets short-lived access-token
*envelopes* instead, and something has to own the long-lived credential that mints
them. That something is this class, running as its own systemd service under its
own OS user on dobby, with its own mTLS listener and its own private state
directory.

What it owns
------------
* **Managed homes.** One authority-only ``CODEX_HOME`` per OpenAI account
  (:mod:`server.codex.auth_store`), outside the Claude account store and outside
  every runner mount.
* **Device login.** ``codex login --device-auth`` as a supervised subprocess —
  the same shape ``server/sessions/copilot_auth.py`` uses for GitHub's device
  flow, and for the same reason: the vendor binary already implements the flow
  correctly, and reimplementing OAuth to avoid a subprocess would be a second
  implementation of someone else's protocol.
* **Generations.** A monotonic, *persisted*, per-account counter. Every envelope
  carries one. A rejected generation is how a runner says "this is stale"; a
  refresh may only ever publish a higher one. This is the entire anti-rollback
  mechanism, and it survives an authority restart because it is on disk.
* **Refresh.** Coalesced per account: concurrent 401s from three runners produce
  exactly one refresh. The refresh itself is performed by a short-lived
  ``codex app-server`` in the managed home calling ``account/read`` with
  ``refreshToken: true`` — the vendor's own rotation path, writing the vendor's
  own cache — after which the cache is re-read through
  :class:`~server.codex.auth_store.CredentialStore`. We do not re-implement the
  OAuth token endpoint.

What it deliberately does not own
---------------------------------
The list of accounts. The authority cannot enumerate accounts, cannot read the
Claude account store, and will not issue a credential for a name the edge has not
*recently* told it about (:meth:`authorize`). The projection carries a lease, so a
silent edge fails closed: an account disabled while the edge was down cannot keep
being served.

There is no automatic failover in v1. Two authorities sharing one state directory
would each believe they own the generation counter, so a second start takes the
instance lock or dies (:class:`InstanceLock`).
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from server.aio import cancel_and_reap
from server.codex import process as codex_process
from server.codex import redaction, schema
from server.codex.auth_store import CredentialStore
from server.codex.auth_types import (
    LOGIN_AUTHORIZED,
    LOGIN_AWAITING_AUTHORIZATION,
    LOGIN_FAILED,
    LOGIN_PENDING,
    AuthorityError,
    AuthorityErrorCode,
    AuthorityHealth,
    AuthorizationRecord,
    AuthorizationSync,
    CredentialEnvelope,
    LoginSnapshot,
)
from server.codex.protocol import CodexAppServerClient
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: How long a device login may run before the authority gives up on it. The user
#: has to visit a URL and type a code; ten minutes is the vendor's own window.
DEFAULT_LOGIN_TIMEOUT_S: Final = 600.0
#: Cap on one refresh (spawn app-server, initialize, account/read, re-read cache).
#: Shorter than the runner's own deadline so the runner sees our typed failure
#: rather than its own timeout — a typed failure names the remedy.
DEFAULT_REFRESH_TIMEOUT_S: Final = 6.0
#: Re-issue rather than refresh while the cached envelope has this much life left.
DEFAULT_EXPIRY_SKEW_S: Final = 60.0

#: Extracted from ``codex login --device-auth`` output purely to show the operator
#: where to go. Deliberately permissive: the login's SUCCESS is decided by the
#: credential cache appearing, never by matching this text, so a vendor wording
#: change degrades the UI hint and cannot break the flow.
_URI_RE: Final = re.compile(r"https://[^\s\x1b]+")
# Codex 0.153 emits e.g. ``CODE-12345`` while earlier builds/documentation used
# ``ABCD-1234``.  Match the vendor's bounded uppercase token shape rather than a
# single release's segment lengths; ANSI color resets sit outside the match.
_CODE_RE: Final = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{4,12}-[A-Z0-9]{4,12})(?![A-Z0-9])"
)


class InstanceLock:
    """An exclusive, advisory lock on the authority's state directory.

    Fails loudly. A second authority is not a warm spare — it is a second writer
    of the generation counter and a second device-login driver against the same
    managed homes, so the only safe response to "someone else holds this" is to
    refuse to start.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fh: Any = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = open(self._path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AuthorityError(
                AuthorityErrorCode.NOT_AUTHORITY,
                f"another codex credential authority already holds {self._path}; "
                f"v1 has no authority failover",
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def held(self) -> bool:
        return self._fh is not None

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            self._fh.close()
        self._fh = None


#: Runs one device login against a managed home. Injected so the whole state
#: machine is testable without a network or the vendor binary.
LoginRunner = Callable[[str, Path], Awaitable[None]]
#: Rotates the credential cache in a managed home. Same rationale.
RefreshRunner = Callable[[str, Path], Awaitable[None]]


@dataclass
class _Login:
    """In-flight device login for one account."""

    snapshot: LoginSnapshot = field(default_factory=LoginSnapshot)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    process: Any = field(default=None, repr=False)


class CredentialAuthority:
    """Device login, envelope issuance, coalesced refresh, and revocation."""

    def __init__(
        self,
        *,
        store: CredentialStore,
        binary: str = codex_process.CODEX_EXE,
        login_runner: LoginRunner | None = None,
        refresh_runner: RefreshRunner | None = None,
        login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        refresh_timeout_s: float = DEFAULT_REFRESH_TIMEOUT_S,
        expiry_skew_s: float = DEFAULT_EXPIRY_SKEW_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._binary = binary
        self._login_runner = login_runner or self._default_login_runner
        self._refresh_runner = refresh_runner or self._default_refresh_runner
        self._login_timeout_s = login_timeout_s
        self._refresh_timeout_s = refresh_timeout_s
        self._expiry_skew_s = expiry_skew_s
        self._clock = clock

        self._sync: AuthorizationSync | None = None
        self._sync_at: float = 0.0
        self._restore_sync()
        self._logins: dict[str, _Login] = {}
        #: account -> the single in-flight refresh. Every concurrent caller awaits
        #: the SAME task, which is what makes a refresh storm one refresh.
        self._refreshes: dict[str, asyncio.Task[CredentialEnvelope]] = {}
        #: account -> last envelope handed out. Held in memory only, never written.
        self._cache: dict[str, CredentialEnvelope] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    def __repr__(self) -> str:
        revision = self._sync.revision if self._sync is not None else None
        return f"<CredentialAuthority revision={revision} accounts={self.account_count}>"

    @property
    def account_count(self) -> int:
        return len(self._sync.records) if self._sync is not None else 0

    # -- authorization projection -----------------------------------------

    async def authorize(self, sync: AuthorizationSync) -> AuthorityHealth:
        """Install the edge's projection of who may be issued credentials.

        Rejects a *lower* revision outright. Accepting one would let a replayed or
        out-of-order sync re-enable an account the operator has just disabled —
        the projection is the only thing standing between "disabled in the UI" and
        "still minting tokens".
        """
        current = self._sync
        if current is not None and (
            sync.revision < current.revision
            or (sync.revision == current.revision and sync != current)
        ):
            raise AuthorityError(
                AuthorityErrorCode.REVISION_ROLLBACK,
                f"refusing non-advancing manifest revision {sync.revision}; "
                f"revision {current.revision} is already bound to another projection",
            )
        self._sync = sync
        self._sync_at = self._clock()
        try:
            self._store.save_sync(sync.to_wire(), at=self._sync_at)
        except OSError:
            # In force in memory but not durable: a restart before the next sync
            # will fail closed, which is the safe direction.
            logger.warning("codex authorization projection could not be persisted",
                           exc_info=True)
        log_event(
            logger,
            logging.INFO,
            "codex.auth.authorized",
            revision=sync.revision,
            accounts=len(sync.records),
            lease_s=sync.lease_s,
        )
        return self.health()

    def _restore_sync(self) -> None:
        """Reload the persisted projection, if any, on startup.

        The lease is still measured from the ORIGINAL install time, so a restart
        cannot extend an authorization the edge stopped renewing — it only avoids
        a multi-minute Codex outage every time a service that holds no sessions is
        restarted.
        """
        restored = self._store.load_sync()
        if restored is None:
            return
        payload, installed_at = restored
        try:
            sync = AuthorizationSync.from_wire(payload)
        except AuthorityError:
            logger.warning("codex authorization projection on disk is unusable",
                           exc_info=True)
            return
        self._sync = sync
        self._sync_at = installed_at
        log_event(
            logger,
            logging.INFO,
            "codex.auth.sync_restored",
            revision=sync.revision,
            accounts=len(sync.records),
            age_s=int(max(0.0, self._clock() - installed_at)),
        )

    def health(self) -> AuthorityHealth:
        expires_in: float | None = None
        if self._sync is not None:
            expires_in = (self._sync_at + self._sync.lease_s) - self._clock()
        return AuthorityHealth(
            reachable=True,
            sync_revision=self._sync.revision if self._sync is not None else None,
            sync_expires_in_s=expires_in,
            accounts=self.account_count,
        )

    def _authorized(self, account: str, *, require_enabled: bool = True) -> AuthorizationRecord:
        """The projection's record for *account*, or a typed refusal.

        Order matters. The lease is checked *first*, because an expired lease means
        we do not know the account's current state — reporting "enabled" from a
        projection we can no longer vouch for is exactly the failure this design
        exists to prevent.
        """
        sync = self._sync
        if sync is None:
            raise AuthorityError(
                AuthorityErrorCode.SYNC_EXPIRED,
                "no authorization sync has been received yet",
                account=account,
            )
        if self._clock() > self._sync_at + sync.lease_s:
            raise AuthorityError(
                AuthorityErrorCode.SYNC_EXPIRED,
                f"authorization sync (revision {sync.revision}) is past its lease",
                account=account,
            )
        record = sync.get(account)
        if record is None:
            raise AuthorityError(
                AuthorityErrorCode.UNKNOWN_ACCOUNT,
                "account is not in the authorization sync",
                account=account,
            )
        if require_enabled and not record.enabled:
            raise AuthorityError(
                AuthorityErrorCode.ACCOUNT_DISABLED,
                "account is disabled in the account manifest",
                account=account,
            )
        return record

    async def _lock_for(self, account: str) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._locks.get(account)
            if lock is None:
                lock = self._locks[account] = asyncio.Lock()
            return lock

    # -- device login ------------------------------------------------------

    async def login_start(self, account: str) -> LoginSnapshot:
        """Begin (or refuse to duplicate) a device login for *account*.

        Note the ``require_enabled=False``: an operator adding an account is
        allowed to log it in before it is enabled for sessions, and re-authing a
        disabled account is how you fix one. Only *issuance* requires enabled.
        """
        self._authorized(account, require_enabled=False)
        lock = await self._lock_for(account)
        async with lock:
            existing = self._logins.get(account)
            if existing is not None and not existing.snapshot.terminal:
                raise AuthorityError(
                    AuthorityErrorCode.LOGIN_BUSY,
                    "a device login is already running for this account",
                    account=account,
                )
            home = self._store.ensure_home(account)
            state = _Login(snapshot=LoginSnapshot(state=LOGIN_PENDING))
            self._logins[account] = state
            state.task = asyncio.create_task(
                self._run_login(account, home), name=f"codex-auth-login-{account}"
            )
            return state.snapshot

    def login_status(self, account: str) -> LoginSnapshot:
        state = self._logins.get(account)
        return state.snapshot if state is not None else LoginSnapshot()

    async def login_cancel(self, account: str) -> LoginSnapshot:
        state = self._logins.get(account)
        if state is None:
            return LoginSnapshot()
        # cancel_and_reap, not `cancel()` + `suppress(CancelledError)`: this can run
        # inside the authority's own shutdown, where that suppress would swallow the
        # CALLER's cancellation and let shutdown run on as though it were never
        # cancelled (#1164). Bounded, because a wedged login must not hold shutdown.
        if state.task is not None and not state.task.done():
            await cancel_and_reap(
                state.task, phase=f"codex-auth-login:{account}", timeout=5.0
            )
        # The task may have completed successfully during the cancel/reap above
        # (or between the caller's own status check and this call). A login that
        # already succeeded must never be relabelled failed-and-discardable: that
        # would hand the add-flow cancel route a false "safe to delete" signal for
        # an account that now holds a real credential.
        if state.snapshot.state != LOGIN_AUTHORIZED:
            state.snapshot = LoginSnapshot(state=LOGIN_FAILED, error="cancelled")
        return state.snapshot

    async def _run_login(self, account: str, home: Path) -> None:
        lock = await self._lock_for(account)
        async with lock:
            try:
                await asyncio.wait_for(
                    self._login_runner(account, home), self._login_timeout_s
                )
            except asyncio.CancelledError:
                self._set_login(account, LoginSnapshot(state=LOGIN_FAILED, error="cancelled"))
                raise
            except TimeoutError:
                self._set_login(
                    account,
                    LoginSnapshot(state=LOGIN_FAILED, error="device login timed out"),
                )
                return
            except Exception as exc:  # noqa: BLE001 -- one typed login failure
                self._set_login(
                    account,
                    LoginSnapshot(
                        state=LOGIN_FAILED, error=redaction.scrub_exception(exc)
                    ),
                )
                return
            # Validate the newly-written cache before advancing or clearing the
            # durable revoke fence. A subprocess exit code is not authentication.
            try:
                self._store.assert_private(account)
                next_generation = self._store.generation(account) + 1
                envelope = self._store.read_envelope(
                    account, generation=next_generation
                )
                generation = self._store.mark_authenticated(account)
                if generation != next_generation:  # defensive: lock owns this writer
                    raise AuthorityError(
                        AuthorityErrorCode.INTERNAL,
                        "credential generation changed during login publication",
                        account=account,
                    )
            except AuthorityError as exc:
                self._set_login(
                    account,
                    LoginSnapshot(
                        state=LOGIN_FAILED, error=exc.detail or exc.code.value
                    ),
                )
                return
        self._cache[account] = envelope
        self._set_login(
            account,
            LoginSnapshot(state=LOGIN_AUTHORIZED, generation=envelope.generation),
        )
        log_event(
            logger,
            logging.INFO,
            "codex.auth.login_completed",
            account=account,
            generation=envelope.generation,
        )

    def _set_login(self, account: str, snapshot: LoginSnapshot) -> None:
        state = self._logins.get(account)
        if state is None:
            self._logins[account] = _Login(snapshot=snapshot)
        else:
            state.snapshot = snapshot

    def _note_login_prompt(self, account: str, line: str) -> None:
        """Lift the verification URI / user code out of the login's own output."""
        state = self._logins.get(account)
        if state is None or state.snapshot.terminal:
            return
        uri_match = _URI_RE.search(line)
        code_match = _CODE_RE.search(line)
        if uri_match is None and code_match is None:
            return
        snapshot = state.snapshot
        state.snapshot = replace(
            snapshot,
            state=LOGIN_AWAITING_AUTHORIZATION,
            verification_uri=(
                uri_match.group(0) if uri_match else snapshot.verification_uri
            ),
            user_code=code_match.group(1) if code_match else snapshot.user_code,
        )

    async def _default_login_runner(self, account: str, home: Path) -> None:
        """Drive ``codex login --device-auth`` in the account's managed home.

        stdout/stderr are consumed line by line so the verification URI reaches the
        operator while the flow is still open. Lines are **not** logged: the device
        code is short-lived but it is still an authentication factor.
        """
        child = await asyncio.create_subprocess_exec(
            self._binary,
            "login",
            "--device-auth",
            env=self._child_env(home),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        state = self._logins.get(account)
        if state is not None:
            state.process = child
        try:
            assert child.stdout is not None
            while True:
                raw = await child.stdout.readline()
                if not raw:
                    break
                self._note_login_prompt(
                    account, raw.decode("utf-8", "replace").strip()
                )
            code = await child.wait()
        finally:
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    child.kill()
                with contextlib.suppress(Exception):
                    await child.wait()
        if code != 0:
            raise AuthorityError(
                AuthorityErrorCode.LOGIN_REQUIRED,
                f"codex login exited {code}",
                account=account,
            )

    def _child_env(self, home: Path) -> dict[str, str]:
        """A minimal environment for a vendor subprocess.

        ``CODEX_HOME`` is the whole point; ``HOME`` is pinned to the same directory
        so that a vendor code path that falls back to ``~`` cannot write the
        authority user's real home, and ``PATH``/``TERM`` are the least a CLI needs.
        Nothing else is inherited — an authority subprocess has no business seeing
        ``CCSWITCH_*``, ``ANTHROPIC_*``, or proxy variables.
        """
        return {
            "CODEX_HOME": str(home),
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "TERM": "dumb",
        }

    async def logout(self, account: str) -> None:
        """Clear the managed credential. The generation is deliberately kept.

        Keeping it is what stops a logout→login cycle from re-minting a number an
        in-flight runner is still fencing against.
        """
        self._authorized(account, require_enabled=False)
        await self.login_cancel(account)
        lock = await self._lock_for(account)
        async with lock:
            home = self._store.home(account)
            if home.is_dir():
                with contextlib.suppress(Exception):
                    await self._run_vendor(("logout",), home)
            self._store.clear_credentials(account)
            self._cache.pop(account, None)
        log_event(logger, logging.INFO, "codex.auth.logged_out", account=account)

    async def _run_vendor(self, args: tuple[str, ...], home: Path) -> int:
        child = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            env=self._child_env(home),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await child.wait()

    # -- issuance ----------------------------------------------------------

    async def get_envelope(
        self, account: str, *, minimum_generation: int = 0
    ) -> CredentialEnvelope:
        """Hand out a credential envelope at or above *minimum_generation*.

        The cached envelope is reused when it is new enough AND not near expiry.
        Otherwise this falls through to :meth:`refresh`, which is coalesced — so
        "the cache is stale" costs one refresh no matter how many runners noticed.
        """
        self._authorized(account)
        if self._store.revoked(account):
            raise AuthorityError(
                AuthorityErrorCode.REVOKED,
                "credentials were explicitly revoked; device login is required",
                account=account,
            )
        cached = self._cache.get(account)
        if cached is None:
            generation = self._store.generation(account)
            if generation < 1:
                raise AuthorityError(
                    AuthorityErrorCode.LOGIN_REQUIRED,
                    "this account has never completed a device login",
                    account=account,
                )
            cached = self._store.read_envelope(account, generation=generation)
            self._cache[account] = cached
        if cached.generation >= minimum_generation and not cached.expired(
            now=self._clock(), skew_s=self._expiry_skew_s
        ):
            return cached
        refreshed = await self.refresh(
            account, rejected_generation=cached.generation
        )
        if refreshed.generation < minimum_generation:
            raise AuthorityError(
                AuthorityErrorCode.REFRESH_FAILED,
                "one credential rotation could not satisfy the requested generation fence",
                account=account,
            )
        return refreshed

    async def refresh(
        self, account: str, *, rejected_generation: int = 0
    ) -> CredentialEnvelope:
        """Rotate *account*'s credential, at most once concurrently.

        A **stale rejection** — one naming a generation we have already moved past
        — is answered with the current envelope and performs no rotation at all.
        Without that check, three runners rejecting generation 4 in sequence would
        each trigger a rotation, and the third would publish a token the first two
        had already been told to stop using.
        """
        self._authorized(account)
        if self._store.revoked(account):
            raise AuthorityError(
                AuthorityErrorCode.REVOKED,
                "credentials were explicitly revoked; device login is required",
                account=account,
            )
        cached = self._cache.get(account)
        if (
            cached is not None
            and rejected_generation
            and cached.generation > rejected_generation
        ):
            log_event(
                logger,
                logging.INFO,
                "codex.auth.refresh_stale_rejection",
                account=account,
                rejected=rejected_generation,
                current=cached.generation,
            )
            return cached

        existing = self._refreshes.get(account)
        if existing is not None and not existing.done():
            # Detaching from a shared task must not cancel it: a waiter that gives
            # up on its own deadline would otherwise abort the rotation every other
            # waiter is still relying on.
            return await asyncio.shield(existing)

        task = asyncio.create_task(
            self._do_refresh(account), name=f"codex-auth-refresh-{account}"
        )
        self._refreshes[account] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._refreshes.get(account) is task and task.done():
                self._refreshes.pop(account, None)

    async def _do_refresh(self, account: str) -> CredentialEnvelope:
        lock = await self._lock_for(account)
        async with lock:
            if self._store.revoked(account):
                raise AuthorityError(
                    AuthorityErrorCode.REVOKED,
                    "credentials were explicitly revoked; device login is required",
                    account=account,
                )
            home = self._store.home(account)
            if not self._store.has_credentials(account):
                raise AuthorityError(
                    AuthorityErrorCode.LOGIN_REQUIRED,
                    "no credential cache to refresh",
                    account=account,
                )
            started = self._clock()
            current_generation = self._store.generation(account)
            previous = self._store.read_envelope(
                account, generation=current_generation
            )
            try:
                await asyncio.wait_for(
                    self._refresh_runner(account, home), self._refresh_timeout_s
                )
            except asyncio.CancelledError:
                raise
            except AuthorityError:
                raise
            except TimeoutError:
                raise AuthorityError(
                    AuthorityErrorCode.REFRESH_FAILED,
                    "the credential refresh did not complete in time",
                    account=account,
                ) from None
            except Exception as exc:  # noqa: BLE001 -- one typed refresh failure
                raise AuthorityError(
                    AuthorityErrorCode.REFRESH_FAILED,
                    redaction.scrub_exception(exc),
                    account=account,
                ) from None
            candidate = self._store.read_envelope(
                account, generation=current_generation + 1
            )
            if candidate.access_token == previous.access_token:
                raise AuthorityError(
                    AuthorityErrorCode.REFRESH_FAILED,
                    "credential refresh did not advance the rejected access token",
                    account=account,
                )
            if candidate.chatgpt_account_id != previous.chatgpt_account_id:
                # The vendor has already replaced auth.json. Poison this cache
                # durably before returning so a later refresh cannot adopt the
                # switched identity as its new baseline.
                self._cache.pop(account, None)
                self._store.mark_revoked(account)
                raise AuthorityError(
                    AuthorityErrorCode.REFRESH_FAILED,
                    "credential refresh changed the native account identity",
                    account=account,
                )
            generation = self._store.bump_generation(account)
            if generation != candidate.generation:
                raise AuthorityError(
                    AuthorityErrorCode.INTERNAL,
                    "credential generation changed during refresh publication",
                    account=account,
                )
            envelope = candidate
            self._cache[account] = envelope
        log_event(
            logger,
            logging.INFO,
            "codex.auth.refreshed",
            account=account,
            generation=generation,
            elapsed_ms=int((self._clock() - started) * 1000),
        )
        return envelope

    async def _default_refresh_runner(self, account: str, home: Path) -> None:
        """Rotate the cache through the vendor's own ``account/read`` path.

        A short-lived ``codex app-server`` in the managed home, initialized with
        ``external_auth_enabled=False`` — the authority is the one place that uses
        *managed* credentials, so it must not advertise the external-token surface
        and must never be asked to answer a refresh callback itself.
        """
        process = codex_process.CodexProcess(self._binary, home)
        client: CodexAppServerClient | None = None
        try:
            await process.start()
            client = CodexAppServerClient(
                process.transport, external_auth_enabled=False
            )
            await client.start()
            await client.initialize()
            await client.read_account(schema.GetAccountParams(refresh_token=True))
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()
            with contextlib.suppress(Exception):
                await process.stop()

    # -- revocation --------------------------------------------------------

    async def revoke(self, account: str) -> int:
        """Invalidate every envelope in circulation for *account*.

        Bumping the generation is the revocation: a runner presenting the old one
        is refused, and the edge's fan-out (which the caller performs) tells every
        manager to drop its cache and stop the account's app-servers. Deliberately
        allowed for a disabled account — revoke-then-disable is the safe ordering,
        so refusing on ``enabled == False`` would make the safe ordering impossible.
        """
        self._authorized(account, require_enabled=False)
        await self.login_cancel(account)
        self._cache.pop(account, None)
        pending = self._refreshes.pop(account, None)
        await cancel_and_reap(
            pending, phase=f"codex-auth-refresh:{account}", timeout=5.0
        )
        lock = await self._lock_for(account)
        async with lock:
            generation = self._store.mark_revoked(account)
        log_event(
            logger,
            logging.WARNING,
            "codex.auth.revoked",
            account=account,
            generation=generation,
        )
        return generation

    async def forget(self, account: str) -> None:
        """Revoke and then destroy every trace of *account*'s credentials."""
        self._authorized(account, require_enabled=False)
        if not self._store.revoked(account):
            await self.revoke(account)
        self._logins.pop(account, None)
        lock = await self._lock_for(account)
        async with lock:
            self._cache.pop(account, None)
            self._store.forget(account)
        log_event(logger, logging.WARNING, "codex.auth.forgotten", account=account)

    async def aclose(self) -> None:
        for account in list(self._logins):
            with contextlib.suppress(AuthorityError):
                await self.login_cancel(account)
        for account, task in list(self._refreshes.items()):
            await cancel_and_reap(
                task, phase=f"codex-auth-refresh:{account}", timeout=5.0
            )
        self._refreshes.clear()
        self._cache.clear()
