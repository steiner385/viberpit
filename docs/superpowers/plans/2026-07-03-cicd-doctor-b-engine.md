# CI/CD Doctor — Plan B: Sensing Engine (models + pure analyzers)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The CI/CD Doctor's diagnostic brain: DB tables for CI history and pure, I/O-free analyzers (JUnit parsing, flake/duration stats, graduation-gate verdicts, waste detection, finding assembly). Spec: `docs/superpowers/specs/2026-07-03-cicd-doctor-design.md`.

**Architecture:** New package `server/automations/cicd/` — one focused module per concern, everything a pure function of `(observations, thresholds)`. Four new SQLAlchemy models in `server/db/models.py`. No network, no orchestrator, no loop — that's Plan C. Mirrors the `pr_doctor_engine.py` pure-engine precedent.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async declarative, stdlib `xml.etree` + `dataclasses`, PyYAML (already a server dep), pytest.

## Global Constraints

- Every new model column except primary keys is `nullable=True` (dev-SQLite `init_db` backfill rule — a NOT NULL column needs a real migration).
- Analyzers are PURE: no DB, no network, no clock reads — time arrives as a parameter. Constitution V (TDD) and the AutopilotLoop precedent.
- Percentages are floats 0–100 (not 0–1). Durations are seconds. Timestamps are epoch-seconds floats.
- Single source of truth: thresholds live ONLY in `GateThresholds` (fed from the automation config in Plan C); never re-inline numeric defaults elsewhere.
- CI runs mypy on `server/` — keep annotations complete (`ruff check` + `mypy server/ --ignore-missing-imports` must pass before every commit).
- Test commands: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/<file> -q`. Test style: sync `def test_*` wrapping `asyncio.run(...)` only where DB access is needed; pure tests are plain functions.
- Spec deviation (intentional, YAGNI): no `ci_check_state` table — check tier is read live from branch protection (Plan C) and stability stats derive from `ci_job` rows, so persisted check state would be a second source of truth.

---

### Task 0: Branch setup

- [ ] **Step 1: Create the working branch off origin/master**

```bash
git fetch origin
git checkout -b feat/cicd-doctor-engine origin/master
```

---

### Task 1: CI history models

**Files:**
- Modify: `server/db/models.py` (append four models)
- Test: `server/tests/test_cicd_models.py`

**Interfaces:**
- Produces: `CiRun(id, repo, workflow_path, event, head_sha, head_branch, conclusion, created_ts, duration_s)`, `CiJob(id, run_id, repo, name, conclusion, duration_s, head_sha, head_branch, created_ts)`, `CiTestResult(id, repo, run_id, suite, test_id, outcome, time_s, head_sha, head_branch, created_ts)`, `CiFinding(id, repo, kind, fingerprint, status, title, detail, issue_number, first_seen_at, last_seen_at, resolved_at)`. Plan C's loop reads/writes exactly these attribute names.

- [ ] **Step 1: Write the failing test**

```python
"""CI/CD Doctor history tables exist after init_db and round-trip rows."""
from __future__ import annotations

import asyncio

from server.db import db_session, init_db
from server.db.models import CiFinding, CiJob, CiRun, CiTestResult


def test_ci_tables_roundtrip(tmp_path):
    async def run():
        await init_db()
        async with db_session() as db:
            db.add(CiRun(id=1, repo="o/r", workflow_path=".github/workflows/ci.yml",
                         event="pull_request", head_sha="abc", head_branch="feat",
                         conclusion="success", created_ts=1000.0, duration_s=300.0))
            db.add(CiJob(id=10, run_id=1, repo="o/r", name="e2e", conclusion="failure",
                         duration_s=120.0, head_sha="abc", head_branch="feat",
                         created_ts=1000.0))
            db.add(CiTestResult(repo="o/r", run_id=1, suite="junit-python",
                                test_id="tests/test_x.py::test_y", outcome="failed",
                                time_s=0.5, head_sha="abc", head_branch="feat",
                                created_ts=1000.0))
            db.add(CiFinding(repo="o/r", kind="flaky_test",
                             fingerprint="flaky_test:o/r:tests/test_x.py::test_y",
                             status="open", title="flaky test", detail={"pct": 8.0}))
            await db.flush()
        async with db_session() as db:
            from sqlalchemy import select
            f = (await db.execute(select(CiFinding))).scalars().one()
            assert f.status == "open" and f.issue_number is None
            assert f.first_seen_at is not None
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'CiFinding'`

- [ ] **Step 3: Append the models to `server/db/models.py`**

```python
class CiRun(Base):
    """One completed GitHub Actions workflow run (CI/CD Doctor sensing).
    id is the GitHub run id (globally unique). All non-PK columns nullable so
    init_db backfills the dev SQLite DB without a migration."""

    __tablename__ = "ci_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    workflow_path: Mapped[str | None] = mapped_column(String, nullable=True)
    event: Mapped[str | None] = mapped_column(String, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    head_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)


