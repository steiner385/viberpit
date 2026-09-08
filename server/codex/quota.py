"""Map Codex ``account/rateLimits/read`` into the canonical quota contract.

This module owns only the native boundary and its short last-good cache. The
provider-neutral DTO, persistence shape, selector semantics, and API vocabulary
all live in :mod:`ccswitch.quota`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final, Protocol

from ccswitch.quota import (
    AUTH_ERROR,
    AVAILABLE,
    SCOPE_ACCOUNT,
    SCOPE_MODEL,
    UNKNOWN,
    UNSUPPORTED,
    QuotaSnapshot,
    QuotaWindow,
    unknown_snapshot,
)
from server.codex import redaction, schema
from server.codex.protocol import (
    JSONRPC_METHOD_NOT_FOUND,
    CodexProtocolError,
    CodexRequestFailed,
)
from server.obs.context import log_event
from server.obs.metrics import codex_quota_last_read_timestamp_s

logger = logging.getLogger(__name__)

STALE_CEILING_MULTIPLIER: Final = 10
AUTH_ERROR_CODES: Final[frozenset[int]] = frozenset({401, 403})
_KIND_LABELS: Final[Mapping[str, str]] = {
    "primary": "Primary",
    "secondary": "Secondary",
}


class RateLimitReader(Protocol):
    async def read_rate_limits(self) -> schema.GetAccountRateLimitsResponse: ...


def _window(
    *,
    key: str,
    label: str,
    native: schema.RateLimitWindow,
    model: str | None,
) -> QuotaWindow:
    if not 0 <= native.used_percent <= 100:
        # The wire parser enforces this too. Keep the conversion boundary
        # defensive because tests/fakes and future internal callers can construct
        # the typed response directly; an invalid value must never become an
        # AVAILABLE ledger snapshot.
        raise schema.CodexSchemaError(
            f"rate limit window {key!r} usedPercent must be between 0 and 100"
        )
    return QuotaWindow(
        key=key,
        label=label,
        used_pct=float(native.used_percent),
        resets_at=(
            None
            if native.resets_at is None
            else datetime.fromtimestamp(native.resets_at, tz=timezone.utc)
        ),
        duration_seconds=(
            None
            if native.window_duration_mins is None
            else native.window_duration_mins * 60
        ),
        scope=SCOPE_ACCOUNT if model is None else SCOPE_MODEL,
        applies_to_models=() if model is None else (model,),
    )


def _windows_from(
    snapshot: schema.RateLimitSnapshot,
    *,
    key_prefix: str = "",
    label_base: str | None = None,
    model: str | None = None,
) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    for kind, native in (
        ("primary", snapshot.primary),
        ("secondary", snapshot.secondary),
    ):
        if native is None:
            continue
        windows.append(
            _window(
                key=f"{key_prefix}{kind}",
                label=(
                    f"{label_base} ({kind})"
                    if label_base
                    else _KIND_LABELS[kind]
                ),
                native=native,
                model=model,
            )
        )
    return windows


def snapshot_from_rate_limits(
    response: schema.GetAccountRateLimitsResponse, *, now: float
) -> QuotaSnapshot:
    """Convert every native bucket into canonical account/model windows."""
    limits = response.rate_limits
    windows = _windows_from(limits, label_base=limits.limit_name)
    for limit_id, bucket in response.rate_limits_by_limit_id.items():
        windows.extend(
            _windows_from(
                bucket,
                key_prefix=f"{limit_id}:",
                label_base=bucket.limit_name or limit_id,
                model=limit_id,
            )
        )
    credits = limits.credits
    return QuotaSnapshot(
        provider="openai",
        fetched_at=datetime.fromtimestamp(now, tz=timezone.utc),
        availability=AVAILABLE,
        windows=tuple(windows),
        extra_usage_enabled=bool(
            credits is not None and (credits.has_credits or credits.unlimited)
        ),
    )


@dataclass(frozen=True, slots=True)
class _CachedRead:
    read_at: float
    snapshot: QuotaSnapshot


class CodexQuotaReader:
    """Read canonical Codex quota with a bounded last-good cache."""

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
        if client is None:
            return self._degraded(
                account,
                UNKNOWN,
                "no codex app-server connection is available for this account",
            )
        try:
            response = await client.read_rate_limits()
        except CodexRequestFailed as exc:
            if exc.code == JSONRPC_METHOD_NOT_FOUND:
                availability = UNSUPPORTED
            elif exc.code in AUTH_ERROR_CODES:
                availability = AUTH_ERROR
            else:
                availability = UNKNOWN
            return self._degraded(
                account, availability, redaction.scrub_exception(exc)
            )
        except (CodexProtocolError, schema.CodexSchemaError) as exc:
            return self._degraded(
                account, UNKNOWN, redaction.scrub_exception(exc)
            )
        now = self._clock()
        try:
            snapshot = snapshot_from_rate_limits(response, now=now)
        except schema.CodexSchemaError as exc:
            return self._degraded(
                account, UNKNOWN, redaction.scrub_exception(exc)
            )
        self._last_good[account] = _CachedRead(now, snapshot)
        # Read TIME, not age: a gauge nothing re-sets between reads would be stale
        # by construction, so the consumer computes `now - value` instead.
        codex_quota_last_read_timestamp_s.labels(account).set(now)
        return snapshot

    def cached(self, account: str) -> QuotaSnapshot | None:
        entry = self._last_good.get(account)
        if entry is None:
            return None
        if max(0.0, self._clock() - entry.read_at) >= self._cache_ttl_s:
            return None
        return entry.snapshot

    def invalidate(self, account: str | None = None) -> None:
        if account is None:
            self._last_good.clear()
        else:
            self._last_good.pop(account, None)

    def _degraded(
        self, account: str, availability: str, detail: str
    ) -> QuotaSnapshot:
        entry = self._last_good.get(account)
        age = None if entry is None else max(0.0, self._clock() - entry.read_at)
        if (
            entry is not None
            and age is not None
            and age < self._cache_ttl_s * STALE_CEILING_MULTIPLIER
        ):
            log_event(
                logger,
                logging.INFO,
                "codex.quota.serving_last_good",
                account=account,
                availability=availability,
                age_s=round(age, 3),
                detail=detail,
            )
            return replace(entry.snapshot, availability=availability)
        log_event(
            logger,
            logging.WARNING,
            "codex.quota.unavailable",
            account=account,
            availability=availability,
            age_s=None if age is None else round(age, 3),
            detail=detail,
        )
        return unknown_snapshot(
            "openai",
            fetched_at=datetime.fromtimestamp(self._clock(), tz=timezone.utc),
            availability=availability,
        )
