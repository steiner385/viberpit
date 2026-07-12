# Plotroom Pipeline Replica — Phase 1 (Core Port) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ccswitch's Pipeline tab with a verbatim source-port of plotroom's pipeline view (StatusStrip, per-repo sections, PrRow/MetroTrack, QueueTrain, CheckGantt, Waterfall, attentionSort/cohort), fed by a new SSE endpoint emitting plotroom-shaped `DashboardState`, with ccswitch session extras blended in and the old pipeline components deleted.

**Architecture:** Frontend components are copied from a pinned plotroom snapshot into `web/src/pipeline/` (a scoped CSS island under `.prdash-root`), modified only where the plan shows an exact edit. The backend gains `server/pipeline/dashboard_state.py` (pure mapping from existing `PipelinePr`/`QueueEntry`/`MergeTrainState` data to plotroom's JSON vocabulary), an SSE route `GET /api/pipeline/events`, and `POST /api/pipeline/pr/ready-merge`.

**Tech Stack:** React 19 + vitest + testing-library (web/), FastAPI + pytest (server/), plotroom sources at commit `5166b52`.

**Spec:** `docs/superpowers/specs/2026-07-12-plotroom-pipeline-replica-design.md`

## Global Constraints

- **Pinned plotroom snapshot:** all copies come from a detached worktree at commit `5166b52`. Create once (idempotent):
  `git -C /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/plotroom worktree add /tmp/plotroom-5166b52 5166b52 --detach 2>/dev/null; ls /tmp/plotroom-5166b52/frontend/src`
  Shorthand below: `$PLOT = /tmp/plotroom-5166b52/frontend/src`.
- **Verbatim discipline:** ported files get a one-line header comment `// Ported from plotroom@5166b52 frontend/src/<path>; edits: <none|list>` and are otherwise byte-identical except for edits this plan shows. Do not "improve" ported code, rename identifiers, or convert styling to Tailwind.
- **Branch:** work happens on `feat/plotroom-pipeline-phase1` created from `origin/master` (Task 1). NEVER branch from the local `master` ref. Verify `git branch --show-current` prints `feat/plotroom-pipeline-phase1` before EVERY commit.
- **Worktree has no prebuilt env.** Server tests: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest <paths> -q`. Web: run `npm ci` in `web/` once if `web/node_modules` is missing; then `npx vitest run <paths>`, `npx tsc --noEmit`, `npx eslint src`.
- **CI gates to satisfy before the PR:** `ruff check server/ src/`, `mypy server/ --ignore-missing-imports`, full pytest (`--ignore=server/tests/daemon_properties`, deselect `test_eta_parity_live` and `test_copilot_proxy_resolve` — known local-env failures), and in `web/`: vitest run + tsc + eslint.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Known-flaky CI tests** (don't chase on failure, rerun): `test_batch_unarchive_partial_failure`, `test_rearm_drain_snapshot` drain-cancel.

---

### Task 1: Branch + pure mapping module: `check_view` + `stage_result`

**Files:**
- Create: `server/pipeline/dashboard_state.py`
- Test: `server/tests/test_dashboard_state.py`

**Interfaces:**
- Consumes: `CheckRow`, `PullRequestInfo` from `server/github/pr_status.py` (fields listed in code below).
- Produces: `check_view(row: CheckRow) -> dict` and `stage_result(pr: PullRequestInfo) -> dict` — plotroom-vocabulary dicts (camelCase keys). Later tasks call these from `pr_view`.

- [ ] **Step 1: Create the branch (with the spec commit)**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/ccswitch/fcbfe508
git fetch origin
git checkout -b feat/plotroom-pipeline-phase1 origin/master
git cherry-pick 5ce5afb   # docs(spec): plotroom pipeline exact-replica design
```
Expected: branch created; `git log --oneline -1` shows the spec commit.

- [ ] **Step 2: Write the failing tests**

```python
# server/tests/test_dashboard_state.py
"""Plotroom DashboardState mapping — pure-function unit tests.

Field vocabulary mirrors plotroom@5166b52 frontend/src/types.ts (PrView,
StageResult, CheckView). Phase-1 fields ccswitch can't populate yet are
emitted as honest nulls/False — never fabricated.
"""
from server.github.pr_status import CheckGroup, CheckRow, PullRequestInfo
from server.pipeline.dashboard_state import check_view, stage_result


def _row(**kw) -> CheckRow:
    base = dict(name="test/unit", workflow="CI", status="COMPLETED",
                conclusion="success", duration_seconds=120, required=True,
                overdue=False, rem_low_seconds=None, rem_nominal_seconds=None,
                rem_high_seconds=None)
    base.update(kw)
    return CheckRow(**base)


def _pr(**kw) -> PullRequestInfo:
    base = dict(number=7, url="https://github.com/o/r/pull/7", state="open",
                checks="pending", title="t", stage="ci", repo="o/r")
    base.update(kw)
    return PullRequestInfo(**base)


def test_check_view_completed_success():
    v = check_view(_row())
    assert v == {
        "name": "test/unit", "status": "COMPLETED", "conclusion": "SUCCESS",
        "isRequired": True, "workflowName": "CI",
        "elapsedSeconds": 120, "expectedSeconds": 120, "url": None,
        "expectedLowSeconds": None, "expectedHighSeconds": None,
        "waitKind": None, "blockedOn": None, "waitingSeconds": None,
        "expectedRunnerWaitSeconds": None,
        "flakeRatePct": None, "likelyFlake": False,
        "regressed": False, "regression": None, "rerunInProgress": False,
    }


def test_check_view_in_progress_projects_expected_from_remaining():
    v = check_view(_row(status="IN_PROGRESS", conclusion="pending",
                        duration_seconds=60, rem_low_seconds=10,
                        rem_nominal_seconds=30, rem_high_seconds=90))
    assert v["conclusion"] is None            # not COMPLETED → no conclusion
    assert v["elapsedSeconds"] == 60
    assert v["expectedSeconds"] == 90         # elapsed + rem_nominal
    assert v["expectedLowSeconds"] == 70      # elapsed + rem_low
    assert v["expectedHighSeconds"] == 150    # elapsed + rem_high


def test_stage_result_open_ci():
    s = stage_result(_pr(eta_seconds=300, eta_percent=40, eta_overdue=False,
                         eta_low_seconds=200, eta_high_seconds=500))
    assert s == {"stage": "ci", "substate": None, "percent": 40,
                 "etaSeconds": 300, "etaRangeSeconds": [200, 500],
                 "overdue": False}


def test_stage_result_parked_substates():
    assert stage_result(_pr(stage="parked", state="draft", is_draft=True))["substate"] == "draft"
    assert stage_result(_pr(stage="parked", mergeable="CONFLICTING"))["substate"] == "conflicting"
    assert stage_result(_pr(stage="parked", checks="failing"))["substate"] == "ci-failed"
    assert stage_result(_pr(stage="parked"))["substate"] is None


def test_stage_result_queue_from_merge_queue_flag():
    ok = stage_result(_pr(in_merge_queue=True))
    assert ok["stage"] == "queue" and ok["substate"] is None
    failing_group = CheckGroup(kind="merge", checks=(_row(conclusion="failure"),),
                               total_seconds=0, cost_usd=None)
    bad = stage_result(_pr(in_merge_queue=True, check_groups=(failing_group,)))
    assert bad["substate"] == "group-failed"


def test_stage_result_merged_and_awaiting_prod():
    assert stage_result(_pr(state="merged", merged=True, deployed=True))["stage"] == "merged"
    s = stage_result(_pr(state="merged", merged=True, deployed=False,
                         deployed_envs=("qa",)))
    assert s["stage"] == "awaiting-prod"
    # merged, no deploy pipeline at all → plain merged
    assert stage_result(_pr(state="merged", merged=True))["stage"] == "merged"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_dashboard_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.pipeline.dashboard_state'`.

- [ ] **Step 4: Implement**

```python
# server/pipeline/dashboard_state.py
"""Assemble plotroom-shaped DashboardState JSON from ccswitch pipeline data.

Vocabulary contract: plotroom@5166b52 frontend/src/types.ts. Keys are
camelCase (this is a wire format, not internal state). Fields ccswitch
cannot populate yet (flake, regression, runner-wait, cost, timeline,
mergeEtaSim, workflow-impact) are emitted as honest nulls/False; later
phases fill them in — never fabricate.
"""
from __future__ import annotations

from server.github.pr_status import CheckGroup, CheckRow, PullRequestInfo

# Plotroom's default merge-queue batch size (config.example.json). Becomes a
# real setting in Phase 4 (spec §5); a constant until then.
DEFAULT_BATCH_SIZE = 6


def check_view(row: CheckRow) -> dict:
    elapsed = row.duration_seconds
    expected = expected_low = expected_high = None
    if row.status == "COMPLETED":
        expected = elapsed
    elif row.rem_nominal_seconds is not None:
        base = elapsed or 0
        expected = base + row.rem_nominal_seconds
        if row.rem_low_seconds is not None:
            expected_low = base + row.rem_low_seconds
        if row.rem_high_seconds is not None:
            expected_high = base + row.rem_high_seconds
    conclusion = row.conclusion.upper() if row.status == "COMPLETED" and row.conclusion else None
    return {
        "name": row.name, "status": row.status, "conclusion": conclusion,
        "isRequired": row.required, "workflowName": row.workflow,
        "elapsedSeconds": elapsed, "expectedSeconds": expected, "url": None,
        "expectedLowSeconds": expected_low, "expectedHighSeconds": expected_high,
        "waitKind": None, "blockedOn": None, "waitingSeconds": None,
        "expectedRunnerWaitSeconds": None,
        "flakeRatePct": None, "likelyFlake": False,
        "regressed": False, "regression": None, "rerunInProgress": False,
    }


def _merge_group(pr: PullRequestInfo) -> CheckGroup | None:
    for g in pr.check_groups:
        if g.kind == "merge":
            return g
    return None


def _group_failed(pr: PullRequestInfo) -> bool:
    g = _merge_group(pr)
    return bool(g and any(c.conclusion == "failure" for c in g.checks))


def stage_result(pr: PullRequestInfo) -> dict:
    if pr.merged or pr.state == "merged":
        # qa-deploy detection needs Phase-3 deploy tracking; until then a
        # merged PR on a deploy-enabled repo that hasn't reached prod is
        # awaiting-prod, and everything else is plain merged.
        stage = "awaiting-prod" if (pr.deployed_envs and not pr.deployed) else "merged"
        substate = None
    elif pr.in_merge_queue:
        stage = "queue"
        substate = "group-failed" if _group_failed(pr) else None
    else:
        stage = pr.stage or "ready"
        substate = None
        if stage == "parked":
            if pr.is_draft or pr.state == "draft":
                substate = "draft"
            elif pr.mergeable == "CONFLICTING":
                substate = "conflicting"
            elif pr.checks == "failing":
                substate = "ci-failed"
    rng = (
        [pr.eta_low_seconds, pr.eta_high_seconds]
        if pr.eta_low_seconds is not None and pr.eta_high_seconds is not None
        else None
    )
    return {"stage": stage, "substate": substate, "percent": pr.eta_percent,
            "etaSeconds": pr.eta_seconds, "etaRangeSeconds": rng,
            "overdue": pr.eta_overdue}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_dashboard_state.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Lint + typecheck + commit**

```bash
ruff check server/pipeline/dashboard_state.py server/tests/test_dashboard_state.py
mypy server/pipeline/dashboard_state.py --ignore-missing-imports
git add server/pipeline/dashboard_state.py server/tests/test_dashboard_state.py
git commit -m "feat(pipeline): plotroom CheckView/StageResult mapping (dashboard_state)"
```

---

### Task 2: `pr_view`, `queue_view`, `dashboard_state` assembly

**Files:**
- Modify: `server/pipeline/dashboard_state.py` (append)
- Test: `server/tests/test_dashboard_state.py` (append)

**Interfaces:**
- Consumes: Task 1's `check_view`/`stage_result`; `QueueEntry` from `server/github/merge_queue.py` (`pr_number, position, state, enqueued_at, batch_head_oid, group_checks, checks_total, checks_done, eta_seconds`); `MergeTrainState` from `server/pipeline/merge_train.py`; `PipelinePr` from `server/github/pr_status.py`.
- Produces:
  - `pr_view(p: PipelinePr, queue_ahead: int | None = None, fixable: bool = False) -> dict` — plotroom `PrView` + ccswitch extension key `"session": {"id","name","fixable"} | None`.
  - `queue_view(entries: Sequence[QueueEntry], train: MergeTrainState | None) -> dict` — plotroom `RepoQueueView`.
  - `dashboard_state(prs_by_repo: dict[str, list[PipelinePr]], queues: dict[str, list[QueueEntry]], trains: dict[str, MergeTrainState], has_deploy: dict[str, bool], generated_at: str) -> dict` — plotroom `DashboardState`.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_dashboard_state.py`)

