"""Shared backoff + circuit-breaker wrapper for one-shot LLM *helper* calls.

Background helper calls — title generation, goal inference, PR-intent
classification, autopilot decisions, model assessment — are stateless one-shot
``claude_agent_sdk.query`` streams run under an account's ``CLAUDE_CONFIG_DIR``.
They are best-effort: every call site already degrades to a safe default on any
failure. The problem this module solves is *volume*: when an account is
rate-limited (429) or de-authed (401), every helper call against it fails, and
the schedulers keep re-firing them — production logged ~15,700 such failures in
four days (2026-08-09..13), burning quota that live sessions need and burying
genuine faults in noise.

Two mechanisms, both keyed per ``(subsystem, account_key)`` so one bad account
trips only its own lane while healthy accounts keep flowing:

1. **Backoff** — retry ONLY genuinely transient failures (5xx/overloaded,
   process crashes / "resource temporarily unavailable"): 1s, 2s, 4s, max 3
   retries. 429 rate-limit and 401 auth are NOT retried — retrying a throttled
   account just adds load to the exact thing we are trying to relieve.

2. **Circuit breaker** — after 5 consecutive failures the lane opens for a 60s
   cool-down; while open, calls are *skipped* (the site degrades without a
   network call). A rate-limit / auth failure opens the lane immediately (no
   need to wait for five). After the cool-down the lane goes half-open: one
   trial call is allowed; success closes it, failure re-opens it.

3. **Authorization block** — a 403 organization denial is neither per-lane nor
   transient, so it bars the *account* from every helper subsystem for 30
   minutes and the account pickers above fail over to the next live account
   (#1820: the first-listed account was org-denied, so every helper in the
   daemon failed on it for a week).

4. **Rate-limit block** — a 429 is account-wide the same way a 403 is: every
   subsystem calling that account will fail the same way, and the per-lane
   breaker's 60s cooldown is far shorter than a real rate limit (a 5-hour
   session window, or a 7-day weekly one). So a rate limit ALSO bars the
   account from every helper subsystem, for as long as the ledger's cached
   usage says the account's most-exhausted window needs to reset — falling
   back to the same 30-minute bound as an authorization block when no cached
   reset time is available (#2410: one weekly-exhausted account kept getting
   re-picked and re-failing every 60s until the weekly reset, three days
   away).

Failure classification is delegated to :func:`server.sessions.failure.classify`
(the single source of truth for the ``api_429`` / ``api_401`` / ``overloaded`` /
``process_crash`` taxonomy) — this module adds no parallel regex.

State is process-global and in-memory: helper calls run only in the single
daemon event loop, so no cross-process sharing (unlike the usage-fetch cooldown
in ``viberpit.ledger``, a different concern) and no lock is needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from viberpit import ledger as _ledger

from . import failure

log = logging.getLogger(__name__)


def helper_cwd() -> str:
    """Working directory for every stateless one-shot helper call.

    A dedicated scratch directory (``~/.cache/ccswitch/helper-cwd``, or
    ``VIBERPIT_HELPER_CWD``), created on demand — never the daemon's own cwd and never
    a session's worktree. Claude Code treats the working directory as a project: it
    loads that directory's ``CLAUDE.md`` into the call and files the call's transcript
    under ``~/.claude/projects/<cwd>``. Verified live 2026-09-11: the decider and
    composer ran in the daemon's checkout, so (1) that repo's "ALWAYS flip auto-merge
    on" completion workflow was in the context of every autopilot judgement for every
    governed repo, and (2) ~12,400 helper transcripts in 14 days sat in the checkout's
    projects entry, where a plain ``claude --resume`` listed autopilot's prompts as if
    they were the operator's own sessions. Sibling of :func:`pick_helper_config_dir`:
    that picks WHO the helper runs as, this picks WHERE.
    """
    env = os.environ.get("VIBERPIT_HELPER_CWD")
    path = Path(env) if env else Path.home() / ".cache" / "ccswitch" / "helper-cwd"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover — best-effort; the SDK reports a missing cwd
        log.warning("helper_cwd: cannot create %s: %s", path, exc)
    return str(path)


def _account_cfg_dir(name: str) -> str | None:
    """*name*'s config dir as a string, or None if it cannot be resolved.

    The one place this module maps an account name to a config dir, so the
    readiness check and the picker below key the authorization block on exactly
    the same string ``run_helper_call`` receives as ``account_key``.
    """
    try:
        from viberpit import paths as _paths
        return str(_paths.account_cfg_dir(name))
    except Exception:
        return None


def is_account_helper_ready(name: str) -> bool:
    """True if *name* is enabled, not needs-reauth/org-denied, and not
    authorization-blocked.

    The single source of truth for "would this account's own credentials answer
    a helper call, or just fail" — used both to filter candidates in
    :func:`_pick_enabled_account` and by a caller that already knows WHICH
    account it wants (its governed session's own account) and must verify it
    before composing that account's config dir directly. Composing
    unconditionally let a disabled/de-authed account spawn a CLI call certain to
    answer "Not logged in", which the SDK surfaces as the opaque "Claude Code
    returned an error result: success" (#1817). Fails CLOSED (False) on any
    ledger error — an unverifiable account is never assumed ready; callers fall
    back to :func:`pick_helper_config_dir`, which degrades safely on its own.

    The ledger could not see an organization-level denial at all until a LIVE
    session hit one and flipped the account's status to ``org-denied``
    (orchestrator._mark_org_denied, #2244) — so this still checks
    :func:`is_helper_account_blocked` first (what the calls themselves learned,
    time-boxed) rather than relying on the ledger flip alone: a helper-only
    account that never runs a live session would otherwise never get one.
    """
    cfg_dir = _account_cfg_dir(name)
    if cfg_dir is None or is_helper_account_blocked(cfg_dir):
        return False
    try:
        with _ledger.locked_ledger() as led:
            return (_ledger.is_account_enabled(led, name)
                    and _ledger.get_status(led, name) not in ("needs-reauth", "org-denied"))
    except Exception:
        return False


def _pick_enabled_account(provider: str) -> str | None:
    """Return the name of the first live, enabled account for *provider*.

    Shared selection rule behind every helper-provider picker below: enabled,
    not needs-reauth (see :func:`is_account_helper_ready`). Fail-open: returns
    None (including on any ledger lookup failure) so callers degrade safely
    rather than raising out of a best-effort helper call.
    """
    try:
        from viberpit import accounts as _accts
        for name, acct_provider in _accts.all_accounts():
            if acct_provider != provider:
                continue
            if not is_account_helper_ready(name):
                continue
            return name
    except Exception:
        pass
    return None


def pick_helper_config_dir() -> str | None:
    """Return the config_dir of the first live, enabled Anthropic account.

    Used by stateless LLM helper calls (assess_model, refine_prompt, …) that
    run in a server process with no per-account CLAUDE_CONFIG_DIR set.  Picks
    the cheapest usable credential: enabled, not needs-reauth.  Fail-open:
    returns None when no account is available so callers degrade safely.
    """
    name = _pick_enabled_account("anthropic")
    return None if name is None else _account_cfg_dir(name)


def pick_helper_openai_account() -> str | None:
    """Return the name of the first live, enabled OpenAI (Codex) account.

    The Codex sibling of :func:`pick_helper_config_dir`. Unlike Claude, Codex
    has no host-local config-dir concept a caller can just hand to a fresh SDK
    call (its account state lives behind the daemon-owned
    ``CodexRunnerManager``, see ``server/codex/manager.py``), so this returns
    the bare account name for a caller to route through that manager instead
    of a directory path. Fail-open, same contract as the Anthropic picker.
    """
    return _pick_enabled_account("openai")

# Backoff budget: 3 retries at 1s/2s/4s. Mirrors viberpit.config.retry_max_retries.
_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0

# Circuit-breaker tuning.
_FAIL_THRESHOLD = 5      # consecutive failures before the lane opens
_COOLDOWN_S = 60.0       # how long an open lane stays open before a half-open trial

# Failure kinds (from failure.classify) that are worth retrying: transient
# server/process conditions that a short wait can clear. Rate-limit and auth are
# deliberately excluded — see module docstring.
_TRANSIENT_KINDS = frozenset({"overloaded", "process_crash", "network"})
# Failure kinds that open the breaker on the FIRST occurrence, without waiting
# for _FAIL_THRESHOLD: the account is throttled, de-authed or org-denied, so
# every further call is certain to fail until it recovers.
_OPEN_IMMEDIATELY_KINDS = frozenset({"rate_limit", "auth_401", "auth_forbidden"})

# How long an account that answered with a durable authorization denial (403
# org block — see failure._FORBIDDEN_MARKERS) is kept out of helper duty. A
# breaker lane is per (subsystem, account) and 60s wide, which is right for a
# blip but wrong here twice over: the denial is account-WIDE (every other
# subsystem would have to re-learn it by failing), and it is durable (only a
# change on the account's side clears it, never a minute's wait). In-memory and
# TTL'd rather than written to the ledger: a 403 we misread must expire on its
# own, and a real one re-arms on the next call.
_AUTH_BLOCK_S = 1800.0
# Fallback for a rate-limit block when no cached reset time can be resolved
# (see _reset_deadline_s) — same bound as an authorization block.
_RATE_LIMIT_BLOCK_S = 1800.0
# How stale a cached usage reading may be and still answer "when does this
# account's rate limit clear" — generous on purpose: resets_at is a timestamp,
# not a measurement that goes bad with age, and the weekly window is 7 days
# wide, so a week-old cache entry is still the best information available.
_RESET_LOOKUP_MAX_AGE_S = 8 * 24 * 3600.0

_blocked_until: dict[str, float] = {}   # account config dir -> deadline (monotonic)


def is_helper_account_blocked(config_dir: str,
                              *, _now: Callable[[], float] = time.monotonic) -> bool:
    """True while *config_dir*'s account is barred from helper duty (403 org
    denial, or a rate limit the account has not yet recovered from)."""
    key = config_dir or ""
    until = _blocked_until.get(key)
    if until is None:
        return False
    if _now() >= until:
        _blocked_until.pop(key, None)
        return False
    return True


def _block_account(config_dir: str, now: float, block_s: float,
                   log_fmt: str, *log_args: object) -> None:
    """Bar *config_dir*'s account from helper duty for *block_s* seconds, logging
    *log_fmt* % *log_args* once per block rather than once per failed call."""
    key = config_dir or ""
    if not key:
        return  # no account to bar (a caller with no config dir picks nothing)
    first = _blocked_until.get(key, 0.0) <= now
    _blocked_until[key] = now + block_s
    if first:
        # %r so the volatile text lands inside quotes: the error-sink fingerprint
        # normalizes a quoted run to <str>, which keeps one condition to one
        # auto-error issue instead of one per distinct CLI sentence.
        log.warning(log_fmt, *log_args)


def _note_auth_blocked(config_dir: str, detail: str | None, now: float) -> None:
    """Bar *config_dir*'s account from helper duty and say why, once per block."""
    _block_account(config_dir, now, _AUTH_BLOCK_S,
                   "helper account %s refused authorization — barred from helper "
                   "calls for %.0fs: %r", config_dir, _AUTH_BLOCK_S, detail or "")


def _reset_deadline_s(config_dir: str) -> float | None:
    """Seconds until *config_dir*'s account's most-exhausted cached usage window
    resets, or None when unknown (no cache, or the window carries no reset time).

    The account NAME the ledger keys usage by is *config_dir*'s own last path
    component (:func:`_account_cfg_dir` composes it as ``cfg_root() / name`` —
    the one relationship this module maps in either direction), so no reverse
    lookup over the account list is needed. Reads the ledger's own cache rather
    than fetching — a helper call already failed with a rate limit, so a
    network round-trip here would just add load to the throttled account. The
    BINDING window (the most-used one, the codebase's single definition of
    "which allowance binds this account") is the one that is actually
    exhausted, so its reset time is the deadline: a session-only limit clears
    in hours, a weekly one in days, and this reads whichever one applies
    without needing to parse which kind the CLI hit.
    """
    name = Path(config_dir).name
    if not name:
        return None
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with _ledger.locked_ledger() as led:
            stale = _ledger.stale_usage(led, name, now, _RESET_LOOKUP_MAX_AGE_S)
        if stale is None:
            return None
        snapshot, _fetched = stale
        window = snapshot.binding_window()
        if window is None or window.resets_at is None:
            return None
        delta = (window.resets_at - now).total_seconds()
        return delta if delta > 0 else None
    except Exception:
        return None


def _note_rate_limited(config_dir: str, detail: str | None, now: float) -> None:
    """Bar *config_dir*'s account from helper duty until its usage resets (or a
    bounded fallback), and say why, once per block."""
    block_s = _reset_deadline_s(config_dir) or _RATE_LIMIT_BLOCK_S
    _block_account(config_dir, now, block_s,
                   "helper account %s is rate-limited — barred from helper calls "
                   "for %.0fs: %r", config_dir, block_s, detail or "")


@dataclass
class HelperOutcome:
    """Result of a wrapped helper call.

    Exactly one of these shapes holds:
    - success: ``error is None and not skipped`` — ``messages`` are the yielded
      SDK messages for the caller to parse.
    - failure: ``error is not None`` — the call was attempted (and retried for
      transient kinds) but ultimately failed; ``failure_kind`` /
      ``api_error_status`` describe it and ``messages`` holds any partial stream
      (a caller may salvage from it, as title generation does).
    - skipped: ``skipped is True`` — no call was made; ``skip_reason`` says
      whether the lane's breaker was open (``circuit-open``, a cooling-off after
      failures) or the account itself is barred (``account-blocked``, a 403 org
      denial or a rate limit not yet reset). A caller that reports the skip to a
      human should not call the second one transient.

    ``error_text`` is the CLI's own description of a failure when it gave one
    (see :func:`_error_detail`) — the exception alone is often opaque.
    """
    messages: list = field(default_factory=list)
    error: BaseException | None = None
    failure_kind: str | None = None
    api_error_status: int | None = None
    error_text: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    def describe(self) -> str:
        """One log-ready line for a failure: classified kind, exception, CLI detail.

        The single format every helper call site logs a failure with. The exception
        alone is often "Claude Code returned an error result: success" — the result
        subtype, not a cause (#1841) — so the kind and the CLI's own words ride
        along. The detail is ``%r``-quoted so the error sink normalizes it to
        ``<str>`` and one condition stays one auto-error fingerprint.
        """
        return (f"kind={self.failure_kind or 'unknown'} {self.error} "
                f"detail={self.error_text or ''!r}")

    @property
    def log_level(self) -> int:
        """The level a caller logs this failure at (see :func:`failure.log_level`):
        a host network outage or a throttle is not a defect, so it never files an
        auto-error issue (#2623)."""
        return failure.log_level(self.failure_kind)


@dataclass
class _BreakerState:
    consecutive: int = 0
    open_until: float | None = None

    def allow(self, now: float) -> bool:
        """True if a call may proceed (closed, or half-open trial past cooldown)."""
        if self.open_until is None:
            return True
        return now >= self.open_until

    def record_success(self) -> None:
        self.consecutive = 0
        self.open_until = None

    def record_failure(self, kind: str, now: float) -> None:
        self.consecutive += 1
        if kind in _OPEN_IMMEDIATELY_KINDS or self.consecutive >= _FAIL_THRESHOLD:
            self.open_until = now + _COOLDOWN_S


_breakers: dict[tuple[str, str], _BreakerState] = {}


def reset_breakers() -> None:
    """Clear all breaker and account-block state (tests only)."""
    _breakers.clear()
    _blocked_until.clear()


def _api_error_status(messages: list) -> int | None:
    """First ``ResultMessage.api_error_status`` in *messages*, or None.

    Duck-typed (no SDK import): only a ResultMessage carries the attribute. On an
    HTTP API error the SDK yields this result and *then* raises a RuntimeError
    ("Claude Code returned an error result: success") whose text has no status —
    so the status must be read off the collected messages, not the exception.
    """
    for m in messages:
        status = getattr(m, "api_error_status", None)
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):
                return None
    return None


