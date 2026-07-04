"""Assemble Finding objects from analyzer outputs. Pure."""
from __future__ import annotations

from .types import Finding, GateThresholds

_MIN_SHAS_FOR_FLAKE = 3  # fewer distinct shas is too little evidence to accuse a test

_TRANSITION_TITLES = {
    "graduate": "stable — propose graduating to a required check",
    "demote": "flaky — propose demoting from required checks",
    "retire": "long-term noisy advisory — propose retiring or reassigning",
}


def flaky_test_findings(repo: str, flake_by_test: dict[str, tuple[float, int]],
                        *, th: GateThresholds) -> list[Finding]:
    out = [Finding(kind="flaky_test", repo=repo, key=test_id,
                   title=f"flaky test: {test_id} ({pct:.1f}% of shas)",
                   detail={"flake_pct": pct, "shas": shas})
           for test_id, (pct, shas) in sorted(flake_by_test.items())
           if pct >= th.flaky_test_min_pct and shas >= _MIN_SHAS_FOR_FLAKE]
    return out


def check_transition_findings(repo: str, verdicts: dict[str, str]) -> list[Finding]:
    return [Finding(kind=f"{verdict}_check", repo=repo, key=check,
                    title=f"check `{check}`: {_TRANSITION_TITLES[verdict]}",
                    detail={"check": check, "verdict": verdict})
            for check, verdict in sorted(verdicts.items())]


def missing_junit_finding(repo: str) -> Finding:
    return Finding(kind="missing_junit", repo=repo, key="junit",
                   title="CI emits no JUnit artifacts — test-level sensing is blind",
                   detail={"fix": "emit --junitxml artifacts named junit-* (see plan A)"})
