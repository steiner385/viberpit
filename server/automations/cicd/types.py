"""Value types shared by the CI/CD Doctor's pure analyzers (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TestObs:
    """One test-case outcome observation (a ci_test_result row, detached)."""
    test_id: str
    head_sha: str
    head_branch: str
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
