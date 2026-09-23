"""Health of THIS host's ``gh`` access — one condition, not N failures.

Why this module exists (issue #2052)
------------------------------------
dobby autologins through gdm, and gdm cannot unlock the GNOME login keyring:
every boot logs ``gkr-pam: couldn't unlock the login keyring``. ``gh``'s token
lives only in that keyring (``~/.config/gh/hosts.yml`` carries no
``oauth_token``), and :func:`gitrepos._run_gh` deliberately strips
``GITHUB_TOKEN``/``GH_TOKEN`` so a stray bot login cannot hijack the identity.
With no dedicated gh config dir set (``VIBERPIT_GH_CONFIG_DIR``) there is
therefore no file-backed fallback, and from boot until someone types the
passphrase at the console **every ambient gh call on the host runs
unauthenticated**:

* writes  → ``gh: Requires authentication (HTTP 401)``
* reads   → ``gh: API rate limit exceeded for <public IP>`` — the 60/hr
  **unauthenticated** bucket, which the failing loops then burn through.

A third shape belongs to the same condition on a *different* kind of host: one
with no gh host entry at all (a freshly provisioned runner before ``gh auth
login``) refuses with ``To get started with GitHub CLI…`` before it ever makes a
request, so it never produces either of the above.

Before this module, ViberPit discovered that only as one WARNING per attempted
target, from each of ~7 gh-backed loops (pr_poller, pr_status, merge_queue,
deploy_monitor, backlog_runner, error triage, the CI/CD Doctor). Two costs:
the operator sees N unrelated-looking per-issue failures instead of one host
problem, and each futile retry spends a request from the very bucket that is
already exhausted.

The design in one paragraph
---------------------------
:func:`classify_gh_failure` names the condition from stderr.
:class:`GhCredentialHealth` latches it, reports it **once**, and then admits
only one *probe* call per :data:`PROBE_INTERVAL_S` until a call succeeds —
so a locked keyring costs ~60 requests/hour of probing instead of every loop's
full tick rate, and recovery is automatic within one interval of the unlock.

Three deliberate asymmetries, each load-bearing:

* **Only ``token is None`` calls are evidence.** A call carrying a GitHub App
  installation token sets ``GH_TOKEN`` explicitly and never reads the keyring
  (issue #913), so its 401 says nothing about the host credential and its
  traffic must never be held back by one. ``_run_gh`` enforces this.
* **Only a success clears the latch.** A 404 while latched proves nothing;
  clearing on it would resume the storm. (A public-endpoint read *can* succeed
  while unauthenticated and clear the latch early — that degrades to probing
  sooner, i.e. toward the old behaviour, never past it.)
* **The 403s are not interchangeable.** ``exceeded for user ID 1234567``
  means the token worked and the host is merely busy; ``exceeded for
  203.0.113.7`` means there was no token at all. Latching on the former would
  back the whole fleet off during an ordinary busy hour.

Two limits, stated rather than discovered later:

* **A private-repo READ while unauthenticated answers 404**, not 401 — gh cannot
  distinguish "you may not see this" from "this does not exist". 404 is far too
  ambiguous to latch on (a genuinely deleted issue looks identical), so detection
  comes from a write's 401 or from the exhausted-bucket 403. In the real incident
  both arrive within seconds of boot, but a host doing *only* private reads under
  the rate limit would go undetected.
* **A foreground, user-initiated call can be suppressed** for up to one interval
  after the credential is fixed. It would have failed anyway while the condition
  held; the cost is only that a retry within a minute of the keyring unlock still
  fails fast. :data:`SUPPRESSED_STDERR` names the runbook so that failure explains
  itself, which is more than the raw 401 did.

Two more host conditions, same shape (issues #1922–#1927, #1941)
---------------------------------------------------------------
A missing credential is not the only gh failure that belongs to the *host* rather
than to whatever repo happened to notice it. Two others produced the same fan-out
of auto-error issues, so :func:`classify_gh_failure` names them too:

* :data:`UNREACHABLE` — ``error connecting to api.github.com``. One network
  outage on 2026-09-17 was filed as five issues (#1922, #1923, #1924, #1926,
  #1927) because three loops each reported the target they failed to reach.
* :data:`RATE_LIMITED` — an *authenticated* token over its budget
  (``exceeded for user ID …``, or a secondary limit). Deliberately distinct from
  the unauthenticated-IP 403 above, which means there was no token at all: the
  discriminator is the whole reason :data:`_RE_UNAUTH_RATE_LIMIT` carries a
  negative lookahead, and the two have different fixes.

**Only :data:`UNAUTHENTICATED` latches** (see :meth:`GhCredentialHealth.record`).
The back-off exists to stop burning an already-exhausted request bucket that no
amount of retrying can refill; neither of the new conditions has that property. A
connect error spends no budget and clears the moment the network returns, so
holding traffic down for a probe interval would only delay recovery, and a
rate-limit window resets on its own schedule. They are classified so the *log*
can collapse, not so traffic can be gated.

This module is intentionally dependency-free (stdlib only) so it can sit in the
CLI core beside ``gitrepos`` and be imported from the server without a cycle.

Recovery for an operator is a host-provisioning step, not a code change; see
``docs/runbooks/gh-credential-without-a-keyring.md``.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: This host has no usable ambient gh credential. A str rather than an enum so
#: callers (including the server-side log collapsing) can use it as a throttle key.
UNAUTHENTICATED = "unauthenticated"

#: This host cannot reach api.github.com at all (DNS, routing, a GitHub outage).
UNREACHABLE = "unreachable"

#: This host's gh calls are authenticated but over GitHub's budget. Separate from
#: :data:`UNAUTHENTICATED` on purpose: that one is fixed by unlocking a keyring,
#: this one by waiting, and filing them as one issue would hide both fixes.
RATE_LIMITED = "rate_limited"

#: The closed vocabulary :func:`classify_gh_failure` can return. Exported so a
#: consumer can assert it handles every condition rather than discovering a new
#: one in production — ``server/github/host_failures.py`` has exactly that guard.
CONDITIONS = (UNAUTHENTICATED, UNREACHABLE, RATE_LIMITED)

#: stderr handed back for a call that was never made. It deliberately reads as a
#: gh failure (callers all branch on ``returncode``) *and* classifies as the same
#: condition, so a suppressed call collapses into the one signal instead of
#: minting a second one downstream.
SUPPRESSED_STDERR = (
    "gh: not attempted — this host's gh credential is unauthenticated "
    "(Requires authentication (HTTP 401)); backing off. "
    "See docs/runbooks/gh-credential-without-a-keyring.md"
)

#: Seconds between probes while the condition holds.
#:
#: Picked against the bucket it is protecting, not by feel: unauthenticated
#: GitHub reads get 60 requests/hour per IP, so one probe per 60 s is the
#: fastest cadence that cannot *by itself* exhaust the budget the condition has
#: already strained. It also bounds recovery: once the keyring is unlocked,
#: normal service resumes within one interval with no operator action.
#:
#: Lower and the probes become the storm they replace; higher and a routine
#: unlock leaves automation idle for minutes after it should have recovered.
PROBE_INTERVAL_S = 60.0

#: How often a *standing* condition re-reports, seconds.
#:
#: Windowed rather than one-shot, and for the reason ``server/obs/logdedupe.py``
#: sets out at length: a latch that reports once per process loses the heartbeat,
#: so a day-long outage in a daemon that booted yesterday reads as "nothing is
#: wrong" — and "is this still happening?" is the first question an operator
#: asks. Six hours is that module's measured window (four records a day), reused
#: here for consistency of what the journal looks like.
#:
#: It is reimplemented in eight lines rather than imported: this module sits in
#: the CLI core, which must not depend on ``server.*``. If a *third* place ever
#: needs windowed reporting outside the server, promote ``LogThrottle`` down to
#: the core instead of copying it again.
REPORT_INTERVAL_S = 21600.0

#: ``gh: Requires authentication (HTTP 401)``. Anchored on gh's own wording; note
#: ``Bad credentials (HTTP 401)`` is excluded on purpose — that is a *present but
#: rejected* token, a different defect with a different fix.
_RE_401 = re.compile(r"requires authentication \(http 401\)", re.IGNORECASE)

#: ``API rate limit exceeded for <ip>`` but NOT ``... for user ID <n>``. The
#: lookahead is the entire discriminator between "no credential" and "busy".
#: ``already`` is optional: gh emits it when short-circuiting client-side
#: against its own cached rate-limit state instead of round-tripping to a
#: server 403 — same condition, one word different (#2169).
_RE_UNAUTH_RATE_LIMIT = re.compile(
    r"api rate limit (?:already )?exceeded for (?!user\b)", re.IGNORECASE
)

#: ``gh: To get started with GitHub CLI, please run:  gh auth login`` — gh holds no
#: host entry for github.com, so it refuses *before* making a request and therefore
#: never emits the 401 above. That is the state a freshly provisioned runner is in
#: until someone runs ``gh auth login`` into its config dir, and without this shape
#: such a host backs off from nothing: the loops retry at full tick rate with the
#: condition unreported. Zero false-positive risk — the message cannot mean
#: anything other than "this host has no ambient credential".
_RE_NOT_LOGGED_IN = re.compile(
    r"to get started with github cli|populate the gh_token environment variable",
    re.IGNORECASE,
)

_PATTERNS = (_RE_401, _RE_UNAUTH_RATE_LIMIT, _RE_NOT_LOGGED_IN)

#: gh's own transport-failure wording, both halves of the two-line message it
#: prints when the request never reached GitHub::
#:
#:     error connecting to api.github.com
#:     check your internet connection or https://githubstatus.com
#:
#: Either half alone is enough — matching both independently means a reworded
#: first line (a different host, a wrapped dial error) still classifies. Anchored
#: on observed output rather than on guessed Go/net phrasings, per constitution
#: Principle III.
_RE_UNREACHABLE = re.compile(
    r"error connecting to |check your internet connection", re.IGNORECASE
)

#: ``API rate limit exceeded for user ID 1234567`` — the mirror of
#: :data:`_RE_UNAUTH_RATE_LIMIT`. The token worked; the host is merely busy.
#: ``already`` optional for the same client-side-short-circuit reason (#2169).
_RE_USER_RATE_LIMIT = re.compile(
    r"api rate limit (?:already )?exceeded for user\b", re.IGNORECASE
)

#: GitHub's abuse/secondary limit, which has no "for <who>" clause at all.
_RE_SECONDARY_RATE_LIMIT = re.compile(r"exceeded a secondary rate limit", re.IGNORECASE)

_RATE_LIMITED_PATTERNS = (_RE_USER_RATE_LIMIT, _RE_SECONDARY_RATE_LIMIT)


def classify_gh_failure(stderr: str) -> str | None:
    """The host-wide condition *stderr* proves, or None. Called only for a
    non-zero gh exit.

    Order is load-bearing: the unauthenticated patterns run first because
    :data:`_RE_UNAUTH_RATE_LIMIT` and :data:`_RE_USER_RATE_LIMIT` read the same
    sentence and are told apart only by what follows ``for``.
    """
    if not stderr:
        return None
    if any(p.search(stderr) for p in _PATTERNS):
        return UNAUTHENTICATED
    if any(p.search(stderr) for p in _RATE_LIMITED_PATTERNS):
        return RATE_LIMITED
    if _RE_UNREACHABLE.search(stderr):
        return UNREACHABLE
    return None


def backoff_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether the back-off is armed (``VIBERPIT_GH_UNAUTH_BACKOFF``, default on).

    The kill switch disables *suppression only* — detection, the one-shot report
    and the snapshot stay live, because an operator debugging a false latch needs
    the signal more than anyone. The legacy-prefixed spelling works too, via the
    launch-time mirror in :mod:`env_compat`.
    """
    env = os.environ if environ is None else environ
    return (env.get("VIBERPIT_GH_UNAUTH_BACKOFF") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


@dataclass(frozen=True)
class GhCredentialSnapshot:
    """A consistent read of the latch, for logs and readouts."""

    #: True while the host credential is known-missing.
    unauthenticated: bool
    #: Classified failures observed since the latch set (0 when healthy).
    failures: int
    #: Calls not attempted since the latch set — the requests this saved.
    suppressed: int
    #: How long the condition has held, seconds; None when healthy.
    for_s: float | None


class GhCredentialHealth:
    """Process-wide latch for the ambient gh credential.

    Thread-safe: the daemon runs gh subprocesses from many
    ``asyncio.to_thread`` workers at once, so both the claim
    (:meth:`allow_attempt`) and the update (:meth:`record`) mutate under a lock.

    ``clock`` is monotonic and injected so tests state elapsed time rather than
    sleeping.
    """

    def __init__(
        self,
        *,
        probe_interval_s: float = PROBE_INTERVAL_S,
        report_interval_s: float = REPORT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe_interval_s = probe_interval_s
        self._report_interval_s = report_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._since: float | None = None
        self._last_attempt: float = 0.0
        self._last_report: float | None = None
        self._failures = 0
        self._suppressed = 0

    # -- reads ------------------------------------------------------------
    def snapshot(self) -> GhCredentialSnapshot:
        with self._lock:
            since = self._since
            return GhCredentialSnapshot(
                unauthenticated=since is not None,
                failures=self._failures,
                suppressed=self._suppressed,
                for_s=None if since is None else self._clock() - since,
            )

    # -- the gate ---------------------------------------------------------
    def allow_attempt(self) -> bool:
        """Whether this call may spend a GitHub request.

        True when healthy, or when the caller has just claimed the interval's
        single probe slot. Mutates: claiming the slot is the point, and doing it
        here (rather than in a separate "take a probe" call) is what keeps the
        whole gate one branch at the chokepoint.
        """
        with self._lock:
            if self._since is None:
                return True
            if not backoff_enabled():
                return True
            now = self._clock()
            if now - self._last_attempt >= self._probe_interval_s:
                self._last_attempt = now
                return True
            self._suppressed += 1
            return False

    def suppressed_result(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """A failed :class:`subprocess.CompletedProcess` for a call never made.

        Shaped exactly like a real gh failure — ``args`` includes the ``gh``
        argv[0] the caller would have seen — so none of the ~40 call sites needs
        to know suppression exists.
        """
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=1, stdout="", stderr=SUPPRESSED_STDERR
        )

    # -- updates ----------------------------------------------------------
    def record(self, returncode: int, stderr: str) -> None:
        """Fold one *attempted* ambient-credential gh result into the latch.

        Never call this for a suppressed result (it was not evidence) or for a
        call that carried an App installation token (it did not use this
        credential).

        :data:`UNREACHABLE` and :data:`RATE_LIMITED` fall through the same branch
        as an unrelated failure, and must: this latch gates traffic, and neither
        of those conditions is helped by holding traffic down (see the module
        docstring). They are classified for the log, not for the gate.
        """
        if returncode == 0:
            self._record_success()
            return
        if classify_gh_failure(stderr or "") != UNAUTHENTICATED:
            return  # another condition, or an unrelated failure: no latch change
        self._record_unauthenticated()

    def _record_unauthenticated(self) -> None:
        with self._lock:
            now = self._clock()
            if self._since is None:
                self._since = now
                self._failures = 0
                self._suppressed = 0
                self._last_report = None
            self._failures += 1
            # A failed probe restarts the window, so the next tick's flood is
            # suppressed rather than each loop getting its own retry.
            self._last_attempt = now
            report = (
                self._last_report is None
                or now - self._last_report >= self._report_interval_s
            )
            if report:
                self._last_report = now
            interval = self._probe_interval_s
        if report:
            log.warning(
                "github.host_gh_unauthenticated: this host's ambient gh credential is "
                "missing, so every gh call is unauthenticated (401 on writes, "
                "IP rate-limit 403 on reads). Holding gh traffic to one probe every "
                "%ds until it works. On dobby this is the GNOME login keyring left "
                "locked by autologin; see "
                "docs/runbooks/gh-credential-without-a-keyring.md",
                int(interval),
            )

    def _record_success(self) -> None:
        with self._lock:
            if self._since is None:
                return
            held_s = self._clock() - self._since
            suppressed = self._suppressed
            failures = self._failures
            self._since = None
            self._failures = 0
            self._suppressed = 0
            self._last_report = None
        # INFO, not WARNING: a recovery must not be captured by the error sink and
        # filed as its own issue.
        log.info(
            "github.host_gh_credential_restored after %ds (%d failed, %d suppressed)",
            int(held_s), failures, suppressed,
        )

    def reset(self) -> None:
        """Drop all state. For tests, and for a process that knows the host
        credential changed underneath it."""
        with self._lock:
            self._since = None
            self._last_attempt = 0.0
            self._last_report = None
            self._failures = 0
            self._suppressed = 0


_HOST_HEALTH = GhCredentialHealth()


def host_health() -> GhCredentialHealth:
    """The one latch for this process. A module-level singleton because the
    condition is a property of the *host*, not of any caller — every gh call in
    the process is evidence about the same credential."""
    return _HOST_HEALTH
