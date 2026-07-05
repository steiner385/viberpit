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
                        *, th: GateThresholds,
                        messages: dict[str, str] | None = None) -> list[Finding]:
    messages = messages or {}
    out = []
    for test_id, (pct, shas) in sorted(flake_by_test.items()):
        if pct < th.flaky_test_min_pct or shas < _MIN_SHAS_FOR_FLAKE:
            continue
        detail: dict = {"flake_pct": pct, "shas": shas}
        if messages.get(test_id):  # a concrete failure message, when one was parsed
            detail["message"] = messages[test_id]
        out.append(Finding(kind="flaky_test", repo=repo, key=test_id,
                           title=f"flaky test: {test_id} ({pct:.1f}% of shas)",
                           detail=detail))
    return out


# A graduation verdict is a *candidate* surfaced via the counts + activity event;
# there is nothing for a human to "fix", so it must not spawn a ci-doctor issue.
# Demote/retire are actionable (a required check went flaky / an advisory is noise).
_NON_ACTIONABLE_VERDICTS = frozenset({"graduate"})


def check_transition_findings(repo: str, verdicts: dict[str, str]) -> list[Finding]:
    return [Finding(kind=f"{verdict}_check", repo=repo, key=check,
                    title=f"check `{check}`: {_TRANSITION_TITLES[verdict]}",
                    detail={"check": check, "verdict": verdict},
                    actionable=verdict not in _NON_ACTIONABLE_VERDICTS)
            for check, verdict in sorted(verdicts.items())]


def missing_junit_finding(repo: str) -> Finding:
    return Finding(kind="missing_junit", repo=repo, key="junit",
                   title="CI emits no JUnit artifacts — test-level sensing is blind",
                   detail={"fix": "emit --junitxml artifacts named junit-* (see plan A)"})
