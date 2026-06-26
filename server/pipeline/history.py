"""Read-only helpers over pipeline_history.db, mirroring Plotroom's history.ts.

Pure SQL + the same recency/percentile semantics. No writes (migration owns that).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

# Verbatim from history.ts.
DURATION_FRESH_DAYS = 14
DURATION_FRESH_MIN = 5
DURATION_FALLBACK_LIMIT = 10


def percentile(sorted_xs: list[float], q: float) -> float:
    """p-th percentile of an already-sorted list (math.ts: sorted[min(len-1, ceil(p*len)-1)])."""
    n = len(sorted_xs)
    return sorted_xs[min(n - 1, math.ceil(q * n) - 1)]


def median(xs: list[float]) -> float:
    """Lower median (math.ts: s[floor((len-1)/2)] over a sorted copy)."""
    s = sorted(xs)
    return s[(len(s) - 1) // 2]


@dataclass(frozen=True)
class Expected:
    p10: float
    p50: float
    p90: float
    n: int


def _parse_iso_ms(iso: str) -> float:
    """ms since epoch from an ISO-8601 string (tolerant of trailing Z)."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0


class HistoryStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def recent_durations(self, repo: str, name: str, event: str) -> list[float]:
        rows = self._conn.execute(
            "SELECT duration_secs, completed_at FROM check_durations "
            "WHERE repo=? AND check_name=? AND event=? AND conclusion='SUCCESS' "
            "ORDER BY completed_at DESC LIMIT 20",
            (repo, name, event),
        ).fetchall()
        if not rows:
            return []
        cutoff = _parse_iso_ms(rows[0]["completed_at"]) - DURATION_FRESH_DAYS * 86400_000
        fresh = [r for r in rows if _parse_iso_ms(r["completed_at"]) >= cutoff]
        chosen = fresh if len(fresh) >= DURATION_FRESH_MIN else rows[:DURATION_FALLBACK_LIMIT]
        return [float(r["duration_secs"]) for r in chosen]

    def expected(self, repo: str, name: str, event: str) -> Expected | None:
        vals = self.recent_durations(repo, name, event)
        if not vals:
            return None
        s = sorted(vals)
        return Expected(percentile(s, 0.1), percentile(s, 0.5), percentile(s, 0.9), len(s))

    def samples(self, repo: str, name: str, event: str) -> list[float]:
        return self.recent_durations(repo, name, event)

    def expected_set(self, repo: str, event: str, now: datetime, window_days: int = 14) -> list[str]:
        cutoff = (now - timedelta(days=window_days)).isoformat()
        rows = self._conn.execute(
            "SELECT DISTINCT check_name FROM check_durations "
            "WHERE repo=? AND event=? AND conclusion='SUCCESS' AND completed_at >= ?",
            (repo, event, cutoff),
        ).fetchall()
        return [r["check_name"] for r in rows]

    def expected_runner_wait(self, repo: str, name: str, event: str) -> float | None:
        rows = self._conn.execute(
            "SELECT wait_secs FROM runner_waits WHERE repo=? AND check_name=? AND event=? "
            "ORDER BY started_at DESC LIMIT 20",
            (repo, name, event),
        ).fetchall()
        vals = [float(r["wait_secs"]) for r in rows]
        return median(vals) if vals else None