```python
from server.github.merge_queue import QueueEntry
from server.github.pr_status import PipelinePr
from server.pipeline.dashboard_state import dashboard_state, pr_view, queue_view
from server.pipeline.merge_train import MergeTrainState, TrainBuild


def _entry(**kw) -> QueueEntry:
    base = dict(pr_number=1, position=1, state="AWAITING_CHECKS",
                enqueued_at=None, batch_head_oid=None, group_checks=None,
                checks_total=None, checks_done=None, eta_seconds=None)
    base.update(kw)
    return QueueEntry(**base)


def test_pr_view_shape_and_session_extension():
    p = PipelinePr(pr=_pr(checks="failing", state="open"), session_id="s1",
                   session_name="fix-auth")
    v = pr_view(p, queue_ahead=None, fixable=True)
    assert v["repo"] == "o/r" and v["number"] == 7
    assert v["stage"]["stage"] == "ci"
    assert v["session"] == {"id": "s1", "name": "fix-auth", "fixable": True}
    assert v["mergeEtaSim"] is None and v["timeline"] is None
    assert v["groupChecks"] is None and v["queueAheadCount"] is None
    p2 = PipelinePr(pr=_pr())
    assert pr_view(p2)["session"] is None


def test_queue_view_groups_waiting_and_cascade():
    entries = [
        _entry(pr_number=10, position=1, batch_head_oid="abc",
               checks_total=4, checks_done=2, eta_seconds=120, group_checks="pending"),
        _entry(pr_number=11, position=2, batch_head_oid="abc"),
        _entry(pr_number=12, position=3, state="UNMERGEABLE"),
        _entry(pr_number=13, position=4, state="UNMERGEABLE"),
        _entry(pr_number=14, position=5, state="QUEUED"),
    ]
    q = queue_view(entries, None)
    assert q["groups"] == [{"oid": "abc", "prNumbers": [10, 11], "percent": 50,
                            "etaSeconds": 120, "failed": False}]
    assert q["waiting"] == [{"prNumber": 14, "position": 5, "sim": None}]
    assert q["unmergeable"] == [12]          # lowest-position genuine conflict
    assert q["queueBlocked"] == [13]         # cascade victim behind it
    assert q["unmergeableCulprit"] == 12
    assert q["batchSize"] == 6 and q["depth"] == 5


def test_queue_view_failed_group_and_train_ops():
    entries = [_entry(pr_number=10, position=1, batch_head_oid="abc",
                      group_checks="failing")]
    train = MergeTrainState(repo="o/r", depth=1, trains_per_hour=2.5,
                            batch_success_pct=80.0, ejects_24h=3,
                            oldest_wait_seconds=600, failing=(10,),
                            building=TrainBuild(10, 50, 60), blocked=(),
                            awaiting_qa=0, awaiting_prod=0, merges_next=None)
    q = queue_view(entries, train)
    assert q["groups"][0]["failed"] is True
    assert q["trainsPerHour"] == 2.5 and q["batchSuccessRatePct"] == 80.0
    assert q["ejects24h"] == 3


def test_dashboard_state_assembly_sets_queue_ahead():
    prs = {"o/r": [PipelinePr(pr=_pr(number=14, in_merge_queue=True))]}
    queues = {"o/r": [_entry(pr_number=14, position=3, state="QUEUED")]}
    st = dashboard_state(prs, queues, {}, {"o/r": False},
                         generated_at="2026-07-12T00:00:00Z")
    repo = st["repos"][0]
    assert st["generatedAt"] == "2026-07-12T00:00:00Z" and st["staleSince"] is None
    assert repo["repo"] == "o/r" and repo["hasDeploy"] is False
    assert repo["prs"][0]["queueAheadCount"] == 2      # position - 1
    assert repo["queue"]["depth"] == 1
    # repo with no queue entries → queue is null (QueueTrain renders nothing)
    st2 = dashboard_state(prs, {}, {}, {"o/r": False}, generated_at="x")
    assert st2["repos"][0]["queue"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_dashboard_state.py -q`
