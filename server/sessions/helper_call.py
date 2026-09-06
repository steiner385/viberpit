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

Failure classification is delegated to :func:`server.sessions.failure.classify`
(the single source of truth for the ``api_429`` / ``api_401`` / ``overloaded`` /
``process_crash`` taxonomy) — this module adds no parallel regex.

State is process-global and in-memory: helper calls run only in the single
daemon event loop, so no cross-process sharing (unlike the usage-fetch cooldown
in ``ccswitch.ledger``, a different concern) and no lock is needed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from . import failure

log = logging.getLogger(__name__)


def _pick_enabled_account(provider: str) -> str | None:
    """Return the name of the first live, enabled account for *provider*.

    Shared selection rule behind every helper-provider picker below: enabled,
    not needs-reauth. Fail-open: returns None (including on any ledger lookup
    failure) so callers degrade safely rather than raising out of a
    best-effort helper call.
    """
    try:
        from ccswitch import accounts as _accts
        from ccswitch import ledger as _ledger
        with _ledger.locked_ledger() as led:
            for name, acct_provider in _accts.all_accounts():
                if acct_provider != provider:
                    continue
                if not _ledger.is_account_enabled(led, name):
                    continue
                if _ledger.get_status(led, name) == "needs-reauth":
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
    if name is None:
        return None
    try:
        from ccswitch import paths as _paths
        return str(_paths.account_cfg_dir(name))
    except Exception:
        return None


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

# Backoff budget: 3 retries at 1s/2s/4s. Mirrors ccswitch.config.retry_max_retries.
_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0

# Circuit-breaker tuning.
_FAIL_THRESHOLD = 5      # consecutive failures before the lane opens
_COOLDOWN_S = 60.0       # how long an open lane stays open before a half-open trial

# Failure kinds (from failure.classify) that are worth retrying: transient
# server/process conditions that a short wait can clear. Rate-limit and auth are
# deliberately excluded — see module docstring.
_TRANSIENT_KINDS = frozenset({"overloaded", "process_crash"})
# Failure kinds that open the breaker on the FIRST occurrence, without waiting
# for _FAIL_THRESHOLD: the account is throttled or de-authed, so every further
# call is certain to fail until it recovers.
_OPEN_IMMEDIATELY_KINDS = frozenset({"rate_limit", "auth_401"})


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
    - skipped: ``skipped is True`` — the breaker was open, so no call was made.
    """
    messages: list = field(default_factory=list)
    error: BaseException | None = None
    failure_kind: str | None = None
    api_error_status: int | None = None
    skipped: bool = False


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
    """Clear all breaker state (tests only)."""
    _breakers.clear()


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

    if not st.allow(_now()):
        log.debug("helper %s skipped: circuit open (account=%s)", subsystem, account_key)
        return HelperOutcome(skipped=True)

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
            kind = failure.classify(code, str(exc)).kind
            last_exc, last_kind, last_status = exc, kind, status
            if kind in _TRANSIENT_KINDS and attempt < retries:
                await _sleep(base_delay * (2 ** attempt))
                continue
            st.record_failure(kind, _now())
            return HelperOutcome(messages=messages, error=exc,
                                 failure_kind=kind, api_error_status=status)
        else:
            st.record_success()
            return HelperOutcome(messages=messages)

    # Only reached if retries < 0 (no attempt ran); degrade defensively.
    st.record_failure(last_kind or "other", _now())
    return HelperOutcome(error=last_exc, failure_kind=last_kind, api_error_status=last_status)