class CiJob(Base):
    """One job (== status-check context) inside a CiRun. sha/branch/created_ts
    are denormalized from the run so the analyzers query a single table."""

    __tablename__ = "ci_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # GitHub job id
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)


class CiTestResult(Base):
    """One test-case outcome parsed from a run's JUnit artifact."""

    __tablename__ = "ci_test_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    suite: Mapped[str | None] = mapped_column(String, nullable=True)
    test_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # passed/failed/error/skipped
    time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)


class CiFinding(Base):
    """A persistent CI/CD Doctor finding: fingerprint-deduped, lifecycle
    open -> resolved. issue_number links the filed ci-doctor GitHub issue."""

    __tablename__ = "ci_finding"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="open")
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=_utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
```

- [ ] **Step 4: Run tests + quality gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_models.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS, no lint/type errors.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/tests/test_cicd_models.py
git commit -m "feat(cicd-doctor): CI history + finding models"
```

---

### Task 2: Types + JUnit parser

**Files:**
- Create: `server/automations/cicd/__init__.py` (empty)
- Create: `server/automations/cicd/types.py`
- Create: `server/automations/cicd/junit.py`
- Test: `server/tests/test_cicd_junit.py`

**Interfaces:**
- Produces: `JUnitCase(classname, name, outcome, time_s, message)` with `.test_id`; `parse_junit_xml(data: bytes) -> list[JUnitCase]` (raises `ValueError` on malformed XML); `TestObs`, `JobObs`, `GateThresholds` (with `from_config(dict)`), `CheckStats`, `Finding` (with `.fingerprint`) dataclasses used by Tasks 3–6 and Plan C.

- [ ] **Step 1: Write the failing tests**

```python
"""JUnit XML parsing — pytest/vitest/Playwright dialects, pure."""
from __future__ import annotations

import pytest

from server.automations.cicd.junit import JUnitCase, parse_junit_xml

PYTEST_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_a" name="test_ok" time="0.01"/>
<testcase classname="tests.test_a" name="test_bad" time="0.50">
  <failure message="assert 1 == 2">traceback...</failure></testcase>
<testcase classname="tests.test_a" name="test_skip" time="0">
  <skipped message="no reason"/></testcase>
</testsuite></testsuites>"""

FLAT_XML = b"""<testsuite tests="1">
<testcase classname="" name="dashboard renders" time="2.3">
  <error message="boom"/></testcase></testsuite>"""


def test_parses_outcomes_and_ids():
    cases = parse_junit_xml(PYTEST_XML)
    assert [(c.test_id, c.outcome) for c in cases] == [
        ("tests.test_a::test_ok", "passed"),
        ("tests.test_a::test_bad", "failed"),
        ("tests.test_a::test_skip", "skipped"),
    ]
    assert cases[1].message == "assert 1 == 2"
    assert cases[1].time_s == 0.5


def test_flat_suite_and_error_outcome():
    (c,) = parse_junit_xml(FLAT_XML)
    assert c.test_id == "dashboard renders" and c.outcome == "error"


def test_malformed_raises():
    with pytest.raises(ValueError):
        parse_junit_xml(b"<not-closed")
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_junit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.automations.cicd'`

- [ ] **Step 3: Implement**

`server/automations/cicd/__init__.py`:

```python
"""CI/CD Doctor sensing engine — pure analyzers over ingested CI history."""
```

`server/automations/cicd/types.py`:

```python
"""Value types shared by the CI/CD Doctor's pure analyzers (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TestObs:
    """One test-case outcome observation (a ci_test_result row, detached)."""
    test_id: str
    head_sha: str
    head_branch: str
    outcome: str  # passed | failed | error | skipped


@dataclass(frozen=True)
class JobObs:
    """One job observation (a ci_job row, detached). check == job name."""
    check: str
    head_sha: str
    head_branch: str
    conclusion: str  # success | failure | cancelled | skipped | ...
    duration_s: float
    created_ts: float


@dataclass(frozen=True)
class GateThresholds:
    """Graduation/demotion/retirement thresholds — THE single source of truth.
    Defaults mirror the automation config schema defaults (Plan C)."""
    min_window_days: float = 21.0
    min_runs: int = 30
    max_flake_pct: float = 2.0
    max_p95_minutes: float = 10.0
    demote_flake_pct: float = 5.0
    demote_window_days: float = 7.0
    retire_days: float = 60.0
    flaky_test_min_pct: float = 2.0

    @classmethod
    def from_config(cls, config: dict) -> "GateThresholds":
        """Build from a validated automation config dict; missing keys keep defaults."""
        keys = {f: config[f] for f in (
            "min_window_days", "min_runs", "max_flake_pct", "max_p95_minutes",
            "demote_flake_pct", "demote_window_days", "retire_days",
            "flaky_test_min_pct") if f in config}
        return cls(**{k: (int(v) if k == "min_runs" else float(v)) for k, v in keys.items()})


@dataclass
class CheckStats:
    """Rolling stability stats for one status-check context."""
    check: str
    runs_total: int = 0
    window_days: float = 0.0
    flake_pct: float = 0.0            # % of shas with BOTH success and failure
    recent_flake_pct: float = 0.0     # same, restricted to demote_window_days
    default_branch_failures: int = 0  # shas on the default branch that only ever failed
    p95_duration_s: float = 0.0
    flake_rising: bool = False


@dataclass(frozen=True)
class Finding:
    """One diagnostic finding. fingerprint dedupes across ticks; the DB row and
    GitHub issue lifecycles (Plan C) key off it."""
    kind: str
    repo: str
    key: str
    title: str
    detail: dict = field(default_factory=dict)
    actionable: bool = True

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.repo}:{self.key}"
```

`server/automations/cicd/junit.py`:

```python
"""JUnit XML parsing — pure, no I/O. Handles the pytest, vitest and Playwright
dialects (all nest <testcase> under <testsuite>, optionally under <testsuites>)."""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True)
class JUnitCase:
    classname: str
    name: str
    outcome: str  # passed | failed | error | skipped
    time_s: float
    message: str | None = None

    @property
    def test_id(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


_OUTCOME_TAGS = (("failure", "failed"), ("error", "error"), ("skipped", "skipped"))


def parse_junit_xml(data: bytes) -> list[JUnitCase]:
    """Parse one JUnit document into cases. Raises ValueError on malformed XML —
    the caller (Plan C ingest) decides how to degrade."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ValueError(f"malformed junit xml: {e}") from e
    cases: list[JUnitCase] = []
    for tc in root.iter("testcase"):
        outcome, message = "passed", None
        for tag, out in _OUTCOME_TAGS:
            node = tc.find(tag)
            if node is not None:
                outcome = out
                message = ((node.get("message") or node.text or "").strip()[:500]) or None
                break
        try:
            time_s = float(tc.get("time") or 0.0)
        except ValueError:
            time_s = 0.0
        cases.append(JUnitCase(classname=tc.get("classname") or "",
                               name=tc.get("name") or "",
                               outcome=outcome, time_s=time_s, message=message))
    return cases
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_junit.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/cicd/ server/tests/test_cicd_junit.py
git commit -m "feat(cicd-doctor): value types + JUnit XML parser"
```

---

### Task 3: Flake + check stats

**Files:**
- Create: `server/automations/cicd/stats.py`
- Test: `server/tests/test_cicd_stats.py`

**Interfaces:**
- Consumes: `TestObs`, `JobObs`, `CheckStats` from `types.py`.
- Produces: `test_flake_pcts(obs: list[TestObs]) -> dict[str, tuple[float, int]]` (test_id → (flake %, distinct shas)); `check_stats(jobs: list[JobObs], *, default_branch: str, now_ts: float, demote_window_days: float) -> dict[str, CheckStats]`.

Flake definition (both levels): a sha is *flaky* for a test/check when it has BOTH a passing and a failing outcome across runs/attempts on that same sha — the same code both passed and failed. `flake_pct = 100 * flaky_shas / observed_shas`.

- [ ] **Step 1: Write the failing tests**

