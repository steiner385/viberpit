"""ReauthManager — one-at-a-time account re-authentication state machine.

States: idle | running | cancelling | success | error | timeout

Usage (production):
    manager = ReauthManager()          # uses default production runner
    state = await manager.start("primary", "me@example.com")
    # poll:
    state = manager.status("primary")
    # cancel:
    state = await manager.cancel("primary")
    # startup sweep (call once):
    manager.sweep_stale()
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from server.aio import cancel_and_reap

log = logging.getLogger(__name__)

REAUTH_DISPLAY_KEY = "__reauth__"   # reserved pseudo-session key for the reauth virtual display


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReauthBusy(Exception):
    """Another reauth is already running."""

    def __init__(self, holding_identity: str | None, started_at: float | None):
        self.holding_identity = holding_identity
        self.started_at = started_at
        super().__init__(f"reauth busy (identity={holding_identity!r})")


class ReauthNoKey(Exception):
    """No encryption key is configured — refuse to store plaintext credentials."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class ReauthState:
    state: str = "idle"          # idle|running|cancelling|success|error|timeout|awaiting_code
    message: str | None = None
    identity: str | None = None
    account: str | None = None   # the account/slot this operation targets (scopes status())
    started_at: float | None = None
    # internal only — not serialised
    _task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _launcher_pid: int | None = field(default=None, repr=False, compare=False)
    _temp_profile: str | None = field(default=None, repr=False, compare=False)
    vnc_session: str | None = None   # reserved key while a virtual display is live; None on :0 fallback
    auth_url: str | None = None

    def to_dict(self) -> dict:
        """Return a camelCase dict safe for JSON serialisation."""
        return {
            "state": self.state,
            "message": self.message,
            "identity": self.identity,
            "account": self.account,
            "startedAt": self.started_at,
            "vncSession": self.vnc_session,
            "authUrl": self.auth_url,
        }

    @staticmethod
    def from_dict(d: dict) -> "ReauthState":
        return ReauthState(
            state=d.get("state", "idle"),
            message=d.get("message"),
            identity=d.get("identity"),
            account=d.get("account"),
            started_at=d.get("startedAt"),
            vnc_session=d.get("vncSession"),
            auth_url=d.get("authUrl"),
        )


