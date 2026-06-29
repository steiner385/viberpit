# M-Track-5: Per-Check Drill-Down Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expanding a PR row in the Pipeline board reveals its individual CI checks — two Plotroom-style groups (PR head commit + merge-group), each a list of rows with name, duration bar, status, overdue flag, and a per-group "CI cost this run" line.

**Architecture:** The eta-checks GraphQL query (already run by the population poller) gains a `mergeQueueEntry` sub-selection. A new pure builder (`server/pipeline/checkgroups.py`) turns the head + merge check dicts into `CheckGroup`/`CheckRow` structs, deriving per-check duration, overdue (vs `HistoryStore` p50 history — same source the ETA estimator uses), and per-group cost. These attach to `PullRequestInfo.check_groups` inside the existing `_enrich_with_eta` loop (which already has `store`, `now`, and the parsed checks in hand), ride `to_dict` → `GET /api/prs`, and render in a new inline-expandable `CheckDrillDown` component.

**Tech Stack:** Python 3.11 / dataclasses / pytest (backend); React + TypeScript + Vite / Tailwind / vitest (frontend).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-28-visual-pipeline-track-m5-drilldown-design.md`.
- **No new poller / no extra GitHub round-trip** — merge-group checks ride the existing `build_eta_checks_query`.
- **No re-derivation of stage/CI-node state from check detail** — `check_groups` is presentation-only; `place_pr_on_track`/`track.py` are untouched (constitution VI).
- **Reuse, don't fork:** per-check expected durations come from `HistoryStore.expected(repo, name, "pull_request").p50`; the bar/status palette is the single shared `web/src/lib/checkState.ts` (extracted from `StageTrack`).
- **Timezone-correct:** all duration/elapsed math uses timezone-aware UTC instants; the injected `now_fn` already returns `datetime.now(timezone.utc)`. Never bare naive `utcnow` (memory: `naive-utc-isoformat-0s-ago`).
- **Dual-serializer / exact-shape traps:** a new `PullRequestInfo` wire field touches `to_dict`, `from_dict`, and breaks the exact-dict assert in `server/tests/test_sessions_pr_field.py` (memories: `pullrequestinfo-todict-exact-shape-test`, `session-record-dual-serializer`).
- **Cost is an estimate:** `RUNNER_RATE_USD_PER_MIN = 0.008` (GitHub-hosted Linux 2-core list price, 2026-06), surfaced with a `~$` prefix; never presented as billed truth.
- **Backend gate:** `ruff` + `mypy` + `pytest server/tests/`. **Frontend gate (run in `web/`):** `vitest run`, `tsc -b --force`, `eslint --max-warnings 0`.
- **Backend test env:** run via the main checkout's prebuilt venv with `PYTHONPATH=<worktree>/src`; pin `CCSWITCH_TAILNET_ALLOWLIST` per-test or run with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. Judge full-suite regressions by baseline-diff, not raw counts (memory: `worktree-fullsuite-baseline-diff`).

---

### Task 1: Fetch merge-group checks in the eta query

Add the `mergeQueueEntry` block to the existing per-PR eta query and a separate parser for it. `parse_eta_checks_response` (head checks) is **left untouched** — its only consumer is `compute_pr_eta` plus `test_eta_checks_query.py`, and keeping it stable avoids needless churn.

**Files:**
- Modify: `server/github/pr_status.py` (`build_eta_checks_query` ~200-227; add `parse_merge_group_response` after `parse_eta_checks_response` ~270)
- Test: `server/tests/test_merge_group_checks.py` (new)

**Interfaces:**
- Consumes: the existing `build_eta_checks_query(prs: list[tuple[str,int]]) -> str` and `_alias(i)`.
- Produces: `parse_merge_group_response(response: dict, prs: list[tuple[str,int]]) -> dict[tuple[str,int], dict]` where each value is `{"checks": list[dict], "eta_seconds": int | None}`; each check dict has the same keys as the head parser (`name, status, conclusion, started_at, completed_at, isRequired, workflowName, event`) with `event="merge_group"`, `isRequired=False`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_merge_group_checks.py
from server.github.pr_status import build_eta_checks_query, parse_merge_group_response


def test_query_includes_merge_queue_entry_block():
    q = build_eta_checks_query([("octo/repo", 7)])
    assert "mergeQueueEntry" in q
    assert "estimatedTimeToMerge" in q
    # merge-group checks come from the entry's headCommit rollup, not the PR head commit
    assert "headCommit" in q


def test_parse_merge_group_extracts_checks_and_eta():
    resp = {"data": {"r0": {"pullRequest": {"mergeQueueEntry": {
        "estimatedTimeToMerge": 300,
        "headCommit": {"statusCheckRollup": {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
             "conclusion": "SUCCESS", "startedAt": "2026-06-28T12:00:00Z",
             "completedAt": "2026-06-28T12:05:00Z",
             "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
        ]}}},
    }}}}}
    out = parse_merge_group_response(resp, [("octo/repo", 7)])
    entry = out[("octo/repo", 7)]
    assert entry["eta_seconds"] == 300
    assert len(entry["checks"]) == 1
    c = entry["checks"][0]
    assert c["name"] == "build" and c["event"] == "merge_group" and c["isRequired"] is False
    assert c["workflowName"] == "CI"


def test_parse_merge_group_absent_entry_is_empty():
    resp = {"data": {"r0": {"pullRequest": {"mergeQueueEntry": None}}}}
    out = parse_merge_group_response(resp, [("octo/repo", 7)])
    assert out[("octo/repo", 7)] == {"checks": [], "eta_seconds": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_merge_group_checks.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_merge_group_response'` and the query assert fails.