Expected: FAIL — `ImportError: cannot import name 'pr_view'`.

- [ ] **Step 3: Implement** (append to `server/pipeline/dashboard_state.py`)

```python
from collections.abc import Sequence  # add to imports at top

from server.github.merge_queue import QueueEntry
from server.github.pr_status import PipelinePr
from server.pipeline.merge_train import MergeTrainState

# QueueEntry states that mean "in a building merge group" when a batch oid exists.
_BUILDING_STATES = frozenset({"AWAITING_CHECKS", "MERGEABLE", "LOCKED", "QUEUED"})


def pr_view(p: PipelinePr, queue_ahead: int | None = None, fixable: bool = False) -> dict:
    pr = p.pr
    head = next((g for g in pr.check_groups if g.kind == "head"), None)
    merge = _merge_group(pr)
    return {
        "repo": pr.repo, "number": pr.number, "title": pr.title, "url": pr.url,
        "stage": stage_result(pr),
        "mergeStateStatus": pr.merge_state_status or None,
        "queueAheadCount": queue_ahead,
        "checks": [check_view(c) for c in head.checks] if head else [],
        "timeline": None, "touchesWorkflows": False, "workflowImpact": None,
        "groupChecks": [check_view(c) for c in merge.checks] if (merge and pr.in_merge_queue) else None,
        "mergeEtaSim": None,
        "costMinutes": None, "costDollars": None, "costDollarsPartial": False,
        # ccswitch extension (spec §6): the authoring session + Fix eligibility.
        "session": (
            {"id": p.session_id, "name": p.session_name, "fixable": fixable}
            if p.session_id else None
        ),
    }


def queue_view(entries: Sequence[QueueEntry], train: MergeTrainState | None) -> dict:
    groups: dict[str, dict] = {}
    waiting: list[dict] = []
    unmergeable: list[int] = []
    blocked: list[int] = []
    conflict_seen = False
    for e in sorted(entries, key=lambda e: (e.position is None, e.position or 0)):
        if e.state == "UNMERGEABLE":
            # GitHub marks cascade victims UNMERGEABLE too; without a DIRTY
            # snapshot, presume the front-most is the genuine conflict
            # (plotroom types.ts unmergeableCulprit contract).
            (blocked if conflict_seen else unmergeable).append(e.pr_number)
            conflict_seen = True
        elif e.batch_head_oid and e.state in _BUILDING_STATES:
            g = groups.setdefault(e.batch_head_oid, {
                "oid": e.batch_head_oid, "prNumbers": [],
                "percent": None, "etaSeconds": None, "failed": False,
            })
            g["prNumbers"].append(e.pr_number)
            if e.checks_total and e.checks_done is not None:
                g["percent"] = round(100 * e.checks_done / e.checks_total)
            if e.eta_seconds is not None:
                g["etaSeconds"] = e.eta_seconds
            if e.group_checks == "failing":
                g["failed"] = True
        else:
            waiting.append({"prNumber": e.pr_number, "position": e.position or 0,
                            "sim": None})
    return {
        "groups": list(groups.values()), "waiting": waiting,
        "unmergeable": unmergeable, "queueBlocked": blocked,
        "unmergeableCulprit": unmergeable[0] if unmergeable else None,
        "batchSize": DEFAULT_BATCH_SIZE,
        "depth": train.depth if train else len(entries),
        "trainsPerHour": train.trains_per_hour if train else None,
        "batchSuccessRatePct": train.batch_success_pct if train else None,
        "ejects24h": train.ejects_24h if train else 0,
    }


def _fixable(pr: PullRequestInfo) -> bool:
    return pr.checks == "failing" and pr.state in ("open", "draft")


def dashboard_state(
    prs_by_repo: dict[str, list[PipelinePr]],
    queues: dict[str, list[QueueEntry]],
    trains: dict[str, MergeTrainState],
    has_deploy: dict[str, bool],
    generated_at: str,
) -> dict:
    repos = []
    for repo in sorted(prs_by_repo):
        entries = queues.get(repo, [])
        pos = {e.pr_number: e.position for e in entries}
        prs = [
            pr_view(
                p,
                queue_ahead=(pos[p.pr.number] - 1)
                if pos.get(p.pr.number) is not None else None,
                fixable=_fixable(p.pr),
            )
            for p in prs_by_repo[repo]
        ]
        repos.append({
            "repo": repo, "hasDeploy": has_deploy.get(repo, False),
            "prs": prs,
            "queue": queue_view(entries, trains.get(repo)) if entries else None,
        })
    return {"generatedAt": generated_at, "staleSince": None, "repos": repos}
```

