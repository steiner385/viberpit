"""Pure SDLC eval aggregation core (Phase 3, issue #805).

Exhaustive, I/O-free — covers the pass-rate threshold gate that blocks a merge
when an agent-config change regresses the eval corpus.
"""
from __future__ import annotations

from server.sdlc.evals import (
    DEFAULT_THRESHOLD,
    EvalReport,
    EvalResult,
    aggregate,
)


def _results(passed: int, failed: int) -> list[EvalResult]:
    out = [EvalResult(case_id=f"p{i}", passed=True) for i in range(passed)]
    out += [EvalResult(case_id=f"f{i}", passed=False, detail="boom") for i in range(failed)]
    return out


def test_all_pass_gate_passes_at_default_threshold():
    rep = aggregate(_results(5, 0))
    assert isinstance(rep, EvalReport)
    assert rep.total == 5 and rep.passed == 5
    assert rep.pass_rate == 1.0
    assert rep.threshold == DEFAULT_THRESHOLD
    assert rep.gate_passed is True
    assert rep.failures == ()


def test_one_failure_fails_default_threshold():
    rep = aggregate(_results(9, 1))
    assert rep.passed == 9 and rep.total == 10
    assert rep.pass_rate == 0.9
    assert rep.gate_passed is False  # default threshold is 1.0
    assert rep.failures == ("f0",)


def test_lower_threshold_tolerates_failures():
    rep = aggregate(_results(9, 1), threshold=0.8)
    assert rep.pass_rate == 0.9
    assert rep.gate_passed is True


def test_pass_rate_exactly_at_threshold_passes():
    rep = aggregate(_results(8, 2), threshold=0.8)
    assert rep.pass_rate == 0.8
    assert rep.gate_passed is True


def test_empty_suite_fails_the_gate():
    # "no evals ran" must never read as "all passed".
    rep = aggregate([], threshold=0.0)
    assert rep.total == 0
    assert rep.pass_rate == 0.0
    assert rep.gate_passed is False


def test_failures_preserve_case_ids_in_order():
    results = [
        EvalResult("a", True),
        EvalResult("b", False),
        EvalResult("c", False),
        EvalResult("d", True),
    ]
    rep = aggregate(results)
    assert rep.failures == ("b", "c")


def test_aggregate_is_pure():
    results = _results(3, 1)
    before = list(results)
    aggregate(results)
    assert results == before
