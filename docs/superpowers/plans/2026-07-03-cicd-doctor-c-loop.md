# CI/CD Doctor — Plan C: Control Loop + Integration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Plan-B engine into a live automation: GitHub Actions ingest seams, the `ci-doctor` issue reporter, the `CicdDoctor` automation + `CicdDoctorLoop`, orchestrator ticking, and a findings API. Spec: `docs/superpowers/specs/2026-07-03-cicd-doctor-design.md`.

**Architecture:** Mirrors AutopilotLoop/BacklogRunnerLoop exactly: a non-reentrant loop whose every I/O dependency is an injected seam; the real seams (sync `gh` shell-outs in `server/github/actions_api.py` + `server/github/ci_issues.py`) are wrapped in `asyncio.to_thread` by the orchestrator. The automations UI is schema-driven, so the card appears with zero frontend changes. **Depends on Plan B being merged** (imports `server.automations.cicd.*` and the `Ci*` models).

**Tech Stack:** Python 3.12, `gh` CLI via `gitrepos._run_gh` (+ a bytes variant for artifact zips), stdlib `zipfile`/`io`, FastAPI, SQLAlchemy async.

## Global Constraints

- All GitHub access routes through `src/ccswitch/gitrepos.py`'s `_run_gh` / `_gh_env` — never a parallel subprocess helper (Principle VI). GitHub Actions data is REST-only (no GraphQL API exists for it) — a documented exception to the GraphQL-for-reads norm.
- Artifact contract from Plan A: JUnit artifacts are named `junit-*` and contain `.xml` members.
- The findings API endpoint reads `CiFinding` rows directly from the DB — it must NOT call the orchestrator (a new orchestrator method would need a RemoteOrchestrator proxy + registered RPC; avoid that whole class of breakage).
- New loop counts go on the shared ctx attribute `cicd_doctor_counts`; `automation_statuses()`'s ctx map must gain a `"cicd-doctor"` entry or the card badge will always read "idle".
- Cursor + issue bookkeeping live in `automation_target_state` via `AutomationContext` (no new state table).
- Quality gates before every commit: `ruff check server/ && mypy server/ --ignore-missing-imports`. Tests: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/<file> -q`; loop tests follow the `asyncio.run(run())` + fakes style of `test_backlog_runner_loop.py`.
- Completion workflow (CLAUDE.md): branch off `origin/master`, non-draft PR, `gh pr merge --squash --auto`.

---

### Task 0: Branch setup

- [ ] **Step 1:**

```bash
git fetch origin
git checkout -b feat/cicd-doctor-loop origin/master
```

(If Plan B has not merged yet, STOP — this plan imports its modules.)

---

### Task 1: GitHub Actions ingest seams

**Files:**
- Create: `server/github/actions_api.py`
- Test: `server/tests/test_cicd_actions_api.py`

**Interfaces:**
- Consumes: `gitrepos._run_gh`, `gitrepos._gh_env`.
- Produces (all sync; every function takes `run=`/`run_bytes=` fakes for tests):
  - `list_completed_runs(repo, since_ts: float | None, *, run=...) -> list[dict]` — dicts with keys `id, workflow_path, event, head_sha, head_branch, conclusion, created_ts, duration_s`
  - `list_jobs(repo, run_id, *, run=...) -> list[dict]` — keys `id, name, conclusion, duration_s`
  - `junit_artifacts(repo, run_id, *, run=..., run_bytes=...) -> list[tuple[str, bytes]]` — (artifact name, xml bytes) for artifacts named `junit-*`
  - `repo_ci_meta(repo, *, run=...) -> tuple[str, set[str]]` — (default branch, required check contexts; empty set on error)
  - `workflow_texts(repo, default_branch, *, run=...) -> dict[str, str]` — path → YAML text

- [ ] **Step 1: Write the failing tests**

```python
"""actions_api — gh-backed reads, exercised through injected fake runners."""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

from server.github.actions_api import (
    junit_artifacts, list_completed_runs, list_jobs, repo_ci_meta, workflow_texts,
)


def _ok(payload) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_list_completed_runs_normalizes_and_filters_cursor():
    payload = {"workflow_runs": [
        {"id": 5, "path": ".github/workflows/ci.yml", "event": "pull_request",
         "head_sha": "abc", "head_branch": "feat", "conclusion": "success",
         "run_started_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:05:00Z",
         "created_at": "2026-07-01T10:00:00Z"},
        {"id": 4, "path": ".github/workflows/ci.yml", "event": "push",
         "head_sha": "old", "head_branch": "master", "conclusion": "success",
         "run_started_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-01T10:04:00Z",
         "created_at": "2026-06-01T10:00:00Z"},
    ]}
    calls = []
    def fake_run(args, token=None):
        calls.append(args)
        return _ok(payload)
    from datetime import datetime, timezone
    cursor = datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp()
    runs = list_completed_runs("o/r", cursor, run=fake_run)
    assert [r["id"] for r in runs] == [5]           # cursor filters the June run
    assert runs[0]["duration_s"] == 300.0
    assert runs[0]["created_ts"] > cursor
    assert "actions/runs" in calls[0][1]


def test_list_jobs_durations():
    payload = {"jobs": [{"id": 9, "name": "e2e", "conclusion": "failure",
                         "started_at": "2026-07-01T10:00:00Z",
                         "completed_at": "2026-07-01T10:02:00Z"}]}
    (job,) = list_jobs("o/r", 5, run=lambda a, token=None: _ok(payload))
    assert job == {"id": 9, "name": "e2e", "conclusion": "failure", "duration_s": 120.0}


def test_junit_artifacts_unzips_xml_members_only():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("server.xml", "<testsuite/>")
        z.writestr("report.html", "<html/>")
    listing = {"artifacts": [
        {"id": 1, "name": "junit-python", "expired": False},
        {"id": 2, "name": "playwright-report", "expired": False},
    ]}
    out = junit_artifacts("o/r", 5, run=lambda a, token=None: _ok(listing),
                          run_bytes=lambda a: buf.getvalue())
    assert [(n, b) for n, b in out] == [("junit-python", b"<testsuite/>")]


