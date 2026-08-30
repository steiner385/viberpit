"""Integration guard for the eval runner (scripts/run_evals.py, #805).

Runs the actual runner as a subprocess — the way CI invokes it — so the
load→execute→aggregate wiring and the exit-code gate can't silently break. The
pure scoring is covered separately in test_sdlc_evals.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "scripts" / "run_evals.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_RUNNER), *args],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    )


def test_static_gate_passes_on_current_repo():
    proc = _run("--threshold", "1.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GATE PASSED" in proc.stdout
    # There is a non-trivial corpus — an empty suite would fail the gate.
    assert "evals: " in proc.stdout


def test_impossible_threshold_fails_the_gate():
    # Pass-rate can never reach 200%, so the gate must fail (exit 1).
    proc = _run("--threshold", "2.0")
    assert proc.returncode == 1
    assert "GATE FAILED" in proc.stdout


def test_agent_kind_lists_without_running_gate():
    proc = _run("--kind", "agent")
    assert proc.returncode == 0
    assert "agent-mode cases" in proc.stdout
