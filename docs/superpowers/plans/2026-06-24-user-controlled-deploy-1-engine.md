# User-Controlled Deploy — Plan 1: Deploy Engine (port + retire script) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port every mechanical deploy step out of `scripts/auto-deploy.sh` into a testable Python `server.deploy` package, orchestrated by a `DeployController.deploy_once()`, and retire the bash script + its systemd timer/oneshot units — leaving deploys runnable via `python -m server.deploy --once`.

**Architecture:** A new `server/deploy/` package. Pure step functions (`server/deploy/steps.py`, `server/deploy/git.py`) each wrap one external action (gh, git, npm, systemctl, curl/httpx) behind an injectable callable so they unit-test without network or a live host. `DeployController` (`server/deploy/controller.py`) stitches them in the load-bearing order, holds a phase-status model and a single-deploy lock, and reuses the daemon's existing `begin_drain`/`drain_status` RPCs via `DaemonClient`. No UI, no detection loop, no session snapshot/restore — those are Plans 2 and 3.

**Tech Stack:** Python 3, asyncio, httpx (already a dep, used by `DaemonClient`), pytest, subprocess. No new dependencies.

## Global Constraints

- Constitution VI (single source of truth / no dead code): the bash script and its bats tests are **deleted in this plan**, not left beside the port.
- Never fork/patch the Claude binary (I); account = `CLAUDE_CONFIG_DIR` (II). N/A to this plan but do not regress.
- TDD + quality gates (V): every step function gets a failing test first. Backend gate = `ruff` + `mypy` + `pytest server/tests/`.
- Add new model columns nullable only (N/A here — no schema changes in Plan 1).
- Worktree backend tests run against the prebuilt env: `PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/...`.
- mypy skips untyped function bodies — annotate all new public function signatures.
- The following incident-proofed invariants from the script MUST be preserved verbatim in behavior (each has a task + test below): CI-gate fail-closed; `git rev-parse --absolute-git-dir` for state paths; daemon-first-then-edge restart; health gate asserts `status:ok` AND expected SHA; rollback resets to last-good SHA + rebuilds + restarts both units together; dep-sync only when lockfiles changed; working-tree guard before pull; edge-only allowlist classification (fail-safe daemon_changed); drain handshake (capped, force after cap); GitHub Deployment publish best-effort.

---

## File Structure

- Create `server/deploy/__init__.py` — package marker + re-exports (`DeployController`, `DeployStatus`, `DeployError`).
- Create `server/deploy/types.py` — `DeployError`, `Phase` enum, `PhaseState` enum, `DeployStatus` dataclass.
- Create `server/deploy/git.py` — git helpers: `abs_git_dir`, `current_sha`, `remote_sha`, `fetch`, `log_range`, `short_sha`.
- Create `server/deploy/steps.py` — `ci_verdict`, `classify_change`, `guard_working_tree`, `fast_forward`, `sync_deps_if_changed`, `build_frontend`, `restart_units`, `wait_healthy`, `rollback_to`, `publish_deployment`.
- Create `server/deploy/drain.py` — `drain_and_wait` (reuses `DaemonClient` begin_drain/drain_status).
- Create `server/deploy/controller.py` — `DeployController.deploy_once`, phase status model, single-deploy lock.
- Create `server/deploy/__main__.py` — `python -m server.deploy --once` CLI.
- Create tests: `server/tests/test_deploy_git.py`, `test_deploy_steps_cigate.py`, `test_deploy_steps_classify.py`, `test_deploy_steps_tree.py`, `test_deploy_steps_deps_build.py`, `test_deploy_steps_restart_health.py`, `test_deploy_steps_rollback.py`, `test_deploy_steps_publish.py`, `test_deploy_drain.py`, `test_deploy_controller.py`, `test_deploy_cli.py`.
- Modify `server/tests/test_deploy_classification.py` — retarget from script-text assertions to the Python classifier.
- Delete `scripts/auto-deploy.sh`, `scripts/tests/auto-deploy-*.bats`, `deploy/ccswitch-deploy.service`, `deploy/ccswitch-deploy.timer`.
- Modify `server/tests/test_deploy_daemon.py` — retarget the daemon-restart-ordering assertions to the Python `restart_units`.
- Modify `deploy/README.md` — document `python -m server.deploy --once`; remove timer/oneshot rows.

---

## Task 1: Package scaffold + types + git helpers

**Files:**
- Create: `server/deploy/__init__.py`, `server/deploy/types.py`, `server/deploy/git.py`
- Test: `server/tests/test_deploy_git.py`

**Interfaces:**
- Produces:
  - `class DeployError(Exception)` — carries `phase: str`, `detail: str`.
  - `git.abs_git_dir(repo: str) -> str` (runs `git -C repo rev-parse --absolute-git-dir`).
  - `git.current_sha(repo: str) -> str`; `git.remote_sha(repo: str, ref: str = "origin/master") -> str`; `git.short_sha(repo: str, sha: str) -> str`.
  - `git.fetch(repo: str, ref: str = "master") -> None`.
  - `git.log_range(repo: str, a: str, b: str) -> list[dict]` → `[{"sha","subject","author","iso"}]` via `git log a..b --format=...`.
  - Every git fn takes `run: Callable[..., subprocess.CompletedProcess] = subprocess.run` so tests inject a fake.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_git.py
import subprocess
from pathlib import Path
import pytest
from server.deploy import git


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def g(*a):
        subprocess.run(["git", "-C", str(r), *a], check=True,
                       capture_output=True, env={**__import__("os").environ, **env})
    g("init", "-q")
    (r / "a.txt").write_text("1")
    g("add", "."); g("commit", "-qm", "first")
    return r


def test_current_sha_is_40_hex(repo):
    sha = git.current_sha(str(repo))
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_abs_git_dir_points_into_repo(repo):
    assert git.abs_git_dir(str(repo)).startswith(str(repo))


def test_log_range_lists_new_commits(repo):
    base = git.current_sha(str(repo))
    (repo / "b.txt").write_text("2")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second body"], check=True,
                   capture_output=True,
                   env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})
    head = git.current_sha(str(repo))
    rows = git.log_range(str(repo), base, head)
    assert len(rows) == 1 and rows[0]["subject"] == "second body"
    assert rows[0]["sha"].startswith(head[:7])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_git.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.deploy'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class DeployError(Exception):
    def __init__(self, phase: str, detail: str):
        super().__init__(f"{phase}: {detail}")
        self.phase = phase
        self.detail = detail


class Phase(str, Enum):
    PREFLIGHT = "preflight"
    SAVE = "save"
    UPDATE = "update"
    REPORT = "report"
    RESTART = "restart"
    RESUME = "resume"


