"""Value types shared by the CI/CD Doctor's pure analyzers (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TestObs:
    """One test-case outcome observation (a ci_test_result row, detached).
    Flake analysis keys on (test_id, head_sha, outcome) only — head_branch is
    deliberately absent (no analyzer distinguishes test flakiness by branch)."""
    test_id: str
    head_sha: str
    outcome: str  # passed | failed | error | skipped


@dataclass(frozen=True)
class JobObs:
    """One job observation (a ci_job row, detached). check == job name."""
    check: str
    head_sha: str
    head_branch: str
    conclusion: str  # success | failure | cancelled | skipped | ...
    duration_s: float
    created_ts: float


@dataclass(frozen=True)
class GateThresholds:
    """Graduation/demotion/retirement thresholds — THE single source of truth.
    Defaults mirror the automation config schema defaults (Plan C)."""
    min_window_days: float = 21.0
    min_runs: int = 30
    max_flake_pct: float = 2.0
    max_p95_minutes: float = 10.0
    demote_flake_pct: float = 5.0
    demote_window_days: float = 7.0
    retire_days: float = 60.0
    flaky_test_min_pct: float = 2.0

    @classmethod
    def from_config(cls, config: dict) -> "GateThresholds":
        """Build from a validated automation config dict; missing keys keep defaults."""
        def _f(key: str, default: float) -> float:
            return float(config[key]) if key in config else default

        return cls(
            min_window_days=_f("min_window_days", cls.min_window_days),
            min_runs=int(config["min_runs"]) if "min_runs" in config else cls.min_runs,
            max_flake_pct=_f("max_flake_pct", cls.max_flake_pct),
            max_p95_minutes=_f("max_p95_minutes", cls.max_p95_minutes),
            demote_flake_pct=_f("demote_flake_pct", cls.demote_flake_pct),
            demote_window_days=_f("demote_window_days", cls.demote_window_days),
            retire_days=_f("retire_days", cls.retire_days),
            flaky_test_min_pct=_f("flaky_test_min_pct", cls.flaky_test_min_pct),
        )


@dataclass
class CheckStats:
    """Rolling stability stats for one status-check context."""
    check: str
    runs_total: int = 0
    window_days: float = 0.0
    flake_pct: float = 0.0            # % of shas with BOTH success and failure
    recent_flake_pct: float = 0.0     # same, restricted to demote_window_days
    default_branch_failures: int = 0  # shas on the default branch that only ever failed
    p95_duration_s: float = 0.0
    flake_rising: bool = False


@dataclass(frozen=True)
class CheckAgg:
    """One row of the SQL check-stability aggregate (``cicd_doctor.check_stats_stmt``).

    The database collapses a whole job window to ONE ROW PER CHECK — see
    ``flake_counts_stmt`` for why the raw-row fold on the event loop had to go
    (#791, #827). ``check_stats_from_agg`` turns these pre-aggregated counts into
    the same ``CheckStats`` the in-memory ``check_stats`` fold produces; the two
    share the arithmetic so the SQL path and the Python path cannot drift. Every
    field is a raw count/extremum — the ratios (``flake_pct``, ``window_days``,
    ``flake_rising``) are derived once, in ``check_stats_from_agg``."""
    check: str
    runs_total: int          # jobs after dropping cancelled/skipped
    min_ts: float            # earliest / latest job timestamp -> window_days
    max_ts: float
    p95_duration_s: float    # already the picked p95 value, not a count
    shas_total: int          # distinct head_shas
    flaky_shas: int          # shas showing BOTH a pass and a fail
    recent_total: int        # shas with a job inside demote_window_days
    recent_flaky: int        # of those, shas showing both a pass and a fail
    default_branch_failures: int
    first_total: int         # shas whose earliest job is before the median ts
    first_flaky: int
    second_total: int        # shas whose earliest job is at/after the median ts
    second_flaky: int


@dataclass(frozen=True)
class Finding:
    """One diagnostic finding. fingerprint dedupes across ticks; the DB row and
    GitHub issue lifecycles (Plan C) key off it."""
    kind: str
    repo: str
    key: str
    title: str
    detail: dict = field(default_factory=dict)
    actionable: bool = True

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.repo}:{self.key}"