def _error_detail(messages: list) -> str | None:
    """The CLI's own words for why a call failed, from the messages it yielded.

    When the CLI reports ``is_error`` with an empty ``errors`` array the SDK
    raises ``RuntimeError("Claude Code returned an error result: success")`` —
    "success" is the result *subtype* leaking into the text, so the exception
    describes nothing. What actually happened is in the stream the SDK already
    yielded: a ResultMessage's ``errors``/``result``, and an API-error
    AssistantMessage (``.error`` set, e.g. ``oauth_org_not_allowed``) whose text
    block holds the human sentence. Both feed the classifier and the caller's
    log; #1820 burned a week of daily ERROR records for want of them.

    Only error-shaped text is collected. A normal assistant reply — a partial
    answer before a crash — is NOT the CLI's error message, must never sway
    :func:`failure.classify`, and must never be echoed as one. Duck-typed
    throughout: this module imports no SDK types.
    """
    texts: list[str] = []

    def _add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())

    for m in messages:
        errors = getattr(m, "errors", None)
        if isinstance(errors, list):
            for e in errors:
                _add(str(e))
        if getattr(m, "is_error", False):
            _add(getattr(m, "result", None))
        err = getattr(m, "error", None)
        if err:                       # an API-error assistant message
            _add(str(err))
            content = getattr(m, "content", None)
            if isinstance(content, list):
                for block in content:
                    _add(getattr(block, "text", None))
    if not texts:
        return None
    return " | ".join(dict.fromkeys(texts))[:500]


