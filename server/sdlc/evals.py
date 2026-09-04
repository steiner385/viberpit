"""Pure SDLC eval aggregation core — the pass-rate gate for agent-config changes.

Phase 3 of the AI-Native SDLC design (§7). The eval *suite* is a corpus of real
ccswitch tasks with acceptance checks (``evals/`` in the repo); this module is the
**pure** scoring core that turns per-case results into a merge gate — no file I/O,
no subprocess, so it is exhaustively unit-testable the way ``server.sdlc.engine``
is. The runner (``scripts/run_evals.py``) does the I/O: load specs, execute each
case's acceptance check, then call ``aggregate`` and exit non-zero when the gate
fails.

The gate the playbook demands: *"a pass-rate threshold gates the merge."* A change
to ``CLAUDE.md`` / skills / hooks must keep the eval pass-rate at or above the
threshold or CI blocks it. Each production incident adds a regression eval
(``evals/regressions/``), so the corpus only grows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Static, deterministic evals (acceptance = a shell check that must exit 0) must
# ALL pass — an agent-config invariant is not "usually" true. Agent-mode evals
# (which spawn a live session) run out-of-band and may carry a lower bar; the
# runner passes whatever threshold the caller chooses.
DEFAULT_THRESHOLD = 1.0


@dataclass(frozen=True)
class EvalCase:
    """One eval task loaded from ``evals/``. ``kind`` is ``"static"`` (a
    deterministic acceptance check runnable in CI without an LLM) or ``"agent"``
    (requires a live Claude session; run out-of-band). ``check`` is the shell
    acceptance command for static cases."""
    id: str
    description: str
    kind: str = "static"
    check: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EvalResult:
    """Outcome of running one case. ``detail`` carries the failure reason (e.g.
    the acceptance command's stderr) for the report."""
    case_id: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class EvalReport:
    """The aggregated gate decision. ``pass_rate`` is in [0, 1]; ``gate_passed``
    is the single boolean CI acts on."""
    total: int
    passed: int
    pass_rate: float
    threshold: float
    gate_passed: bool
    failures: tuple[str, ...] = ()


def aggregate(results: list[EvalResult], *, threshold: float = DEFAULT_THRESHOLD) -> EvalReport:
    """Score *results* against *threshold*. Pure + total.

    The gate passes only when there is at least one result AND the pass-rate is at
    or above the threshold. An **empty** suite fails the gate: "no evals ran" must
    never read as "all evals passed" — a silently empty corpus is exactly the
    failure the gate exists to catch.
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    pass_rate = (passed / total) if total else 0.0
    gate_passed = total > 0 and pass_rate >= threshold
    failures = tuple(r.case_id for r in results if not r.passed)
    return EvalReport(
        total=total,
        passed=passed,
        pass_rate=pass_rate,
        threshold=threshold,
        gate_passed=gate_passed,
        failures=failures,
    )
