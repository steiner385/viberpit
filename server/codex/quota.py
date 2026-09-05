"""Codex quota: `account/rateLimits/read` turned into typed windows and states.

**Why a Codex-shaped snapshot next to `ccswitch.quota.QuotaSnapshot`.** That
module stays THE provider-neutral quota DTO — the one the selector, the ledger,
and the accounts wire format speak — and this module does not replace or
duplicate it: it is the app-server-side read whose windows the accounts API maps
onto it. The vocabulary is deliberately the same where it fits: the state strings
`"unknown"` and `"auth_error"` are literally `ccswitch.quota.UNKNOWN` /
`AUTH_ERROR`, and `used_percent` / `resets_at` are the same two facts as
`used_pct` / `resets_at`. It diverges only where the wire forces it:

* percentages are `int` (the app-server reports `int32`, not a float),
* `resets_at` is epoch seconds (the app-server reports `int64` unix seconds;
  converting to an aware datetime here only to convert back at the wire adds a
  timezone bug for no reader), and
* `UNSUPPORTED` is a fourth state with no analogue for Anthropic or Copilot: an
  app-server build that does not implement `account/rateLimits/read` at all.

**Telling the four states apart is the point of the module.** "No quota data"
has four different operator responses — wait and retry, stop asking this build,
re-authenticate, or investigate — and collapsing them into one empty snapshot
sends every one of them to the wrong place. In particular `UNSUPPORTED` must not
read as a broken account, and `AUTH_ERROR` must not read as a transient blip that
a retry will fix.

**Never fabricate a window.** A missing bucket is absent, a `resetsAt` the server
did not send stays `None` — never `0` (renders as 1970) and never "now" (renders
as "resets immediately"). A failed read is expressed in `state`, never as a
0%-used window, which reads as plenty of headroom.

`individualLimit` (spend controls) is intentionally not mapped: it meters a
dollar budget, and filing it beside the rate-limit windows under one label would
make "94% used" ambiguous between "out of turns" and "out of money".
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final, Protocol

from server.codex import redaction, schema
from server.codex.protocol import (
    JSONRPC_METHOD_NOT_FOUND,
    CodexProtocolError,
    CodexRequestFailed,
)
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: How much longer than `cache_ttl_s` a last-good snapshot may still be served
#: once live reads start failing. Quota percentages move on the scale of hours,
#: so a reading a few minutes old still steers rotation and the UI correctly,
#: while an old one is arbitrarily wrong — a burst of turns can empty a window.
#: Past the ceiling the typed failure is the honest answer.
STALE_CEILING_MULTIPLIER: Final = 10

#: The app-server pins no dedicated authentication error code: `protocol.py`
#: carries only the three standard JSON-RPC codes, and a revoked token normally
#: surfaces as the `account/chatgptAuthTokens/refresh` server request
#: (`schema.REFRESH_REASON_UNAUTHORIZED`) rather than an error reply. So this set
#: is an ASSUMPTION — that a build which does answer with an error for a
#: signed-out account passes the upstream HTTP status through as the JSON-RPC
#: code. Every other code stays `UNKNOWN` on purpose: mislabelling a transient
#: failure as an auth error tells the operator to re-login for nothing.
AUTH_ERROR_CODES: Final[frozenset[int]] = frozenset({401, 403})

#: Clock skew tolerated before an already-elapsed reset time is read as evidence
#: that the upstream numbers are stale (see `snapshot_from_rate_limits`).
_RESET_SKEW_SLACK_S: Final = 60.0

_KIND_LABELS: Final[Mapping[str, str]] = {
    "primary": "Primary",
    "secondary": "Secondary",
}


class QuotaState(StrEnum):
    OK = "ok"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    AUTH_ERROR = "auth_error"


class RateLimitReader(Protocol):
    """The one app-server method this module needs (`CodexAppServerClient`)."""

    async def read_rate_limits(self) -> schema.GetAccountRateLimitsResponse: ...


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    """One metered allowance on one account."""

    id: str
    label: str
    used_percent: int
    resets_at: float | None = None
    window_minutes: int | None = None
    model: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "usedPercent": self.used_percent,
            "resetsAt": self.resets_at,
            "windowMinutes": self.window_minutes,
            "model": self.model,
        }


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """Every window one account reported, plus how much to trust them."""

    state: QuotaState
    windows: tuple[QuotaWindow, ...] = ()
    plan_type: str | None = None
    has_credits: bool | None = None
    credits_unlimited: bool | None = None
    rate_limit_reached: str | None = None
    age_s: float | None = None
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "windows": [window.to_wire() for window in self.windows],
            "planType": self.plan_type,
            "hasCredits": self.has_credits,
            "creditsUnlimited": self.credits_unlimited,
            "rateLimitReached": self.rate_limit_reached,
            "ageS": self.age_s,
            "detail": self.detail,
        }


def _windows_from(
    snapshot: schema.RateLimitSnapshot,
    *,
    key_prefix: str = "",
    label_base: str | None = None,
    model: str | None = None,
) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    for kind, window in (("primary", snapshot.primary), ("secondary", snapshot.secondary)):
        if window is None:
            continue
        windows.append(
            QuotaWindow(
                id=f"{key_prefix}{kind}",
                label=(
                    f"{label_base} ({kind})" if label_base else _KIND_LABELS[kind]
                ),
                used_percent=window.used_percent,
                resets_at=(
                    None if window.resets_at is None else float(window.resets_at)
                ),
                window_minutes=window.window_duration_mins,
                model=model,
            )
        )
    return windows


def snapshot_from_rate_limits(
    response: schema.GetAccountRateLimitsResponse, *, now: float
) -> QuotaSnapshot:
    """Map every bucket in a rate-limits response onto quota windows.

    Both the account-wide `rateLimits` and every entry of `rateLimitsByLimitId`
    are mapped; a model-scoped bucket keeps the limit id in both its window id
    and its `model`, so a caller can tell which allowance binds for the model it
    is about to run.

    `now` is the instant the response was observed, injected rather than read
    here so the mapper holds no clock. It is load-bearing: a bucket whose reset
    time has already passed describes a window the account has since rolled out
    of, so those percentages are stale by at least that much, and `age_s` says
    so instead of presenting them as current.
    """
    limits = response.rate_limits
    windows = _windows_from(limits, label_base=limits.limit_name)
    for limit_id, bucket in response.rate_limits_by_limit_id.items():
        # The id is the stable key (it must not change when the backend rewords a
        # label) and so it identifies both the window and the model it meters;
        # `limitName` is the backend's display string and is nullable, so it is
        # the label, falling back to the id when it is absent.
        windows.extend(
            _windows_from(
                bucket,
                key_prefix=f"{limit_id}:",
                label_base=bucket.limit_name or limit_id,
                model=limit_id,
            )
        )
    elapsed = [
        now - window.resets_at
        for window in windows
        if window.resets_at is not None and now - window.resets_at > _RESET_SKEW_SLACK_S
    ]
    credits = limits.credits
    return QuotaSnapshot(
        state=QuotaState.OK,
        windows=tuple(windows),
        plan_type=limits.plan_type,
        has_credits=None if credits is None else credits.has_credits,
        credits_unlimited=None if credits is None else credits.unlimited,
        rate_limit_reached=limits.rate_limit_reached_type,
        age_s=max(elapsed) if elapsed else 0.0,
    )


@dataclass(frozen=True, slots=True)
class _CachedRead:
    read_at: float
    snapshot: QuotaSnapshot


class CodexQuotaReader:
    """Reads quota per account, with a typed failure state and a last-good cache."""

    def __init__(
        self,
        *,
        cache_ttl_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._last_good: dict[str, _CachedRead] = {}

    async def read(
        self, account: str, *, client: RateLimitReader | None
    ) -> QuotaSnapshot:
        """Read live quota for *account*, degrading to the last good snapshot.

        Never raises: an account with no connection, an app-server that does not
        implement the method, and a revoked token are all answers, not errors.
        """
        if client is None:
            return self._degraded(
                account,
                QuotaState.UNKNOWN,
                "no codex app-server connection is available for this account",
            )
        try:
            response = await client.read_rate_limits()
        except CodexRequestFailed as exc:
            if exc.code == JSONRPC_METHOD_NOT_FOUND:
                state = QuotaState.UNSUPPORTED
            elif exc.code in AUTH_ERROR_CODES:
                state = QuotaState.AUTH_ERROR
            else:
                state = QuotaState.UNKNOWN
            return self._degraded(account, state, redaction.scrub_exception(exc))
        except (CodexProtocolError, schema.CodexSchemaError) as exc:
            # Timeouts and transport closes land here: transient by nature.
            return self._degraded(
                account, QuotaState.UNKNOWN, redaction.scrub_exception(exc)
            )
        now = self._clock()
        snapshot = snapshot_from_rate_limits(response, now=now)
        self._last_good[account] = _CachedRead(now, snapshot)
        return snapshot

    def cached(self, account: str) -> QuotaSnapshot | None:
        """The last good snapshot for *account* while it is still fresh.

        This is the no-I/O accessor for callers that must answer without an RPC
        (a list endpoint fanning out over every account). `cache_ttl_s` is what
        "fresh" means; the longer ceiling in `read()` applies only when a live
        read has actually failed and something is better than nothing.
        """
        entry = self._last_good.get(account)
        if entry is None:
            return None
        age = max(0.0, self._clock() - entry.read_at)
        if age >= self._cache_ttl_s:
            return None
        return self._aged(entry.snapshot, age)

    def invalidate(self, account: str | None = None) -> None:
        """Drop the last good snapshot for one account, or for every account."""
        if account is None:
            self._last_good.clear()
            return
        self._last_good.pop(account, None)

    def _degraded(
        self, account: str, state: QuotaState, detail: str
    ) -> QuotaSnapshot:
        entry = self._last_good.get(account)
        age = None if entry is None else max(0.0, self._clock() - entry.read_at)
        if entry is not None and age is not None and (
            age < self._cache_ttl_s * STALE_CEILING_MULTIPLIER
        ):
            # Serve the last good reading rather than the failure: the reason is
            # logged, but a stale percentage steers better than no percentage,
            # and the age says how much to trust it.
            log_event(
                logger,
                logging.INFO,
                "codex.quota.serving_last_good",
                account=account,
                state=state.value,
                age_s=round(age, 3),
                detail=detail,
            )
            return self._aged(entry.snapshot, age)
        log_event(
            logger,
            logging.WARNING,
            "codex.quota.unavailable",
            account=account,
            state=state.value,
            age_s=None if age is None else round(age, 3),
            detail=detail,
        )
        return QuotaSnapshot(state=state, age_s=age, detail=detail)

    @staticmethod
    def _aged(snapshot: QuotaSnapshot, age: float) -> QuotaSnapshot:
        """*snapshot* re-stamped with how old its numbers are.

        The cache age and the upstream staleness the mapper found are combined
        with `max`: `age_s` answers "how old are these numbers", and the older of
        the two reasons is the true answer.
        """
        return replace(snapshot, age_s=max(age, snapshot.age_s or 0.0))