def test_repo_ci_meta_error_degrades_to_empty():
    def fake_run(args, token=None):
        if "protection" in args[1]:
            return SimpleNamespace(returncode=1, stdout="", stderr="404")
        return _ok({"default_branch": "master"})
    branch, required = repo_ci_meta("o/r", run=fake_run)
    assert branch == "master" and required == set()


def test_workflow_texts_decodes_contents():
    import base64
    listing = [{"path": ".github/workflows/ci.yml", "type": "file"}]
    content = {"content": base64.b64encode(b"name: CI\n").decode(), "encoding": "base64"}
    def fake_run(args, token=None):
        # Directory listing vs file read — both URLs end in `?ref=master`.
        is_listing = "contents/.github/workflows?" in args[1]
        return _ok(listing if is_listing else content)
    assert workflow_texts("o/r", "master", run=fake_run) == {
        ".github/workflows/ci.yml": "name: CI\n"}
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_actions_api.py -q`
Expected: FAIL — `ModuleNotFoundError: server.github.actions_api`

- [ ] **Step 3: Implement `server/github/actions_api.py`**

```python
"""GitHub Actions reads for the CI/CD Doctor. Sync (the loop wraps them in
asyncio.to_thread). Actions data has no GraphQL API — REST via gh is the
documented exception to the GraphQL-for-reads norm. Every function accepts an
injected runner so tests never touch the network."""
from __future__ import annotations

import io
import json
import logging
import subprocess
import zipfile
from datetime import datetime, timezone

from ccswitch import gitrepos

log = logging.getLogger(__name__)


def _run_gh_bytes(args: list[str]) -> bytes:
    """gh with binary stdout (artifact zips). Same env discipline as _run_gh."""
    proc = subprocess.run(["gh", *args], capture_output=True, env=gitrepos._gh_env())
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.decode(errors="replace").strip() or "gh failed")
    return proc.stdout


def _iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _json(proc) -> dict | list:
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api failed")
    return json.loads(proc.stdout)


