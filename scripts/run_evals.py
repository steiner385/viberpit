#!/usr/bin/env python3
"""Run the ccswitch eval suite and gate on the pass-rate (AI-Native SDLC §7, #805).

Loads YAML cases from ``evals/cases`` (+ ``evals/regressions``), executes each
selected case's acceptance check, then scores them with the pure aggregator
``server.sdlc.evals.aggregate`` and exits non-zero when the pass-rate is below the
threshold — the merge gate for agent-config changes.

I/O lives here; the scoring is pure and unit-tested in ``test_sdlc_evals.py``.

Usage:
    python scripts/run_evals.py                  # static gate (default threshold 1.0)
    python scripts/run_evals.py --threshold 0.95
    python scripts/run_evals.py --kind agent     # list agent-mode cases (not the CI gate)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from server.sdlc.evals import DEFAULT_THRESHOLD, EvalCase, EvalResult, aggregate  # noqa: E402

_CASE_DIRS = ("evals/cases", "evals/regressions")


def load_cases(kind: str) -> list[EvalCase]:
    """Parse every ``*.yaml`` under the case dirs into ``EvalCase`` of *kind*."""
    cases: list[EvalCase] = []
    for rel in _CASE_DIRS:
        for path in sorted((_REPO_ROOT / rel).glob("*.yaml")):
            doc = yaml.safe_load(path.read_text()) or {}
            if doc.get("kind", "static") != kind:
                continue
            cases.append(EvalCase(
                id=doc["id"],
                description=(doc.get("description") or "").strip(),
                kind=doc.get("kind", "static"),
                check=(doc.get("check") or "").strip(),
                tags=tuple(doc.get("tags") or ()),
            ))
    return cases


def run_static_case(case: EvalCase) -> EvalResult:
    """Execute a static case's shell acceptance check from the repo root."""
    if not case.check:
        return EvalResult(case.id, passed=False, detail="static case has no `check`")
    proc = subprocess.run(
        case.check, shell=True, cwd=str(_REPO_ROOT),
        capture_output=True, text=True,
    )
    passed = proc.returncode == 0
    detail = "" if passed else (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
    return EvalResult(case.id, passed=passed, detail=detail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the ccswitch eval suite.")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"minimum pass-rate to pass the gate (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--kind", choices=("static", "agent"), default="static",
                    help="which cases to run (default static — the CI gate)")
    args = ap.parse_args(argv)

    cases = load_cases(args.kind)
    if args.kind == "agent":
        # Agent-mode cases need a live Claude session; the runner only lists them so
        # the corpus is visible without an API key. Executing them is out-of-band.
        print(f"agent-mode cases ({len(cases)}) — run out-of-band with a live session:")
        for c in cases:
            print(f"  - {c.id}: {c.description}")
        return 0

    results = [run_static_case(c) for c in cases]
    report = aggregate(results, threshold=args.threshold)

    print(f"evals: {report.passed}/{report.total} passed "
          f"(pass-rate {report.pass_rate:.0%}, threshold {report.threshold:.0%})")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        line = f"  [{mark}] {r.case_id}"
        if not r.passed and r.detail:
            line += f" — {r.detail}"
        print(line)

    if not report.gate_passed:
        print(f"\nGATE FAILED: pass-rate {report.pass_rate:.0%} < threshold "
              f"{report.threshold:.0%} (or empty corpus). Failures: "
              f"{', '.join(report.failures) or '(none — empty corpus)'}")
        return 1
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
