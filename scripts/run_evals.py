#!/usr/bin/env python3
"""Run the ViberPit eval suite and gate on the pass-rate (AI-Native SDLC §7, #805).

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
import hashlib
import json
import math
import os
import re
import signal
import time
from dataclasses import asdict
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from viberpit import brand  # noqa: E402
from server.sdlc.evals import DEFAULT_THRESHOLD, EvalCase, EvalReport, EvalResult, aggregate  # noqa: E402

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


def _validate_browser_report(value: object) -> dict:
    """Validate observations, not authenticated admission or agent evidence."""
    if not isinstance(value, dict):
        raise ValueError("browser report must be an object")
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("report_kind") != "ui-acceptance-calibration"
            or value.get("executed") is not True):
        raise ValueError("missing executed browser calibration report")
    browser = value.get("browser")
    if (not isinstance(browser, dict) or browser.get("name") != "chromium"
            or not isinstance(browser.get("version"), str) or not browser["version"].strip()):
        raise ValueError("missing browser identity")
    fixtures = value.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("empty fixture corpus")
    ids: set[str] = set()
    statuses = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError("invalid fixture")
        identity = fixture.get("id")
        digest = fixture.get("sha256")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("missing or duplicate fixture identity")
        ids.add(identity)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid fixture hash")
        for key in ("expected_failures", "observed_failures"):
            failures = fixture.get(key)
            if (not isinstance(failures, list)
                    or any(not isinstance(item, str) or not item for item in failures)
                    or len(set(failures)) != len(failures)):
                raise ValueError("invalid failure identifiers")
        measured = set(fixture["expected_failures"]) == set(fixture["observed_failures"])
        if type(fixture.get("passed")) is not bool or fixture["passed"] != measured:
            raise ValueError("contradictory fixture status")
        statuses.append(measured)
    if type(value.get("passed")) is not bool or value["passed"] != all(statuses):
        raise ValueError("contradictory aggregate status")
    return value


def run_browser_case(case: EvalCase, *, timeout: float) -> tuple[EvalResult, dict]:
    """Execute a trusted repository command; never install tools or start agents."""
    started = time.monotonic()
    observation: dict = {"case_id": case.id, "executed": False, "browser_report": None}
    detail = "browser case has no `check`"
    passed = False
    if case.check:
        try:
            with subprocess.Popen(case.check, shell=True, cwd=str(_REPO_ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  start_new_session=(os.name == "posix")) as proc:
                observation["executed"] = True
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    proc.communicate()
                    detail = f"browser command timeout after {timeout:g}s"
                else:
                    try:
                        value = _validate_browser_report(json.loads(stdout))
                        observation["browser_report"] = value
                        passed = proc.returncode == 0 and value["passed"]
                        if proc.returncode:
                            detail = f"browser command exit {proc.returncode}"
                        else:
                            detail = "" if passed else "browser fixture calibration failed"
                    except (ValueError, TypeError) as exc:
                        detail = f"invalid browser report: {exc}"
                        if proc.returncode:
                            detail = f"browser command exit {proc.returncode}: {detail}"

        except OSError as exc:
            detail = f"browser command unavailable: {exc}"
    observation.update(passed=passed, detail=detail, duration_seconds=time.monotonic() - started)
    return EvalResult(case.id, passed=passed, detail=detail), observation


def _timeout(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 < parsed <= 3600:
        raise argparse.ArgumentTypeError("timeout must be positive and at most 3600 seconds")
    return parsed


def _write_browser_report(path: Path, observations: list[dict], report: EvalReport) -> None:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT,
                              capture_output=True, text=True, timeout=10)
    definitions = {
        str(p.relative_to(_REPO_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for rel in _CASE_DIRS for p in sorted((_REPO_ROOT / rel).glob("*.yaml"))
        if (yaml.safe_load(p.read_text()) or {}).get("kind") == "browser"
    }
    payload = {"schema_version": 1, "kind": "browser", "report_kind": "acceptance-calibration",
               "evidence_scope": "runner observations; not authenticated admission or native-agent evidence",
               "executed": any(o["executed"] for o in observations),
               "repository_revision": revision.stdout.strip() if revision.returncode == 0 else None,
               "definition_sha256": definitions, "results": observations, "aggregate": asdict(report)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"Run the {brand.NAME} eval suite.")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"minimum pass-rate to pass the gate (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--kind", choices=("static", "agent", "browser"), default="static",
                    help="which cases to run (default static — the CI gate)")
    ap.add_argument("--timeout", type=_timeout, default=120, help="browser seconds per case (0 < n <= 3600)")
    ap.add_argument("--report", type=Path, help="write browser calibration JSON observations")
    args = ap.parse_args(argv)
    if args.report and args.kind != "browser":
        ap.error("--report requires --kind browser; agent mode is LIST ONLY")

    cases = load_cases(args.kind)
    if args.kind == "agent":
        # Agent-mode cases need a live Claude session; the runner only lists them so
        # the corpus is visible without an API key. Executing them is out-of-band.
        print(f"agent-mode cases ({len(cases)}) — LIST ONLY; run out-of-band with a live session:")
        for c in cases:
            print(f"  - {c.id}: {c.description}")
        return 0

    observations = []
    if args.kind == "browser":
        outcomes = [run_browser_case(c, timeout=args.timeout) for c in cases]
        results = [result for result, _ in outcomes]
        observations = [observation for _, observation in outcomes]
    else:
        results = [run_static_case(c) for c in cases]
    report = aggregate(results, threshold=args.threshold)
    if args.report:
        _write_browser_report(args.report, observations, report)

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