- [ ] **Step 3: Add the query block**

In `build_eta_checks_query`, inside the `pullRequest(number: N) { … }` block, add after the closing `}` of the `commits(last: 1) { … }` line (i.e. after line 223's `} } } }`):

```python
            f"    mergeQueueEntry {{ estimatedTimeToMerge headCommit {{ statusCheckRollup {{\n"
            f"      contexts(first: 100) {{ nodes {{\n"
            f"        __typename\n"
            f"        ... on CheckRun {{ name status conclusion startedAt completedAt "
            f"checkSuite {{ workflowRun {{ workflow {{ name }} }} }} }}\n"
            f"        ... on StatusContext {{ context state }}\n"
            f"      }} }}\n"
            f"    }} }} }}\n"
```

(Note: merge-group `CheckRun` omits `isRequired(pullRequestNumber:)` — that arg is PR-head-scoped; merge-group rows are reported `required=False`.)

- [ ] **Step 4: Add the parser**

Add after `parse_eta_checks_response`:

```python
def parse_merge_group_response(
    response: dict, prs: list[tuple[str, int]]
) -> dict[tuple[str, int], dict]:
    """Parse the mergeQueueEntry block of a ``build_eta_checks_query`` response.

    Returns (repo, number) -> {"checks": [...], "eta_seconds": int | None}.
    Empty checks + None eta when the PR is not enqueued (mergeQueueEntry null).
    """
    data = (response or {}).get("data") or {}
    out: dict[tuple[str, int], dict] = {}
    for i, key in enumerate(prs):
        node = data.get(_alias(i)) or {}
        entry = (node.get("pullRequest") or {}).get("mergeQueueEntry")
        if not entry:
            out[key] = {"checks": [], "eta_seconds": None}
            continue
        rollup = (entry.get("headCommit") or {}).get("statusCheckRollup") or {}
        contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
        checks: list[dict] = []
        for c in contexts:
            if c.get("__typename") == "CheckRun":
                wf = (((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {})
                checks.append({
                    "name": c.get("name", ""), "status": c.get("status", ""),
                    "conclusion": c.get("conclusion"), "started_at": c.get("startedAt"),
                    "completed_at": c.get("completedAt"),
                    "isRequired": False, "workflowName": wf.get("name"),
                    "event": "merge_group",
                })
            else:
                checks.append({
                    "name": c.get("context", ""), "status": "COMPLETED",
                    "conclusion": c.get("state"), "started_at": None,
                    "completed_at": None, "isRequired": False,
                    "workflowName": None, "event": "merge_group",
                })
        out[key] = {"checks": checks, "eta_seconds": entry.get("estimatedTimeToMerge")}
    return out
```

> **Unit caveat (document, don't silently trust):** `estimatedTimeToMerge` is stored raw and labelled `eta_seconds`. GitHub documents it in seconds; verify against one live enqueued PR before relying on the displayed value. If it proves to be minutes, multiply in the builder (Task 3), not here.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_merge_group_checks.py server/tests/test_eta_checks_query.py -v`
Expected: PASS (new file green; existing eta-query tests still green — the head parser was untouched).

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_status.py server/tests/test_merge_group_checks.py
git commit -m "feat(pipeline): fetch merge-group checks via mergeQueueEntry in eta query"
```

---

### Task 2: `CheckRow` / `CheckGroup` data structures

Pure, serializable dataclasses next to `CheckDetail` in `pr_status.py`. They carry no I/O — duration/overdue/cost are computed by the Task 3 builder and passed in. Kept **separate from the existing `checks_detail`** (which is coarse name+conclusion feeding the parity oracle, not the UI).

**Files:**
- Modify: `server/github/pr_status.py` (add after `CheckDetail`, ~31)
- Test: `server/tests/test_check_group_structs.py` (new)

**Interfaces:**
- Produces:
  - `CheckRow(name: str, workflow: str | None, status: str, conclusion: str, duration_seconds: int | None, required: bool, overdue: bool)` with `to_dict()`/`from_dict(d)`.
  - `CheckGroup(kind: str, checks: tuple[CheckRow, ...], total_seconds: int, cost_usd: float | None, eta_seconds: int | None = None)` with `to_dict()`/`from_dict(d)`. `kind` ∈ `{"head", "merge"}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_check_group_structs.py
from server.github.pr_status import CheckRow, CheckGroup


def test_checkrow_roundtrip():
    r = CheckRow(name="lint", workflow="CI", status="COMPLETED",
                 conclusion="success", duration_seconds=42, required=True, overdue=False)
    assert CheckRow.from_dict(r.to_dict()) == r


def test_checkgroup_roundtrip_and_defaults():
    g = CheckGroup(kind="head", checks=(
        CheckRow("lint", "CI", "COMPLETED", "success", 42, True, False),
    ), total_seconds=42, cost_usd=0.008, eta_seconds=None)
    d = g.to_dict()
    assert d["kind"] == "head" and d["total_seconds"] == 42 and d["cost_usd"] == 0.008
    assert CheckGroup.from_dict(d) == g
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_check_group_structs.py -v`
Expected: FAIL — `ImportError: cannot import name 'CheckRow'`.

- [ ] **Step 3: Add the dataclasses**

```python
@dataclass(frozen=True)
class CheckRow:
    name: str
    workflow: str | None
    status: str        # GraphQL status passthrough: COMPLETED/IN_PROGRESS/QUEUED/PENDING/...
    conclusion: str    # normalised lowercase: success/failure/pending/neutral/skipped/...
    duration_seconds: int | None
    required: bool
    overdue: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name, "workflow": self.workflow, "status": self.status,
            "conclusion": self.conclusion, "duration_seconds": self.duration_seconds,
            "required": self.required, "overdue": self.overdue,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckRow":
        return cls(
            name=d.get("name", ""), workflow=d.get("workflow"),
            status=d.get("status", ""), conclusion=d.get("conclusion", ""),
            duration_seconds=d.get("duration_seconds"),
            required=bool(d.get("required", False)), overdue=bool(d.get("overdue", False)),
        )


@dataclass(frozen=True)
class CheckGroup:
    kind: str  # "head" | "merge"
    checks: tuple[CheckRow, ...]
    total_seconds: int
    cost_usd: float | None
    eta_seconds: int | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "checks": [c.to_dict() for c in self.checks],
            "total_seconds": self.total_seconds,
            "cost_usd": self.cost_usd,
            "eta_seconds": self.eta_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckGroup":
        return cls(
            kind=d.get("kind", ""),
            checks=tuple(CheckRow.from_dict(c) for c in d.get("checks", [])),
            total_seconds=int(d.get("total_seconds", 0)),
            cost_usd=d.get("cost_usd"),
            eta_seconds=d.get("eta_seconds"),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_check_group_structs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_status.py server/tests/test_check_group_structs.py
git commit -m "feat(pipeline): add CheckRow/CheckGroup serializable structs"
```

---

### Task 3: `build_check_groups` — duration, overdue, cost

The pure builder that turns raw head + merge check dicts into `CheckGroup`s, reading expected durations from `HistoryStore` (same source as the ETA estimator). Lives in a new `server/pipeline/` module so `pr_status.py` stays I/O-free.

**Files:**
- Create: `server/pipeline/checkgroups.py`
- Test: `server/tests/test_build_check_groups.py` (new)

**Interfaces:**
- Consumes: `CheckRow`, `CheckGroup` (Task 2); `HistoryStore.expected(repo, name, event) -> Expected | None` where `Expected.p50: float` is the median expected duration in seconds.
- Produces: `build_check_groups(head_checks: list[dict], merge: dict, store, repo: str, now: datetime) -> tuple[CheckGroup, ...]`. `merge` is the `{"checks", "eta_seconds"}` dict from `parse_merge_group_response`. Returns a tuple with the head group always present and the merge group appended only when it has checks. Module constant `RUNNER_RATE_USD_PER_MIN = 0.008`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_build_check_groups.py
import math
from datetime import datetime, timezone

from server.pipeline.checkgroups import build_check_groups, RUNNER_RATE_USD_PER_MIN


class _FakeExpected:
    def __init__(self, p50): self.p50 = p50


class _FakeStore:
    def __init__(self, p50s): self._p50s = p50s
    def expected(self, repo, name, event):
        v = self._p50s.get(name)
        return _FakeExpected(v) if v is not None else None


NOW = datetime(2026, 6, 28, 12, 10, 0, tzinfo=timezone.utc)


def _head(name, started, completed, conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "started_at": started, "completed_at": completed,
            "isRequired": True, "workflowName": "CI", "event": "pull_request"}


def test_completed_check_duration_and_cost():
    head = [_head("lint", "2026-06-28T12:00:00Z", "2026-06-28T12:05:00Z")]
    groups = build_check_groups(head, {"checks": [], "eta_seconds": None},
                                _FakeStore({}), "octo/repo", NOW)
    assert len(groups) == 1  # no merge group when empty
    g = groups[0]
    assert g.kind == "head"
    assert g.checks[0].duration_seconds == 300
    assert g.checks[0].conclusion == "success"
    assert g.total_seconds == 300
    assert g.cost_usd == round(math.ceil(300 / 60) * RUNNER_RATE_USD_PER_MIN, 4)


def test_running_check_overdue_vs_p50():
    # started 10m ago, still running, p50 expected 120s -> overdue
    head = [_head("slow", "2026-06-28T12:00:00Z", None, conclusion=None, status="IN_PROGRESS")]
    groups = build_check_groups(head, {"checks": [], "eta_seconds": None},
                                _FakeStore({"slow": 120.0}), "octo/repo", NOW)
    row = groups[0].checks[0]
    assert row.duration_seconds == 600  # elapsed now - started
    assert row.overdue is True
    assert row.conclusion == "pending"  # null conclusion while running


def test_running_check_no_history_not_overdue():
    head = [_head("new", "2026-06-28T12:09:00Z", None, conclusion=None, status="IN_PROGRESS")]
    groups = build_check_groups(head, {"checks": [], "eta_seconds": None},
                                _FakeStore({}), "octo/repo", NOW)
    assert groups[0].checks[0].overdue is False


def test_merge_group_appended_with_eta():
    head = [_head("lint", "2026-06-28T12:00:00Z", "2026-06-28T12:01:00Z")]
    merge = {"checks": [{"name": "e2e", "status": "COMPLETED", "conclusion": "SUCCESS",
                         "started_at": "2026-06-28T12:00:00Z",
                         "completed_at": "2026-06-28T12:02:00Z",
                         "isRequired": False, "workflowName": "E2E", "event": "merge_group"}],
             "eta_seconds": 180}
    groups = build_check_groups(head, merge, _FakeStore({}), "octo/repo", NOW)
    assert [g.kind for g in groups] == ["head", "merge"]
    assert groups[1].eta_seconds == 180
    assert groups[1].checks[0].name == "e2e"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_build_check_groups.py -v`
Expected: FAIL — module `server.pipeline.checkgroups` does not exist.

- [ ] **Step 3: Implement the builder**

```python
# server/pipeline/checkgroups.py
"""Pure builder: raw GitHub check dicts -> CheckGroup/CheckRow for the drill-down.

Reads expected per-check durations from HistoryStore (the same p50 history the ETA
estimator uses) for the overdue heuristic and never re-derives stage state.
"""
from __future__ import annotations

import math
from datetime import datetime

from server.github.pr_status import CheckGroup, CheckRow, _CONCLUSION_MAP

RUNNER_RATE_USD_PER_MIN = 0.008  # GitHub-hosted Linux 2-core list price (2026-06); estimate only.
_EVENT = "pull_request"


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalise_conclusion(raw: str | None) -> str:
    if raw is None:
        return "pending"  # null conclusion = still running
    return _CONCLUSION_MAP.get(raw, str(raw).lower())


def _row(c: dict, store, repo: str, now: datetime) -> CheckRow:
    started = _parse_ts(c.get("started_at"))
    completed = _parse_ts(c.get("completed_at"))
    running = completed is None
    duration: int | None
    if started and completed:
        duration = int((completed - started).total_seconds())
    elif started:  # running: elapsed so far
        duration = max(0, int((now - started).total_seconds()))
    else:
        duration = None  # legacy StatusContext / no timing

    overdue = False
    if running and started is not None and duration is not None:
        exp = store.expected(repo, c.get("name", ""), _EVENT)
        if exp is not None and exp.p50 and duration > exp.p50:
            overdue = True

    return CheckRow(
        name=c.get("name", ""), workflow=c.get("workflowName"),
        status=c.get("status", ""), conclusion=_normalise_conclusion(c.get("conclusion")),
        duration_seconds=duration, required=bool(c.get("isRequired", False)), overdue=overdue,
    )


def _group(kind: str, raw_checks: list[dict], store, repo: str, now: datetime,
           eta_seconds: int | None = None) -> CheckGroup:
    rows = tuple(_row(c, store, repo, now) for c in raw_checks)
    total = sum(r.duration_seconds for r in rows if r.duration_seconds is not None)
    cost = round(math.ceil(total / 60) * RUNNER_RATE_USD_PER_MIN, 4) if total > 0 else None
    return CheckGroup(kind=kind, checks=rows, total_seconds=total, cost_usd=cost,
                      eta_seconds=eta_seconds)


def build_check_groups(
    head_checks: list[dict], merge: dict, store, repo: str, now: datetime,
) -> tuple[CheckGroup, ...]:
    groups = [_group("head", head_checks, store, repo, now)]
    merge_checks = (merge or {}).get("checks") or []
    if merge_checks:
        groups.append(_group("merge", merge_checks, store, repo, now,
                             eta_seconds=(merge or {}).get("eta_seconds")))
    return tuple(groups)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_build_check_groups.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/checkgroups.py server/tests/test_build_check_groups.py
git commit -m "feat(pipeline): build_check_groups with duration/overdue/cost from p50 history"
```

---

### Task 4: Wire `check_groups` onto `PullRequestInfo` and populate it

Add the field + serializers, then populate it inside `_enrich_with_eta` (which already parsed the head checks, holds `store` and `now`, and `replace()`s the PR).

**Files:**
- Modify: `server/github/pr_status.py` (`PullRequestInfo` field ~51, `to_dict` ~71, `from_dict` ~94)
- Modify: `server/github/pr_population.py` (import `parse_merge_group_response` ~24; in `_enrich_with_eta`, fetch+parse merge once ~181 and build groups in the per-PR loop ~203)
- Test: `server/tests/test_pr_info_check_groups.py` (new) + `server/tests/test_pr_population_eta.py` (extend if present)

**Interfaces:**
- Consumes: `CheckGroup` (Task 2), `build_check_groups` (Task 3), `parse_merge_group_response` (Task 1).
- Produces: `PullRequestInfo.check_groups: tuple[CheckGroup, ...] = ()` serialized as `"check_groups"`.

- [ ] **Step 1: Write the failing serializer test**

```python
# server/tests/test_pr_info_check_groups.py
from server.github.pr_status import PullRequestInfo, CheckGroup, CheckRow


def test_pullrequestinfo_check_groups_roundtrip():
    g = CheckGroup(kind="head", checks=(
        CheckRow("lint", "CI", "COMPLETED", "success", 30, True, False),
    ), total_seconds=30, cost_usd=0.008, eta_seconds=None)
    pr = PullRequestInfo(number=1, url="u", state="open", checks="passing",
                         title="t", check_groups=(g,))
    d = pr.to_dict()
    assert d["check_groups"] == [g.to_dict()]
    assert PullRequestInfo.from_dict(d).check_groups == (g,)


def test_pullrequestinfo_check_groups_default_empty():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="none", title="t")
    assert pr.to_dict()["check_groups"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_pr_info_check_groups.py -v`
Expected: FAIL — `to_dict()` has no `check_groups` key.

- [ ] **Step 3: Add the field + serializers**

In `PullRequestInfo` dataclass, after `deployed_envs` (line 51):
```python
    check_groups: tuple[CheckGroup, ...] = ()
```
In `to_dict`, after the `deployed_envs` entry:
```python
            "check_groups": [g.to_dict() for g in self.check_groups],
```
In `from_dict`, after the `deployed_envs` entry:
```python
            check_groups=tuple(CheckGroup.from_dict(g) for g in d.get("check_groups", [])),
```

- [ ] **Step 4: Run serializer test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_pr_info_check_groups.py -v`
Expected: PASS.

- [ ] **Step 5: Populate in `_enrich_with_eta`**

In `server/github/pr_population.py`, extend the import (line 24 area):
```python
from server.github.pr_status import (
    PullRequestInfo,
    build_eta_checks_query,
    parse_eta_checks_response,
    parse_merge_group_response,
)
```
After `checks_by_pr = parse_eta_checks_response(response, open_keys)` (~181), add:
```python
            merge_by_pr = parse_merge_group_response(response, open_keys)
```
(If the `except` returns early, `merge_by_pr` stays unused — fine.) Import the builder near the `compute_pr_eta` import (~189):
```python
        from server.pipeline.checkgroups import build_check_groups
```
In the per-PR enrich block, change the `dataclasses.replace(pr, …)` (lines 204-209) to also set `check_groups`:
```python
                        groups = build_check_groups(
                            checks, merge_by_pr.get(key, {}), store, repo, now,
                        )
                        pr = dataclasses.replace(
                            pr,
                            eta_seconds=eta_s,
                            eta_percent=eta_pct,
                            eta_overdue=eta_over,
                            check_groups=groups,
                        )
```

- [ ] **Step 6: Write a population integration test**

```python
# append to server/tests/test_pr_info_check_groups.py
import asyncio
from datetime import datetime, timezone
from server.github.pr_population import PrPopulation  # adjust to the real class/init


def test_enrich_attaches_check_groups(monkeypatch):
    # Minimal harness: one open PR, stubbed eta query returning head+merge checks.
    # Use the population class's existing injection points (query_fn/eta_query_fn/now_fn/store).
    # Assert the cached PR exposes a non-empty check_groups[0].kind == "head".
    ...
```

> **Implementer note:** model this on the existing `_enrich_with_eta` test in `server/tests/` (grep `eta_query_fn`/`now_fn` for the construction pattern). If no population test fixture exists, assert via a direct call to `_enrich_with_eta` with a stub store and a hand-built `prs_by_repo`. Keep it to one open PR; verify `result[repo][0].check_groups[0].kind == "head"`.

- [ ] **Step 7: Run the population + full pr_status tests**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_pr_info_check_groups.py server/tests/ -k "population or pr_status or eta" -v`
Expected: PASS; no regressions in eta/population tests.

- [ ] **Step 8: Commit**

```bash
git add server/github/pr_status.py server/github/pr_population.py server/tests/test_pr_info_check_groups.py
git commit -m "feat(pipeline): populate PullRequestInfo.check_groups in eta enrichment"
```

---

### Task 5: API surface — fix the exact-shape test

`_record_to_dict` already serializes PRs via `p.to_dict()`, so `check_groups` rides along automatically. The only required change is the exact-dict equality assertion that now has a new key.

**Files:**
- Modify: `server/tests/test_sessions_pr_field.py` (the `assert d["prs"] == [{…}]` block, ~18-25)
- Test: same file + a real-response check.

**Interfaces:**
- Consumes: `PullRequestInfo.to_dict` (Task 4).

- [ ] **Step 1: Run the exact-shape test to see it fail**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_sessions_pr_field.py -v`
Expected: FAIL — actual dict has an extra `"check_groups": []` key not in the expected literal.

- [ ] **Step 2: Add the key to the expected literal**

In `test_record_to_dict_includes_prs_when_given`, add to the expected PR dict (next to `"checks_detail": []`):
```python
        "check_groups": [],
```

- [ ] **Step 3: Add a real-response assertion**

```python
def test_record_to_dict_pr_includes_check_groups_key():
    prs = _resolve_prs(...)  # reuse the helper used above to build a PullRequestInfo with one head group
    d = _record_to_dict(_rec(), repo="o/r", prs=prs)
    assert "check_groups" in d["prs"][0]
    assert d["prs"][0]["check_groups"][0]["kind"] == "head"
```

> **Implementer note:** build the input PR with `PullRequestInfo(..., check_groups=(CheckGroup("head", (...), 30, 0.008),))` directly; `_resolve_prs` is only for the session-attach path. Keep it to one group.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src <main-venv>/bin/python -m pytest server/tests/test_sessions_pr_field.py -v`
Expected: PASS.

- [ ] **Step 5: Backend gate + baseline diff**

Run: `cd <worktree> && ruff check server/ && PYTHONPATH=$PWD/src <main-venv>/bin/python -m mypy server/github/pr_status.py server/pipeline/checkgroups.py server/github/pr_population.py`
Then full suite vs baseline (memory `worktree-fullsuite-baseline-diff`). Expected: no new failing node-ids vs `origin/master`.

- [ ] **Step 6: Commit**

```bash
git add server/tests/test_sessions_pr_field.py
git commit -m "test(api): assert /api/prs PR dict carries check_groups"
```

---

### Task 6: Extract the shared check-state palette

Lift the state→Tailwind-class vocabulary out of `StageTrack.nodeCircleClass` into a shared module so the drill-down bars and the stage nodes share one palette (constitution VI). `StageTrack` must render identically afterward.

**Files:**
- Create: `web/src/lib/checkState.ts`
- Modify: `web/src/components/StageTrack.tsx` (replace inline `nodeCircleClass` body)
- Test: `web/src/lib/__tests__/checkState.test.ts` (new); existing `StageTrack.test.tsx` must stay green.

**Interfaces:**
- Produces: `export type CheckVisualState = 'done' | 'active' | 'failed' | 'pending' | 'overdue'`; `export function checkStateColor(state: CheckVisualState): string` (returns the bar/fill Tailwind classes); `export function nodeCircleClass(state: NodeState | 'pending'): string` (the existing circle classes, moved verbatim).

- [ ] **Step 1: Write the failing test**

```ts
// web/src/lib/__tests__/checkState.test.ts
import { describe, it, expect } from 'vitest'
import { checkStateColor, nodeCircleClass } from '@/lib/checkState'

describe('checkState palette', () => {
  it('maps done to green and failed to red', () => {
    expect(checkStateColor('done')).toContain('green')
    expect(checkStateColor('failed')).toContain('red')
    expect(checkStateColor('overdue')).toContain('amber')
  })
  it('nodeCircleClass keeps the active ring', () => {
    expect(nodeCircleClass('active')).toContain('ring-2')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/lib/__tests__/checkState.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the module + refactor StageTrack**

```ts
// web/src/lib/checkState.ts
import type { NodeState } from '@/api/types'

export type CheckVisualState = 'done' | 'active' | 'failed' | 'pending' | 'overdue'

// Bar/fill classes for drill-down check rows.
export function checkStateColor(state: CheckVisualState): string {
  switch (state) {
    case 'done': return 'bg-green-500'
    case 'active': return 'bg-accent'
    case 'failed': return 'bg-red-500'
    case 'overdue': return 'bg-amber-500'
    case 'pending':
    default: return 'bg-gray-300 dark:bg-gray-600'
  }
}

// Circle classes for stage-track nodes (moved verbatim from StageTrack).
export function nodeCircleClass(state: NodeState | 'pending'): string {
  switch (state) {
    case 'done':
      return 'bg-green-500 border-green-500 text-white dark:bg-green-500 dark:border-green-500'
    case 'active':
      return 'bg-accent border-accent text-accent-foreground ring-2 ring-accent/40'
    case 'failed':
      return 'bg-red-500 border-red-500 text-white dark:bg-red-500 dark:border-red-500'
    case 'pending':
    default:
      return 'bg-transparent border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500'
  }
}
```
In `StageTrack.tsx`: delete the local `nodeCircleClass` function and import it: `import { nodeCircleClass } from '@/lib/checkState'`.

- [ ] **Step 4: Run checkState + StageTrack tests**

Run (in `web/`): `npx vitest run src/lib/__tests__/checkState.test.ts src/components/__tests__/StageTrack.test.tsx`
Expected: PASS (StageTrack visually unchanged — same class strings).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/checkState.ts web/src/lib/__tests__/checkState.test.ts web/src/components/StageTrack.tsx
git commit -m "refactor(web): extract shared check-state palette from StageTrack"
```

---

### Task 7: Frontend types for check groups

**Files:**
- Modify: `web/src/api/types.ts` (`PullRequestInfo` interface ~131)
- Test: none (type-only; covered by `tsc -b` and downstream component tests).

**Interfaces:**
- Produces: `CheckRow`, `CheckGroup` interfaces; `PullRequestInfo.check_groups?: CheckGroup[]`.

- [ ] **Step 1: Add the interfaces**

After the `PullRequestInfo` interface block, and add the field inside it:
```ts
export interface CheckRow {
  name: string
  workflow: string | null
  status: string
  conclusion: string
  duration_seconds: number | null
  required: boolean
  overdue: boolean
}

export interface CheckGroup {
  kind: 'head' | 'merge'
  checks: CheckRow[]
  total_seconds: number
  cost_usd: number | null
  eta_seconds?: number | null
}
```
Inside `PullRequestInfo` (after `eta_overdue`):
```ts
  /** Per-check drill-down groups (head commit + merge group); snake_case to match backend. */
  check_groups?: CheckGroup[]
```

- [ ] **Step 2: Typecheck**

Run (in `web/`): `npx tsc -b --force`
Expected: PASS (no errors).

- [ ] **Step 3: Commit**

```bash
git add web/src/api/types.ts
git commit -m "feat(web): CheckRow/CheckGroup types + PipelinePr.check_groups"
```

---

### Task 8: `CheckRowList` — render one group's rows

**Files:**
- Create: `web/src/components/CheckRowList.tsx`
- Test: `web/src/components/__tests__/CheckRowList.test.tsx` (new)

**Interfaces:**
- Consumes: `CheckRow`, `CheckGroup` (Task 7); `checkStateColor` (Task 6); `formatEta` (`@/lib/formatEta`, existing).
- Produces: `export function CheckRowList({ checks }: { checks: CheckRow[] }): JSX.Element`. Each row `data-testid={`check-row-${name}`}` with `data-state` ∈ done/active/failed/pending/overdue, a bar `<div>` whose width = `min(1, duration/expected)` (expected unknown → indeterminate; for done rows the bar is full), and a right-aligned duration label.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/CheckRowList.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CheckRowList } from '@/components/CheckRowList'
import type { CheckRow } from '@/api/types'

const row = (o: Partial<CheckRow>): CheckRow => ({
  name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'success',
  duration_seconds: 300, required: true, overdue: false, ...o,
})

describe('CheckRowList', () => {
  it('renders a row per check with state + duration', () => {
    render(<CheckRowList checks={[row({}), row({ name: 'test', conclusion: 'failure' })]} />)
    expect(screen.getByTestId('check-row-lint')).toHaveAttribute('data-state', 'done')
    expect(screen.getByTestId('check-row-test')).toHaveAttribute('data-state', 'failed')
    expect(screen.getByTestId('check-row-lint')).toHaveTextContent('5m')
  })
  it('marks overdue running checks', () => {
    render(<CheckRowList checks={[row({ name: 'slow', conclusion: 'pending', overdue: true,
      status: 'IN_PROGRESS' })]} />)
    expect(screen.getByTestId('check-row-slow')).toHaveAttribute('data-state', 'overdue')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/CheckRowList.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// web/src/components/CheckRowList.tsx
import { cn } from '@/lib/utils'
import { checkStateColor, type CheckVisualState } from '@/lib/checkState'
import { formatEta } from '@/lib/formatEta'
import type { CheckRow } from '@/api/types'

function visualState(c: CheckRow): CheckVisualState {
  if (c.overdue) return 'overdue'
  if (c.conclusion === 'failure') return 'failed'
  if (c.conclusion === 'pending' || c.status === 'IN_PROGRESS' || c.status === 'QUEUED') return 'active'
  if (c.conclusion === 'success') return 'done'
  return 'pending'
}

export function CheckRowList({ checks }: { checks: CheckRow[] }) {
  return (
    <ul className="space-y-0.5">
      {checks.map((c) => {
        const state = visualState(c)
        const done = state === 'done' || state === 'failed'
        return (
          <li
            key={c.name}
            data-testid={`check-row-${c.name}`}
            data-state={state}
            className="flex items-center gap-2 text-xs"
          >
            <span className="w-48 shrink-0 truncate text-muted-foreground">{c.name}</span>
            <span className="relative h-1.5 flex-1 overflow-hidden rounded bg-gray-200 dark:bg-gray-700">
              <span
                className={cn('absolute inset-y-0 left-0 rounded', checkStateColor(state))}
                style={{ width: done ? '100%' : '40%' }}
                aria-hidden
              />
            </span>
            <span className="w-16 shrink-0 text-right tabular-nums text-muted-foreground">
              {c.duration_seconds != null ? formatEta(c.duration_seconds) : '—'}
              {c.overdue ? ' ⚠' : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
```

- [ ] **Step 4: Run to verify pass**

Run (in `web/`): `npx vitest run src/components/__tests__/CheckRowList.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/CheckRowList.tsx web/src/components/__tests__/CheckRowList.test.tsx
git commit -m "feat(web): CheckRowList renders per-check rows with state + duration"
```

---

### Task 9: `CheckDrillDown` — group headers + cost line

**Files:**
- Create: `web/src/components/CheckDrillDown.tsx`
- Test: `web/src/components/__tests__/CheckDrillDown.test.tsx` (new)

**Interfaces:**
- Consumes: `CheckGroup` (Task 7); `CheckRowList` (Task 8); `formatEta`.
- Produces: `export function CheckDrillDown({ groups }: { groups: CheckGroup[] }): JSX.Element`. Renders groups in **merge-first, then head** order (matching Plotroom). Group header label: `merge` → `MERGE GROUP BUILD`, `head` → `PR CHECKS (HEAD COMMIT)`. Cost line `data-testid="ci-cost-${kind}"`: `CI cost this run: {formatEta(total_seconds)} (~${cost_usd})` when `cost_usd != null`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/CheckDrillDown.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CheckDrillDown } from '@/components/CheckDrillDown'
import type { CheckGroup } from '@/api/types'

const g = (kind: 'head' | 'merge', cost: number | null): CheckGroup => ({
  kind, checks: [{ name: `${kind}-lint`, workflow: 'CI', status: 'COMPLETED',
    conclusion: 'success', duration_seconds: 1920, required: true, overdue: false }],
  total_seconds: 1920, cost_usd: cost, eta_seconds: null,
})

describe('CheckDrillDown', () => {
  it('renders merge group before head group with labels + cost', () => {
    render(<CheckDrillDown groups={[g('head', 0.25), g('merge', 0.25)]} />)
    const headers = screen.getAllByRole('heading')
    expect(headers[0]).toHaveTextContent('MERGE GROUP BUILD')
    expect(headers[1]).toHaveTextContent('PR CHECKS (HEAD COMMIT)')
    expect(screen.getByTestId('ci-cost-head')).toHaveTextContent('(~$0.25)')
  })
  it('omits cost line when cost is null', () => {
    render(<CheckDrillDown groups={[g('head', null)]} />)
    expect(screen.queryByTestId('ci-cost-head')).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/CheckDrillDown.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// web/src/components/CheckDrillDown.tsx
import { CheckRowList } from '@/components/CheckRowList'
import { formatEta } from '@/lib/formatEta'
import type { CheckGroup } from '@/api/types'

const GROUP_LABEL: Record<CheckGroup['kind'], string> = {
  merge: 'MERGE GROUP BUILD',
  head: 'PR CHECKS (HEAD COMMIT)',
}
const GROUP_ORDER: CheckGroup['kind'][] = ['merge', 'head']

export function CheckDrillDown({ groups }: { groups: CheckGroup[] }) {
  const ordered = [...groups].sort(
    (a, b) => GROUP_ORDER.indexOf(a.kind) - GROUP_ORDER.indexOf(b.kind),
  )
  return (
    <div className="space-y-3 px-2 py-2">
      {ordered.map((grp) => (
        <section key={grp.kind}>
          <h4 className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {GROUP_LABEL[grp.kind]}
          </h4>
          {grp.cost_usd != null && (
            <p data-testid={`ci-cost-${grp.kind}`} className="mb-1 text-[11px] text-muted-foreground">
              CI cost this run: {formatEta(grp.total_seconds)} (~${grp.cost_usd})
            </p>
          )}
          <CheckRowList checks={grp.checks} />
        </section>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Run to verify pass**

Run (in `web/`): `npx vitest run src/components/__tests__/CheckDrillDown.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/CheckDrillDown.tsx web/src/components/__tests__/CheckDrillDown.test.tsx
git commit -m "feat(web): CheckDrillDown groups with labels + CI cost line"
```

---

### Task 10: Inline expand wiring in `PipelineBoard`

Add a per-row chevron that toggles the `CheckDrillDown` panel inline. Expansion is client-only state, in the same `Record<string, boolean>` style already used for repo collapse.

**Files:**
- Modify: `web/src/components/PipelineView.tsx` (`PipelineBoard` — add `expanded` state, chevron button, conditional panel ~64-118)
- Test: `web/src/components/__tests__/PipelineBoard.test.tsx` (extend)

**Interfaces:**
- Consumes: `CheckDrillDown` (Task 9); `PipelinePr.check_groups` (Task 7).

- [ ] **Step 1: Write the failing test**

```tsx
// add to web/src/components/__tests__/PipelineBoard.test.tsx
it('expands a PR row to show its check drill-down', async () => {
  const user = userEvent.setup()
  const prs = [{
    repo: 'o/r', number: 5, url: 'u', state: 'open', checks: 'passing', title: 'feat',
    deployed: false, session: null, node_states: [],
    check_groups: [{ kind: 'head', total_seconds: 60, cost_usd: 0.01, eta_seconds: null,
      checks: [{ name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'success',
        duration_seconds: 60, required: true, overdue: false }] }],
  }] as unknown as PipelinePr[]
  render(<PipelineBoard prs={prs} stageModels={{}} />)
  expect(screen.queryByTestId('check-row-lint')).toBeNull()
  await user.click(screen.getByTestId('pipeline-expand-5'))
  expect(screen.getByTestId('check-row-lint')).toBeInTheDocument()
})

it('expand chevron does not trigger row navigation', async () => {
  const user = userEvent.setup()
  const onOpenSession = vi.fn()
  const prs = [{ repo: 'o/r', number: 5, url: 'u', state: 'open', checks: 'passing',
    title: 'feat', deployed: false, session: { id: 's1', name: 'sess' }, node_states: [],
    check_groups: [{ kind: 'head', total_seconds: 0, cost_usd: null, eta_seconds: null,
      checks: [] }] }] as unknown as PipelinePr[]
  render(<PipelineBoard prs={prs} stageModels={{}} onOpenSession={onOpenSession} />)
  await user.click(screen.getByTestId('pipeline-expand-5'))
  expect(onOpenSession).not.toHaveBeenCalled()
})
```

(Ensure the test file imports `userEvent` and `vi`; mirror the existing imports at the top of `PipelineBoard.test.tsx`.)

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineBoard.test.tsx`
Expected: FAIL — no `pipeline-expand-5` element.

- [ ] **Step 3: Wire the expand affordance**

In `PipelineBoard`, add near the `collapsed` state:
```tsx
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const toggleExpand = (key: string) => setExpanded((e) => ({ ...e, [key]: !e[key] }))
```
Import at the top: `import { ChevronRight, ChevronDown } from 'lucide-react'` and `import { CheckDrillDown } from './CheckDrillDown'`.

Inside the row `<li>`, before `<PrPill pr={p} />`, add the chevron (only when there are groups):
```tsx
                        {p.check_groups && p.check_groups.length > 0 && (
                          <button
                            type="button"
                            data-testid={`pipeline-expand-${p.number}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              toggleExpand(`${p.repo}#${p.number}`)
                            }}
                            aria-label={`Toggle checks for PR #${p.number}`}
                            className="shrink-0 text-muted-foreground"
                          >
                            {expanded[`${p.repo}#${p.number}`]
                              ? <ChevronDown size={14} aria-hidden />
                              : <ChevronRight size={14} aria-hidden />}
                          </button>
                        )}
```
After the row `<div>` (still inside the `<li>`), add the panel:
```tsx
                      {expanded[`${p.repo}#${p.number}`] && p.check_groups && (
                        <CheckDrillDown groups={p.check_groups} />
                      )}
```

- [ ] **Step 4: Run to verify pass**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineBoard.test.tsx`
Expected: PASS (both new cases + existing).

- [ ] **Step 5: Frontend gate**

Run (in `web/`): `npx vitest run && npx tsc -b --force && npx eslint . --max-warnings 0`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/PipelineView.tsx web/src/components/__tests__/PipelineBoard.test.tsx
git commit -m "feat(web): inline expand PR rows to show per-check drill-down"
```

---

## Self-Review

**Spec coverage:**
- Merge-group + head groups → Tasks 1, 3, 9. ✅
- Per-check duration/status/required/overdue → Tasks 3, 8. ✅
- CI-cost line → Tasks 3 (`cost_usd`), 9 (render). ✅
- Inline expand/collapse → Task 10. ✅
- Reuse expected-duration history (no fork) → Task 3 (`store.expected(...).p50`). ✅
- Shared palette (no fork) → Task 6. ✅
- Dual-serializer / exact-shape traps → Tasks 4, 5. ✅
- Timezone-correct now/elapsed → Task 3 (`_parse_ts` aware, injected `now`). ✅
- No poller / no extra round-trip → Task 1 (rides existing query). ✅
- `mergeQueueEntry` schema verified → spec §3.1 probe (2026-06-28). ✅
- Cost-rate constant + `~$` honesty → Task 3 constant, Task 9 render. ✅
- `contexts(first:100)` cap → Task 1 (documented in query; matches existing head cap). ✅

**Open items deliberately deferred (flagged, not gaps):**
- `estimatedTimeToMerge` unit (sec vs min) — verify against a live enqueued PR (Task 1 note); display-only, low risk.
- Bar fill currently a coarse `40%` for in-flight rows (Task 8) — refining to elapsed/expected needs the per-check p50 on the wire; out of scope for parity-of-content, can be a fast follow.

**Placeholder scan:** the two `...` markers (Task 4 Step 6, Task 5 Step 3) are explicitly accompanied by an implementer note pinning the exact construction; no bare TODO/TBD.

**Type consistency:** `CheckRow`/`CheckGroup` field names match across Python (Task 2), wire (`to_dict`), and TS (Task 7); `checkStateColor`/`nodeCircleClass`/`visualState`/`build_check_groups`/`parse_merge_group_response` names are used identically wherever referenced.