class PhaseState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class DeployStatus:
    target_sha: str
    running: bool = False
    phases: dict[str, str] = field(default_factory=dict)  # phase -> PhaseState value
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
```

```python
# server/deploy/git.py
from __future__ import annotations
import subprocess
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess]


def _out(repo: str, *args: str, run: Run = subprocess.run) -> str:
    r = run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)
    return r.stdout.strip()


def abs_git_dir(repo: str, run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", "--absolute-git-dir", run=run)


def current_sha(repo: str, run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", "@", run=run)


def remote_sha(repo: str, ref: str = "origin/master", run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", ref, run=run)


def short_sha(repo: str, sha: str, run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", "--short", sha, run=run)


def fetch(repo: str, ref: str = "master", run: Run = subprocess.run) -> None:
    run(["git", "-C", repo, "fetch", "--quiet", "origin", ref], check=True,
        capture_output=True, text=True)


def log_range(repo: str, a: str, b: str, run: Run = subprocess.run) -> list[dict]:
    fmt = "%H%x1f%s%x1f%an%x1f%cI"
    raw = _out(repo, "log", f"{a}..{b}", f"--format={fmt}", run=run)
    rows: list[dict] = []
    for line in raw.splitlines():
        if not line:
            continue
        sha, subject, author, iso = line.split("\x1f")
        rows.append({"sha": sha, "subject": subject, "author": author, "iso": iso})
    return rows
```

```python
# server/deploy/__init__.py
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState

__all__ = ["DeployError", "DeployStatus", "Phase", "PhaseState"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_git.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/__init__.py server/deploy/types.py server/deploy/git.py server/tests/test_deploy_git.py
git commit -m "feat(deploy): server.deploy package scaffold + git helpers"
```

---

## Task 2: CI gate (`ci_verdict`)

**Files:**
- Create: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_cigate.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `steps.ci_verdict(sha: str, gh_api: Callable[[str], str] | None = None) -> str` returning `"green" | "pending" | "red" | "unavailable"`. `gh_api(path)` returns the raw `check-runs` JSON string; default calls `gh api`. Mirrors the script's jq: 0 runs or any non-completed → pending; any conclusion not in {success,neutral,skipped} → red; else green; exception/None → unavailable.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_cigate.py
import json
from server.deploy import steps


def _runs(*concs, status="completed"):
    return json.dumps({"check_runs": [
        {"status": status, "conclusion": c} for c in concs]})


def test_all_success_is_green():
    assert steps.ci_verdict("s", gh_api=lambda p: _runs("success", "skipped")) == "green"


def test_zero_runs_is_pending():
    assert steps.ci_verdict("s", gh_api=lambda p: json.dumps({"check_runs": []})) == "pending"


def test_incomplete_run_is_pending():
    assert steps.ci_verdict("s", gh_api=lambda p: _runs("success", None, status="in_progress")) == "pending"


def test_failure_is_red():
    assert steps.ci_verdict("s", gh_api=lambda p: _runs("success", "failure")) == "red"


def test_exception_is_unavailable():
    def boom(p):
        raise RuntimeError("network")
    assert steps.ci_verdict("s", gh_api=boom) == "unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_cigate.py -v`
Expected: FAIL — `AttributeError: module 'server.deploy.steps' has no attribute 'ci_verdict'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/steps.py
from __future__ import annotations
import json
import subprocess
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess]
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def _default_gh_api(path: str) -> str:
    r = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return r.stdout


def ci_verdict(sha: str, gh_api: Callable[[str], str] | None = None) -> str:
    gh_api = gh_api or _default_gh_api
    try:
        body = json.loads(gh_api(f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs"))
    except Exception:
        return "unavailable"
    runs = body.get("check_runs") or []
    if not runs:
        return "pending"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    if any((r.get("conclusion") or "") not in _OK_CONCLUSIONS for r in runs):
        return "red"
    return "green"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_cigate.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_cigate.py
git commit -m "feat(deploy): port CI gate (fail-closed) to Python"
```

---

## Task 3: Change classification (`classify_change`)

**Files:**
- Modify: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_classify.py`

**Interfaces:**
- Produces: `steps.classify_change(changed_files: list[str] | None) -> str` returning `"edge_only"` or `"daemon_changed"`. `None` (diff uncomputable) → `"daemon_changed"` (fail-safe). Edge-only iff EVERY file matches the allowlist regex. Allowlist mirrors the script exactly: `^(web/|server/api/|server/app\.py|server/bridge/|server/spa\.py|server/tests/|docs/|deploy/|\.github/|.*\.md$)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_classify.py
from server.deploy import steps


def test_all_edge_files_is_edge_only():
    assert steps.classify_change(["web/src/App.tsx", "server/api/x.py", "docs/y.md"]) == "edge_only"


def test_shared_daemon_module_forces_daemon_changed():
    assert steps.classify_change(["web/src/App.tsx", "server/config.py"]) == "daemon_changed"


def test_orchestrator_change_is_daemon_changed():
    assert steps.classify_change(["server/sessions/orchestrator.py"]) == "daemon_changed"


def test_none_diff_fails_safe_to_daemon_changed():
    assert steps.classify_change(None) == "daemon_changed"


def test_app_py_is_edge_only_but_db_is_not():
    assert steps.classify_change(["server/app.py"]) == "edge_only"
    assert steps.classify_change(["server/db/models.py"]) == "daemon_changed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_classify.py -v`
Expected: FAIL — `AttributeError: ... 'classify_change'`.

- [ ] **Step 3: Write minimal implementation (append to `server/deploy/steps.py`)**

```python
import re

# Edge-only ALLOWLIST — mirrors scripts/auto-deploy.sh exactly. Anything NOT matching
# (incl. shared modules the daemon imports: server/config, server/db, server/audit,
# server/usage, the orchestrator, IPC, agent-SDK glue) forces a daemon restart.
_EDGE_ONLY = re.compile(
    r"^(web/|server/api/|server/app\.py|server/bridge/|server/spa\.py|"
    r"server/tests/|docs/|deploy/|\.github/|.*\.md$)"
)


def classify_change(changed_files: list[str] | None) -> str:
    if changed_files is None:
        return "daemon_changed"   # fail-safe: cannot classify → treat as daemon change
    if all(_EDGE_ONLY.match(f) for f in changed_files):
        return "edge_only"
    return "daemon_changed"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_classify.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_classify.py
git commit -m "feat(deploy): port edge-only/daemon classifier (fail-safe) to Python"
```

---

## Task 4: Working-tree guard + fast-forward (`guard_working_tree`, `fast_forward`)

**Files:**
- Modify: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_tree.py`

**Interfaces:**
- Produces:
  - `steps.guard_working_tree(repo: str, run: Run = subprocess.run) -> bool` — if `git diff --quiet HEAD --` is dirty, run `git reset --hard HEAD` and return `True` (discarded); else `False`. Untracked files untouched (no `-x`).
  - `steps.fast_forward(repo: str, ref: str = "master", run: Run = subprocess.run) -> None` — `git pull --ff-only origin <ref>`; raise `DeployError("update", ...)` on non-zero.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_tree.py
import os, subprocess
import pytest
from server.deploy import steps
from server.deploy.types import DeployError

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"; r.mkdir()
    def g(*a): subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, env=_ENV)
    g("init", "-q"); (r / "a.txt").write_text("1"); g("add", "."); g("commit", "-qm", "first")
    return r


def test_guard_discards_tracked_edit(repo):
    (repo / "a.txt").write_text("DIRTY")
    assert steps.guard_working_tree(str(repo)) is True
    assert (repo / "a.txt").read_text() == "1"


def test_guard_clean_tree_is_noop(repo):
    assert steps.guard_working_tree(str(repo)) is False


def test_fast_forward_failure_raises_deploy_error(repo):
    # No 'origin' remote → pull fails → DeployError(update).
    with pytest.raises(DeployError) as ei:
        steps.fast_forward(str(repo))
    assert ei.value.phase == "update"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_tree.py -v`
Expected: FAIL — `AttributeError: ... 'guard_working_tree'`.

- [ ] **Step 3: Write minimal implementation (append to `server/deploy/steps.py`)**

```python
from server.deploy.types import DeployError


def guard_working_tree(repo: str, run: Run = subprocess.run) -> bool:
    dirty = run(["git", "-C", repo, "diff", "--quiet", "HEAD", "--"]).returncode != 0
    if dirty:
        run(["git", "-C", repo, "reset", "--hard", "HEAD"], check=True, capture_output=True)
    return dirty


def fast_forward(repo: str, ref: str = "master", run: Run = subprocess.run) -> None:
    r = run(["git", "-C", repo, "pull", "--ff-only", "origin", ref],
            capture_output=True, text=True)
    if r.returncode != 0:
        raise DeployError("update", f"fast-forward pull failed: {r.stderr.strip()}")
```

Note: `guard_working_tree`'s first `run` call intentionally omits `check=True` (it inspects `returncode`).

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_tree.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_tree.py
git commit -m "feat(deploy): port working-tree guard + ff-only pull to Python"
```

---

## Task 5: Dep-sync + frontend build (`sync_deps_if_changed`, `build_frontend`)

**Files:**
- Modify: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_deps_build.py`

**Interfaces:**
- Produces:
  - `steps.changed_paths(repo, a, b, paths: list[str], run=subprocess.run) -> list[str]` — `git diff --name-only a b -- <paths>`.
  - `steps.sync_deps_if_changed(repo, a, b, *, venv_pip: str, run=subprocess.run) -> list[str]` — returns the list of dep actions taken (`["pip"]`, `["npm"]`, both, or `[]`). Runs `pip install -e .[server]` iff `pyproject.toml`/`uv.lock` changed; `npm ci` (cwd `web`) iff `web/package-lock.json` changed. Raises `DeployError("update", ...)` on failure.
  - `steps.build_frontend(repo: str, run=subprocess.run) -> None` — `npm run build` in `web`; raise `DeployError("update", ...)` on failure.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_deps_build.py
import subprocess
from types import SimpleNamespace
from server.deploy import steps
from server.deploy.types import DeployError
import pytest


def make_run(diff_map, fail_cmd=None):
    """diff_map: {path: changed?}. Fakes git-diff and records non-git commands."""
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:4] == ["git", "-C", cmd[2], "diff"]:
            # last args after '--' are the paths
            paths = cmd[cmd.index("--") + 1:]
            out = "\n".join(p for p in paths if diff_map.get(p))
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        rc = 1 if (fail_cmd and fail_cmd in cmd) else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")
    return run, calls


def test_no_lockfile_change_skips_dep_sync():
    run, calls = make_run({})
    assert steps.sync_deps_if_changed("/r", "a", "b", venv_pip="/v/pip", run=run) == []
    assert not any("install" in c for c in calls)


def test_pyproject_change_runs_pip():
    run, calls = make_run({"pyproject.toml": True})
    assert steps.sync_deps_if_changed("/r", "a", "b", venv_pip="/v/pip", run=run) == ["pip"]
    assert ["/v/pip", "install", "-e", ".[server]", "--quiet"] in calls


def test_npm_lock_change_runs_npm_ci():
    run, calls = make_run({"web/package-lock.json": True})
    assert steps.sync_deps_if_changed("/r", "a", "b", venv_pip="/v/pip", run=run) == ["npm"]


def test_pip_failure_raises():
    run, _ = make_run({"uv.lock": True}, fail_cmd="install")
    with pytest.raises(DeployError):
        steps.sync_deps_if_changed("/r", "a", "b", venv_pip="/v/pip", run=run)


def test_build_failure_raises():
    run, _ = make_run({}, fail_cmd="build")
    with pytest.raises(DeployError):
        steps.build_frontend("/r", run=run)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_deps_build.py -v`
Expected: FAIL — `AttributeError: ... 'sync_deps_if_changed'`.

- [ ] **Step 3: Write minimal implementation (append to `server/deploy/steps.py`)**

```python
import os


def changed_paths(repo: str, a: str, b: str, paths: list[str], run: Run = subprocess.run) -> list[str]:
    r = run(["git", "-C", repo, "diff", "--name-only", a, b, "--", *paths],
            capture_output=True, text=True)
    return [p for p in r.stdout.splitlines() if p.strip()]


def sync_deps_if_changed(repo: str, a: str, b: str, *, venv_pip: str,
                         run: Run = subprocess.run) -> list[str]:
    done: list[str] = []
    if changed_paths(repo, a, b, ["pyproject.toml", "uv.lock"], run=run):
        r = run([venv_pip, "install", "-e", ".[server]", "--quiet"],
                cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            raise DeployError("update", f"pip install failed: {r.stderr.strip()}")
        done.append("pip")
    if changed_paths(repo, a, b, ["web/package-lock.json"], run=run):
        r = run(["npm", "ci"], cwd=os.path.join(repo, "web"), capture_output=True, text=True)
        if r.returncode != 0:
            raise DeployError("update", f"npm ci failed: {r.stderr.strip()}")
        done.append("npm")
    return done


def build_frontend(repo: str, run: Run = subprocess.run) -> None:
    r = run(["npm", "run", "build"], cwd=os.path.join(repo, "web"),
            capture_output=True, text=True)
    if r.returncode != 0:
        raise DeployError("update", f"frontend build failed: {r.stderr.strip()}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_deps_build.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_deps_build.py
git commit -m "feat(deploy): port dep-sync (lockfile-gated) + frontend build to Python"
```

---

## Task 6: Restart + health gate (`restart_units`, `wait_healthy`)

**Files:**
- Modify: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_restart_health.py`

**Interfaces:**
- Produces:
  - `steps.restart_units(*, daemon_changed: bool, daemon_enabled: bool, run: Run = subprocess.run) -> list[str]` — restarts `ccswitch-daemon.service` FIRST (only when `daemon_changed and daemon_enabled`), THEN `ccswitch.service`. Returns the ordered list of units restarted. Order is load-bearing (browser reconnect-flush race).
  - `steps.wait_healthy(expected_sha: str, *, url: str, attempts: int = 15, sleep_s: float = 2.0, fetch: Callable[[str], str] | None = None, sleep: Callable[[float], None] = time.sleep) -> bool` — polls `url`; returns True once body contains `"status":"ok"` AND `"sha":"<expected_sha>"`. `fetch(url)` returns the body (default httpx GET); unreachable → empty string. `expected_sha` is the SHORT sha.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_restart_health.py
from types import SimpleNamespace
from server.deploy import steps


def test_daemon_restarts_before_edge():
    order = []
    def run(cmd, **kw):
        if "restart" in cmd:
            order.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    units = steps.restart_units(daemon_changed=True, daemon_enabled=True, run=run)
    assert units == ["ccswitch-daemon.service", "ccswitch.service"]
    assert order == ["ccswitch-daemon.service", "ccswitch.service"]


def test_edge_only_restarts_edge_alone():
    units = steps.restart_units(daemon_changed=False, daemon_enabled=True,
                                run=lambda c, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert units == ["ccswitch.service"]


def test_daemon_disabled_skips_daemon_restart():
    units = steps.restart_units(daemon_changed=True, daemon_enabled=False,
                                run=lambda c, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert units == ["ccswitch.service"]


def test_health_requires_both_status_and_sha():
    bodies = iter([
        '{"status":"ok","sha":"deadbee"}',     # right status, WRONG sha → keep waiting
        '{"status":"ok","sha":"abc1234"}',     # match
    ])
    ok = steps.wait_healthy("abc1234", url="http://h/healthz", attempts=5,
                            fetch=lambda u: next(bodies), sleep=lambda s: None)
    assert ok is True


def test_health_gives_up_after_attempts():
    ok = steps.wait_healthy("abc1234", url="http://h/healthz", attempts=3,
                            fetch=lambda u: '{"status":"ok","sha":"other00"}',
                            sleep=lambda s: None)
    assert ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_restart_health.py -v`
Expected: FAIL — `AttributeError: ... 'restart_units'`.

- [ ] **Step 3: Write minimal implementation (append to `server/deploy/steps.py`)**

```python
import time


def restart_units(*, daemon_changed: bool, daemon_enabled: bool,
                  run: Run = subprocess.run) -> list[str]:
    units: list[str] = []
    # DAEMON FIRST — order is load-bearing (browser reconnect-flush race, #190).
    if daemon_changed and daemon_enabled:
        run(["sudo", "systemctl", "restart", "ccswitch-daemon.service"],
            check=True, capture_output=True)
        units.append("ccswitch-daemon.service")
    run(["sudo", "systemctl", "restart", "ccswitch.service"], check=True, capture_output=True)
    units.append("ccswitch.service")
    return units


def _default_fetch(url: str) -> str:
    import httpx
    try:
        return httpx.get(url, timeout=3.0).text
    except Exception:
        return ""


def wait_healthy(expected_sha: str, *, url: str, attempts: int = 15, sleep_s: float = 2.0,
                 fetch: Callable[[str], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    fetch = fetch or _default_fetch
    needle_status = '"status":"ok"'
    needle_sha = f'"sha":"{expected_sha}"'
    for _ in range(attempts):
        body = fetch(url)
        if needle_status in body and needle_sha in body:
            return True
        sleep(sleep_s)
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_restart_health.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_restart_health.py
git commit -m "feat(deploy): port restart ordering + sha-asserting health gate to Python"
```

---

## Task 7: Rollback (`rollback_to`)

**Files:**
- Modify: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_rollback.py`

**Interfaces:**
- Produces: `steps.rollback_to(repo: str, sha: str, *, daemon_changed: bool, daemon_enabled: bool, run: Run = subprocess.run) -> None` — `git reset --hard <sha>`, rebuild frontend (best-effort; a build failure here only warns via the returned-None contract — it must not raise, mirroring the script), then restart BOTH units together (so they run the same old code — never split-version). Uses `restart_units(daemon_changed=daemon_changed, ...)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_rollback.py
from types import SimpleNamespace
from server.deploy import steps


def test_rollback_resets_then_restarts_both():
    seq = []
    def run(cmd, **kw):
        if cmd[:2] == ["git", "-C"] and "reset" in cmd:
            seq.append(("reset", cmd[-1]))
        if "restart" in cmd:
            seq.append(("restart", cmd[-1]))
        if cmd[:2] == ["npm", "run"]:
            seq.append(("build", None))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    steps.rollback_to("/r", "lastgood", daemon_changed=True, daemon_enabled=True, run=run)
    assert seq[0] == ("reset", "lastgood")
    assert ("restart", "ccswitch-daemon.service") in seq
    assert ("restart", "ccswitch.service") in seq
    # daemon restarted before edge
    assert seq.index(("restart", "ccswitch-daemon.service")) < seq.index(("restart", "ccswitch.service"))


def test_rollback_build_failure_does_not_raise():
    def run(cmd, **kw):
        rc = 1 if cmd[:2] == ["npm", "run"] else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="x")
    # must not raise even though build returns non-zero
    steps.rollback_to("/r", "lastgood", daemon_changed=False, daemon_enabled=True, run=run)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_rollback.py -v`
Expected: FAIL — `AttributeError: ... 'rollback_to'`.

- [ ] **Step 3: Write minimal implementation (append to `server/deploy/steps.py`)**

```python
def rollback_to(repo: str, sha: str, *, daemon_changed: bool, daemon_enabled: bool,
                run: Run = subprocess.run) -> None:
    run(["git", "-C", repo, "reset", "--hard", sha], check=True, capture_output=True)
    # Rebuild best-effort — a rollback build failure only leaves web/dist stale; never raise.
    try:
        build_frontend(repo, run=run)
    except DeployError:
        pass
    # Restart BOTH together so they run the same (old) code — split-version is worse.
    restart_units(daemon_changed=daemon_changed, daemon_enabled=daemon_enabled, run=run)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_rollback.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_rollback.py
git commit -m "feat(deploy): port rollback (reset+rebuild+restart-both) to Python"
```

---

## Task 8: Publish GitHub deployment (`publish_deployment`)

**Files:**
- Modify: `server/deploy/steps.py`
- Test: `server/tests/test_deploy_steps_publish.py`

**Interfaces:**
- Produces: `steps.publish_deployment(sha: str, gh_api_json: Callable[[str, str], str] | None = None) -> bool` — best-effort; returns True on success, False on any failure (never raises). `gh_api_json(path, json_body)` posts and returns the response body. Creates a `production` deployment with `required_contexts: []`, then a `success` status. Mirrors the script's two-call flow.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_steps_publish.py
import json
from server.deploy import steps


def test_publish_success_returns_true():
    calls = []
    def gh(path, body):
        calls.append((path, json.loads(body)))
        if path.endswith("/deployments"):
            return json.dumps({"id": 99})
        return json.dumps({"id": 1234})
    assert steps.publish_deployment("abc", gh_api_json=gh) is True
    assert calls[0][1]["required_contexts"] == []
    assert "deployments/99/statuses" in calls[1][0]


def test_publish_failure_returns_false_not_raises():
    def gh(path, body):
        raise RuntimeError("403")
    assert steps.publish_deployment("abc", gh_api_json=gh) is False


def test_publish_missing_id_returns_false():
    assert steps.publish_deployment("abc", gh_api_json=lambda p, b: json.dumps({})) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_publish.py -v`
Expected: FAIL — `AttributeError: ... 'publish_deployment'`.

- [ ] **Step 3: Write minimal implementation (append to `server/deploy/steps.py`)**

```python
def _default_gh_api_json(path: str, body: str) -> str:
    r = subprocess.run(["gh", "api", "-X", "POST", path, "--input", "-"],
                       input=body, check=True, capture_output=True, text=True)
    return r.stdout


def publish_deployment(sha: str, gh_api_json: Callable[[str, str], str] | None = None) -> bool:
    gh_api_json = gh_api_json or _default_gh_api_json
    try:
        dep_body = json.dumps({
            "ref": sha, "environment": "production", "production_environment": True,
            "auto_merge": False, "required_contexts": [],
        })
        dep = json.loads(gh_api_json("repos/{owner}/{repo}/deployments", dep_body))
        dep_id = dep.get("id")
        if not dep_id:
            return False
        gh_api_json(f"repos/{{owner}}/{{repo}}/deployments/{dep_id}/statuses",
                    json.dumps({"state": "success", "auto_inactive": True}))
        return True
    except Exception:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_steps_publish.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_steps_publish.py
git commit -m "feat(deploy): port best-effort GitHub Deployment publish to Python"
```

---

## Task 9: Drain handshake (`drain_and_wait`)

**Files:**
- Create: `server/deploy/drain.py`
- Test: `server/tests/test_deploy_drain.py`

**Interfaces:**
- Consumes: `DaemonClient` from `server.ipc.client` (has `async call(method, **params)`), and its existing daemon RPCs `begin_drain` / `drain_status` (registered in `orchestrator_rpc.py` `_SIMPLE`). `drain_status()` returns a dict with a `state` key (`"ready"` when no session is mid-turn).
- Produces: `async drain.drain_and_wait(client, *, cap_s: float = 180.0, poll_s: float = 2.0, now: Callable[[], float] = time.monotonic, sleep=asyncio.sleep) -> str` returning `"ready" | "cap" | "unavailable"`. Calls `begin_drain`; if it raises `DaemonUnavailable`/`DaemonError` → `"unavailable"`. Polls `drain_status` until `state == "ready"` (→ `"ready"`) or `cap_s` elapsed (→ `"cap"`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_drain.py
import asyncio
import pytest
from server.deploy import drain
from server.ipc.client import DaemonUnavailable


class FakeClient:
    def __init__(self, statuses, begin_exc=None):
        self._statuses = list(statuses)
        self._begin_exc = begin_exc
        self.began = False
    async def call(self, method, **params):
        if method == "begin_drain":
            if self._begin_exc:
                raise self._begin_exc
            self.began = True
            return {}
        if method == "drain_status":
            return self._statuses.pop(0) if self._statuses else {"state": "working"}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_ready_when_daemon_reports_ready():
    c = FakeClient([{"state": "working"}, {"state": "ready"}])
    res = await drain.drain_and_wait(c, cap_s=100, poll_s=0, sleep=lambda s: asyncio.sleep(0))
    assert res == "ready" and c.began is True


@pytest.mark.asyncio
async def test_unavailable_when_begin_drain_fails():
    c = FakeClient([], begin_exc=DaemonUnavailable("no sock"))
    assert await drain.drain_and_wait(c) == "unavailable"


@pytest.mark.asyncio
async def test_cap_when_never_ready():
    ticks = iter([0, 0, 1, 999])   # now() jumps past cap
    c = FakeClient([{"state": "working"}] * 10)
    res = await drain.drain_and_wait(c, cap_s=5, poll_s=0,
                                     now=lambda: next(ticks), sleep=lambda s: asyncio.sleep(0))
    assert res == "cap"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_drain.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.drain`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/drain.py
from __future__ import annotations
import asyncio
import time
from typing import Callable
from server.ipc.client import DaemonError, DaemonUnavailable


async def drain_and_wait(client, *, cap_s: float = 180.0, poll_s: float = 2.0,
                         now: Callable[[], float] = time.monotonic,
                         sleep=asyncio.sleep) -> str:
    try:
        await client.call("begin_drain")
    except (DaemonUnavailable, DaemonError):
        return "unavailable"
    start = now()
    while True:
        try:
            status = await client.call("drain_status")
        except (DaemonUnavailable, DaemonError):
            return "unavailable"
        if status.get("state") == "ready":
            return "ready"
        if now() - start >= cap_s:
            return "cap"
        await sleep(poll_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_drain.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/drain.py server/tests/test_deploy_drain.py
git commit -m "feat(deploy): drain handshake over existing daemon begin_drain/drain_status RPCs"
```

---

## Task 10: Controller orchestration (`DeployController.deploy_once`)

**Files:**
- Create: `server/deploy/controller.py`
- Modify: `server/deploy/__init__.py` (re-export `DeployController`)
- Test: `server/tests/test_deploy_controller.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces:
  - `class DeployController` with:
    - `__init__(self, repo: str, *, health_url: str, venv_pip: str, daemon_client_factory: Callable[[], object] | None = None, daemon_enabled: bool = False, steps_mod=steps, git_mod=git, drain_mod=drain)` — `*_mod` params injected for tests.
    - `status: DeployStatus` property (current phase map).
    - `async deploy_once(self) -> DeployStatus` — runs the sequence below. Returns the final status. Raises nothing; failures land in `status.error` and trigger rollback.
  - Sequence (preflight → update → report → restart; SAVE/RESUME are stubs in Plan 1, wired in Plan 3):
    1. **preflight**: `git.fetch`; `local=current_sha`, `remote=remote_sha`. If equal → status all-done, return. `ci_verdict(remote)` must be `"green"` else `DeployError("preflight", verdict)`. Acquire `self._lock` (asyncio.Lock, non-blocking via `locked()` → raise if already running).
    2. **update**: `guard_working_tree`; `fast_forward`; `sync_deps_if_changed`; `build_frontend`. (After ff, `repo` HEAD == remote.)
    3. **report**: `changed = git diff --name-only local remote`; `klass = classify_change(changed)`; `daemon_changed = klass == "daemon_changed"`.
    4. **restart**: if `daemon_changed`: `drain_and_wait(client)` (record `"cap"`/`"unavailable"` as a warning, never abort); `restart_units(daemon_changed=True, daemon_enabled=...)`. else `restart_units(daemon_changed=False, ...)`. Then `wait_healthy(short_sha(remote))`; if not healthy → `rollback_to(local, daemon_changed, ...)` and `DeployError("restart", "health gate failed")`.
    5. On success: `publish_deployment(remote)` (best-effort warning).
  - On any `DeployError` after the fast-forward: `rollback_to(local, ...)`, set `status.error`, mark the failing phase FAILED, return.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_controller.py
import asyncio
import pytest
from types import SimpleNamespace
from server.deploy.controller import DeployController
from server.deploy.types import PhaseState


class FakeGit:
    def __init__(self, local, remote, changed):
        self._local, self._remote, self._changed = local, remote, changed
        self.reset_to = None
    def fetch(self, repo, ref="master", run=None): pass
    def current_sha(self, repo, run=None): return self._local
    def remote_sha(self, repo, ref="origin/master", run=None): return self._remote
    def short_sha(self, repo, sha, run=None): return sha[:7]


def make_steps(*, ci="green", healthy=True, changed=None):
    calls = SimpleNamespace(restarted=[], rolled_back=False, published=False)
    mod = SimpleNamespace()
    mod.ci_verdict = lambda sha, gh_api=None: ci
    mod.guard_working_tree = lambda repo, run=None: False
    mod.fast_forward = lambda repo, ref="master", run=None: None
    mod.sync_deps_if_changed = lambda repo, a, b, *, venv_pip, run=None: []
    mod.build_frontend = lambda repo, run=None: None
    mod.classify_change = lambda files: "daemon_changed" if files and any("orchestrator" in f for f in files) else "edge_only"
    mod._changed_files = changed or []
    def restart(*, daemon_changed, daemon_enabled, run=None):
        u = (["ccswitch-daemon.service"] if daemon_changed and daemon_enabled else []) + ["ccswitch.service"]
        calls.restarted = u; return u
    mod.restart_units = restart
    mod.wait_healthy = lambda sha, **k: healthy
    def rollback(repo, sha, *, daemon_changed, daemon_enabled, run=None):
        calls.rolled_back = True
    mod.rollback_to = rollback
    def publish(sha, gh_api_json=None):
        calls.published = True; return True
    mod.publish_deployment = publish
    return mod, calls


@pytest.mark.asyncio
async def test_up_to_date_is_noop():
    g = FakeGit("same", "same", [])
    s, _ = make_steps()
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.running is False and st.error is None


@pytest.mark.asyncio
async def test_red_ci_refuses_before_any_change():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(ci="red")
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip", git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.error and "preflight" in st.error
    assert calls.restarted == [] and calls.rolled_back is False


@pytest.mark.asyncio
async def test_health_failure_triggers_rollback():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=False)
    # force daemon_changed path so a restart happens
    s.classify_change = lambda files: "daemon_changed"
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert calls.rolled_back is True
    assert st.phases["restart"] == PhaseState.FAILED.value


@pytest.mark.asyncio
async def test_edge_only_skips_daemon_restart_and_publishes():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.restarted == ["ccswitch.service"]   # no daemon restart
    assert calls.published is True and st.error is None


@pytest.mark.asyncio
async def test_concurrent_deploy_rejected():
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s,
                         changed_files_fn=lambda repo, a, b: ["web/x.tsx"])
    c._lock_acquired = True   # simulate an in-flight deploy
    with pytest.raises(RuntimeError, match="in progress"):
        await c.deploy_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_controller.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.controller`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/controller.py
from __future__ import annotations
import logging
import subprocess
from typing import Callable

from server.deploy import git as _git
from server.deploy import steps as _steps
from server.deploy import drain as _drain
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState

log = logging.getLogger("server.deploy.controller")


def _default_changed_files(repo: str, a: str, b: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "--name-only", a, b],
                           check=True, capture_output=True, text=True)
        return [p for p in r.stdout.splitlines() if p.strip()]
    except subprocess.CalledProcessError:
        return None   # uncomputable → classify_change fails safe to daemon_changed


class DeployController:
    def __init__(self, repo: str, *, health_url: str, venv_pip: str,
                 daemon_client_factory: Callable[[], object] | None = None,
                 daemon_enabled: bool = False,
                 steps_mod=_steps, git_mod=_git, drain_mod=_drain,
                 changed_files_fn: Callable[[str, str, str], list[str] | None] = _default_changed_files):
        self.repo = repo
        self.health_url = health_url
        self.venv_pip = venv_pip
        self._client_factory = daemon_client_factory
        self.daemon_enabled = daemon_enabled
        self._steps = steps_mod
        self._git = git_mod
        self._drain = drain_mod
        self._changed_files = changed_files_fn
        self._lock_acquired = False
        self.status = DeployStatus(target_sha="")

    def _set(self, phase: Phase, state: PhaseState) -> None:
        self.status.phases[phase.value] = state.value

    async def deploy_once(self) -> DeployStatus:
        if self._lock_acquired:
            raise RuntimeError("deploy already in progress")
        self._lock_acquired = True
        self.status = DeployStatus(target_sha="", running=True)
        local = ""
        daemon_changed = False
        try:
            # --- preflight ---
            self._set(Phase.PREFLIGHT, PhaseState.RUNNING)
            self._git.fetch(self.repo)
            local = self._git.current_sha(self.repo)
            remote = self._git.remote_sha(self.repo)
            self.status.target_sha = remote
            if local == remote:
                for p in Phase:
                    self._set(p, PhaseState.DONE)
                return self.status
            verdict = self._steps.ci_verdict(remote)
            if verdict != "green":
                raise DeployError("preflight", f"CI verdict={verdict}")
            self._set(Phase.PREFLIGHT, PhaseState.DONE)

            # --- update ---
            self._set(Phase.UPDATE, PhaseState.RUNNING)
            self._steps.guard_working_tree(self.repo)
            self._steps.fast_forward(self.repo)
            self._steps.sync_deps_if_changed(self.repo, local, remote, venv_pip=self.venv_pip)
            self._steps.build_frontend(self.repo)
            self._set(Phase.UPDATE, PhaseState.DONE)

            # --- report ---
            self._set(Phase.REPORT, PhaseState.RUNNING)
            changed = self._changed_files(self.repo, local, remote)
            daemon_changed = self._steps.classify_change(changed) == "daemon_changed"
            self._set(Phase.REPORT, PhaseState.DONE)

            # --- restart ---
            self._set(Phase.RESTART, PhaseState.RUNNING)
            if daemon_changed and self._client_factory is not None:
                client = self._client_factory()
                result = await self._drain.drain_and_wait(client)
                if result != "ready":
                    self.status.warnings.append(f"drain {result} — restarting anyway")
            self._steps.restart_units(daemon_changed=daemon_changed,
                                      daemon_enabled=self.daemon_enabled)
            short = self._git.short_sha(self.repo, remote)
            if not self._steps.wait_healthy(short, url=self.health_url):
                raise DeployError("restart", "health gate failed")
            self._set(Phase.RESTART, PhaseState.DONE)

            # --- publish (best-effort) ---
            if not self._steps.publish_deployment(remote):
                self.status.warnings.append("could not publish GitHub deployment")
            return self.status
        except DeployError as e:
            self._set(Phase[e.phase.upper()], PhaseState.FAILED)
            self.status.error = str(e)
            # Roll back only if we already fast-forwarded (preflight failures changed nothing).
            if e.phase != "preflight" and local:
                self._steps.rollback_to(self.repo, local, daemon_changed=daemon_changed,
                                        daemon_enabled=self.daemon_enabled)
            return self.status
        finally:
            self.status.running = False
            self._lock_acquired = False
```

Then re-export in `server/deploy/__init__.py`:

```python
from server.deploy.controller import DeployController
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState

__all__ = ["DeployController", "DeployError", "DeployStatus", "Phase", "PhaseState"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_controller.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/controller.py server/deploy/__init__.py server/tests/test_deploy_controller.py
git commit -m "feat(deploy): DeployController.deploy_once stitches the ported steps (load-bearing order)"
```

---

## Task 11: CLI entrypoint (`python -m server.deploy --once`)

**Files:**
- Create: `server/deploy/__main__.py`
- Test: `server/tests/test_deploy_cli.py`

**Interfaces:**
- Consumes: `DeployController`.
- Produces: `__main__.main(argv: list[str]) -> int` — `--once` runs one `deploy_once()`; reads `repo` from `--repo` (default: cwd), `health_url` from `CCSWITCH_HOST`/`CCSWITCH_PORT` (default `http://127.0.0.1:8787/healthz`), `venv_pip` from `--venv-pip` (default `<repo>/.venv/bin/pip`), `daemon_enabled` from `systemctl is-enabled ccswitch-daemon.service` (overridable `--daemon-enabled`). Returns `0` on success (`status.error is None`), `1` otherwise. Prints phase transitions to stdout.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_cli.py
import asyncio
from server.deploy import __main__ as cli
from server.deploy.types import DeployStatus


def test_once_returns_0_on_success(monkeypatch):
    async def fake_deploy(self):
        self.status = DeployStatus(target_sha="abc", running=False, error=None)
        return self.status
    monkeypatch.setattr("server.deploy.controller.DeployController.deploy_once", fake_deploy)
    assert cli.main(["--once", "--repo", "/tmp", "--daemon-enabled", "false"]) == 0


def test_once_returns_1_on_error(monkeypatch):
    async def fake_deploy(self):
        self.status = DeployStatus(target_sha="abc", running=False, error="restart: boom")
        return self.status
    monkeypatch.setattr("server.deploy.controller.DeployController.deploy_once", fake_deploy)
    assert cli.main(["--once", "--repo", "/tmp", "--daemon-enabled", "false"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.__main__` (or AttributeError `main`).

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/__main__.py
from __future__ import annotations
import argparse
import asyncio
import os
import subprocess
import sys

from server.deploy.controller import DeployController


def _daemon_enabled() -> bool:
    try:
        return subprocess.run(["systemctl", "is-enabled", "ccswitch-daemon.service"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m server.deploy")
    p.add_argument("--once", action="store_true", help="run a single deploy and exit")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--venv-pip", default=None)
    p.add_argument("--daemon-enabled", choices=["true", "false"], default=None)
    args = p.parse_args(argv)

    if not args.once:
        p.error("only --once is supported in Plan 1")

    host = os.environ.get("CCSWITCH_HOST", "127.0.0.1")
    port = os.environ.get("CCSWITCH_PORT", "8787")
    health_url = f"http://{host}:{port}/healthz"
    venv_pip = args.venv_pip or os.path.join(args.repo, ".venv", "bin", "pip")
    daemon_enabled = (args.daemon_enabled == "true") if args.daemon_enabled else _daemon_enabled()

    controller = DeployController(args.repo, health_url=health_url, venv_pip=venv_pip,
                                  daemon_enabled=daemon_enabled)
    status = asyncio.run(controller.deploy_once())
    for phase, state in status.phases.items():
        print(f"[deploy] {phase}: {state}")
    for w in status.warnings:
        print(f"[deploy] WARNING: {w}", file=sys.stderr)
    if status.error:
        print(f"[deploy] FAILED: {status.error}", file=sys.stderr)
        return 1
    print(f"[deploy] live at {status.target_sha[:9]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_cli.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/__main__.py server/tests/test_deploy_cli.py
git commit -m "feat(deploy): python -m server.deploy --once CLI entrypoint"
```

---

## Task 12: Retire the bash script + units; retarget legacy tests; rewrite README

**Files:**
- Delete: `scripts/auto-deploy.sh`, `scripts/tests/auto-deploy-defer-worktree.bats`, `scripts/tests/auto-deploy-dirty-tree.bats`, `scripts/tests/auto-deploy-drain.bats`, `scripts/tests/auto-deploy-idle-gate.bats`, `scripts/tests/auto-deploy-vnc-gate.bats`, `deploy/ccswitch-deploy.service`, `deploy/ccswitch-deploy.timer`
- Modify: `server/tests/test_deploy_classification.py`, `server/tests/test_deploy_daemon.py`, `deploy/README.md`

**Interfaces:**
- Consumes: `steps.classify_change`, `steps.restart_units` (Tasks 3, 6).
- Produces: no new symbols — this task removes the parallel implementation and re-points its coverage at the Python port (single source of truth).

- [ ] **Step 1: Rewrite `server/tests/test_deploy_classification.py` to target Python (failing first)**

Replace the entire file with:

```python
# server/tests/test_deploy_classification.py
from server.deploy import steps


def test_edge_only_paths_classify_edge_only():
    files = ["web/src/App.tsx", "server/api/x.py", "server/app.py",
             "server/bridge/ws.py", "docs/y.md", ".github/workflows/ci.yml"]
    assert steps.classify_change(files) == "edge_only"


def test_shared_daemon_modules_force_daemon_changed():
    for shared in ("server/config.py", "server/db/models.py",
                   "server/audit.py", "server/usage.py",
                   "server/sessions/orchestrator.py", "server/ipc/daemon.py"):
        assert steps.classify_change([shared]) == "daemon_changed", shared


def test_uncomputable_diff_fails_safe():
    assert steps.classify_change(None) == "daemon_changed"
```

- [ ] **Step 2: Delete the script, bats tests, and timer/oneshot units**

```bash
git rm scripts/auto-deploy.sh \
       scripts/tests/auto-deploy-defer-worktree.bats \
       scripts/tests/auto-deploy-dirty-tree.bats \
       scripts/tests/auto-deploy-drain.bats \
       scripts/tests/auto-deploy-idle-gate.bats \
       scripts/tests/auto-deploy-vnc-gate.bats \
       deploy/ccswitch-deploy.service \
       deploy/ccswitch-deploy.timer
```

- [ ] **Step 3: Retarget `server/tests/test_deploy_daemon.py`**

Open `server/tests/test_deploy_daemon.py`. Any test that asserts against `auto-deploy.sh` text (`SCRIPT.read_text()`, daemon-first ordering, `is-enabled` guard) must be rewritten to assert against `steps.restart_units`. Replace the daemon-ordering test body with:

```python
from types import SimpleNamespace
from server.deploy import steps


def test_daemon_restarts_before_edge_when_daemon_changed():
    order = []
    def run(cmd, **kw):
        if "restart" in cmd:
            order.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    steps.restart_units(daemon_changed=True, daemon_enabled=True, run=run)
    assert order == ["ccswitch-daemon.service", "ccswitch.service"]


def test_daemon_skipped_when_not_enabled():
    order = []
    def run(cmd, **kw):
        if "restart" in cmd:
            order.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    steps.restart_units(daemon_changed=True, daemon_enabled=False, run=run)
    assert order == ["ccswitch.service"]
```

Delete any remaining `SCRIPT = ROOT / "scripts" / "auto-deploy.sh"` references in that file.

- [ ] **Step 4: Rewrite `deploy/README.md`**

Replace the "Components", "Install", and "Operate" sections so they describe `python -m server.deploy --once` instead of the script, and remove the `ccswitch-deploy.service`/`ccswitch-deploy.timer` rows. Add this note (the long-lived controller service + UI trigger arrive in Plans 2–3):

```markdown
## Deploy (Plan 1: manual)

Deploys are no longer automatic. Run one on demand:

    PYTHONPATH=<repo>/src <repo>/.venv/bin/python -m server.deploy --once

This gates on GitHub CI being green, fast-forwards `origin/master`, syncs deps only
when lockfiles changed, rebuilds `web/dist`, restarts the daemon (if daemon code
changed) then the edge, health-gates on the deployed SHA, and rolls back on failure.

> The notify-on-pending UI and one-click trigger (a long-lived `ccswitch-deploy-controller`
> service) land in Plans 2–3. Until then, `--once` is the supported path and the
> `ccswitch-deploy.timer`/`.service` units have been removed (no more auto-deploy).
```

- [ ] **Step 5: Run the full deploy suite + lint, then commit**

Run:
```bash
PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_*.py -v
.venv/bin/ruff check server/deploy/
.venv/bin/mypy server/deploy/
```
Expected: all deploy tests PASS; ruff clean; mypy clean. Verify no source file still references `auto-deploy.sh`:
```bash
! grep -rn "auto-deploy.sh" server/ scripts/ deploy/ docs/superpowers/plans/ 2>/dev/null
```
Expected: prints nothing (exit 0 from the negation).

```bash
git add -A
git commit -m "refactor(deploy): retire auto-deploy.sh + timer units; retarget coverage to Python port"
```

---

## Self-Review

**Spec coverage (Plan 1 scope only):**
- "Port everything, retire the script" → Tasks 2–9 (each mechanical step) + Task 12 (deletion). ✓
- "Single end-state, no parallel deploy engine" → Task 12 deletes the script + bats in the same plan. ✓
- 10 incident-proofed invariants → CI-gate (T2), classification fail-safe (T3), working-tree guard (T4), dep-sync lockfile-gated (T5), daemon-first restart + sha health gate (T6), rollback resets+rebuilds+restarts-both (T7), GitHub Deployment publish (T8), drain capped (T9), `--absolute-git-dir` available (T1 `abs_git_dir`, consumed by Plan 3's state paths). ✓
- Detection/notify, edge API proxy, UI, snapshot/restore → **explicitly deferred to Plans 2 & 3** (noted in goal + README). ✓

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N" — every step has runnable code and a concrete command. ✓

**Type consistency:** `Run` alias used uniformly; `classify_change(list|None)->str`; `restart_units(*, daemon_changed, daemon_enabled, run)->list[str]` identical in T6, T7, T10, T12; `wait_healthy(expected_sha, *, url, ...)->bool` consistent; `DeployError(phase, detail)` consistent; controller injects `steps_mod/git_mod/drain_mod/changed_files_fn` matching the test doubles. ✓

**Note for Plan 3:** the SAVE and RESUME phases are present in the `Phase` enum but not driven in `deploy_once` yet (no snapshot/restore RPCs exist). Plan 3 adds the daemon RPCs and wires phases 1 & 5 into the controller between preflight/restart.