```python
"""Pure flake/stability statistics."""
from __future__ import annotations

from server.automations.cicd.stats import check_stats, test_flake_pcts
from server.automations.cicd.types import JobObs, TestObs

DAY = 86400.0


def _t(test_id, sha, outcome, branch="feat"):
    return TestObs(test_id=test_id, head_sha=sha, head_branch=branch, outcome=outcome)


def test_test_flake_pcts_same_sha_pass_and_fail():
    obs = [_t("t::a", "s1", "failed"), _t("t::a", "s1", "passed"),   # flaky sha
           _t("t::a", "s2", "passed"),                               # clean sha
           _t("t::b", "s1", "passed"), _t("t::b", "s2", "passed")]   # never flaky
    out = test_flake_pcts(obs)
    assert out["t::a"] == (50.0, 2)
    assert out["t::b"] == (0.0, 2)


def test_test_flake_skipped_ignored():
    out = test_flake_pcts([_t("t::a", "s1", "skipped"), _t("t::a", "s1", "failed")])
    assert out["t::a"] == (0.0, 1)  # fail-only sha: a true failure, not a flake


def _j(check, sha, conclusion, ts, branch="feat", dur=60.0):
    return JobObs(check=check, head_sha=sha, head_branch=branch,
                  conclusion=conclusion, duration_s=dur, created_ts=ts)


def test_check_stats_core_metrics():
    now = 100 * DAY
    jobs = [
        _j("e2e", "s1", "failure", now - 30 * DAY), _j("e2e", "s1", "success", now - 30 * DAY),
        _j("e2e", "s2", "success", now - 20 * DAY, dur=120.0),
        _j("e2e", "s3", "failure", now - 1 * DAY, branch="master"),  # unexplained master failure
    ]
    st = check_stats(jobs, default_branch="master", now_ts=now, demote_window_days=7.0)["e2e"]
    assert st.runs_total == 4
    assert 28.9 < st.window_days < 29.1
    assert round(st.flake_pct, 1) == 33.3          # 1 flaky sha of 3
    assert st.default_branch_failures == 1
    assert st.p95_duration_s == 120.0
    assert st.recent_flake_pct == 0.0              # the recent sha s3 is fail-only


def test_check_stats_flake_rising():
    now = 100 * DAY
    jobs = []
    for i, sha in enumerate(["a", "b", "c", "d"]):
        ts = now - (40 - i * 10) * DAY
        jobs.append(_j("e2e", sha, "success", ts))
        if i >= 2:  # later half both passes and fails -> rising
            jobs.append(_j("e2e", sha, "failure", ts))
    st = check_stats(jobs, default_branch="master", now_ts=now, demote_window_days=7.0)["e2e"]
    assert st.flake_rising is True
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_stats.py -q`
Expected: FAIL — `ModuleNotFoundError` for `stats`.

- [ ] **Step 3: Implement `server/automations/cicd/stats.py`**

