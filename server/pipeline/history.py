"""Read-only helpers over pipeline_history.db, mirroring Plotroom's history.ts.

Pure SQL + the same recency/percentile semantics. No writes (migration owns that).
"""
from __future__ import annotations

import json
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


@dataclass(frozen=True)
class CheckDurationStat:
    check_name: str
    event: str
    p50: float
    p90: float
    ratio: float | None
    n: int

    def to_dict(self) -> dict:
        return {"check_name": self.check_name, "event": self.event, "p50": self.p50,
                "p90": self.p90, "ratio": self.ratio, "n": self.n}


@dataclass(frozen=True)
class CheckFlakeStat:
    check_name: str
    event: str
    flake_pct: float
    flaky_commits: int
    total_commits: int
    n: int

    def to_dict(self) -> dict:
        return {"check_name": self.check_name, "event": self.event,
                "flake_pct": self.flake_pct, "flaky_commits": self.flaky_commits,
                "total_commits": self.total_commits, "n": self.n}


_FLAKE_PASS = {"success"}
_FLAKE_FAIL = {"failure", "cancelled", "timed_out", "error", "action_required"}

_REG_RECENT_K = 10
_REG_PRIOR_N = 20
_REG_MIN = 30
_REG_RATIO = 1.5
_REG_MIN_DELTA = 60.0


@dataclass(frozen=True)
class CheckRegressionStat:
    check_name: str
    event: str
    prior_p50: float
    recent_p50: float
    ratio: float
    since: str

    def to_dict(self) -> dict:
        return {
            "check_name": self.check_name,
            "event": self.event,
            "prior_p50": self.prior_p50,
            "recent_p50": self.recent_p50,
            "ratio": self.ratio,
            "since": self.since,
        }


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
        cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._conn.execute(
            "SELECT DISTINCT check_name FROM check_durations "
            "WHERE repo=? AND event=? AND conclusion='SUCCESS' AND completed_at >= ?",
            (repo, event, cutoff),
        ).fetchall()
        return [r["check_name"] for r in rows]

    def slowest_checks(
        self, repo: str, since_iso: str, limit: int = 10, event: str | None = None,
    ) -> "list[CheckDurationStat]":
        q = ("SELECT check_name, event, duration_secs FROM check_durations "
             "WHERE repo=? AND conclusion='SUCCESS' AND completed_at >= ?")
        args: list = [repo, since_iso]
        if event is not None:
            q += " AND event=?"
            args.append(event)
        groups: dict[tuple[str, str], list[float]] = {}
        for r in self._conn.execute(q, args).fetchall():
            if r["duration_secs"] is None:
                continue
            groups.setdefault((r["check_name"], r["event"] or ""), []).append(
                float(r["duration_secs"]))
        stats: list[CheckDurationStat] = []
        for (name, ev), vals in groups.items():
            s = sorted(vals)
            p50 = percentile(s, 0.5)
            p90 = percentile(s, 0.9)
            stats.append(CheckDurationStat(
                check_name=name, event=ev, p50=p50, p90=p90,
                ratio=(p90 / p50) if p50 else None, n=len(s)))
        stats.sort(key=lambda x: x.p50, reverse=True)
        return stats[:limit]

    def flakiest_checks(
        self, repo: str, since_iso: str, limit: int = 10, event: str | None = None,
    ) -> "list[CheckFlakeStat]":
        q = ("SELECT check_name, event, head_sha, conclusion FROM check_durations "
             "WHERE repo=? AND completed_at >= ?")
        args: list = [repo, since_iso]
        if event is not None:
            q += " AND event=?"
            args.append(event)
        # (check, event) -> head_sha -> {"pass": bool, "fail": bool}
        by_check: dict[tuple[str, str], dict[str, dict[str, bool]]] = {}
        totals: dict[tuple[str, str], int] = {}
        for r in self._conn.execute(q, args).fetchall():
            key = (r["check_name"], r["event"] or "")
            totals[key] = totals.get(key, 0) + 1
            c = (r["conclusion"] or "").lower()
            commits = by_check.setdefault(key, {})
            flags = commits.setdefault(r["head_sha"] or "", {"pass": False, "fail": False})
            if c in _FLAKE_PASS:
                flags["pass"] = True
            elif c in _FLAKE_FAIL:
                flags["fail"] = True
        stats: list[CheckFlakeStat] = []
        for key, commits in by_check.items():
            decided = [f for f in commits.values() if f["pass"] or f["fail"]]
            total = len(decided)
            if total == 0:
                continue
            flaky = sum(1 for f in decided if f["pass"] and f["fail"])
            stats.append(CheckFlakeStat(
                check_name=key[0], event=key[1], flake_pct=100.0 * flaky / total,
                flaky_commits=flaky, total_commits=total, n=totals[key]))
        stats.sort(key=lambda x: (x.flake_pct, x.flaky_commits), reverse=True)
        return stats[:limit]

    def duration_regressions(self, repo: str) -> list[CheckRegressionStat]:
        """Checks whose rolling-median SUCCESS duration stepped up (Plotroom
        measureDurationStep). Window-independent: last 10 vs prior 20 samples."""
        pairs = self._conn.execute(
            "SELECT DISTINCT check_name, event FROM check_durations WHERE repo=?",
            (repo,),
        ).fetchall()
        out: list[CheckRegressionStat] = []
        for p in pairs:
            rows = self._conn.execute(
                "SELECT duration_secs, completed_at FROM check_durations "
                "WHERE repo=? AND check_name=? AND event=? AND conclusion='SUCCESS' "
                "ORDER BY completed_at DESC LIMIT ?",
                (repo, p["check_name"], p["event"], _REG_MIN),
            ).fetchall()
            if len(rows) < _REG_MIN:
                continue
            durs = [float(r["duration_secs"]) for r in rows]
            recent = median(durs[:_REG_RECENT_K])
            prior = median(durs[_REG_RECENT_K:_REG_MIN])
            if prior <= 0:
                continue
            ratio = recent / prior
            if ratio < _REG_RATIO or (recent - prior) < _REG_MIN_DELTA:
                continue
            out.append(
                CheckRegressionStat(
                    check_name=p["check_name"],
                    event=p["event"],
                    prior_p50=prior,
                    recent_p50=recent,
                    ratio=ratio,
                    since=rows[_REG_RECENT_K - 1]["completed_at"],
                )
            )
        out.sort(key=lambda s: s.ratio, reverse=True)
        return out

    def expected_runner_wait(self, repo: str, name: str, event: str) -> float | None:
        rows = self._conn.execute(
            "SELECT wait_secs FROM runner_waits WHERE repo=? AND check_name=? AND event=? "
            "ORDER BY started_at DESC LIMIT 20",
            (repo, name, event),
        ).fetchall()
        vals = [float(r["wait_secs"]) for r in rows]
        return median(vals) if vals else None

    def ci_graph(self, repo: str) -> dict | None:
        """Return the stored ciGraph for a repo (from the migrated meta table), or None.

        The ci-graph is the Plotroom-derived BFS closure of the rollup job's
        ``needs:`` in ci.yml — it contains ``prefixes`` (required-check name
        prefixes) and ``workflowName`` (the rollup workflow's display name).
        Returns None when the meta table is absent or has no entry for this repo.
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (f"ciGraph:{repo}",),
            ).fetchone()
        except Exception:
            return None
        return json.loads(row["value"]) if row else None