# ---------------------------------------------------------------------------
# Production default runner (defined as a manager method so the runner
# closure can record/clear _state._temp_profile for _kill_launcher)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ReauthManager:
    """One-at-a-time reauth state machine with injectable runner and timeout."""

    def __init__(
        self,
        *,
        runner: Callable[[str], Awaitable[bool]] | None = None,
        key_available: Callable[[], bool] | None = None,
        timeout_s: float = 900.0,
        x11: "Callable[[], Any] | None" = None,
    ) -> None:
        if key_available is None:
            from ccswitch.crypto import key_available as _ka
            key_available = _ka

        self._key_available = key_available
        self._timeout_s = timeout_s
        self._x11 = x11
        self._state = ReauthState()
        self._lock = asyncio.Lock()
        self._setup_challenge: Any = None   # SetupTokenChallenge while awaiting_code
        # True once a broadened-scope exchange has been refused and re-issued at
        # the narrow scope. Latched per reauth run so the fallback can fire at
        # most once — two scopes, one retry, never a loop.
        self._setup_scope_fallback_used = False

        # Wire default production runner AFTER self._state is initialised so
        # the closure can write self._state._temp_profile.
        if runner is None:
            runner = self._make_default_runner()
        self._runner = runner

    def _make_default_runner(self) -> Callable[[str], Awaitable[bool]]:
        """Build the production runner as a closure over *self*.

        Records the temp-profile path on self._state immediately after
        prepare_reauth_profile() returns, so that _kill_launcher() can read
        <temp>/launcher.pid and SIGTERM the Chrome window even if cancel() or
        a timeout fires before the runner finishes.

        prepare_reauth_profile(chrome_profile, extension_ids=settings.reauth_extension_id_items)
          → env_extra {BROWSER: realpath(launcher), DISPLAY: reauth_display,
                       CCSWITCH_REAUTH_PROFILE: temp}
          → asyncio.to_thread(capture_login, name, env_extra=...)
          → finally: cleanup_reauth_profile(temp)
        """
        manager = self

        async def _production_runner(name: str) -> bool:
            from ccswitch import auth_login
            from server.config import get_settings

            settings = get_settings()
            chrome_profile = settings.chrome_profile
            reauth_display = settings.reauth_display

            def _progress(msg: str) -> None:
                """Publish a step label so the UI can say what the wait is for.

                Chrome cold-starting on a fresh virtual display takes several
                seconds, during which the viewer shows only "Reconnecting…" —
                which reads as a failure rather than as progress. Only overwrite
                while still running, so a terminal message is never clobbered."""
                if manager._state.state == "running":
                    manager._state.message = msg

            _progress("Preparing a temporary browser profile\u2026")

            # Locate the launcher script (shipped with the package)
            launcher = (
                Path(__file__).parent.parent / "scripts" / "ccswitch-reauth-browser"
            )
            launcher_real = os.path.realpath(str(launcher))

            temp = await asyncio.to_thread(
                auth_login.prepare_reauth_profile,
                chrome_profile,
                extension_ids=settings.reauth_extension_id_items,
                cookie_hosts=settings.reauth_cookie_host_items,
                copy_login_data=settings.reauth_copy_login_data,
            )
            # Record immediately so _kill_launcher can find <temp>/launcher.pid
            manager._state._temp_profile = temp
            x11 = manager._x11() if manager._x11 else None
            spec = None
            # The virtual display has to be started INSIDE the try: it is the step
            # that has actually been failing, and starting it before the profile's
            # try/finally left the temp profile on disk on every such failure (#1106).
            try:
                if x11 is not None and getattr(settings, "reauth_virtual_display", True):
                    _progress("Starting the virtual display\u2026")
                    spec = await x11.start_display(REAUTH_DISPLAY_KEY)
                env_extra = {
                    "BROWSER": launcher_real,
                    "CCSWITCH_REAUTH_PROFILE": temp,
                }
                if spec is not None:
                    env_extra["DISPLAY"] = f":{spec.lease.display}"
                    env_extra["XAUTHORITY"] = spec.xauthority
                    manager._state.vnc_session = REAUTH_DISPLAY_KEY
                    _progress("Opening the Claude login \u2014 the browser takes a few seconds to appear\u2026")
                else:
                    env_extra["DISPLAY"] = reauth_display
                    _progress("Opening Chrome on the host screen \u2014 sign in there\u2026")
                ok = await asyncio.to_thread(
                    auth_login.capture_login, name, env_extra=env_extra
                )
                if ok:
                    # Clear the stale needs-reauth status synchronously so the
                    # UI's post-success refetch shows the account as healthy.
                    from ccswitch import accounts

                    await asyncio.to_thread(accounts.mark_reauthenticated, name)
                return ok
            finally:
                manager._state._temp_profile = None
                auth_login.cleanup_reauth_profile(temp)
                if spec is not None and x11 is not None:
                    await x11.stop_display(REAUTH_DISPLAY_KEY)

        return _production_runner

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, name: str, identity: str, mode: str = "login") -> ReauthState:
        """Start a reauth for *name*.

        Args:
            name:     account name.
            identity: identity/email for display.
            mode:     "login" (default) uses the OAuth browser flow (Chrome runner);
                      "setup_token" uses the in-process PKCE OAuth exchange.

        Raises:
            ReauthNoKey:  no encryption key is configured (would store plaintext).
            ReauthBusy:   another reauth is already in progress.
        """
        if not self._key_available():
            raise ReauthNoKey("No encryption key configured — refusing to store plaintext credentials")

        async with self._lock:
            if self._state.state in ("running", "cancelling"):
                raise ReauthBusy(self._state.identity, self._state.started_at)

            # Reclaim anything a previously killed run left behind before adding to
            # the pile. Boot-time sweeping alone let copies accumulate for the whole
            # uptime of a long-lived daemon (#1106); nothing is in flight here (the
            # busy check above just passed), and the TTL spares a fresh sibling.
            try:
                self.sweep_stale()
            except Exception:
                log.warning("reauth: pre-start sweep failed", exc_info=True)

            if mode == "setup_token":
                # In-process PKCE flow — no PTY/subprocess needed.
                # begin() is fast (pure crypto), safe to call under the lock.
                from ccswitch import setup_token_oauth
                challenge = setup_token_oauth.begin()
                self._setup_challenge = challenge
                self._setup_scope_fallback_used = False
                self._state = ReauthState(
                    state="awaiting_code",
                    identity=identity,
                    account=name,
                    started_at=time.time(),
                    auth_url=challenge.authorize_url,
                )
                # No background task — submit_code() does the async exchange.
            else:
                self._state = ReauthState(
                    state="running",
                    identity=identity,
                    account=name,
                    started_at=time.time(),
                )
                self._mode = mode
                task = asyncio.create_task(self._run(name))
                self._state._task = task

        return ReauthState(
            state=self._state.state,
            message=self._state.message,
            identity=self._state.identity,
            account=self._state.account,
            started_at=self._state.started_at,
            auth_url=self._state.auth_url,
        )

    def status(self, name: str, *, scoped: bool = False) -> ReauthState:
        """Return a snapshot of the current state.

        The manager runs one reauth at a time over a single global ``_state``.
        With ``scoped=True`` the snapshot is returned only to the account that
        owns the current operation; any *other* account gets a fresh ``idle``
        state. This stops a terminal result (``success``/``error``/``timeout``)
        from one account leaking into a different account's first poll — which
        otherwise froze reauth on every other account (the frontend stops
        polling the instant it sees a terminal state). ``scoped=False`` (default)
        returns the raw global snapshot, which the nameless add-account flow
        relies on.
        """
        s = self._state
        if scoped and s.account is not None and s.account != name:
            return ReauthState()
        return ReauthState(
            state=s.state,
            message=s.message,
            identity=s.identity,
            account=s.account,
            started_at=s.started_at,
            vnc_session=s.vnc_session,
            auth_url=s.auth_url,
        )

    async def cancel(self, name: str) -> ReauthState:
        """Cancel a running reauth.

        Sets state to 'cancelling', cancels the asyncio task, and kills any
        tracked launcher PID.
        """
        async with self._lock:
            if self._state.state not in ("running", "cancelling", "awaiting_code"):
                return self.status(name)

            self._state.state = "cancelling"
            task = self._state._task

        # Kill the launcher Chrome window if we know its PID (login mode only)
        self._kill_launcher()

        if task is not None and not task.done():
            # Bounded reap; an outer cancellation still unwinds us (issue #1164).
            await cancel_and_reap(task, phase="reauth.runner", timeout=5.0)

        # Drive cleanup from the state machine rather than relying solely on the
        # runner's finally block: asyncio.to_thread cannot be interrupted, so the
        # runner thread may still be alive when we reach this point, meaning its
        # finally has not yet executed.  Clearing _temp_profile here (under the
        # lock) makes the runner's later cleanup a safe no-op.
        temp = None
        async with self._lock:
            if self._state.state == "cancelling":
                temp = self._state._temp_profile
                self._state._temp_profile = None
                self._state.state = "idle"
                self._state.message = "cancelled"

        self._cleanup_temp_profile(temp)
        return self.status(name)

    def sweep_stale(self, max_age_s: float = 600.0) -> None:
        """Remove reauth-tmp entries older than *max_age_s*.

        Covers both shapes that land there — the ``reauth-*`` profile copies and the
        ``capture-*`` CLAUDE_CONFIG_DIRs — since it simply ages out every child.

        Called at startup AND at the top of each ``start()``: a run killed with its
        process (SIGKILL, host reboot, a dying X display) never reaches any
        ``finally``, and a boot-only sweep leaves those copies on disk for as long
        as the daemon happens to stay up (#1106).

        The profile of a run that is still in flight is skipped, so a login that
        outlives the TTL cannot have the directory pulled out from under it.
        """
        from ccswitch.paths import reauth_tmp_dir

        tmp = reauth_tmp_dir()
        if not tmp.exists():
            return

        active = self._state._temp_profile
        active_resolved = None
        if active:
            try:
                active_resolved = Path(active).resolve()
            except OSError:
                active_resolved = None

        now = time.time()
        for child in tmp.iterdir():
            # Symlinks are never legitimate reauth-tmp dirs — remove without
            # following, so we can't be tricked into stat-ing or rmtree-ing
            # arbitrary filesystem paths via a dangling or crafted symlink.
            # Checked before the in-flight comparison so a symlink can never be
            # spared by pointing at the active profile.
            if child.is_symlink():
                child.unlink(missing_ok=True)
                log.info("sweep_stale: removed symlink %s", child)
                continue
            if active_resolved is not None:
                try:
                    if child.resolve() == active_resolved:
                        continue
                except OSError:
                    pass
            try:
                age = now - os.stat(child).st_mtime
                if age > max_age_s:
                    import shutil
                    shutil.rmtree(child, ignore_errors=True)
                    log.info("sweep_stale: removed %s (age=%.0fs)", child, age)
            except OSError:
                pass

    async def shutdown(self) -> None:
        """Graceful shutdown — cancel any running reauth and wait."""
        await self.cancel("*")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, name: str) -> None:
        """Execute the login runner under a timeout and update state on completion."""
        try:
            result = await asyncio.wait_for(self._runner(name), timeout=self._timeout_s)
            async with self._lock:
                if self._state.state in ("running", "awaiting_code"):
                    self._state.state = "success" if result else "error"
                    self._state.message = None if result else "runner returned False"
            if result:
                try:
                    from server.db import db_session
                    from server.audit import record_audit
                    async with db_session() as db:
                        await record_audit(db, "account.reauth.success", self._state.identity or "system", {"account": name})
                except Exception:
                    log.warning("reauth.success_audit_failed account=%s", name, exc_info=True)
        except asyncio.TimeoutError:
            temp = None
            async with self._lock:
                if self._state.state in ("running", "awaiting_code"):
                    self._state.state = "timeout"
                    self._state.message = f"timed out after {self._timeout_s}s"
                    temp = self._state._temp_profile
                    self._state._temp_profile = None
            self._kill_launcher()
            self._cleanup_temp_profile(temp)
            log.warning("reauth timed out for %r after %.0fs", name, self._timeout_s)
        except asyncio.CancelledError:
            # cancel() handles both the state transition and the temp-profile cleanup.
            raise
        except Exception as exc:
            temp = None
            async with self._lock:
                if self._state.state in ("running", "awaiting_code"):
                    self._state.state = "error"
                    self._state.message = str(exc)
                    temp = self._state._temp_profile
                    self._state._temp_profile = None
            self._cleanup_temp_profile(temp)
            log.exception("reauth error for %r: %s", name, exc)

    async def submit_code(self, name: str, code: str) -> ReauthState:
        """Exchange the pasted OAuth code for a long-lived setup-token and store it.

        If the manager is not in the ``awaiting_code`` state this is a no-op
        (returns the current state snapshot without raising).
        """
        async with self._lock:
            if self._state.state != "awaiting_code":
                return self.status(name)   # nothing waiting; no-op snapshot
            challenge = self._setup_challenge
            # M-A: claim the slot before releasing the lock so a concurrent
            # duplicate submit_code (e.g. mobile double-tap) sees state !=
            # "awaiting_code" and returns the in-progress snapshot instead of
            # firing a second exchange.
            self._state.state = "running"
            self._state.message = None

        from ccswitch import accounts, crypto, setup_token_oauth

        try:
            token = await asyncio.to_thread(setup_token_oauth.exchange, code, challenge)
            await asyncio.to_thread(crypto.store_oauth_token, name, token)
            await asyncio.to_thread(accounts.mark_reauthenticated, name)
            scope_note = await asyncio.to_thread(
                self._usage_scope_note, name, token, getattr(challenge, "scope", None))
            async with self._lock:
                self._state.state = "success"
                self._state.message = scope_note
        except setup_token_oauth.SetupTokenStateMismatch:
            await self._fail(name, "Code didn't match this request — restart generation.")
        except setup_token_oauth.SetupTokenInvalidCode as exc:
            if await self._retry_at_fallback_scope(exc, challenge):
                return self.status(name)
            await self._fail(name, "Code invalid or expired — generate a new one.")
        except setup_token_oauth.SetupTokenTransient:
            await self._fail(name, "Couldn't reach the token service — try again.")
        except setup_token_oauth.SetupTokenError as exc:
            log.warning("setup-token exchange failed for %r: %s", name, exc)
            await self._fail(name, "Token service returned no token.")
        except Exception as exc:
            # M-C: catch unexpected failures (e.g. crypto/accounts errors) so
            # the UI never hangs in awaiting_code/running.  Never log the token
            # or the pasted code.
            log.warning("setup-token unexpected error for %r: %s", name, exc)
            await self._fail(name, "Token generation failed — try again.")

        return self.status(name)

    @staticmethod
    def _usage_scope_note(name: str, token: str, requested_scope: str | None) -> str | None:
        """Confirm the freshly minted token can read usage; a message if it cannot.

        This is the one scope failure the exchange path cannot see: the authorize
        page grants a code but silently narrows it, so ``exchange()`` succeeds and
        the token launches sessions — yet the usage endpoint will refuse it and
        the account shows no quota. Confirming against the real API at mint time
        (``oauth.has_usage_scope``) surfaces that immediately, on the reauth
        panel, instead of as a blank badge minutes later. The log line names the
        scope that was REQUESTED so the next attempt is diagnosable.

        Advisory only: the token is already stored and the run is a success
        either way; an oracle that cannot answer says nothing. NEVER logs the
        token.
        """
        from ccswitch import oauth

        try:
            if oauth.has_usage_scope(token):
                return None
        except Exception as exc:  # noqa: BLE001 — advisory probe, never fails the reauth
            log.debug("setup-token scope probe for %r could not answer: %s", name, exc)
            return None
        log.warning(
            "setup-token for %r was minted WITHOUT user:profile although %r was requested — "
            "sessions will launch, but quota will report UNSUPPORTED until re-authenticated",
            name, requested_scope)
        return ("Signed in, but this token lacks the user:profile scope, so the account "
                "will not report usage. Re-authenticate to fix that.")

    async def _retry_at_fallback_scope(self, exc, challenge) -> bool:
        """Re-issue the challenge at the narrow scope after a SCOPE refusal.

        Returns True when a fresh ``awaiting_code`` challenge was published (the
        caller must not also mark the run failed), False to fall through to the
        normal error.

        A code is bound to the scope it was consented to, so recovery cannot be a
        retry of ``exchange()`` — it has to restart at ``begin()`` and cost one
        more paste. That is the price of never leaving the fleet's last-resort
        recovery path worse off than before the scope was broadened. Fires only
        on a genuine ``invalid_scope`` (not an expired code), only when the
        broadened scope was actually in play, and at most once per run.
        """
        from ccswitch import setup_token_oauth

        if self._setup_scope_fallback_used or not exc.is_scope_rejection:
            return False
        if getattr(challenge, "scope", None) == setup_token_oauth.FALLBACK_SCOPE:
            return False  # already narrow — nothing left to fall back to

        retry = setup_token_oauth.begin(setup_token_oauth.FALLBACK_SCOPE)
        log.warning(
            "setup-token: scope %r refused; re-issued at %r (quota reporting will be "
            "unavailable for this credential)",
            getattr(challenge, "scope", None), setup_token_oauth.FALLBACK_SCOPE)
        async with self._lock:
            self._setup_challenge = retry
            self._setup_scope_fallback_used = True
            self._state.state = "awaiting_code"
            self._state.auth_url = retry.authorize_url
            self._state.message = (
                "Expanded permissions were refused — open the new link and paste a "
                "fresh code. Sign-in will work, but this account won't report usage."
            )
        return True

    async def _fail(self, name: str, msg: str) -> None:
        """Set error state with a user-facing message; log at WARNING (no token/code)."""
        log.warning("reauth setup-token error for %r: %s", name, msg)
        async with self._lock:
            self._state.state = "error"
            self._state.message = msg

    def _cleanup_temp_profile(self, temp: str | None) -> None:
        """Remove a temp reauth profile directory.

        Idempotent: safe to call even if the runner's finally already cleaned it
        up (shutil.rmtree with ignore_errors, and _under_reauth_tmp's strict=True
        returns False for a missing path rather than raising).
        """
        if temp is None:
            return
        try:
            from ccswitch import auth_login
            auth_login.cleanup_reauth_profile(temp)
            log.info("reauth: removed temp profile %s", temp)
        except Exception:
            log.warning("reauth: failed to remove temp profile %s", temp, exc_info=True)

    def _kill_launcher(self) -> None:
        """Signal any tracked Chrome launcher process to terminate.

        _state._launcher_pid — set by the Chrome login path if needed.
            Kills the whole process group to catch sub-processes it spawned.

        _state._temp_profile/launcher.pid — used for the Chrome reauth browser.
            A single SIGTERM to the launcher script PID is sufficient here.
        """
        pid = self._state._launcher_pid
        if pid is not None:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                log.info("sent SIGTERM to process group of launcher PID %d", pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self._state._launcher_pid = None

        # Also try reading launcher.pid from the temp profile dir (Chrome reauth path)
        temp = self._state._temp_profile
        if temp:
            pid_file = Path(temp) / "launcher.pid"
            try:
                pid_str = pid_file.read_text().strip()
                pid = int(pid_str)
                os.kill(pid, signal.SIGTERM)
                log.info("sent SIGTERM to launcher PID %d (from pid file)", pid)
            except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
                pass