```python
"""Pure stability statistics over detached CI observations (no I/O)."""
from __future__ import annotations

import math
from collections import defaultdict

from .types import CheckStats, JobObs, TestObs

_FAIL = {"failed", "error", "failure"}
_PASS = {"passed", "success"}
_DAY = 86400.0


def _flake_pct(sha_outcomes: dict[str, set[str]]) -> float:
    """% of shas whose outcome set contains BOTH a pass and a fail."""
    if not sha_outcomes:
        return 0.0
    flaky = sum(1 for o in sha_outcomes.values() if (o & _PASS) and (o & _FAIL))
    return 100.0 * flaky / len(sha_outcomes)


def test_flake_pcts(obs: list[TestObs]) -> dict[str, tuple[float, int]]:
    """Per test_id: (flake %, distinct shas observed). Skipped runs are ignored."""
    by_test: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for o in obs:
        if o.outcome in _PASS or o.outcome in _FAIL:
            by_test[o.test_id][o.head_sha].add(o.outcome)
    return {t: (_flake_pct(shas), len(shas)) for t, shas in by_test.items()}


def check_stats(jobs: list[JobObs], *, default_branch: str, now_ts: float,
                demote_window_days: float) -> dict[str, CheckStats]:
    by_check: dict[str, list[JobObs]] = defaultdict(list)
    for j in jobs:
        if j.conclusion in ("cancelled", "skipped"):
            continue  # not evidence about stability either way
        by_check[j.check].append(j)
    out: dict[str, CheckStats] = {}
    for check, js in by_check.items():
        shas: dict[str, set[str]] = defaultdict(set)
        recent: dict[str, set[str]] = defaultdict(set)
        master_shas: dict[str, set[str]] = defaultdict(set)
        for j in js:
            shas[j.head_sha].add(j.conclusion)
            if j.created_ts > now_ts - demote_window_days * _DAY:
                recent[j.head_sha].add(j.conclusion)
            if j.head_branch == default_branch:
                master_shas[j.head_sha].add(j.conclusion)
        ts = sorted(j.created_ts for j in js)
        durations = sorted(j.duration_s for j in js)
        mid = ts[len(ts) // 2]
        first = {s: o for s, o in shas.items()
                 if min(j.created_ts for j in js if j.head_sha == s) < mid}
        second = {s: o for s, o in shas.items() if s not in first}
        second_pct = _flake_pct(second)
        out[check] = CheckStats(
            check=check,
            runs_total=len(js),
            window_days=(ts[-1] - ts[0]) / _DAY if len(ts) > 1 else 0.0,
            flake_pct=_flake_pct(shas),
            recent_flake_pct=_flake_pct(recent),
            default_branch_failures=sum(
                1 for o in master_shas.values() if (o & _FAIL) and not (o & _PASS)),
            # ceil, not floor: with few samples the slowest run must count
            # (p95 of [60,60,60,120] is 120, not 60).
            p95_duration_s=durations[min(len(durations) - 1,
                                         math.ceil(0.95 * (len(durations) - 1)))]
            if durations else 0.0,
            flake_rising=second_pct > _flake_pct(first) and second_pct > 0.0,
        )
    return out
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_stats.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/cicd/stats.py server/tests/test_cicd_stats.py
git commit -m "feat(cicd-doctor): pure flake + check stability statistics"
```

---

### Task 4: Graduation gates + state-machine verdicts

**Files:**
- Create: `server/automations/cicd/gates.py`
- Test: `server/tests/test_cicd_gates.py`

**Interfaces:**
- Consumes: `CheckStats`, `GateThresholds`.
- Produces: `check_verdict(stats: CheckStats, *, is_required: bool, th: GateThresholds) -> str | None` returning `"graduate"`, `"demote"`, `"retire"`, or `None`.

- [ ] **Step 1: Write the failing tests**

```python
"""Gate evaluation: graduate / demote / retire verdicts with hysteresis."""
from __future__ import annotations

from server.automations.cicd.gates import check_verdict
from server.automations.cicd.types import CheckStats, GateThresholds

TH = GateThresholds()


def _stats(**kw) -> CheckStats:
    base = dict(check="e2e", runs_total=50, window_days=30.0, flake_pct=0.5,
                recent_flake_pct=0.0, default_branch_failures=0,
                p95_duration_s=300.0, flake_rising=False)
    base.update(kw)
    return CheckStats(**base)


def test_graduates_when_all_gates_pass():
    assert check_verdict(_stats(), is_required=False, th=TH) == "graduate"


def test_each_gate_blocks_graduation():
    for kw in (dict(window_days=10.0), dict(runs_total=10), dict(flake_pct=3.0),
               dict(default_branch_failures=1), dict(p95_duration_s=700.0),
               dict(flake_rising=True)):
        assert check_verdict(_stats(**kw), is_required=False, th=TH) is None, kw


def test_demote_needs_sustained_recent_flake():
    assert check_verdict(_stats(recent_flake_pct=6.0), is_required=True, th=TH) == "demote"
    assert check_verdict(_stats(recent_flake_pct=3.0), is_required=True, th=TH) is None


def test_hysteresis_no_oscillation():
    # 3% flake: too flaky to graduate, not flaky enough to demote.
    s = _stats(flake_pct=3.0, recent_flake_pct=3.0)
    assert check_verdict(s, is_required=False, th=TH) is None
    assert check_verdict(s, is_required=True, th=TH) is None


def test_retire_after_long_noisy_advisory():
    s = _stats(window_days=70.0, flake_pct=8.0)
    assert check_verdict(s, is_required=False, th=TH) == "retire"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_gates.py -q`
Expected: FAIL — `ModuleNotFoundError` for `gates`.

- [ ] **Step 3: Implement `server/automations/cicd/gates.py`**