def list_completed_runs(repo: str, since_ts: float | None, *,
                        run=gitrepos._run_gh) -> list[dict]:
    date = ""
    if since_ts:
        date = "&created=>=" + datetime.fromtimestamp(
            since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    data = _json(run(["api", f"repos/{repo}/actions/runs"
                             f"?status=completed&per_page=100{date}"]))
    out = []
    for r in data.get("workflow_runs", []):
        created = _iso_ts(r.get("created_at")) or 0.0
        if since_ts and created <= since_ts:
            continue
        started, updated = _iso_ts(r.get("run_started_at")), _iso_ts(r.get("updated_at"))
        out.append({
            "id": r["id"], "workflow_path": r.get("path") or "",
            "event": r.get("event") or "", "head_sha": r.get("head_sha") or "",
            "head_branch": r.get("head_branch") or "",
            "conclusion": r.get("conclusion") or "", "created_ts": created,
            "duration_s": max(0.0, (updated - started)) if started and updated else 0.0,
        })
    out.sort(key=lambda r: r["created_ts"])
    return out


def list_jobs(repo: str, run_id: int, *, run=gitrepos._run_gh) -> list[dict]:
    data = _json(run(["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]))
    out = []
    for j in data.get("jobs", []):
        s, c = _iso_ts(j.get("started_at")), _iso_ts(j.get("completed_at"))
        out.append({"id": j["id"], "name": j.get("name") or "",
                    "conclusion": j.get("conclusion") or "",
                    "duration_s": max(0.0, (c - s)) if s and c else 0.0})
    return out


def junit_artifacts(repo: str, run_id: int, *, run=gitrepos._run_gh,
                    run_bytes=_run_gh_bytes) -> list[tuple[str, bytes]]:
    """(artifact name, xml bytes) for every junit-* artifact of the run.
    Contract: Plan A names all JUnit artifacts with the junit- prefix."""
    data = _json(run(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts"]))
    out: list[tuple[str, bytes]] = []
    for a in data.get("artifacts", []):
        if not str(a.get("name", "")).startswith("junit") or a.get("expired"):
            continue
        try:
            blob = run_bytes(["api", f"repos/{repo}/actions/artifacts/{a['id']}/zip"])
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for member in z.namelist():
                    if member.endswith(".xml"):
                        out.append((a["name"], z.read(member)))
        except Exception:
            log.warning("cicd.artifact_fetch_failed repo=%s run=%s artifact=%s",
                        repo, run_id, a.get("name"), exc_info=True)
    return out


def repo_ci_meta(repo: str, *, run=gitrepos._run_gh) -> tuple[str, set[str]]:
    """(default branch, required status-check contexts). Branch-protection reads
    need admin scope — degrade to an empty set (verdicts then skip demotion)."""
    branch = "master"
    try:
        meta = _json(run(["api", f"repos/{repo}"]))
        branch = meta.get("default_branch") or "master"
    except Exception:
        log.warning("cicd.repo_meta_failed repo=%s", repo, exc_info=True)
    required: set[str] = set()
    try:
        data = _json(run(["api",
                          f"repos/{repo}/branches/{branch}/protection/required_status_checks"]))
        required = set(data.get("contexts") or [])
    except Exception:
        log.debug("cicd.protection_unreadable repo=%s", repo, exc_info=True)
    return branch, required


def workflow_texts(repo: str, default_branch: str, *, run=gitrepos._run_gh) -> dict[str, str]:
    import base64
    try:
        listing = _json(run(["api",
                             f"repos/{repo}/contents/.github/workflows?ref={default_branch}"]))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for item in listing if isinstance(listing, list) else []:
        path = item.get("path", "")
        if item.get("type") != "file" or not path.endswith((".yml", ".yaml")):
            continue
        try:
            content = _json(run(["api", f"repos/{repo}/contents/{path}?ref={default_branch}"]))
            out[path] = base64.b64decode(content.get("content") or "").decode(errors="replace")
        except Exception:
            log.debug("cicd.workflow_read_failed repo=%s path=%s", repo, path, exc_info=True)
    return out
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_actions_api.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/actions_api.py server/tests/test_cicd_actions_api.py
git commit -m "feat(cicd-doctor): GitHub Actions ingest seams (runs/jobs/artifacts/protection)"
```

---

### Task 2: ci-doctor issue reporter

**Files:**
- Create: `server/github/ci_issues.py`
- Test: `server/tests/test_cicd_issues.py`

**Interfaces:**
- Produces: `create_issue(repo, title, body, *, run=...) -> int | None` (ensures the `ci-doctor` label exists first, then files; returns issue number or None on failure); `close_issue(repo, number, *, run=...) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
"""ci-doctor issue lifecycle via injected gh runner."""
from __future__ import annotations

import json
from types import SimpleNamespace

from server.github.ci_issues import close_issue, create_issue


def test_create_ensures_label_then_files():
    calls = []
    def fake_run(args, token=None):
        calls.append(args)
        if args[1].endswith("/labels"):
            return SimpleNamespace(returncode=1, stdout="", stderr="already_exists")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"number": 77}), stderr="")
    assert create_issue("o/r", "t", "b", run=fake_run) == 77
    assert calls[0][1] == "repos/o/r/labels"          # ensure-label first
    assert calls[1][1] == "repos/o/r/issues"
    assert "labels[]=ci-doctor" in " ".join(calls[1])


def test_create_failure_returns_none():
    def fake_run(args, token=None):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")
    assert create_issue("o/r", "t", "b", run=fake_run) is None


def test_close_issue():
    calls = []
    def fake_run(args, token=None):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    assert close_issue("o/r", 77, run=fake_run) is True
    assert "repos/o/r/issues/77" in calls[0] and "-X" in calls[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_issues.py -q`
Expected: FAIL — `ModuleNotFoundError: server.github.ci_issues`

- [ ] **Step 3: Implement `server/github/ci_issues.py`**

```python
"""File/close ci-doctor GitHub issues (REST mutations — no GraphQL path in gh).
Best-effort: a failed create returns None; the loop retries next tick because
the finding row keeps issue_number NULL."""
from __future__ import annotations

import json
import logging

from ccswitch import gitrepos

log = logging.getLogger(__name__)

LABEL = "ci-doctor"


def _ensure_label(repo: str, run) -> None:
    # 422 already_exists is the steady state; any failure is non-fatal.
    run(["api", f"repos/{repo}/labels", "-f", f"name={LABEL}",
         "-f", "color=1d76db", "-f", "description=Filed by the ccswitch CI/CD Doctor"])


def create_issue(repo: str, title: str, body: str, *,
                 run=gitrepos._run_gh) -> int | None:
    try:
        _ensure_label(repo, run)
        proc = run(["api", f"repos/{repo}/issues", "-f", f"title={title}",
                    "-f", f"body={body}", "-f", f"labels[]={LABEL}"])
        if proc.returncode != 0:
            log.warning("cicd.issue_create_failed repo=%s: %s", repo, proc.stderr.strip())
            return None
        return int(json.loads(proc.stdout)["number"])
    except Exception:
        log.warning("cicd.issue_create_failed repo=%s", repo, exc_info=True)
        return None


def close_issue(repo: str, number: int, *, run=gitrepos._run_gh) -> bool:
    try:
        proc = run(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
                    "-f", "state=closed"])
        return proc.returncode == 0
    except Exception:
        log.warning("cicd.issue_close_failed repo=%s #%s", repo, number, exc_info=True)
        return False
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_issues.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/ci_issues.py server/tests/test_cicd_issues.py
git commit -m "feat(cicd-doctor): ci-doctor issue reporter"
```

---

### Task 3: CicdDoctor automation class + registration

**Files:**
- Create: `server/automations/cicd_doctor.py` (class only; the loop is Task 4 in the same file)
- Modify: `server/automations/registry.py` (import + register)
- Test: `server/tests/test_cicd_doctor_automation.py`

**Interfaces:**
- Produces: `CicdDoctor` with `id="cicd-doctor"`, `modes=("off","propose")`, the config schema below, `status(ctx)` reading `ctx.cicd_doctor_counts`. Registered so `GET /api/automations` lists it (schema-driven card → zero frontend work).

- [ ] **Step 1: Write the failing tests**

```python
"""CicdDoctor identity, config schema, status rendering, registration."""
from __future__ import annotations

from types import SimpleNamespace

from server.automations.base import validate_config
from server.automations.cicd_doctor import CicdDoctor
from server.automations.registry import registry


def test_registered_with_stable_slug():
    assert registry.get("cicd-doctor").__class__ is CicdDoctor


def test_config_schema_validates_defaults():
    cfg = validate_config(CicdDoctor.config_schema, {})
    assert cfg["min_window_days"] == 21 and cfg["min_runs"] == 30
    assert cfg["max_flake_pct"] == 2 and cfg["max_p95_minutes"] == 10
    assert cfg["demote_flake_pct"] == 5 and cfg["demote_window_days"] == 7
    assert cfg["retire_days"] == 60 and cfg["flaky_test_min_pct"] == 2
    assert cfg["retention_days"] == 90
    assert cfg["file_issues"] is True and cfg["enabled_repos"] == []


def test_status_levels():
    d = CicdDoctor()
    assert d.status(None).level == "idle"
    ctx = SimpleNamespace(cicd_doctor_counts={"open": 3, "candidates": 1})
    st = d.status(ctx)
    assert st.level == "active" and "3 open" in st.text and "1 graduation" in st.text
    quiet = SimpleNamespace(cicd_doctor_counts={"open": 0, "candidates": 0})
    assert d.status(quiet).level == "idle"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_doctor_automation.py -q`
Expected: FAIL — `ModuleNotFoundError: server.automations.cicd_doctor`

- [ ] **Step 3: Implement the class (top of `server/automations/cicd_doctor.py`)**

```python
"""The cicd-doctor automation: system-level CI health — flaky tests, advisory-
check graduation, waste. Proactive/per-system, where pr-doctor is reactive/
per-PR (it never touches an individual PR). v1 is propose-only: every verdict
is a finding row + optional ci-doctor GitHub issue, never a mutation."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)


class CicdDoctor:
    id = "cicd-doctor"
    name = "CI/CD Doctor"
    description = ("Watches CI health across repos: flaky tests, advisory-check "
                   "stability and graduation, and workflow waste.")
    modes: tuple[str, ...] = ("off", "propose")
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit to these repos (empty = all ccswitch-managed)."),
        ConfigField(key="file_issues", label="File GitHub issues", type="bool", default=True,
                    help="Actionable findings become ci-doctor labeled issues."),
        ConfigField(key="min_window_days", label="Graduation window (days)", type="int",
                    default=21),
        ConfigField(key="min_runs", label="Graduation min runs", type="int", default=30),
        ConfigField(key="max_flake_pct", label="Graduation max flake %", type="int", default=2),
        ConfigField(key="max_p95_minutes", label="Graduation max p95 (min)", type="int",
                    default=10),
        ConfigField(key="demote_flake_pct", label="Demotion flake %", type="int", default=5,
                    help="Sustained over the demotion window (hysteresis vs graduation)."),
        ConfigField(key="demote_window_days", label="Demotion window (days)", type="int",
                    default=7),
        ConfigField(key="retire_days", label="Retirement age (days)", type="int", default=60),
        ConfigField(key="flaky_test_min_pct", label="Flaky-test threshold %", type="int",
                    default=2),
        ConfigField(key="retention_days", label="History retention (days)", type="int",
                    default=90),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "cicd_doctor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        open_n, cand = counts.get("open", 0), counts.get("candidates", 0)
        bits = [f"{open_n} open finding(s)"]
        if cand:
            bits.append(f"{cand} graduation candidate(s)")
        return AutomationStatus(text=", ".join(bits),
                                level="active" if open_n else "idle", counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...
```

- [ ] **Step 4: Register in `server/automations/registry.py`**

Replace the registration block at the bottom:

```python
from .pr_doctor import PrDoctor  # noqa: E402  (register the built-in automations)
from .backlog_runner import BacklogRunner  # noqa: E402
from .autopilot import Autopilot  # noqa: E402
from .cicd_doctor import CicdDoctor  # noqa: E402

registry.register(PrDoctor())
registry.register(BacklogRunner())
registry.register(Autopilot())
registry.register(CicdDoctor())
```

- [ ] **Step 5: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_doctor_automation.py server/tests/test_automations_api.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS (including the existing automations-API tests — the new registration must not break their expected list; if a test pins the exact automation set, extend it to include `cicd-doctor`).

- [ ] **Step 6: Commit**

```bash
git add server/automations/cicd_doctor.py server/automations/registry.py server/tests/test_cicd_doctor_automation.py
git commit -m "feat(cicd-doctor): automation identity, config schema, registration"
```

---

### Task 4: CicdDoctorLoop (ingest → analyze → reconcile)

**Files:**
- Modify: `server/automations/cicd_doctor.py` (append the loop)
- Test: `server/tests/test_cicd_doctor_loop.py`

**Interfaces:**
- Consumes: Plan B engine (`parse_junit_xml`, `test_flake_pcts`, `check_stats`, `check_verdict`, `workflow_waste_findings`, `flaky_test_findings`, `check_transition_findings`, `missing_junit_finding`, `GateThresholds`, `TestObs`, `JobObs`), models `CiRun/CiJob/CiTestResult/CiFinding`, `AutomationContext` (cursor state under target `f"cursor:{repo}"`).
- Produces: `CicdDoctorLoop(ctx, *, read_config, list_repos, fetch_runs, fetch_jobs, fetch_junit, fetch_repo_meta, fetch_workflows, file_issue, close_issue, clock)` with async `tick()`. Seam signatures (all async): `read_config() -> tuple[str, dict]`; `list_repos(config) -> list[str]`; `fetch_runs(repo, since_ts) -> list[dict]`; `fetch_jobs(repo, run_id) -> list[dict]`; `fetch_junit(repo, run_id) -> list[tuple[str, bytes]]`; `fetch_repo_meta(repo) -> tuple[str, set[str]]`; `fetch_workflows(repo, default_branch) -> dict[str, str]`; `file_issue(repo, title, body) -> int | None`; `close_issue(repo, number) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
"""CicdDoctorLoop — driven entirely through fakes (no network)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from server.automations.cicd_doctor import CicdDoctorLoop
from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import CiFinding, CiTestResult

JUNIT_FLAKY = b"""<testsuite><testcase classname="t" name="flaky" time="1">
<failure message="boom"/></testcase></testsuite>"""
JUNIT_PASS = b'<testsuite><testcase classname="t" name="flaky" time="1"/></testsuite>'


def _runs(n, sha="s1", ts=1000.0):
    return [{"id": i, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": sha, "head_branch": "feat", "conclusion": "success",
             "created_ts": ts + i, "duration_s": 60.0} for i in range(1, n + 1)]


def _loop(ctx, *, runs, junit_by_run, mode="propose", config=None, issues=None):
    issues = issues if issues is not None else []
    async def read_config():
        return mode, {"enabled_repos": [], "file_issues": True, "min_window_days": 21,
                      "min_runs": 30, "max_flake_pct": 2, "max_p95_minutes": 10,
                      "demote_flake_pct": 5, "demote_window_days": 7, "retire_days": 60,
                      "flaky_test_min_pct": 2, "retention_days": 90, **(config or {})}
    async def fetch_junit(repo, run_id):
        return junit_by_run.get(run_id, [])
    return CicdDoctorLoop(
        ctx,
        read_config=read_config,
        list_repos=lambda config: _async(["o/r"]),
        fetch_runs=lambda repo, since: _async([r for r in runs
                                               if not since or r["created_ts"] > since]),
        fetch_jobs=lambda repo, rid: _async([{"id": rid * 10, "name": "e2e",
                                              "conclusion": "success", "duration_s": 60.0}]),
        fetch_junit=fetch_junit,
        fetch_repo_meta=lambda repo: _async(("master", {"lint-and-test"})),
        fetch_workflows=lambda repo, br: _async({}),
        file_issue=lambda repo, t, b: _async(issues.append(t) or 42),
        close_issue=lambda repo, n: _async(True),
        clock=lambda: 2000.0,
    )


async def _async(v):
    return v


def test_mode_off_ingests_nothing(tmp_path):
    async def run():
        await init_db()
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(2), junit_by_run={}, mode="off")
        await loop.tick()
        async with db_session() as db:
            assert (await db.execute(select(CiTestResult))).scalars().all() == []
    asyncio.run(run())


def test_ingest_is_cursor_idempotent(tmp_path):
    async def run():
        await init_db()
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(2),
                     junit_by_run={1: [("junit-python", JUNIT_PASS)]})
        await loop.tick()
        await loop.tick()  # second tick: cursor skips both runs
        async with db_session() as db:
            rows = (await db.execute(select(CiTestResult))).scalars().all()
            assert len(rows) == 1 and rows[0].test_id == "t::flaky"
        cursor = await ctx.get_target_state("cursor:o/r")
        assert cursor["last_created_ts"] == 1002.0
    asyncio.run(run())


def test_flaky_test_becomes_finding_and_issue(tmp_path):
    async def run():
        await init_db()
        ctx = AutomationContext("cicd-doctor")
        issues: list[str] = []
        # Evidence volume matters: flaky_test_findings needs >=3 distinct shas.
        # sha s1 both fails (run 1) and passes (run 2); s2 and s3 are clean
        # -> 33.3% flaky across 3 shas, above the 2% threshold.
        runs = [
            {"id": 1, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1001.0, "duration_s": 60.0},
            {"id": 2, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1002.0, "duration_s": 60.0},
            {"id": 3, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s2", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1003.0, "duration_s": 60.0},
            {"id": 4, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s3", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1004.0, "duration_s": 60.0},
        ]
        loop = _loop(ctx, runs=runs,
                     junit_by_run={1: [("junit-python", JUNIT_FLAKY)],
                                   2: [("junit-python", JUNIT_PASS)],
                                   3: [("junit-python", JUNIT_PASS)],
                                   4: [("junit-python", JUNIT_PASS)]},
                     issues=issues)
        await loop.tick()
        async with db_session() as db:
            f = (await db.execute(select(CiFinding).where(
                CiFinding.kind == "flaky_test"))).scalars().one()
            assert f.status == "open" and f.issue_number == 42
        assert any("flaky" in t for t in issues)
        assert ctx.cicd_doctor_counts["open"] >= 1
    asyncio.run(run())


def test_non_reentrant(tmp_path):
    async def run():
        await init_db()
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(1), junit_by_run={})
        await asyncio.gather(loop.tick(), loop.tick())  # must not double-ingest
        async with db_session() as db:
            from server.db.models import CiRun
            assert len((await db.execute(select(CiRun))).scalars().all()) == 1
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_doctor_loop.py -q`
Expected: FAIL — `ImportError: cannot import name 'CicdDoctorLoop'`

- [ ] **Step 3: Append the loop to `server/automations/cicd_doctor.py`**

```python
class CicdDoctorLoop:
    """Restart-safe, non-reentrant sense+advise loop. Every I/O dependency is
    injected. Per tick and per repo: ingest new completed runs (cursor in
    automation_target_state), run the pure analyzers over stored history, then
    reconcile findings (open/update/resolve + file/close ci-doctor issues)."""

    def __init__(self, ctx, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 list_repos: Callable[[dict], Awaitable[list[str]]],
                 fetch_runs: Callable[[str, float | None], Awaitable[list[dict]]],
                 fetch_jobs: Callable[[str, int], Awaitable[list[dict]]],
                 fetch_junit: Callable[[str, int], Awaitable[list[tuple[str, bytes]]]],
                 fetch_repo_meta: Callable[[str], Awaitable[tuple[str, set[str]]]],
                 fetch_workflows: Callable[[str, str], Awaitable[dict[str, str]]],
                 file_issue: Callable[[str, str, str], Awaitable[int | None]],
                 close_issue: Callable[[str, int], Awaitable[bool]],
                 clock: Callable[[], float]) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._list_repos = list_repos
        self._fetch_runs = fetch_runs
        self._fetch_jobs = fetch_jobs
        self._fetch_junit = fetch_junit
        self._fetch_repo_meta = fetch_repo_meta
        self._fetch_workflows = fetch_workflows
        self._file_issue = file_issue
        self._close_issue = close_issue
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:  # a tick must never crash the loop
                log.warning("cicd_doctor.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        open_total = candidates = 0
        for repo in await self._list_repos(config):
            try:
                await self._ingest(repo)
                fresh = await self._analyze(repo, config)
                o, c = await self._reconcile(repo, fresh, config)
                open_total += o
                candidates += c
            except Exception:  # one repo must never stall the tick
                log.warning("cicd_doctor.repo_failed repo=%s", repo, exc_info=True)
        await self._prune(config)
        setattr(self._ctx, "cicd_doctor_counts",
                {"open": open_total, "candidates": candidates})

    async def _ingest(self, repo: str) -> None:
        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import CiJob, CiRun, CiTestResult
        from .cicd.junit import parse_junit_xml

        cursor = await self._ctx.get_target_state(f"cursor:{repo}")
        since = cursor.get("last_created_ts")
        runs = await self._fetch_runs(repo, since)
        if not runs:
            return
        max_ts = since or 0.0
        async with db_session() as db:
            seen = set((await db.execute(select(CiRun.id).where(
                CiRun.repo == repo))).scalars().all())
            for r in runs:
                max_ts = max(max_ts, r["created_ts"])
                if r["id"] in seen:
                    continue
                db.add(CiRun(id=r["id"], repo=repo, workflow_path=r["workflow_path"],
                             event=r["event"], head_sha=r["head_sha"],
                             head_branch=r["head_branch"], conclusion=r["conclusion"],
                             created_ts=r["created_ts"], duration_s=r["duration_s"]))
                for j in await self._fetch_jobs(repo, r["id"]):
                    db.add(CiJob(id=j["id"], run_id=r["id"], repo=repo, name=j["name"],
                                 conclusion=j["conclusion"], duration_s=j["duration_s"],
                                 head_sha=r["head_sha"], head_branch=r["head_branch"],
                                 created_ts=r["created_ts"]))
                for suite, xml in await self._fetch_junit(repo, r["id"]):
                    try:
                        cases = parse_junit_xml(xml)
                    except ValueError:
                        log.warning("cicd_doctor.junit_malformed repo=%s run=%s",
                                    repo, r["id"])
                        continue
                    for c in cases:
                        db.add(CiTestResult(repo=repo, run_id=r["id"], suite=suite,
                                            test_id=c.test_id, outcome=c.outcome,
                                            time_s=c.time_s, head_sha=r["head_sha"],
                                            head_branch=r["head_branch"],
                                            created_ts=r["created_ts"]))
            await db.flush()
        await self._ctx.set_target_state(f"cursor:{repo}", {"last_created_ts": max_ts})

    async def _analyze(self, repo: str, config: dict) -> list:
        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import CiJob, CiRun, CiTestResult
        from .cicd.findings import (check_transition_findings, flaky_test_findings,
                                    missing_junit_finding)
        from .cicd.gates import check_verdict
        from .cicd.stats import check_stats, test_flake_pcts
        from .cicd.types import GateThresholds, JobObs, TestObs
        from .cicd.waste import workflow_waste_findings

        th = GateThresholds.from_config(config)
        async with db_session() as db:
            jobs = (await db.execute(select(CiJob).where(CiJob.repo == repo))).scalars().all()
            tests = (await db.execute(select(CiTestResult).where(
                CiTestResult.repo == repo))).scalars().all()
            run_count = len((await db.execute(select(CiRun.id).where(
                CiRun.repo == repo))).scalars().all())
        default_branch, required = await self._fetch_repo_meta(repo)
        now = self._clock()
        job_obs = [JobObs(check=j.name or "", head_sha=j.head_sha or "",
                          head_branch=j.head_branch or "", conclusion=j.conclusion or "",
                          duration_s=j.duration_s or 0.0, created_ts=j.created_ts or 0.0)
                   for j in jobs]
        test_obs = [TestObs(test_id=t.test_id or "", head_sha=t.head_sha or "",
                            head_branch=t.head_branch or "", outcome=t.outcome or "")
                    for t in tests]
        stats = check_stats(job_obs, default_branch=default_branch, now_ts=now,
                            demote_window_days=th.demote_window_days)
        verdicts = {c: v for c, s in stats.items()
                    if (v := check_verdict(s, is_required=c in required, th=th))}
        fresh = flaky_test_findings(repo, test_flake_pcts(test_obs), th=th)
        fresh += check_transition_findings(repo, verdicts)
        for path, text in (await self._fetch_workflows(repo, default_branch)).items():
            fresh += workflow_waste_findings(repo, path, text)
        if run_count and not tests:
            fresh.append(missing_junit_finding(repo))
        return fresh

    async def _reconcile(self, repo: str, fresh: list, config: dict) -> tuple[int, int]:
        import json as _json
        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import CiFinding
        from server.db.models import _utcnow  # single source for tz-aware now

        fresh_by_fp = {f.fingerprint: f for f in fresh}
        async with db_session() as db:
            open_rows = (await db.execute(select(CiFinding).where(
                CiFinding.repo == repo, CiFinding.status == "open"))).scalars().all()
            open_by_fp = {r.fingerprint: r for r in open_rows}
            for fp, f in fresh_by_fp.items():
                row = open_by_fp.get(fp)
                if row is None:
                    row = CiFinding(repo=repo, kind=f.kind, fingerprint=fp,
                                    status="open", title=f.title, detail=f.detail)
                    db.add(row)
                    await db.flush()
                    await self._ctx.emit("info", f"{repo}: {f.title}", kind="detected",
                                         target=fp, detail=f.detail)
                    if config.get("file_issues") and f.actionable:
                        body = (f"{f.title}\n\n```json\n"
                                f"{_json.dumps(f.detail, indent=2)}\n```\n\n"
                                "Filed by the ccswitch CI/CD Doctor (propose mode).")
                        row.issue_number = await self._file_issue(repo, f.title, body)
                else:
                    row.title, row.detail, row.last_seen_at = f.title, f.detail, _utcnow()
            for fp, row in open_by_fp.items():
                if fp not in fresh_by_fp:
                    row.status, row.resolved_at = "resolved", _utcnow()
                    await self._ctx.emit("info", f"{repo}: resolved — {row.title}",
                                         kind="recovered", target=fp)
                    if row.issue_number:
                        await self._close_issue(repo, row.issue_number)
            await db.flush()
            open_n = sum(1 for fp in fresh_by_fp)
            candidates = sum(1 for f in fresh_by_fp.values() if f.kind == "graduate_check")
        return open_n, candidates

    async def _prune(self, config: dict) -> None:
        """Bounded tables: drop history rows older than retention_days."""
        from sqlalchemy import delete
        from server.db import db_session
        from server.db.models import CiJob, CiRun, CiTestResult

        horizon = self._clock() - float(config.get("retention_days", 90)) * 86400.0
        async with db_session() as db:
            for model in (CiRun, CiJob, CiTestResult):
                await db.execute(delete(model).where(model.created_ts < horizon))
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_doctor_loop.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/cicd_doctor.py server/tests/test_cicd_doctor_loop.py
git commit -m "feat(cicd-doctor): ingest/analyze/reconcile control loop"
```

---

### Task 5: Orchestrator wiring

**Files:**
- Modify: `server/sessions/orchestrator.py` — loop construction in `__init__` (beside `_autopilot_loop`), tick task in `start()`, cancel in `stop()` (mirror `_backlog_task` / `_autopilot_task` handling exactly), `_cicd_*` seam methods, and the `automation_statuses()` ctx map.
- Test: `server/tests/test_cicd_orchestrator_wiring.py`

**Interfaces:**
- Consumes: `CicdDoctorLoop`, `actions_api`, `ci_issues`, `AutomationContext`.
- Produces: `self._cicd_ctx`, `self._cicd_loop`, `self._cicd_task`; `_cicd_read_config() -> tuple[str, dict]`; `_cicd_list_repos(config) -> list[str]`; `_cicd_tick_loop(interval: float)` / `_cicd_tick_once()` with the enabled-only `last_active_at` heartbeat (mirror `_backlog_tick_once`).

- [ ] **Step 1: Write the failing tests**

```python
"""Orchestrator wiring: config seam, repo listing, status ctx map."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import Automation, Session as DbSession


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator.__new__(SessionOrchestrator)  # no full init needed


def test_read_config_off_unless_enabled_with_mode(tmp_path):
    async def run():
        await init_db()
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        mode, config = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "off" and config["min_runs"] == 30
        async with db_session() as db:
            db.add(Automation(id="cicd-doctor", enabled=True, mode="propose",
                              config={"min_runs": 10}))
            await db.flush()
        mode, config = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "propose" and config["min_runs"] == 10
    asyncio.run(run())


def test_list_repos_config_overrides_managed(tmp_path):
    async def run():
        await init_db()
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp",
                             repo="o/managed"))
            await db.flush()
        assert await SessionOrchestrator._cicd_list_repos(orch, {"enabled_repos": []}) \
            == ["o/managed"]
        assert await SessionOrchestrator._cicd_list_repos(
            orch, {"enabled_repos": ["o/pinned"]}) == ["o/pinned"]
    asyncio.run(run())


def test_automation_statuses_includes_cicd_ctx():
    from server.sessions import orchestrator as orch_mod
    import inspect
    src = inspect.getsource(orch_mod.SessionOrchestrator.automation_statuses)
    assert '"cicd-doctor"' in src  # ctx map entry — badge must not be stuck on idle
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_orchestrator_wiring.py -q`
Expected: FAIL — `AttributeError: _cicd_read_config`

- [ ] **Step 3: Wire the orchestrator**

In `__init__`, directly after the `self._autopilot_task: asyncio.Task | None = None` line:

```python
        # CI/CD Doctor control loop: system-level CI health (flaky tests, check
        # graduation, waste). Real seams shell out to gh via asyncio.to_thread.
        from server.automations.cicd_doctor import CicdDoctorLoop
        from server.github import actions_api, ci_issues
        self._cicd_ctx = AutomationContext("cicd-doctor")
        self._cicd_loop = CicdDoctorLoop(
            self._cicd_ctx,
            read_config=self._cicd_read_config,
            list_repos=self._cicd_list_repos,
            fetch_runs=lambda repo, since: asyncio.to_thread(
                actions_api.list_completed_runs, repo, since),
            fetch_jobs=lambda repo, rid: asyncio.to_thread(
                actions_api.list_jobs, repo, rid),
            fetch_junit=lambda repo, rid: asyncio.to_thread(
                actions_api.junit_artifacts, repo, rid),
            fetch_repo_meta=lambda repo: asyncio.to_thread(
                actions_api.repo_ci_meta, repo),
            fetch_workflows=lambda repo, branch: asyncio.to_thread(
                actions_api.workflow_texts, repo, branch),
            file_issue=lambda repo, title, body: asyncio.to_thread(
                ci_issues.create_issue, repo, title, body),
            close_issue=lambda repo, n: asyncio.to_thread(
                ci_issues.close_issue, repo, n),
            clock=time.time,  # wall clock: created_ts from GitHub is epoch time
        )
        self._cicd_task: asyncio.Task | None = None
```

In `start()`, after the autopilot task creation:

```python
        # CI/CD Doctor: slow cadence — CI history moves in minutes, not seconds,
        # and each tick costs several REST calls per repo.
        self._cicd_task = asyncio.create_task(
            self._cicd_tick_loop(interval=900.0), name="cicd-doctor-loop"
        )
```

In `stop()`, cancel/await `self._cicd_task` exactly where and how `self._autopilot_task` is handled (same pattern, same ordering).

New methods (place beside `_backlog_read_config`):

```python
    async def _cicd_tick_loop(self, interval: float) -> None:
        while True:
            await self._cicd_tick_once()
            await asyncio.sleep(interval)

    async def _cicd_tick_once(self) -> None:
        """One tick + enabled-only last_active_at heartbeat (mirrors backlog)."""
        from datetime import datetime, timezone
        from sqlalchemy import select
        from server.db.models import Automation
        await self._cicd_loop.tick()
        try:
            mode, _ = await self._cicd_read_config()
            if mode != "off":
                async with db_session() as db:
                    row = (await db.execute(select(Automation).where(
                        Automation.id == "cicd-doctor"))).scalar_one_or_none()
                    if row is not None:
                        row.last_active_at = datetime.now(timezone.utc)
        except Exception:
            log.debug("cicd_doctor.heartbeat_failed", exc_info=True)

    async def _cicd_read_config(self) -> tuple[str, dict]:
        """(effective mode, validated config) for cicd-doctor; off + defaults on
        any bad row so a corrupt config can never act."""
        from sqlalchemy import select
        from server.automations.base import validate_config
        from server.automations.cicd_doctor import CicdDoctor
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "cicd-doctor"))).scalar_one_or_none()
                if row is not None:
                    enabled, mode = bool(row.enabled), row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("cicd_doctor.config.load_failed", exc_info=True)
        try:
            config = validate_config(CicdDoctor.config_schema, raw)
        except ValueError:
            config = validate_config(CicdDoctor.config_schema, {})
        return (mode if (enabled and mode == "propose") else "off"), config

    async def _cicd_list_repos(self, config: dict) -> list[str]:
        """enabled_repos config, or every distinct repo ccswitch manages."""
        from sqlalchemy import select
        pinned = config.get("enabled_repos") or []
        if pinned:
            return list(pinned)
        async with db_session() as db:
            rows = (await db.execute(
                select(DbSession.repo).where(DbSession.repo.is_not(None))
                .distinct())).scalars().all()
        return sorted(r for r in rows if r)
```

(`DbSession` is the orchestrator module's existing top-level alias — `from server.db.models import INACTIVE_SESSION_STATUSES, Session as DbSession` at `server/sessions/orchestrator.py:39`. `select`/`Automation` are imported locally inside seam methods, matching `_backlog_read_config`'s pattern.)

In `automation_statuses()`, extend the ctx map:

```python
        ctxs = {
            "pr-doctor": getattr(self, "_pr_doctor_ctx", None),
            "backlog-runner": getattr(self, "_backlog_ctx", None),
            "autopilot": getattr(self, "_autopilot_ctx", None),
            "cicd-doctor": getattr(self, "_cicd_ctx", None),
        }
```

(Adding `autopilot` here fixes a latent gap — its counts were stashed on a ctx the map never exposed; same change-shape, same PR.)

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_orchestrator_wiring.py server/tests/test_automation_startup.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS. (Local orchestrator-adjacent failures about daemon sockets/X11/OAuth are environmental noise — CI is the arbiter; the two named files must pass.)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_cicd_orchestrator_wiring.py
git commit -m "feat(cicd-doctor): orchestrator tick loop, seams, status wiring"
```

---

### Task 6: Findings API + PR

**Files:**
- Modify: `server/api/automations.py` (one new GET route)
- Test: `server/tests/test_cicd_findings_api.py`

**Interfaces:**
- Produces: `GET /api/automations/cicd-doctor/findings?status=open|resolved|all&repo=<nameWithOwner>` → list of `{id, repo, kind, title, status, detail, issueNumber, firstSeenAt, lastSeenAt}`. Reads `CiFinding` directly — deliberately NO orchestrator call (no RPC proxy needed).

- [ ] **Step 1: Write the failing test**

House pattern from `server/tests/test_automations_api.py`: a temp-FILE DB (an in-memory
SQLite gives each pooled connection its own empty DB — the known seed-visibility flake),
env pinned via `patch.dict`, app built with a fake-SDK orchestrator, seeding via
`asyncio.run` INSIDE the TestClient context (after init_db has run), auth header `H`.

```python
"""GET /api/automations/cicd-doctor/findings — filters + shape."""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


async def _seed():
    from server.db import db_session
    from server.db.models import CiFinding
    async with db_session() as db:
        db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="f1",
                         status="open", title="flaky", detail={"flake_pct": 8.0}))
        db.add(CiFinding(repo="o/r", kind="waste_double_run", fingerprint="f2",
                         status="resolved", title="waste", detail={}))
        await db.flush()


def test_findings_default_open_only_and_shape():
    with _client() as c:
        asyncio.run(_seed())
        body = c.get("/api/automations/cicd-doctor/findings", headers=H).json()
        assert [f["kind"] for f in body] == ["flaky_test"]
        f = body[0]
        assert f["status"] == "open" and f["issueNumber"] is None
        assert f["detail"] == {"flake_pct": 8.0} and f["firstSeenAt"]


def test_findings_status_and_repo_filters():
    with _client() as c:
        asyncio.run(_seed())
        assert len(c.get("/api/automations/cicd-doctor/findings?status=all",
                         headers=H).json()) == 2
        assert c.get("/api/automations/cicd-doctor/findings?repo=o/other",
                     headers=H).json() == []


def test_findings_requires_auth():
    with _client() as c:
        assert c.get("/api/automations/cicd-doctor/findings").status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_cicd_findings_api.py -q`
Expected: FAIL — 404 (route does not exist).

- [ ] **Step 3: Add the route to `server/api/automations.py`**

After the `backlog_runner_implement` route:

```python
@router.get("/cicd-doctor/findings")
async def cicd_doctor_findings(status: str = "open", repo: str | None = None,
                               _identity: str = Depends(current_identity),
                               db=Depends(get_db)) -> list[dict]:
    """CI/CD Doctor findings, newest first. Reads the DB directly — the loop's
    state is all persisted, so no orchestrator round-trip is needed."""
    from server.db.models import CiFinding
    _automation_or_404("cicd-doctor")
    stmt = select(CiFinding).order_by(CiFinding.last_seen_at.desc()).limit(200)
    if status != "all":
        stmt = stmt.where(CiFinding.status == status)
    if repo is not None:
        stmt = stmt.where(CiFinding.repo == repo)
    rows = (await db.execute(stmt)).scalars().all()
    return [{"id": f.id, "repo": f.repo, "kind": f.kind, "title": f.title,
             "status": f.status, "detail": f.detail or {},
             "issueNumber": f.issue_number,
             "firstSeenAt": f.first_seen_at.isoformat() if f.first_seen_at else None,
             "lastSeenAt": f.last_seen_at.isoformat() if f.last_seen_at else None}
            for f in rows]
```

- [ ] **Step 4: Run tests + gates**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_cicd_findings_api.py server/tests/test_automations_api.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit, push, PR**

```bash
git add server/api/automations.py server/tests/test_cicd_findings_api.py
git commit -m "feat(cicd-doctor): findings read API"
git push -u origin feat/cicd-doctor-loop
gh pr create --title "feat(cicd-doctor): control loop + integration (plan C)" --body "$(cat <<'EOF'
CI/CD Doctor plan C (spec: docs/superpowers/specs/2026-07-03-cicd-doctor-design.md).
Builds on plan B (engine) and plan A (JUnit artifacts).

- GitHub Actions ingest seams (runs/jobs/junit-* artifacts/branch protection/workflow texts)
- ci-doctor issue reporter (file on detect, close on resolve)
- CicdDoctor automation (off/propose) + schema-driven card (zero frontend changes)
- CicdDoctorLoop: cursor-idempotent ingest -> pure analyzers -> finding reconciliation
- Orchestrator wiring (15-min tick, heartbeat, status ctx map — also fixes the latent
  missing autopilot ctx-map entry)
- GET /api/automations/cicd-doctor/findings

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Expected: PR open, non-draft, auto-merge armed.

---

### Post-merge verification (manual, after all three plans land)

1. Enable the automation: dashboard → Automations → CI/CD Doctor → enabled + mode `propose` (or `PATCH /api/automations/cicd-doctor {"enabled": true, "mode": "propose"}`).
2. After ~15 min: `GET /api/automations/cicd-doctor/findings` returns rows; the card badge shows counts; `gh issue list --label ci-doctor` shows filed proposals.
3. Sanity-check the first findings against reality before trusting the analyzers (constitution III): the known flaky `test_batch_unarchive_partial_failure` should eventually surface with a measured rate.
4. File the one-time Lighthouse proposal (spec 1c — v1 deliberately has no "check you never had" analyzer):

```bash
gh issue create --repo steiner385/ccswitch --label ci-doctor \
  --title "ci-doctor: wire Lighthouse audit of the built dashboard as an advisory job" \
  --body "Proposed by the CI/CD Doctor rollout (spec docs/superpowers/specs/2026-07-03-cicd-doctor-design.md, Part 1c). Add a non-required Lighthouse CI job against the vite preview build; it enters the advisory tier and the Doctor tracks it toward graduation."
```
