"""Browser calibration executes commands and rejects unsupported success claims."""
from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

from server.sdlc.evals import EvalCase

SPEC = importlib.util.spec_from_file_location("browser_eval_runner", Path(__file__).resolve().parents[2] / "scripts/run_evals.py")
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def payload():
    return {"schema_version": 1, "report_kind": "ui-acceptance-calibration", "executed": True,
            "passed": True, "browser": {"name": "chromium", "version": "123"},
            "fixtures": [{"id": "clean", "sha256": "a" * 64, "expected_failures": [],
                          "observed_failures": [], "passed": True}]}


def command(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def run_output(value):
    return runner.run_browser_case(EvalCase("fixture", "", "browser", command(f"print({json.dumps(value)!r})")), timeout=2)


def test_browser_executes_and_validates():
    result, observation = run_output(payload())
    assert result.passed
    assert observation["executed"] is True
    assert observation["browser_report"] == payload()
    assert observation["duration_seconds"] >= 0


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(executed=False), lambda p: p.update(fixtures=[]),
    lambda p: p["fixtures"][0].update(passed=False),
    lambda p: p["fixtures"][0].update(observed_failures=["contrast"]),
    lambda p: p.update(passed=False), lambda p: p.update(browser={}),
])
def test_browser_rejects_invalid_success(mutation):
    doc = payload()
    mutation(doc)
    assert not run_output(doc)[0].passed


@pytest.mark.parametrize("check", ["", "command-that-does-not-exist-for-eval", command("print('garbage')"), command("pass"), command("raise SystemExit(3)")])
def test_browser_execution_failures(check):
    assert not runner.run_browser_case(EvalCase("bad", "", "browser", check), timeout=2)[0].passed


def test_browser_timeout():
    result, _ = runner.run_browser_case(EvalCase("slow", "", "browser", command("import time; time.sleep(10)")), timeout=0.05)
    assert not result.passed
    assert "timeout" in result.detail


def test_browser_empty_corpus_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "load_cases", lambda kind: [])
    path = tmp_path / "report.json"
    assert runner.main(["--kind", "browser", "--report", str(path)]) == 1
    report = json.loads(path.read_text())
    assert report["executed"] is False
    assert report["results"] == []


def test_agent_report_rejected(tmp_path):
    with pytest.raises(SystemExit):
        runner.main(["--kind", "agent", "--report", str(tmp_path / "report.json")])
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("timeout", ["0", "-1", "3601", "nan", "inf"])
def test_timeout_bounds(timeout):
    with pytest.raises(SystemExit):
        runner.main(["--kind", "browser", "--timeout", timeout])


@pytest.mark.skipif(sys.platform != "linux", reason="checks Linux process state")
@pytest.mark.slow
def test_timeout_reaps_browser_process_group(tmp_path):
    import os
    import time

    pid_file = tmp_path / "browser.pid"
    child = f"import os,time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    result, _ = runner.run_browser_case(EvalCase("descendant", "", "browser", command(code)), timeout=0.5)
    assert not result.passed
    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            # procfs can open stat before exit, then report ESRCH on read.
            break
        if state == "Z":  # dead, waiting for the container's init to reap
            break
        time.sleep(0.01)
    else:
        os.kill(pid, 9)
        pytest.fail("browser descendant survived timeout")


def test_report_contains_revision_and_definition_hashes(tmp_path, monkeypatch):
    import hashlib

    root = tmp_path / "repo"
    cases = root / "evals/cases"
    cases.mkdir(parents=True)
    definition = cases / "browser.yaml"
    definition.write_text("kind: browser\nid: clean\n")
    monkeypatch.setattr(runner, "_REPO_ROOT", root)
    monkeypatch.setattr(runner, "load_cases", lambda kind: [EvalCase("fixture", "", "browser", command(f"print({json.dumps(payload())!r})"))])
    report_path = tmp_path / "result.json"
    assert runner.main(["--kind", "browser", "--report", str(report_path)]) == 0
    report = json.loads(report_path.read_text())
    assert report["kind"] == "browser"
    assert report["report_kind"] == "acceptance-calibration"
    assert report["definition_sha256"]["evals/cases/browser.yaml"] == hashlib.sha256(definition.read_bytes()).hexdigest()
    assert report["repository_revision"] is None
    assert report["results"][0]["browser_report"] == payload()


@pytest.mark.parametrize("claims_passed", [False, True])
def test_nonzero_preserves_valid_measurements_but_always_fails(claims_passed):
    doc = payload()
    if not claims_passed:
        doc["fixtures"][0].update(observed_failures=["contrast"], passed=False)
        doc["passed"] = False
    check = command(f"print({json.dumps(doc)!r}); raise SystemExit(1)")
    result, observation = runner.run_browser_case(EvalCase("failure", "", "browser", check), timeout=2)
    assert not result.passed
    assert observation["browser_report"] == doc
    assert "exit 1" in result.detail


def test_browser_workflow_routes_fixtures_and_persists_reports():
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/evals.yml").read_text())
    # PyYAML's YAML 1.1 parser treats GitHub's `on` key as boolean true.
    triggers = workflow[True]
    for event in ("push", "pull_request"):
        assert {"web/evals/**", "web/package.json", "web/package-lock.json"} <= set(triggers[event]["paths"])
    job = workflow["jobs"]["browser-evals"]
    assert 0 < job["timeout-minutes"] <= 15
    steps = job["steps"]
    assert any(step.get("run") == "npm ci" and step.get("working-directory") == "web" for step in steps)
    assert any(step.get("run") == "npx playwright install chromium --with-deps" and step.get("working-directory") == "web" for step in steps)
    report = "test-results/framework-ui-calibration.json"
    assert any(step.get("run") == f"python scripts/run_evals.py --kind browser --report {report}" for step in steps)
    assert any(step.get("uses", "").startswith("actions/upload-artifact@") and step.get("if") == "always()" and step["with"]["path"] == report for step in steps)