```python
"""Check-tier state-machine verdicts (spec Part 3). Pure.

Graduation and demotion thresholds deliberately differ (2% vs 5% sustained) —
hysteresis so a borderline check never oscillates between tiers."""
from __future__ import annotations

from .types import CheckStats, GateThresholds


def check_verdict(stats: CheckStats, *, is_required: bool,
                  th: GateThresholds) -> str | None:
    """One of 'graduate' | 'demote' | 'retire' | None for a single check."""
    if is_required:
        if stats.recent_flake_pct >= th.demote_flake_pct:
            return "demote"
        return None
    if (stats.window_days >= th.min_window_days
            and stats.runs_total >= th.min_runs
            and stats.default_branch_failures == 0
            and stats.flake_pct < th.max_flake_pct
            and stats.p95_duration_s <= th.max_p95_minutes * 60.0
            and not stats.flake_rising):
        return "graduate"
    if stats.window_days >= th.retire_days and stats.flake_pct >= th.max_flake_pct:
        return "retire"
    return None
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_gates.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/cicd/gates.py server/tests/test_cicd_gates.py
git commit -m "feat(cicd-doctor): graduation/demotion/retirement gate verdicts"
```

---

### Task 5: Workflow waste analyzers

**Files:**
- Create: `server/automations/cicd/waste.py`
- Test: `server/tests/test_cicd_waste.py`

**Interfaces:**
- Consumes: `Finding` from `types.py`.
- Produces: `workflow_waste_findings(repo: str, path: str, text: str) -> list[Finding]` with kinds `waste_double_run`, `waste_no_concurrency`, `waste_no_pip_cache`.

GOTCHA the implementation must handle: YAML 1.1 parses a bare `on:` key as boolean `True`, so `doc.get("on")` misses it — read `doc.get("on", doc.get(True))`.

- [ ] **Step 1: Write the failing tests**

```python
"""Workflow-YAML waste detection."""
from __future__ import annotations

from server.automations.cicd.waste import workflow_waste_findings

WASTEFUL = """
name: CI
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e .
"""

CLEAN = """
name: CI
on:
  push:
    branches: [master]
  pull_request:
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e .
"""


def test_detects_all_three_waste_kinds():
    kinds = {f.kind for f in workflow_waste_findings("o/r", "ci.yml", WASTEFUL)}
    assert kinds == {"waste_double_run", "waste_no_concurrency", "waste_no_pip_cache"}


def test_clean_workflow_no_findings():
    assert workflow_waste_findings("o/r", "ci.yml", CLEAN) == []


def test_malformed_yaml_no_findings():
    assert workflow_waste_findings("o/r", "ci.yml", ": not yaml [") == []


def test_fingerprint_keys_on_path():
    (f,) = [f for f in workflow_waste_findings("o/r", "ci.yml", WASTEFUL)
            if f.kind == "waste_double_run"]
    assert f.fingerprint == "waste_double_run:o/r:ci.yml"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_waste.py -q`
Expected: FAIL — `ModuleNotFoundError` for `waste`.

- [ ] **Step 3: Implement `server/automations/cicd/waste.py`**

```python
"""Workflow-YAML waste detection (spec Part 4b item 4). Pure text analysis."""
from __future__ import annotations

from typing import Any

import yaml

from .types import Finding


def _triggers(doc: dict) -> dict:
    on = doc.get("on", doc.get(True))  # YAML 1.1: bare `on:` parses as boolean True
    if isinstance(on, list):
        return {k: None for k in on}
    return on if isinstance(on, dict) else {}


def workflow_waste_findings(repo: str, path: str, text: str) -> list[Finding]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    findings: list[Finding] = []
    on = _triggers(doc)
    push: Any = on.get("push") if "push" in on else None
    unfiltered_push = "push" in on and not (isinstance(push, dict) and push.get("branches"))
    if unfiltered_push and "pull_request" in on:
        findings.append(Finding(
            kind="waste_double_run", repo=repo, key=path,
            title=f"{path}: unfiltered `push` + `pull_request` runs every PR twice",
            detail={"path": path, "fix": "restrict push to the default branch"}))
    if "concurrency" not in doc:
        findings.append(Finding(
            kind="waste_no_concurrency", repo=repo, key=path,
            title=f"{path}: no concurrency group — superseded runs are never cancelled",
            detail={"path": path, "fix": "add a concurrency group with cancel-in-progress"}))
    jobs = doc.get("jobs") or {}
    for job in (jobs.values() if isinstance(jobs, dict) else []):
        steps = job.get("steps") or [] if isinstance(job, dict) else []
        uses_pip = any("pip install" in str(s.get("run", "")) for s in steps if isinstance(s, dict))
        uncached_setup = any(
            str(s.get("uses", "")).startswith("actions/setup-python")
            and not (s.get("with") or {}).get("cache")
            for s in steps if isinstance(s, dict))
        if uses_pip and uncached_setup:
            findings.append(Finding(
                kind="waste_no_pip_cache", repo=repo, key=path,
                title=f"{path}: setup-python without pip cache",
                detail={"path": path, "fix": "set `cache: pip` on actions/setup-python"}))
            break  # one finding per workflow file
    return findings
```

