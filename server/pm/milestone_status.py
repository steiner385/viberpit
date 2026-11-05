"""Release-milestone condition evaluation and read-time freshness.

Pure code, no DB. A stale or errored reading never reads as ``met``.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from server.pm.milestone_def import _ALL_CLOSED_KINDS, MEASURE_KINDS, Measure
from server.time_iso import aware_utc

#: The ``key`` of a milestone's leaf-list row (scope ``leaves:<milestone key>``). Here,
#: in the pure module, so the view reads it without importing the refresh service.
LEAVES_KEY = "leaves"


def leaves_scope(milestone_key: str) -> str:
    return f"leaves:{milestone_key}"


_OPS: dict[str, Callable[[float, float], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}


@dataclass(frozen=True)
class Reading:
    value: dict[str, Any] | None
    measured_at: datetime | None
    error: str | None
    source_url: str | None


def effective_max_age(measure: Measure) -> int | None:
    """The measure's own max age, else its kind's default (None = never stale)."""
    if measure.max_age_s is not None:
        return measure.max_age_s
    return MEASURE_KINDS[measure.kind].default_max_age_s


def _number(value: dict[str, Any], measure: Measure) -> float | None:
    if measure.kind in _ALL_CLOSED_KINDS and "total" in value and "done" in value:
        return value["total"] - value["done"]  # open count
    n = value.get("n")
    return n if isinstance(n, int | float) and not isinstance(n, bool) else None


def evaluate(measure: Measure, reading: Reading) -> str:
    """Stored status (met | unmet | unknown | info) for a raw reading."""
    value = reading.value
    if reading.error is not None or value is None:
        return "unknown"
    if measure.kind == "manual":
        if isinstance(value.get("met"), bool):
            return "met" if value["met"] else "unmet"
        return "info" if "n" in value else "unknown"
    if measure.op is None or measure.target is None:
        if measure.kind in _ALL_CLOSED_KINDS and "total" in value and "done" in value:
            return "met" if value["total"] > 0 and value["done"] == value["total"] else "unmet"
        return "info"
    n = _number(value, measure)
    if n is None:
        return "unknown"
    return "met" if _OPS[measure.op](n, measure.target) else "unmet"


def status_at(stored: str, measured_at: datetime | None, max_age_s: int | None, now: datetime) -> str:
    """Apply freshness at read time: a stale measurement reads ``unknown``. Either
    time may be naive (UTC, as SQLite reads it back) or aware (as Postgres does)."""
    if max_age_s is None:
        return stored
    measured, at = aware_utc(measured_at), aware_utc(now)
    if measured is None or at is None or at - measured > timedelta(seconds=max_age_s):
        return "unknown"
    return stored


def gate_met(statuses: Iterable[str]) -> bool:
    """A gate is met iff it has conditions and every one is ``met``."""
    s = list(statuses)
    return bool(s) and all(x == "met" for x in s)