- [ ] **Step 4: Run tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_dashboard_state.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
ruff check server/pipeline/dashboard_state.py server/tests/test_dashboard_state.py
mypy server/pipeline/dashboard_state.py --ignore-missing-imports
git add -A server/pipeline server/tests/test_dashboard_state.py
git commit -m "feat(pipeline): PrView/RepoQueueView/DashboardState assembly"
```

---

### Task 3: Orchestrator accessor for queue entries + SSE endpoint

**Files:**
- Modify: `server/sessions/orchestrator.py` (near `all_merge_trains`, orchestrator.py:3138)
- Modify: the daemon RPC layer — find with `grep -rn "all_merge_trains" server/ --include="*.py" | grep -v tests` and mirror EVERY registration site (RemoteOrchestrator proxy + RPC handler table). A new orch method an API route calls MUST have a RemoteOrchestrator proxy + registered RPC, or breadth tests fail.
- Modify: `server/api/pipeline.py` (add SSE route on the existing `repos_router`, prefix `/api/pipeline`)
- Test: `server/tests/test_pipeline_events.py`

**Interfaces:**
- Consumes: Task 2's `dashboard_state(...)`; existing `orch.all_prs()` (list[PipelinePr], each pr has `.repo`), `orch.all_merge_trains()` (dict[str, MergeTrainState]), `orch.all_repos_stage_models()` (dict[str, StageModel]; a repo `hasDeploy` iff any node.kind == "deploy").
- Produces:
  - Orchestrator method `latest_queue_entries(self) -> dict[str, list[QueueEntry]]` (latest observation per repo from the merge-queue store; mirror how `all_merge_trains` reads it — see orchestrator.py:3138-3197).
  - `GET /api/pipeline/events` — SSE (`text/event-stream`); every ~10 s emits `data: <DashboardState JSON>\n\n`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pipeline_events.py
"""SSE feed: /api/pipeline/events streams DashboardState frames."""
import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

# Follow the app/client fixture idiom used by server/tests/test_api_pipeline*.py
# (or the nearest existing API test) — reuse its fixtures rather than
# re-inventing app construction. The test below assumes an `app` fixture that
# yields a FastAPI app with app.state.sessions set to the test orchestrator
# and CCSWITCH_DEV auth satisfied.


@pytest.mark.asyncio
async def test_events_stream_emits_dashboard_state_frame(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("GET", "/api/pipeline/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frame = json.loads(line[len("data: "):])
                    break
    assert "generatedAt" in frame and isinstance(frame["repos"], list)
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_pipeline_events.py -q`
Expected: FAIL — 404 (route missing) or fixture error to resolve first by copying the existing API-test fixture.

- [ ] **Step 3: Implement the orchestrator accessor**

In `server/sessions/orchestrator.py`, next to `all_merge_trains` (orchestrator.py:3138 — it awaits the module-level async `merge_queue_store.window(repo, since)` which returns `list[Observation]`, each with `.captured_at` and `.entries: tuple[QueueEntry, ...]`), add:

```python
async def latest_queue_entries(self) -> "dict[str, list[QueueEntry]]":
    """Newest merge-queue observation per repo (for the pipeline SSE feed).

    Same 24-hour window all_merge_trains reads; only the last (newest)
    observation matters here — it is the queue's current shape.
    """
    from server.pipeline import merge_queue_store
    import time as _time
    since = int(_time.time()) - 86400
    out: dict[str, list[QueueEntry]] = {}
    for repo in self._pr_population.all():
        obs = await merge_queue_store.window(repo, since)
        if obs:
            out[repo] = list(obs[-1].entries)
    return out
```
Then add the RemoteOrchestrator proxy + RPC registration by copying the `all_merge_trains` pattern at every site `grep` found.

- [ ] **Step 4: Implement the SSE route** (append to `server/api/pipeline.py`)

```python
import asyncio
import json
from datetime import datetime, timezone

from fastapi.responses import StreamingResponse

from server.pipeline.dashboard_state import dashboard_state

_EVENTS_INTERVAL_S = 10.0


async def _state_frame(orch) -> str:
    prs = await orch.all_prs()
    trains = await orch.all_merge_trains()
    queues = await orch.latest_queue_entries()
    models = orch.all_repos_stage_models()
    if inspect.isawaitable(models):
        models = await models
    prs_by_repo: dict[str, list] = {}
    for p in prs:
        prs_by_repo.setdefault(p.pr.repo, []).append(p)
    has_deploy = {
        repo: any(n.kind == "deploy" for n in m.nodes) for repo, m in models.items()
    }
    state = dashboard_state(
        prs_by_repo, queues, trains, has_deploy,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return f"data: {json.dumps(state)}\n\n"


@repos_router.get("/events")
async def pipeline_events(
    request: Request,
    _identity: str = Depends(current_identity),
) -> StreamingResponse:
    orch = request.app.state.sessions

    async def gen():
        while True:
            if await request.is_disconnected():
                return
            yield await _state_frame(orch)
            await asyncio.sleep(_EVENTS_INTERVAL_S)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 5: Run tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_pipeline_events.py server/tests/test_dashboard_state.py -q`
Expected: PASS. Also run the API breadth test that checks RemoteOrchestrator coverage (find with `grep -rln "RemoteOrchestrator" server/tests/`) — PASS.

- [ ] **Step 6: Lint + typecheck + commit**

```bash
ruff check server/ && mypy server/ --ignore-missing-imports
git add -A server/
git commit -m "feat(pipeline): /api/pipeline/events SSE DashboardState feed"
```

---

### Task 4: `POST /api/pipeline/pr/ready-merge`

**Files:**
- Modify: `server/sessions/orchestrator.py` (+ RemoteOrchestrator proxy + RPC registration, same drill as Task 3)
- Modify: `server/api/pipeline.py`
- Test: `server/tests/test_ready_merge.py`

**Interfaces:**
- Consumes: existing GitHub mutation helpers — find the established pattern with `grep -rn "gh pr\|_run_gh\|gh_json" server/github/ server/sessions/orchestrator.py | grep -v tests | head`; reuse it (do not spawn raw subprocesses if a helper exists).
- Produces: `POST /api/pipeline/pr/ready-merge` body `{"repo": "o/r", "number": 7}` → 200 `{"markedReady": bool, "autoMergeArmed": bool}`; orch method `ready_merge(repo: str, number: int) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ready_merge.py
"""Ready + auto-merge action (plotroom PrRow parity)."""
import pytest


@pytest.mark.asyncio
async def test_ready_merge_route_calls_orchestrator(app, client, monkeypatch):
    # Reuse the app/client fixtures from the existing API tests (same file
    # you copied for test_pipeline_events.py). Stub the orchestrator method:
    calls = []

    async def fake_ready_merge(repo, number):
        calls.append((repo, number))
        return {"markedReady": True, "autoMergeArmed": True}

    app.state.sessions.ready_merge = fake_ready_merge
    resp = await client.post("/api/pipeline/pr/ready-merge",
                             json={"repo": "o/r", "number": 7})
    assert resp.status_code == 200
    assert resp.json() == {"markedReady": True, "autoMergeArmed": True}
    assert calls == [("o/r", 7)]


@pytest.mark.asyncio
async def test_ready_merge_validates_body(app, client):
    resp = await client.post("/api/pipeline/pr/ready-merge", json={"repo": "o/r"})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run to verify failure** — 404. Same command shape as Task 3.

- [ ] **Step 3: Implement**

Route (append to `server/api/pipeline.py`):

```python
from pydantic import BaseModel