- [ ] **Step 4: Run tests + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_waste.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/cicd/waste.py server/tests/test_cicd_waste.py
git commit -m "feat(cicd-doctor): workflow waste analyzers"
```

---

### Task 6: Finding assembly

**Files:**
- Create: `server/automations/cicd/findings.py`
- Test: `server/tests/test_cicd_findings.py`

**Interfaces:**
- Consumes: `Finding`, `GateThresholds`, outputs of `test_flake_pcts` and `check_verdict`.
- Produces: `flaky_test_findings(repo, flake_by_test, *, th) -> list[Finding]`; `check_transition_findings(repo, verdicts: dict[str, str]) -> list[Finding]` (kinds `graduate_check` / `demote_check` / `retire_check`); `missing_junit_finding(repo) -> Finding` (kind `missing_junit`). Plan C's `_analyze` calls exactly these three.

- [ ] **Step 1: Write the failing tests**

```python
"""Finding assembly from analyzer outputs."""
from __future__ import annotations

from server.automations.cicd.findings import (
    check_transition_findings, flaky_test_findings, missing_junit_finding,
)
from server.automations.cicd.types import GateThresholds

TH = GateThresholds()


def test_flaky_test_findings_threshold_and_volume():
    out = flaky_test_findings("o/r", {
        "t::hot": (8.0, 12),   # above threshold, enough shas -> finding
        "t::ok": (0.0, 30),    # clean
        "t::thin": (50.0, 2),  # too few shas to trust
    }, th=TH)
    assert [f.key for f in out] == ["t::hot"]
    assert out[0].kind == "flaky_test" and out[0].detail["flake_pct"] == 8.0


def test_check_transition_findings_kinds():
    out = check_transition_findings("o/r", {"e2e": "graduate", "frontend": "demote"})
    assert {(f.kind, f.key) for f in out} == {
        ("graduate_check", "e2e"), ("demote_check", "frontend")}


def test_missing_junit_finding():
    f = missing_junit_finding("o/r")
    assert f.kind == "missing_junit" and f.fingerprint == "missing_junit:o/r:junit"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_findings.py -q`
Expected: FAIL — `ModuleNotFoundError` for `findings`.

- [ ] **Step 3: Implement `server/automations/cicd/findings.py`**

```python
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
```

- [ ] **Step 4: Run the full new-module suite + gates**

Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_cicd_models.py server/tests/test_cicd_junit.py server/tests/test_cicd_stats.py server/tests/test_cicd_gates.py server/tests/test_cicd_waste.py server/tests/test_cicd_findings.py -q && ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/automations/cicd/findings.py server/tests/test_cicd_findings.py
git commit -m "feat(cicd-doctor): finding assembly from analyzer outputs"
```

---

### Task 7: PR

- [ ] **Step 1: Push, open PR, arm auto-merge**

```bash
git push -u origin feat/cicd-doctor-engine
gh pr create --title "feat(cicd-doctor): sensing engine — models + pure analyzers (plan B)" --body "$(cat <<'EOF'
CI/CD Doctor plan B (spec: docs/superpowers/specs/2026-07-03-cicd-doctor-design.md).

Pure, I/O-free diagnostic engine: CI history models (ci_run/ci_job/ci_test_result/ci_finding),
JUnit parser, flake + check-stability stats, graduation/demotion/retirement gate verdicts
(with hysteresis), workflow waste analyzers, and finding assembly. The control loop, GitHub
seams, and orchestrator wiring land in plan C.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Expected: PR open, non-draft, auto-merge armed; lands when `lint-and-test` + `frontend` are green.