async def run_helper_call(
    make_stream: Callable[[], AsyncIterator],
    *,
    subsystem: str,
    account_key: str,
    retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY_S,
    per_attempt_timeout: float | None = None,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _now: Callable[[], float] = time.monotonic,
) -> HelperOutcome:
    """Consume ``make_stream()`` into a message list with backoff + a breaker.

    ``make_stream`` must return a *fresh* async iterator each call (a lambda over
    ``query_fn(prompt=..., options=...)``) so a retry can restart the stream.
    ``account_key`` is the account's ``CLAUDE_CONFIG_DIR``; ``subsystem`` names
    the caller (e.g. ``"generate_title"``). Never raises — every failure mode is
    reported through the returned :class:`HelperOutcome`.
    """
    key = (subsystem, account_key or "")
    st = _breakers.setdefault(key, _BreakerState())

    if is_helper_account_blocked(account_key, _now=_now):
        log.debug("helper %s skipped: account authorization-blocked (account=%s)",
                  subsystem, account_key)
        return HelperOutcome(skipped=True, skip_reason="account-blocked")
    if not st.allow(_now()):
        log.debug("helper %s skipped: circuit open (account=%s)", subsystem, account_key)
        return HelperOutcome(skipped=True, skip_reason="circuit-open")

    last_exc: BaseException | None = None
    last_kind: str | None = None
    last_status: int | None = None

    for attempt in range(retries + 1):
        messages: list = []
        try:
            stream = make_stream()
            if per_attempt_timeout is not None:
                async with asyncio.timeout(per_attempt_timeout):
                    async for msg in stream:
                        messages.append(msg)
            else:
                async for msg in stream:
                    messages.append(msg)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # A wedged call must fail open immediately, never be retried into
            # minutes of blocking (mirrors assess_model's #567 guard).
            st.record_failure("other", _now())
            return HelperOutcome(messages=messages, error=exc, failure_kind="timeout")
        except Exception as exc:  # noqa: BLE001 — best-effort; classify then decide
            status = _api_error_status(messages)
            code = f"api_{status}" if status is not None else None
            detail = _error_detail(messages)
            # Classify on the exception AND the CLI's own error text: on its own
            # the exception is often "returned an error result: success", which
            # carries neither the status nor the reason.
            kind = failure.classify(code, " | ".join(filter(None, (str(exc), detail)))).kind
            last_exc, last_kind, last_status = exc, kind, status
            if kind in _TRANSIENT_KINDS and attempt < retries:
                await _sleep(base_delay * (2 ** attempt))
                continue
            now = _now()
            st.record_failure(kind, now)
            if kind == "auth_forbidden":
                _note_auth_blocked(account_key, detail, now)
            elif kind == "rate_limit":
                _note_rate_limited(account_key, detail, now)
            return HelperOutcome(messages=messages, error=exc, failure_kind=kind,
                                 api_error_status=status, error_text=detail)
        else:
            st.record_success()
            return HelperOutcome(messages=messages)

    # Only reached if retries < 0 (no attempt ran); degrade defensively.
    st.record_failure(last_kind or "other", _now())
    return HelperOutcome(error=last_exc, failure_kind=last_kind, api_error_status=last_status)