class ReadyMergeBody(BaseModel):
    repo: str
    number: int


@repos_router.post("/pr/ready-merge")
async def ready_merge(
    body: ReadyMergeBody,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    return await orch.ready_merge(body.repo, body.number)
```

Orchestrator method — the repo's gh helper is `gitrepos._run_gh(args: list[str], token=None, *, input=None) -> subprocess.CompletedProcess[str]` (gitrepos.py:70; the orchestrator already holds it as `self._gh_run`, orchestrator.py:356). Subprocess calls must not block the event loop — wrap in `asyncio.to_thread`:

```python
async def ready_merge(self, repo: str, number: int) -> dict:
    """Plotroom PrRow 'Ready + auto-merge' action: mark a draft PR ready,
    then arm squash auto-merge. Tolerates already-ready / already-armed."""
    import asyncio

    def _do() -> dict:
        ready = self._gh_run(["pr", "ready", str(number), "-R", repo])
        # gh exits non-zero when the PR is already ready — that's not an error.
        marked = ready.returncode == 0
        merge = self._gh_run(
            ["pr", "merge", str(number), "-R", repo, "--squash", "--auto"]
        )
        armed = merge.returncode == 0 or "already enabled" in (merge.stderr or "")
        return {"markedReady": marked, "autoMergeArmed": armed}

    return await asyncio.to_thread(_do)
```
Add the RemoteOrchestrator proxy + RPC registration (same drill as Task 3).

- [ ] **Step 4: Run tests + breadth test** — PASS.
- [ ] **Step 5: Lint + typecheck + commit**

```bash
ruff check server/ && mypy server/ --ignore-missing-imports
git add -A server/ && git commit -m "feat(pipeline): ready+auto-merge mutation endpoint"
```

---

### Task 5: Frontend scaffold — types, utils, ApiBase, scoped CSS

**Files:**
- Create: `web/src/pipeline/types.ts`, `web/src/pipeline/format.ts`, `web/src/pipeline/motion.ts`, `web/src/pipeline/leadtime.ts`, `web/src/pipeline/definitions.ts`, `web/src/pipeline/notification-events.ts`, `web/src/pipeline/ApiBaseContext.tsx`, `web/src/pipeline/plotroom.css`
- Test: `web/src/pipeline/__tests__/format.test.ts` (ported from `$PLOT/__tests__/format.test.ts`)

**Interfaces:**
- Produces (consumed by every later frontend task): the plotroom type vocabulary (`StageId`, `StageResult`, `CheckView`, `PrView` — extended with `session?: { id: string; name: string; fixable: boolean } | null` —, `MergeEtaSimulation`, `QueueGroupView`, `RepoQueueView`, `DashboardState`, `PrTimeline`, `LeadTimeSegmentId`, `NotificationEvent`), `formatDur/formatSince/formatEta/stageLabel/STAGE_LABEL`, `scrollBehavior()`, `LEAD_TIME_SEGMENTS`, `DEFS/defTitle/subLineTitle`, `ApiBaseProvider/useApiBase`, and the `.prdash-root`-scoped stylesheet.

- [ ] **Step 1: Copy verbatim files**

```bash
mkdir -p web/src/pipeline/__tests__
cp /tmp/plotroom-5166b52/frontend/src/format.ts        web/src/pipeline/format.ts
cp /tmp/plotroom-5166b52/frontend/src/motion.ts        web/src/pipeline/motion.ts
cp /tmp/plotroom-5166b52/frontend/src/leadtime.ts      web/src/pipeline/leadtime.ts
cp /tmp/plotroom-5166b52/frontend/src/definitions.ts   web/src/pipeline/definitions.ts
cp /tmp/plotroom-5166b52/shared/notification-events.ts web/src/pipeline/notification-events.ts
cp /tmp/plotroom-5166b52/frontend/src/embed/ApiBaseContext.tsx web/src/pipeline/ApiBaseContext.tsx
cp /tmp/plotroom-5166b52/frontend/src/__tests__/format.test.ts web/src/pipeline/__tests__/format.test.ts
```
Add the `// Ported from plotroom@5166b52 …` header to each; fix `format.test.ts`'s import to `../format`.

- [ ] **Step 2: Create `web/src/pipeline/types.ts`** — copy `$PLOT/types.ts`, then:
  1. Change the notification-events import to `./notification-events`.
  2. Delete the sections NOT needed by the pipeline island (constitution VI — no dead code): everything from `// ---- Config API mirrors` through the end of `RunnerPlanResponse`, and the `MetricsPayload`/`DemotionCandidate`/`PromotionCandidate`/`HeadlineStat`/`MetricsWindow`/`MetricsBucket` block, and `Lane`/`LaneView` (keep `LaneStatus` — `DashboardState.laneHealth` references it). Keep: `StageId…PrTimeline`, `PrView`, `MergeEtaSimulation`, `QueueHealth*`, `QueueGroupView`, `RepoQueueView`, `DashboardState`, `NotificationEvent`, `NotificationsConfig` may go, `LeadTimeSegmentId` (move up from the metrics block — `Waterfall`/`leadtime.ts` import it).
  3. Extend `PrView` (mark the edit in the header comment):
```ts
  /** ccswitch extension (spec §6): the authoring ccswitch session, if any. */
  session?: { id: string; name: string; fixable: boolean } | null;
```
  4. Run `npx tsc --noEmit` scoped sanity later in Step 5 — `definitions.ts`/`leadtime.ts` must still typecheck against the trimmed types; if `definitions.ts` references a deleted type, restore that type rather than editing `definitions.ts`.

- [ ] **Step 3: Create the scoped stylesheet**

```bash
cp /tmp/plotroom-5166b52/frontend/src/styles.css web/src/pipeline/plotroom.css
```
Then scope it (native CSS nesting — Vite/Lightning CSS handles downleveling):
  1. Replace the top-level `:root {` selector with `.prdash-root {` (custom properties then cascade to the island only).
  2. Wrap ALL remaining rules in one block: after the (former) `:root` rule, open `.prdash-root {` and close `}` at end of file, so every selector nests under the island root. Rules already inside `@media` blocks nest fine.
  3. Hoist every `@keyframes` block (grep `@keyframes` — 5 of them: `conn-pulse`, `pulse`, `settings-slide-in`, `skeleton-shimmer`, +1) to the TOP level (outside the wrapper) and rename each to `prdash-<name>` (`pulse` collides with Tailwind's). Update every `animation:`/`animation-name:` reference accordingly (`grep -n "animation" web/src/pipeline/plotroom.css`).
  4. Delete rules that target plotroom shell chrome the island never renders (`body`, `html` resets if any at top level) rather than nesting them.
Verification: `cd web && npx vite build 2>&1 | tail -5` → build succeeds, no CSS syntax errors.

- [ ] **Step 4: Run the ported format test**

Run: `cd web && npx vitest run src/pipeline/__tests__/format.test.ts`
Expected: PASS (unmodified plotroom expectations).

- [ ] **Step 5: Typecheck + lint + commit**

```bash
cd web && npx tsc --noEmit && npx eslint src/pipeline
git add web/src/pipeline
git commit -m "feat(web/pipeline): plotroom scaffold — types, utils, scoped stylesheet"
```
(If eslint objects to plotroom idioms, add a `web/src/pipeline/.eslintrc`-style override or file-level disables for the ported files — do NOT rewrite ported code to appease lint.)

---

### Task 6: `useDashboard` SSE hook

**Files:**
- Create: `web/src/pipeline/useDashboard.ts`
- Test: `web/src/pipeline/__tests__/useDashboard.test.ts`

**Interfaces:**
- Consumes: `ApiBaseContext` (Task 5). The hook opens `EventSource(`${base}/events`)`.
- Produces: `useDashboard(): DashboardHook` — `{ state, connected, stale, notifyEnabled, setNotifyEnabled, … }` exactly as plotroom's (read `$PLOT/useDashboard.ts:30-43` for the full interface). Consumed by Task 10's `PipelineTab`.

- [ ] **Step 1: Copy + adjust imports**

```bash
cp /tmp/plotroom-5166b52/frontend/src/useDashboard.ts web/src/pipeline/useDashboard.ts
```
Edit imports only: `./types` stays, `./embed/ApiBaseContext` → `./ApiBaseContext`. No other changes.

- [ ] **Step 2: Write the test** (jsdom has no EventSource — stub it)

```ts
// web/src/pipeline/__tests__/useDashboard.test.ts
import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { ApiBaseProvider } from '../ApiBaseContext'
import { useDashboard } from '../useDashboard'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {}
  constructor(url: string) { this.url = url; FakeEventSource.instances.push(this) }
  addEventListener(type: string, fn: (ev: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn)
  }
  close() {}
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <ApiBaseProvider base="/api/pipeline">{children}</ApiBaseProvider>
)

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
})

describe('useDashboard', () => {
  it('connects to <base>/events and applies full-state frames', async () => {
    const { result } = renderHook(() => useDashboard(), { wrapper })
    expect(FakeEventSource.instances[0].url).toBe('/api/pipeline/events')
    const frame = { generatedAt: 'now', staleSince: null, repos: [] }
    act(() => {
      FakeEventSource.instances[0].onmessage?.(
        new MessageEvent('message', { data: JSON.stringify(frame) }))
    })
    await waitFor(() => expect(result.current.state).toEqual(frame))
    expect(result.current.connected).toBe(true)
  })
})
```
(File must be `.tsx` if JSX in wrapper — name it `useDashboard.test.tsx`.)
Adjust the assertion details to plotroom's actual hook shape after reading it — if the hook uses `addEventListener('message', …)` instead of `onmessage`, drive `listeners['message']`.

- [ ] **Step 3: Run** — `cd web && npx vitest run src/pipeline/__tests__/useDashboard.test.tsx`. Expected: PASS (fix the stub-driving detail, never the hook).
- [ ] **Step 4: Commit** — `git add web/src/pipeline && git commit -m "feat(web/pipeline): useDashboard SSE hook (ported)"`.

---

### Task 7: StatusStrip + ordering + queueFront (ported, with ported tests)

**Files:**
- Create: `web/src/pipeline/StatusStrip.tsx`, `web/src/pipeline/ordering.ts`, `web/src/pipeline/queueFront.ts`
- Test: `web/src/pipeline/__tests__/StatusStrip.test.tsx`, `__tests__/ordering.test.ts`, `__tests__/queueFront.test.ts`

**Interfaces:**
- Consumes: `types.ts`, `definitions.ts` (Task 5).
- Produces: `StatusStrip({prs, activeFilter, onFilter, interactive})`, `bucketPr/isFailedPr/isActivePr/TILE_DEFINITIONS`, `type Bucket`; `attentionRank/attentionSort/splitCohort/deployBreakdown`; `nextToMerge(queue) → NextToMerge | null`. Consumed by Task 10's PipelineView.

- [ ] **Step 1: Copy + adjust imports**

```bash
cp $PLOT/StatusStrip.tsx                    web/src/pipeline/StatusStrip.tsx
cp $PLOT/sections/pipeline/ordering.ts      web/src/pipeline/ordering.ts
cp $PLOT/sections/pipeline/queueFront.ts    web/src/pipeline/queueFront.ts
cp $PLOT/__tests__/StatusStrip.test.tsx     web/src/pipeline/__tests__/StatusStrip.test.tsx
cp $PLOT/__tests__/pipelineOrdering.test.ts web/src/pipeline/__tests__/ordering.test.ts
cp $PLOT/__tests__/queueFront.test.ts       web/src/pipeline/__tests__/queueFront.test.ts
```
Import fixes only: `../../types` → `./types` (sources) / `../types` (tests), `../../StatusStrip` → `../StatusStrip`, etc. Read each file's imports and repoint mechanically.

- [ ] **Step 2: Run the ported tests**

`cd web && npx vitest run src/pipeline/__tests__/StatusStrip.test.tsx src/pipeline/__tests__/ordering.test.ts src/pipeline/__tests__/queueFront.test.ts`
Expected: PASS with zero assertion edits. If an assertion fails, the port deviated — fix the port, not the test. (Timer-based failed-delta tests may need `vi.useFakeTimers()` exactly as plotroom's test already does — keep it verbatim.)

- [ ] **Step 3: Commit** — `git add web/src/pipeline && git commit -m "feat(web/pipeline): StatusStrip + attention ordering + queueFront (ported)"`.

---

### Task 8: MetroTrack + CheckGantt + Waterfall (ported, with ported tests)

**Files:**
- Create: `web/src/pipeline/MetroTrack.tsx`, `web/src/pipeline/CheckGantt.tsx`, `web/src/pipeline/Waterfall.tsx`
- Test: ported `__tests__/MetroTrack.test.tsx`, `__tests__/CheckGantt.test.tsx`, `__tests__/Waterfall.test.tsx`

**Interfaces:**
- Consumes: `types.ts`, `format.ts`, `leadtime.ts` (Task 5).
- Produces: `MetroTrack({stage, hasDeploy})`, `trackState/nodeTitle`; `CheckGantt({checks, stage, showLegend})`, `ganttScale/groupByWorkflow`; `Waterfall({timeline})`, `waterfallSegments`. Consumed by Task 9's PrRow.

- [ ] **Step 1: Copy + repoint imports** (same mechanical drill as Task 7; sources import `./types`, `./format`, `./leadtime`).
- [ ] **Step 2: Run ported tests** — `npx vitest run src/pipeline/__tests__/{MetroTrack,CheckGantt,Waterfall}.test.tsx`. Expected: PASS unmodified.
- [ ] **Step 3: Commit** — `git commit -m "feat(web/pipeline): MetroTrack + CheckGantt + Waterfall (ported)"`.

---

### Task 9: QueueTrain + PrRow with blended session extras

**Files:**
- Create: `web/src/pipeline/QueueTrain.tsx`, `web/src/pipeline/PrRow.tsx`
- Modify: `web/src/pipeline/plotroom.css` (append ccswitch-extension styles)
- Test: ported `__tests__/QueueTrain.test.tsx`, `__tests__/PrRow.test.tsx` + new `__tests__/PrRow.session.test.tsx`

**Interfaces:**
- Consumes: Tasks 5–8 exports; `useApiBase` (PrRow's ready-merge POST goes to `${base}/pr/ready-merge` → `/api/pipeline/pr/ready-merge`, matching Task 4).
- Produces: `QueueTrain({queue})`; `PrRow({pr, hasDeploy, queueCulprit, expandable, onFixPr?})` + exports `subLine/readyMergeGate/areEqual`. `onFixPr?: (sessionId: string, prNumber: number) => void` is the ccswitch extension.

- [ ] **Step 1: Copy + repoint imports**

```bash
cp $PLOT/QueueTrain.tsx web/src/pipeline/QueueTrain.tsx
cp $PLOT/PrRow.tsx      web/src/pipeline/PrRow.tsx
cp $PLOT/__tests__/QueueTrain.test.tsx web/src/pipeline/__tests__/QueueTrain.test.tsx
cp $PLOT/__tests__/PrRow.test.tsx      web/src/pipeline/__tests__/PrRow.test.tsx
```
PrRow import fix: `./embed/ApiBaseContext` → `./ApiBaseContext`; others mechanical.

- [ ] **Step 2: Run ported tests first** — they must PASS before the blend edit (proves the verbatim port). Tests that render PrRow need the ApiBase provider — plotroom's own test file already wraps it; keep whatever it does.

- [ ] **Step 3: Write the failing blended-extras test**

```tsx
// web/src/pipeline/__tests__/PrRow.session.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ApiBaseProvider } from '../ApiBaseContext'
import { PrRow } from '../PrRow'
import type { PrView } from '../types'

const pr = (over: Partial<PrView> = {}): PrView => ({
  repo: 'o/r', number: 7, title: 'fix auth', url: 'https://x/pull/7',
  stage: { stage: 'ci', substate: null, percent: null, etaSeconds: null,
           etaRangeSeconds: null, overdue: false },
  queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
  session: { id: 's1', name: 'fix-auth', fixable: false },
  ...over,
})

const wrap = (ui: React.ReactNode) => render(
  <MemoryRouter><ApiBaseProvider base="/api/pipeline">{ui}</ApiBaseProvider></MemoryRouter>)

describe('PrRow ccswitch extras', () => {
  it('shows the session label linking to the session', () => {
    wrap(<PrRow pr={pr()} hasDeploy={false} queueCulprit={null} />)
    const link = screen.getByRole('link', { name: /fix-auth/ })
    expect(link).toHaveAttribute('href', '/session/s1')
  })
  it('shows Fix only when fixable, and calls onFixPr', () => {
    const onFix = vi.fn()
    wrap(<PrRow pr={pr({ session: { id: 's1', name: 'fix-auth', fixable: true } })}
                hasDeploy={false} queueCulprit={null} onFixPr={onFix} />)
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    expect(onFix).toHaveBeenCalledWith('s1', 7)
  })
  it('renders no session UI when session is null', () => {
    wrap(<PrRow pr={pr({ session: null })} hasDeploy={false} queueCulprit={null} />)
    expect(screen.queryByRole('link', { name: /fix-auth/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Fix' })).toBeNull()
  })
})
```

- [ ] **Step 4: Run to verify failure**, then implement the blend in `PrRow.tsx`:
  1. Add `import { Link } from 'react-router-dom';` and extend the props type with `onFixPr?: (sessionId: string, prNumber: number) => void;` (thread through `PrRowProps` and `areEqual` — `onFixPr` identity must NOT break memoization: compare like plotroom compares other callbacks, or exclude it and require a stable callback from the caller).
  2. In the `pr-head` row, after the ETA chip, add:
```tsx
{pr.session && (
  <span className="pr-session-wrap" onClick={(e) => e.stopPropagation()}>
    <Link className="pr-session" to={`/session/${pr.session.id}`}
          title={`Open session ${pr.session.name}`}>⌗ {pr.session.name}</Link>
    {pr.session.fixable && onFixPr && (
      <button type="button" className="pr-fix"
              onClick={() => onFixPr(pr.session!.id, pr.number)}>Fix</button>
    )}
  </span>
)}
```
  (Keep it OUT of any element with a button role — nested-interactive a11y rule; `stopPropagation` guards the row-expand handler like plotroom's `.pr-title` does.)
  3. Append to `plotroom.css` inside the `.prdash-root` wrapper, in a marked section:
```css
/* --- ccswitch extensions (spec §6): session label + Fix action --- */
.pr-session-wrap { margin-left: auto; display: inline-flex; gap: 6px; align-items: center; }
.pr-session { font-size: 11px; color: var(--muted, #8b949e); text-decoration: none; }
.pr-session:hover { text-decoration: underline; }
.pr-fix { font-size: 11px; padding: 1px 8px; border-radius: 4px;
  border: 1px solid #d29922; color: #d29922; background: transparent; cursor: pointer; }
```
- [ ] **Step 5: Run all Task-9 tests** — ported + session tests PASS; re-run PrRow.test.tsx to prove the blend broke nothing.
- [ ] **Step 6: Commit** — `git commit -m "feat(web/pipeline): QueueTrain + PrRow with blended session label/Fix (ported)"`.

---

### Task 10: PipelineView + PipelineTab wiring; replace the old tab

**Files:**
- Create: `web/src/pipeline/PipelineView.tsx`, `web/src/pipeline/PipelineTab.tsx`
- Modify: every old-PipelineView consumer — find with `grep -rn "PipelineView" web/src --include="*.tsx" --include="*.ts" | grep -v src/pipeline | grep -v test` (expect `Dashboard.tsx`, `DesktopShell.tsx`, `tabDefs.ts`).
- Test: ported `__tests__/PipelineView.test.tsx` + new `__tests__/PipelineTab.test.tsx`

**Interfaces:**
- Consumes: everything from Tasks 5–9; `useFixPr` from `@/api/hooks` (hooks.ts:243).
- Produces: `PipelineView({state, focusedRepo, onFixPr?})` (ported; signature gains optional `onFixPr` threaded to `PrRow`); `PipelineTab()` — the drop-in tab component (provider + `.prdash-root` wrapper + scroll container).

- [ ] **Step 1: Copy + repoint**

```bash
cp $PLOT/sections/pipeline/PipelineView.tsx web/src/pipeline/PipelineView.tsx
cp $PLOT/__tests__/PipelineView.test.tsx    web/src/pipeline/__tests__/PipelineView.test.tsx
```
Import fixes (`../../PrRow` → `./PrRow` etc.). Thread `onFixPr`: add to props `onFixPr?: (sessionId: string, prNumber: number) => void`, and in the `row()` helper pass `onFixPr={onFixPr}` to `PrRow`.

- [ ] **Step 2: Write `PipelineTab.tsx`**

```tsx
// web/src/pipeline/PipelineTab.tsx
// ccswitch host wrapper for the ported plotroom pipeline island (spec §2, §6).
// focusedRepo is null until the PipelineSwitcher lands (Phase 2).
import { useCallback } from 'react'
import { useFixPr } from '@/api/hooks'
import { ApiBaseProvider } from './ApiBaseContext'
import { useDashboard } from './useDashboard'
import { PipelineView } from './PipelineView'
import './plotroom.css'

function Inner() {
  const fixPr = useFixPr()
  const onFixPr = useCallback(
    (id: string, number: number) => fixPr.mutate({ id, number }),
    [fixPr.mutate],
  )
  const { state, stale } = useDashboard()
  return (
    <>
      {stale && <p className="pipeline-stale" role="status">data stale — reconnecting…</p>}
      <PipelineView state={state} focusedRepo={null} onFixPr={onFixPr} />
    </>
  )
}

export function PipelineTab() {
  return (
    <ApiBaseProvider base="/api/pipeline">
      <div className="prdash-root flex-1 min-h-0 overflow-y-auto">
        <Inner />
      </div>
    </ApiBaseProvider>
  )
}
```
(Check plotroom's stale-banner idiom in `$PLOT/App.tsx` first; if it has a canonical class/copy for staleness, use that verbatim instead of `pipeline-stale`.)

- [ ] **Step 3: Ported PipelineView test green**, plus a PipelineTab smoke test:

```tsx
// web/src/pipeline/__tests__/PipelineTab.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PipelineTab } from '../PipelineTab'

vi.stubGlobal('EventSource', class {
  onmessage = null; onerror = null
  addEventListener() {} close() {}
} as unknown as typeof EventSource)

it('renders the loading state before the first SSE frame', () => {
  const qc = new QueryClient()
  render(<MemoryRouter><QueryClientProvider client={qc}><PipelineTab /></QueryClientProvider></MemoryRouter>)
  expect(screen.getByRole('status')).toHaveTextContent('Loading pipeline…')
})
```

- [ ] **Step 4: Swap the tab.** In each consumer found by the grep, replace the old `PipelineView` import/usage with `import { PipelineTab } from '@/pipeline/PipelineTab'` and render `<PipelineTab />` (it takes no props — the old `onOpenSession`/`onFixPr` wiring dies here; session navigation now lives inside PrRow). Update `tabDefs.ts` metadata only if it names the component.
- [ ] **Step 5: Run the whole web suite** — `cd web && npx vitest run`. Expected: old pipeline tests now fail/orphaned — deleting them is Task 11; everything else PASS. If Dashboard/DesktopShell tests mock the old component path, update those mocks to `@/pipeline/PipelineTab`.
- [ ] **Step 6: Commit** — `git commit -m "feat(web/pipeline): ported PipelineView + PipelineTab wired into the dashboard"`.

---

### Task 11: Delete the old pipeline implementation

**Files:**
- Delete: `web/src/components/PipelineView.tsx`, `StageTiles.tsx`, `StageTrack.tsx`, `MergeTrainWidget.tsx`, `CheckDrillDown.tsx`, `CheckRowList.tsx`, `GanttBar.tsx`, `web/src/lib/checkState.ts`, `web/src/session/stageBucket.ts`, and their tests (`__tests__/StageTiles.test.tsx`, `__tests__/PipelineTab.test.tsx` (old), `__tests__/PipelineBoard.test.tsx`, `PipelineView.mergeTrain.test.tsx`, `session/__tests__/stageBucket.test.ts`, `api/__tests__/useStageModels.test.ts` if the hook goes).
- Modify: `web/src/api/hooks.ts` — remove `useStageModels`/`useMergeTrains` IF the Task-11 grep proves nothing else uses them (`useAllPrs` and `useFixPr` STAY); `server/api/pipeline.py` — remove `/api/pipeline/repos` + `/api/pipeline/trains` routes only if the same grep shows no remaining frontend consumer; keep `GET /api/prs` (Dashboard.tsx:559, DesktopShell.tsx:340).

**Interfaces:** none produced — this task is the constitution-VI purge.

- [ ] **Step 1: Prove deadness** — `cd web && grep -rn "StageTiles\|StageTrack\|MergeTrainWidget\|CheckDrillDown\|CheckRowList\|GanttBar\|checkState\|stageBucket\|useStageModels\|useMergeTrains" src | grep -v src/pipeline` → only hits inside the files being deleted. Any external hit = STOP and resolve before deleting.
- [ ] **Step 2: Delete files, prune hooks/routes per grep evidence.** Keep the backend `StageModel`/track machinery — `all_repos_stage_models` still feeds `hasDeploy` in the SSE assembler (Task 3).
- [ ] **Step 3: Full suites green**
```bash
cd web && npx vitest run && npx tsc --noEmit && npx eslint src
cd .. && ruff check server/ src/ && mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ --ignore=server/tests/daemon_properties -q \
  --deselect server/tests/test_eta_parity_live.py -k "not copilot_proxy_resolve"
```
Expected: all PASS.
- [ ] **Step 4: Commit** — `git commit -m "refactor(web): delete superseded pipeline components (plotroom port replaces them)"`.

---

### Task 12: Verify against the running app + PR

**Files:** none new.

- [ ] **Step 1: Manual verification** — run the dev server (backend + `cd web && npx vite`), open the Pipeline tab: StatusStrip tiles render with counts; repos collapse/expand; a PR row expands to CheckGantt; queue repos show the QueueTrain; session-linked PRs show the label; a failing session PR shows Fix. Screenshot if the environment allows.
- [ ] **Step 2: Re-run every gate from Task 11 Step 3** (fresh, from a clean `git status`).
- [ ] **Step 3: Push + PR**
```bash
git push -u origin feat/plotroom-pipeline-phase1
gh pr create --title "feat(pipeline): plotroom pipeline exact replica — Phase 1 core port" --body "$(cat <<'EOF'
Source-ports plotroom@5166b52's pipeline view into web/src/pipeline/ (scoped CSS island),
adds /api/pipeline/events SSE emitting plotroom-shaped DashboardState, ready+auto-merge
endpoint, blends ccswitch session label + Fix into PrRow, and deletes the superseded
pipeline components.

Spec: docs/superpowers/specs/2026-07-12-plotroom-pipeline-replica-design.md (Phase 1 of 4).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```
Expected: PR open (non-draft), auto-merge armed, CI green.

---

## Self-Review Notes

- **Spec coverage (Phase 1 scope):** SSE + DashboardState (Tasks 1–3), ready-merge (4), frontend core port (5–10), session extras (9), old-view deletion (11), verification+PR (12). Chrome/gap-fill/config are Phases 2–4 (separate plans).
- **Deliberate approximations (documented in code):** `expectedSeconds = elapsed + remaining`; `awaiting-prod` from `deployed_envs`-without-prod; `batchSize` constant 6; `mergeEtaSim`/`timeline`/cost/flake nulls. All are honest-null phase boundaries the spec assigns to Phases 3–4.
- **Type consistency:** `onFixPr(sessionId, prNumber)` is uniform across PrRow → PipelineView → PipelineTab; `session` extension `{id, name, fixable}` is identical in `dashboard_state.pr_view` and `types.ts`.
