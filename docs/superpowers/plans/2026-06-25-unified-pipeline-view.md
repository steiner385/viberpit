# Unified Pipeline View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a top-level **Pipeline** tab showing the full open + recently-merged PR population for every repo ccswitch works in, grouped into stacked collapsible stage sections.

**Architecture:** A new repo-keyed `PrPopulationPoller` sweeps all open + merged-≤7d PRs per watched repo (repos auto-derived from sessions), reusing the existing parity-proven stage classifier in `server/github/pr_status.py`. A new `all_prs()` orchestrator method (wired through the daemon↔edge RPC boundary) joins each PR to its ccswitch session (if any) and is served at `GET /api/prs`. The frontend adds a `useAllPrs()` hook and a `PipelineBoard` that buckets PRs by stage.

**Tech Stack:** Python 3 / FastAPI / asyncio (backend) · React + TypeScript + Tailwind + react-query (frontend) · GitHub GraphQL via the `gh` CLI · pytest (backend) · vitest + @testing-library/react (frontend).

## Global Constraints

- **Reuse the stage classifier — never re-derive.** All stage values come from `server/pipeline/stage.py` via `_to_info` in `server/github/pr_status.py`. The population poller adds NO stage logic (constitution VI; see memory: combined-app program).
- **`PullRequestInfo` is RPC-shape-stable.** New fields are additive with defaults so `from_dict` tolerates old payloads (no migration). Update the single `to_dict`/`from_dict` pair AND the frontend `PullRequestInfo` type together.
- **Daemon↔edge: wire all 5 layers.** A new orchestrator method must be added to the in-process orchestrator, `InProcessOrchestratorAdapter`, `RemoteOrchestrator`, AND registered in `orchestrator_rpc.py`. A remote round-trip test is required (memory: remote-mode-path-asymmetry).
- **Backend tests:** run with the clone venv + worktree on PYTHONPATH:
  `PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/41b21f76/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/<file> -v`
  (memory: ccswitch-worktree-backend-pythonpath, venv-ccswitch-stale-import). Lint the whole tree before pushing: `ruff check src/ server/` + `mypy`.
- **Frontend tests (run in `web/`):** `npx vitest run`, real typecheck `npx tsc -b --force` (NOT `tsc --noEmit` — it checks nothing), `npx eslint . --max-warnings 0`. If `web/node_modules` is a stale symlink: `rm -f web/node_modules && npm ci` (memory: ccswitch-web-node-modules-source, frontend-real-typecheck).
- **Scope guards (from the spec §2 non-goals):** no ETA/waterfall, no owner-wide search, no new user config, no closed-unmerged PRs.

---

### Task 1: Add `repo` field to `PullRequestInfo` + extract shared PR-node fields

**Files:**
- Modify: `server/github/pr_status.py` (the `PullRequestInfo` dataclass, `to_dict`/`from_dict`; extract a `_PR_NODE_FIELDS` constant used by `build_pr_query`)
- Test: `server/tests/test_pr_status.py` (append)

**Interfaces:**
- Produces: `PullRequestInfo` now has `repo: str = ""` (round-trips through `to_dict`/`from_dict`). A module constant `_PR_NODE_FIELDS: str` holds the shared GraphQL node selection.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_pr_status.py`:

```python
from server.github.pr_status import PullRequestInfo


def test_pull_request_info_repo_round_trips():
    info = PullRequestInfo(
        number=7, url="https://github.com/o/n/pull/7", state="open",
        checks="passing", title="t", repo="o/n",
    )
    d = info.to_dict()
    assert d["repo"] == "o/n"
    assert PullRequestInfo.from_dict(d).repo == "o/n"


def test_pull_request_info_repo_defaults_empty_for_old_payload():
    # An old daemon payload has no "repo" key — from_dict must tolerate it.
    d = {"number": 1, "url": "u", "state": "open", "checks": "none"}
    assert PullRequestInfo.from_dict(d).repo == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pr_status.py -k repo -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'repo'`.

- [ ] **Step 3: Add the field + serialization**

In `server/github/pr_status.py`, add to the `PullRequestInfo` dataclass (after `stage`):

```python
    repo: str = ""  # nameWithOwner, e.g. "owner/name"; populated by the population poller
```

In `to_dict`, add `"repo": self.repo,` to the returned dict. In `from_dict`, add `repo=d.get("repo", ""),` to the constructor call.

- [ ] **Step 4: Extract the shared node-fields constant (DRY refactor)**

In `server/github/pr_status.py`, above `build_pr_query`, add:

```python
# Shared PR-node selection so build_pr_query (per-branch) and the population
# query (per-repo) can never drift in which fields they fetch.
_PR_NODE_FIELDS = (
    "number url title state isDraft updatedAt mergedAt headRefOid mergeStateStatus\n"
    "      commits(last: 1) { nodes { commit { statusCheckRollup { state\n"
    "        contexts(first: 100) { nodes {\n"
    "          __typename\n"
    "          ... on CheckRun { name conclusion }\n"
    "          ... on StatusContext { context state }\n"
    "        } }\n"
    "      } } } }"
)
```

Then in `build_pr_query`, replace the inline `nodes {{ number url title ... }}` block (the part inside `pullRequests(...) {{ nodes {{ ... }} }}`) so it reads:

```python
            f"  pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
            f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{\n"
            f"    nodes {{ {_PR_NODE_FIELDS} }}\n"
            f"  }}\n"
```

- [ ] **Step 5: Run tests to verify they pass (incl. existing query tests)**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pr_status.py -v`
Expected: PASS (new repo tests + all pre-existing `build_pr_query`/`parse_pr_response` tests still green — the refactor is behavior-preserving).

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status.py
git commit -m "feat(pr): add repo field to PullRequestInfo + share PR-node GraphQL fields"
```

---

### Task 2: Population GraphQL query builder + parser

**Files:**
- Modify: `server/github/pr_status.py` (add `build_population_query`, `parse_population_response`)
- Test: `server/tests/test_pr_status.py` (append)

**Interfaces:**
- Consumes: `_PR_NODE_FIELDS`, `_to_info`, `_active_deploy_date` (Task 1 + existing).
- Produces:
  - `build_population_query(repo: str, merged_since: str) -> str` — one GraphQL doc with a `repo: repository(...) { deployments... pullRequests(states: OPEN...) }` block and a `merged: search(query: "repo:<repo> is:pr is:merged merged:>=<merged_since>", type: ISSUE...)` block.
  - `parse_population_response(response: dict, repo: str) -> tuple[PullRequestInfo, ...]` — open + merged PRs for one repo, each with `repo` set, deduped by number (open wins).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_pr_status.py`:

```python
from server.github.pr_status import build_population_query, parse_population_response


def test_build_population_query_scopes_to_repo_and_date():
    q = build_population_query("steiner385/cc-account-switcher", "2026-06-18")
    assert 'repository(owner: "steiner385", name: "cc-account-switcher")' in q
    assert "pullRequests(states: OPEN" in q
    assert 'is:pr is:merged merged:>=2026-06-18' in q
    assert "statusCheckRollup" in q  # shared node fields are present


def test_parse_population_response_sets_repo_and_buckets_open_and_merged():
    response = {"data": {
        "repo": {
            "deployments": {"nodes": []},
            "pullRequests": {"nodes": [
                {"number": 10, "url": "u10", "title": "open one", "state": "OPEN",
                 "isDraft": False, "updatedAt": "2026-06-25T00:00:00Z", "mergedAt": None,
                 "headRefOid": "abc", "mergeStateStatus": "CLEAN",
                 "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS",
                   "contexts": {"nodes": []}}}}]}},
            ]},
        },
        "merged": {"nodes": [
            {"number": 9, "url": "u9", "title": "merged one", "state": "MERGED",
             "isDraft": False, "updatedAt": "2026-06-24T00:00:00Z",
             "mergedAt": "2026-06-24T00:00:00Z", "headRefOid": "def",
             "mergeStateStatus": None, "commits": {"nodes": []}},
        ]},
    }}
    prs = parse_population_response(response, "o/n")
    by_num = {p.number: p for p in prs}
    assert by_num[10].repo == "o/n"
    assert by_num[10].stage == "ready"          # open, passing, clean → ready
    assert by_num[10].state == "open"
    assert by_num[9].state == "merged"
    assert by_num[9].stage == "merged"          # merged within retention
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pr_status.py -k population -v`
Expected: FAIL — `ImportError: cannot import name 'build_population_query'`.

- [ ] **Step 3: Implement the builder + parser**

Add to `server/github/pr_status.py` (after `build_pr_query`). Note `replace` from `dataclasses`:

```python
from dataclasses import dataclass, replace  # update the existing dataclass import


def build_population_query(repo: str, merged_since: str) -> str:
    """One GraphQL doc: all OPEN PRs (incl. drafts) + production deployments for
    ``repo``, plus a search() for PRs merged on/after ``merged_since`` (ISO date).

    Strings are JSON-encoded so a repo/owner/name can't break the literal.
    """
    owner, _, name = repo.partition("/")
    merged_q = f"repo:{repo} is:pr is:merged merged:>={merged_since}"
    return (
        "query {\n"
        f"  repo: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    deployments(environments: [{json.dumps(PRODUCTION_ENVIRONMENT)}], "
        f"orderBy: {{field: CREATED_AT, direction: DESC}}, first: 5) {{\n"
        f"      nodes {{ commitOid latestStatus {{ state }} commit {{ committedDate }} }}\n"
        f"    }}\n"
        f"    pullRequests(states: OPEN, first: 100, "
        f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{\n"
        f"      nodes {{ {_PR_NODE_FIELDS} }}\n"
        f"    }}\n"
        f"  }}\n"
        f"  merged: search(query: {json.dumps(merged_q)}, type: ISSUE, first: 50) {{\n"
        f"    nodes {{ ... on PullRequest {{ {_PR_NODE_FIELDS} }} }}\n"
        f"  }}\n"
        "}"
    )


def parse_population_response(response: dict, repo: str) -> tuple[PullRequestInfo, ...]:
    """Open + recently-merged PRs for one repo, each with ``repo`` set.

    Deduped by PR number; the OPEN listing wins over the merged search (a PR
    can't be both, but a search index lag could surface a stale duplicate).
    Reuses ``_to_info`` so stage/checks/state come from the single classifier.
    """
    data = (response or {}).get("data") or {}
    repo_node = data.get("repo") or {}
    active_date = _active_deploy_date(repo_node)
    open_prs = ((repo_node.get("pullRequests") or {}).get("nodes")) or []
    merged_prs = ((data.get("merged") or {}).get("nodes")) or []
    by_number: dict[int, PullRequestInfo] = {}
    for raw in (*open_prs, *merged_prs):
        if not raw:  # search() can return null union members
            continue
        info = replace(_to_info(raw, active_date), repo=repo)
        by_number.setdefault(info.number, info)
    return tuple(by_number.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pr_status.py -k population -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status.py
git commit -m "feat(pr): population query builder + parser (full open+merged per repo)"
```

---

### Task 3: `PrPopulationPoller`

**Files:**
- Create: `server/github/pr_population.py`
- Test: `server/tests/test_pr_population.py`

**Interfaces:**
- Consumes: `build_population_query`, `parse_population_response` (Task 2); `gitrepos._run_gh` (existing).
- Produces: `PrPopulationPoller` with:
  - `set_repos_provider(fn)` — `fn() -> list[str]` (sync or async).
  - `cached(repo: str) -> tuple[PullRequestInfo, ...]` and `all() -> dict[str, tuple[PullRequestInfo, ...]]`.
  - `async refresh_once(repos: list[str]) -> bool` — per-repo isolated; one failure never blanks others.
  - `async start()` / `async stop()`.
  - Constructor accepts `query_fn: Callable[[str], dict] | None` (injected in tests), `retention_days: int = 7`, plus jitter/backoff/clock knobs mirroring `PrStatusPoller`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pr_population.py`:

```python
import asyncio

from server.github.pr_population import PrPopulationPoller


def _resp(number: int, state: str = "OPEN"):
    return {"data": {"repo": {
        "deployments": {"nodes": []},
        "pullRequests": {"nodes": [{
            "number": number, "url": f"u{number}", "title": "t", "state": state,
            "isDraft": False, "updatedAt": "2026-06-25T00:00:00Z", "mergedAt": None,
            "headRefOid": "x", "mergeStateStatus": "CLEAN",
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                {"state": "SUCCESS", "contexts": {"nodes": []}}}}]},
        }]},
    }, "merged": {"nodes": []}}}


def test_refresh_once_caches_per_repo():
    calls = []

    def qfn(repo: str) -> dict:
        calls.append(repo)
        return _resp(1 if repo == "o/a" else 2)

    p = PrPopulationPoller(query_fn=qfn)
    p.set_repos_provider(lambda: ["o/a", "o/b"])
    asyncio.run(p.refresh_once(["o/a", "o/b"]))

    assert sorted(calls) == ["o/a", "o/b"]
    assert [pr.number for pr in p.cached("o/a")] == [1]
    assert [pr.number for pr in p.cached("o/b")] == [2]
    assert p.cached("o/a")[0].repo == "o/a"


def test_one_failing_repo_does_not_blank_others():
    def qfn(repo: str) -> dict:
        if repo == "o/bad":
            raise RuntimeError("gh exploded")
        return _resp(5)

    p = PrPopulationPoller(query_fn=qfn)
    asyncio.run(p.refresh_once(["o/good", "o/bad"]))

    assert [pr.number for pr in p.cached("o/good")] == [5]
    assert p.cached("o/bad") == ()  # failed repo simply has no entry; good one survives


def test_cached_unknown_repo_is_empty_tuple():
    p = PrPopulationPoller(query_fn=lambda r: _resp(1))
    assert p.cached("never/seen") == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pr_population.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.github.pr_population'`.

- [ ] **Step 3: Implement the poller**

Create `server/github/pr_population.py`:

```python
"""PrPopulationPoller — background cache of the FULL PR population per repo.

Sibling of PrStatusPoller, but keyed by repo (not (repo, branch)): it sweeps
every open + recently-merged PR in each watched repo so the Pipeline board can
show PRs that have no ccswitch session. Stage/checks/state come from the shared
classifier (parse_population_response → _to_info); this module adds no PR logic.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Union

from ccswitch import gitrepos

from .pr_status import PullRequestInfo, build_population_query, parse_population_response

log = logging.getLogger(__name__)


def _merged_since(retention_days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return since.date().isoformat()


def _default_query(repo: str, retention_days: int) -> dict:
    query = build_population_query(repo, _merged_since(retention_days))
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"])
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    return json.loads(proc.stdout or "{}")


class PrPopulationPoller:
    def __init__(
        self,
        poll_interval: float = 75.0,
        query_fn: Callable[[str], dict] | None = None,
        *,
        retention_days: int = 7,
        jitter_frac: float = 0.1,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float | None = None,
        backoff_cap: float = 900.0,
    ) -> None:
        self._poll_interval = poll_interval
        self._retention_days = retention_days
        self._query_fn = query_fn or (lambda repo: _default_query(repo, retention_days))
        self._jitter_frac = jitter_frac
        self._rng = rng
        self._clock = clock
        self._backoff_base = backoff_base if backoff_base is not None else poll_interval
        self._backoff_cap = backoff_cap
        self._fail_count = 0
        self._cooldown_until = 0.0
        self._cache: dict[str, tuple[PullRequestInfo, ...]] = {}
        self._repos_provider: (
            Callable[[], list[str]] | Callable[[], Awaitable[list[str]]] | None
        ) = None
        self._task: asyncio.Task | None = None

    def set_repos_provider(
        self, fn: Union[Callable[[], list[str]], Callable[[], Awaitable[list[str]]]]
    ) -> None:
        self._repos_provider = fn  # type: ignore[assignment]

    async def _resolve_repos(self) -> list[str]:
        if not self._repos_provider:
            return []
        result = self._repos_provider()
        if inspect.isawaitable(result):
            result = await result
        return result or []

    def cached(self, repo: str | None) -> tuple[PullRequestInfo, ...]:
        if not repo:
            return ()
        return self._cache.get(repo, ())

    def all(self) -> dict[str, tuple[PullRequestInfo, ...]]:
        return dict(self._cache)

    async def refresh_once(self, repos: list[str]) -> bool:
        if not repos:
            return True
        any_ok = False
        any_fail = False
        for repo in repos:
            try:
                response = await asyncio.to_thread(self._query_fn, repo)
                self._cache[repo] = parse_population_response(response, repo)
                any_ok = True
            except Exception:
                log.warning("pr_population.refresh_failed repo=%s", repo, exc_info=True)
                any_fail = True
        if any_fail and not any_ok:
            self._fail_count += 1
            self._cooldown_until = self._clock() + min(
                self._backoff_base * (2 ** (self._fail_count - 1)), self._backoff_cap
            )
            return False
        self._fail_count = 0
        self._cooldown_until = 0.0
        return True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="pr-population-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            delay = self._poll_interval * (1 + self._jitter_frac * self._rng())
            if self._clock() < self._cooldown_until:
                delay = max(delay, self._cooldown_until - self._clock())
            await asyncio.sleep(delay)
            repos = await self._resolve_repos()
            await self.refresh_once(repos)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pr_population.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_population.py server/tests/test_pr_population.py
git commit -m "feat(pr): PrPopulationPoller — repo-keyed full PR population cache"
```

---

### Task 4: `PipelinePr` + orchestrator `all_prs()` + poller wiring

**Files:**
- Modify: `server/github/pr_status.py` (add `PipelinePr` dataclass)
- Modify: `server/sessions/orchestrator.py` (`__init__` poller instantiation ~line 230; add `_pr_repos`, `all_prs`; `start`/`stop` ~lines 288/357)
- Test: `server/tests/test_orchestrator_all_prs.py`

**Interfaces:**
- Consumes: `PrPopulationPoller` (Task 3); existing `self.prs_for` (orchestrator).
- Produces:
  - `PipelinePr` dataclass: `pr: PullRequestInfo`, `session_id: str | None = None`, `session_name: str | None = None`; `to_dict()` → `{**pr.to_dict(), "session": {"id","name"} | None}`; `from_dict(d)` inverse.
  - `SessionOrchestrator.all_prs() -> list[PipelinePr]` — flattened population, session-joined, sorted by stage urgency then PR number.
  - `SessionOrchestrator._pr_repos() -> list[str]` — sorted distinct `record.repo`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_all_prs.py`:

```python
from server.github.pr_status import PipelinePr, PullRequestInfo


def test_pipeline_pr_round_trip_with_and_without_session():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="none", title="t", repo="o/n")
    linked = PipelinePr(pr=pr, session_id="s1", session_name="Fix it")
    d = linked.to_dict()
    assert d["number"] == 1 and d["repo"] == "o/n"
    assert d["session"] == {"id": "s1", "name": "Fix it"}
    back = PipelinePr.from_dict(d)
    assert back.session_id == "s1" and back.session_name == "Fix it" and back.pr.repo == "o/n"

    unlinked = PipelinePr(pr=pr)
    assert unlinked.to_dict()["session"] is None
    assert PipelinePr.from_dict(unlinked.to_dict()).session_id is None


def test_all_prs_joins_population_to_sessions(make_orchestrator):
    """all_prs() attaches a session to population PRs that a session authored,
    and leaves unsessioned PRs with session_id=None. Stage order: parked first."""
    orch = make_orchestrator()
    # Population cache: repo o/n has PR #1 (parked) with a session, #2 (ready) without.
    orch._pr_population._cache["o/n"] = (
        PullRequestInfo(number=1, url="u1", state="open", checks="failing",
                        title="broken", repo="o/n", stage="parked"),
        PullRequestInfo(number=2, url="u2", state="open", checks="passing",
                        title="fine", repo="o/n", stage="ready"),
    )
    # A session that authored PR #1 (its session-branch poller knows it).
    orch._pr_poller._cache[("o/n", "feat/x")] = PullRequestInfo(
        number=1, url="u1", state="open", checks="failing", title="broken")
    rec = _make_record(orch, session_id="s1", name="Fix it", repo="o/n",
                       authored_branches=["feat/x"])

    result = orch.all_prs()
    by_num = {p.pr.number: p for p in result}
    assert by_num[1].session_id == "s1" and by_num[1].session_name == "Fix it"
    assert by_num[2].session_id is None
    # parked (urgency 0) sorts before ready
    assert [p.pr.number for p in result][0] == 1
```

Add a helper near the top of the file (or reuse the suite's existing fixture — check `test_orchestrator_prs_for.py` for the established `make_orchestrator` / record-construction pattern and mirror it; the snippet below assumes a minimal in-memory record):

```python
from server.sessions.types import SessionRecord


def _make_record(orch, *, session_id, name, repo, authored_branches):
    rec = SessionRecord(id=session_id, name=name, repo=repo)
    rec.authored_branches = authored_branches
    orch._records[session_id] = rec
    return rec
```

> NOTE for the implementer: open `server/tests/test_orchestrator_prs_for.py` first and copy its exact orchestrator-construction fixture and `SessionRecord(...)` call signature — field names/required args must match the real model. Adjust `_make_record` accordingly before running.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_all_prs.py -v`
Expected: FAIL — `ImportError: cannot import name 'PipelinePr'`.

- [ ] **Step 3: Add `PipelinePr`**

In `server/github/pr_status.py`, after `PullRequestInfo`:

```python
@dataclass(frozen=True)
class PipelinePr:
    """A population PR plus the ccswitch session that authored it (if any)."""
    pr: PullRequestInfo
    session_id: str | None = None
    session_name: str | None = None

    def to_dict(self) -> dict:
        d = self.pr.to_dict()
        d["session"] = (
            {"id": self.session_id, "name": self.session_name}
            if self.session_id else None
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PipelinePr":
        sess = d.get("session") or {}
        return cls(
            pr=PullRequestInfo.from_dict(d),
            session_id=sess.get("id"),
            session_name=sess.get("name"),
        )
```

- [ ] **Step 4: Instantiate the poller + add `_pr_repos` / `all_prs`**

In `server/sessions/orchestrator.py.__init__`, right after the existing PrStatusPoller block (around line 232):

```python
        from server.github.pr_population import PrPopulationPoller
        self._pr_population: PrPopulationPoller = PrPopulationPoller()
        self._pr_population.set_repos_provider(self._pr_repos)
```

Add these methods near `prs_for` (after line 1089):

```python
    def _pr_repos(self) -> list[str]:
        """Sorted distinct repos across all live sessions — the population to watch."""
        return sorted({r.repo for r in self._records.values() if r.repo})

    def all_prs(self) -> "list[PipelinePr]":
        """Full PR population (all watched repos) joined to authoring sessions.

        Session join reuses ``prs_for`` (the session-branch poller's knowledge),
        so a PR is linked iff some session authored its branch. Sorted by stage
        urgency (parked→ci→ready→queue→merged), then PR number ascending.
        """
        from server.github.pr_status import PipelinePr
        sess_by_pr: dict[tuple[str, int], tuple[str, str]] = {}
        for rec in self._records.values():
            if not rec.repo:
                continue
            for info in self.prs_for(rec.id):
                sess_by_pr.setdefault((rec.repo, info.number), (rec.id, rec.name))
        order = {"parked": 0, "ci": 1, "ready": 2, "queue": 3, "merged": 4}
        out: list[PipelinePr] = []
        for repo, prs in self._pr_population.all().items():
            for info in prs:
                sid, sname = sess_by_pr.get((repo, info.number), (None, None))
                out.append(PipelinePr(pr=info, session_id=sid, session_name=sname))
        out.sort(key=lambda p: (order.get(p.pr.stage, 99), p.pr.number))
        return out
```

Add the start/stop calls. After `await self._pr_poller.start()` (~line 288):

```python
        await self._pr_population.start()
```

After `await self._pr_poller.stop()` (~line 357):

```python
        await self._pr_population.stop()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_all_prs.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add server/github/pr_status.py server/sessions/orchestrator.py server/tests/test_orchestrator_all_prs.py
git commit -m "feat(pipeline): orchestrator all_prs() joins population to sessions"
```

---

### Task 5: Wire `all_prs()` across the daemon↔edge boundary

**Files:**
- Modify: `server/sessions/facade.py` (`InProcessOrchestratorAdapter.all_prs`)
- Modify: `server/sessions/remote.py` (`RemoteOrchestrator.all_prs`)
- Modify: `server/ipc/orchestrator_rpc.py` (register `all_prs`)
- Test: `server/tests/test_orchestrator_rpc_breadth.py` (append) — verifies the RPC is registered and round-trips.

**Interfaces:**
- Consumes: `orch.all_prs()` (Task 4), `PipelinePr.from_dict` (Task 4).
- Produces: `adapter.all_prs()` and `remote.all_prs()` both return `list[PipelinePr]`; RPC `all_prs` returns `{"prs": [PipelinePr.to_dict(), ...]}`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_orchestrator_rpc_breadth.py` (mirror the file's existing fake-orchestrator + `register_orchestrator_rpc` harness — inspect the top of that file first):

```python
def test_all_prs_rpc_registered_and_round_trips(rpc_app_and_orch):
    """all_prs must be a registered RPC returning a JSON-safe {'prs': [...]}."""
    app, orch = rpc_app_and_orch  # adapt to the file's actual fixture name
    assert "all_prs" in app.state.rpc_handlers  # adapt to how handlers are stored
```

> NOTE: This file already tests RPC breadth. Follow its exact assertion style for "method X is registered" (it may introspect `register_rpc` via a recording stub rather than `app.state.rpc_handlers`). The goal: assert `all_prs` is registered. If the suite has a list of expected RPC names, add `"all_prs"` to it.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc_breadth.py -k all_prs -v`
Expected: FAIL — `all_prs` not registered.

- [ ] **Step 3: Register the RPC handler**

In `server/ipc/orchestrator_rpc.py`, after the `prs_for` handler (line 61), add:

```python
    async def all_prs(params: dict) -> dict:
        items = orch.all_prs()
        if inspect.isawaitable(items):
            items = await items
        return {"prs": [p.to_dict() for p in items]}
```

And register it next to `register_rpc(app, "prs_for", prs_for)` (line 69):

```python
    register_rpc(app, "all_prs", all_prs)
```

(`inspect` is already imported lower in the file; if the handler is defined above that import, move `import inspect` to the top of the function or module.)

- [ ] **Step 4: Add the facade + remote methods**

In `server/sessions/facade.py`, next to `pr_for` (line 60):

```python
    async def all_prs(self): return await _maybe_await(self._orch.all_prs())
```

In `server/sessions/remote.py`, after `prs_for` (line 112). Ensure `PipelinePr` is importable here (mirror how `PullRequestInfo` is imported in this module):

```python
    async def all_prs(self) -> "list[PipelinePr]":
        """Resolve the full PR population (joined to sessions) via the daemon."""
        from server.github.pr_status import PipelinePr
        r = await self._client.call("all_prs")
        return [PipelinePr.from_dict(d) for d in (r.get("prs") or [])]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc_breadth.py server/tests/test_remote_orchestrator.py -v`
Expected: PASS (new test + existing remote tests still green).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/facade.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_orchestrator_rpc_breadth.py
git commit -m "feat(pipeline): wire all_prs() through facade/remote/RPC (daemon<->edge)"
```

---

### Task 6: `GET /api/prs` route

**Files:**
- Create: `server/api/pipeline.py`
- Modify: `server/app.py` (import + `include_router` ~lines 36/406)
- Test: `server/tests/test_pipeline_route.py`

**Interfaces:**
- Consumes: `request.app.state.sessions.all_prs()` (Task 5), `current_identity` (existing).
- Produces: `GET /api/prs` → `list[dict]` (each = `PipelinePr.to_dict()`); 401 without identity.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pipeline_route.py`. Mirror the auth + TestClient + injected-orchestrator pattern from `server/tests/test_sessions_pr_field.py` (open it first for the exact `create_app`/identity-injection helper this suite uses):

```python
def test_get_prs_requires_identity(client_no_identity):
    # adapt fixture name to the suite's "unauthenticated client"
    resp = client_no_identity.get("/api/prs")
    assert resp.status_code == 401


def test_get_prs_returns_population(client_with_identity, fake_orchestrator):
    from server.github.pr_status import PipelinePr, PullRequestInfo
    fake_orchestrator.all_prs_result = [
        PipelinePr(
            pr=PullRequestInfo(number=3, url="u3", state="open", checks="failing",
                               title="t", repo="o/n", stage="parked"),
            session_id="s1", session_name="Fix it"),
        PipelinePr(
            pr=PullRequestInfo(number=4, url="u4", state="merged", checks="none",
                               title="m", repo="o/n", stage="merged")),
    ]
    resp = client_with_identity.get("/api/prs")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["number"] for p in body] == [3, 4]
    assert body[0]["repo"] == "o/n" and body[0]["stage"] == "parked"
    assert body[0]["session"] == {"id": "s1", "name": "Fix it"}
    assert body[1]["session"] is None
```

> NOTE: give the suite's fake orchestrator adapter an `async def all_prs(self): return self.all_prs_result` (or extend the existing fake). Follow `test_sessions_pr_field.py` for how it stubs the adapter on `app.state.sessions`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pipeline_route.py -v`
Expected: FAIL — 404 (route not mounted).

- [ ] **Step 3: Create the router**

Create `server/api/pipeline.py`:

```python
"""Pipeline REST API — the full PR population for the board view.

GET /api/prs → every open + recently-merged PR across watched repos, each
joined to its authoring ccswitch session (or session=null). Requires a verified
identity. Reads the in-memory population cache (cheap); no GitHub call per request.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from server.auth.middleware import current_identity

router = APIRouter(prefix="/api/prs", tags=["pipeline"])


@router.get("")
async def list_prs(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    orch = request.app.state.sessions
    items = await orch.all_prs()
    return [p.to_dict() for p in items]
```

- [ ] **Step 4: Mount it in `app.py`**

In `server/app.py`, with the other router imports (~line 36):

```python
from server.api.pipeline import router as pipeline_router
```

And with the other `include_router` calls (~line 406):

```python
    app.include_router(pipeline_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_pipeline_route.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add server/api/pipeline.py server/app.py server/tests/test_pipeline_route.py
git commit -m "feat(pipeline): GET /api/prs serves the joined PR population"
```

---

### Task 7: Frontend types + `useAllPrs()` hook

**Files:**
- Modify: `web/src/api/types.ts` (`PullRequestInfo.repo`; new `PipelinePr`)
- Modify: `web/src/api/hooks.ts` (`QK.prs`, `useAllPrs`)
- Test: `web/src/api/__tests__/useAllPrs.test.ts`

**Interfaces:**
- Produces:
  - `PullRequestInfo` gains `repo?: string`.
  - `PipelinePr = PullRequestInfo & { session: { id: string; name: string } | null }`.
  - `useAllPrs()` → react-query result of `PipelinePr[]` from `GET /prs`.

- [ ] **Step 1: Write the failing test**

Create `web/src/api/__tests__/useAllPrs.test.ts` (mirror `hooks.settings.test.ts` for the QueryClient + `renderHook` + `api` mock setup):

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useAllPrs } from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({ api: { get: vi.fn() }, ApiRequestError: class {} }))

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useAllPrs', () => {
  beforeEach(() => vi.clearAllMocks())

  it('fetches the PR population from /prs', async () => {
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      { number: 1, url: 'u', state: 'open', checks: 'failing', title: 't',
        deployed: false, repo: 'o/n', stage: 'parked', session: { id: 's1', name: 'Fix' } },
    ])
    const { result } = renderHook(() => useAllPrs(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/prs')
    expect(result.current.data?.[0].session?.id).toBe('s1')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/api/__tests__/useAllPrs.test.ts`
Expected: FAIL — `useAllPrs` is not exported.

- [ ] **Step 3: Add the types**

In `web/src/api/types.ts`, add `repo` to `PullRequestInfo` (after `stage`):

```ts
  /** nameWithOwner of the PR's repo (populated for population/pipeline PRs). */
  repo?: string
```

And add, below `PullRequestInfo`:

```ts
export interface PipelinePr extends PullRequestInfo {
  /** The ccswitch session that authored this PR's branch, or null. */
  session: { id: string; name: string } | null
}
```

- [ ] **Step 4: Add the hook**

In `web/src/api/hooks.ts`: add `PipelinePr` to the `types` import; add `prs: ['prs'] as const,` to `QK`; and add:

```ts
export function useAllPrs() {
  return useQuery<PipelinePr[]>({
    queryKey: QK.prs,
    queryFn: () => api.get<PipelinePr[]>('/prs'),
    staleTime: 5_000,
    refetchInterval: 10_000,
  })
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/api/__tests__/useAllPrs.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/__tests__/useAllPrs.test.ts
git commit -m "feat(web): PipelinePr type + useAllPrs hook (GET /prs)"
```

---

### Task 8: `PipelineBoard` + `PipelineView` components

**Files:**
- Create: `web/src/components/PipelineView.tsx` (exports pure `PipelineBoard` + wrapper `PipelineView`)
- Test: `web/src/components/__tests__/PipelineBoard.test.tsx`

**Interfaces:**
- Consumes: `PipelinePr` (Task 7), `PrPill` (existing), `GroupHeader` (existing), `useAllPrs`/`useFixPr` (existing/Task 7).
- Produces:
  - `PipelineBoard({ prs, onOpenSession, onFixPr })` — pure; renders one collapsible section per non-empty stage in order Parked→In CI→Ready→Queue→Merged; empty board → empty state.
  - `PipelineView()` — wrapper calling `useAllPrs()` + `useFixPr()`, passing `navigate`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/PipelineBoard.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PipelineBoard } from '../PipelineView'
import type { PipelinePr } from '@/api/types'

function pr(over: Partial<PipelinePr>): PipelinePr {
  return {
    number: 1, url: 'u', state: 'open', checks: 'none', title: 't',
    deployed: false, repo: 'o/n', stage: 'ready', session: null, ...over,
  }
}

describe('PipelineBoard', () => {
  it('renders sections in urgency order and hides empty stages', () => {
    render(<PipelineBoard prs={[
      pr({ number: 2, stage: 'ready' }),
      pr({ number: 1, stage: 'parked', checks: 'failing' }),
      pr({ number: 3, stage: 'merged', state: 'merged' }),
    ]} />)
    const headings = screen.getAllByRole('button').map((b) => b.textContent)
    const order = headings.join('|')
    expect(order.indexOf('Parked')).toBeLessThan(order.indexOf('Ready'))
    expect(order.indexOf('Ready')).toBeLessThan(order.indexOf('Merged'))
    expect(screen.queryByText('In CI')).toBeNull()  // no ci-stage PR → hidden
  })

  it('shows an empty state when there are no PRs', () => {
    render(<PipelineBoard prs={[]} />)
    expect(screen.getByText(/no open prs/i)).toBeInTheDocument()
  })

  it('session-linked failing PR opens its session and offers Fix; unlinked does not', () => {
    const onOpenSession = vi.fn()
    const onFixPr = vi.fn()
    render(<PipelineBoard
      prs={[
        pr({ number: 5, stage: 'parked', checks: 'failing',
             session: { id: 's1', name: 'Fix me' } }),
        pr({ number: 6, stage: 'parked', checks: 'failing', session: null }),
      ]}
      onOpenSession={onOpenSession}
      onFixPr={onFixPr}
    />)
    fireEvent.click(screen.getByTestId('pipeline-row-5'))
    expect(onOpenSession).toHaveBeenCalledWith('s1')

    fireEvent.click(screen.getByTestId('pipeline-fix-5'))
    expect(onFixPr).toHaveBeenCalledWith('s1', 5)

    // Unlinked PR #6 has no Fix button.
    expect(screen.queryByTestId('pipeline-fix-6')).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineBoard.test.tsx`
Expected: FAIL — cannot resolve `../PipelineView`.

- [ ] **Step 3: Implement the components**

Create `web/src/components/PipelineView.tsx`:

```tsx
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Wrench } from 'lucide-react'
import { GroupHeader } from './GroupHeader'
import { PrPill } from './PrPill'
import { useAllPrs, useFixPr } from '@/api/hooks'
import { isActionablePrState } from '@/session/prStatus'
import type { PipelinePr } from '@/api/types'

// Stage sections in urgency order; '' / unknown stages are dropped.
const STAGE_SECTIONS: { key: string; label: string }[] = [
  { key: 'parked', label: 'Parked' },
  { key: 'ci', label: 'In CI' },
  { key: 'ready', label: 'Ready' },
  { key: 'queue', label: 'Queue' },
  { key: 'merged', label: 'Merged' },
]

export function PipelineBoard({
  prs,
  onOpenSession,
  onFixPr,
}: {
  prs: PipelinePr[]
  onOpenSession?: (sessionId: string) => void
  onFixPr?: (sessionId: string, number: number) => void
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const toggle = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }))

  if (prs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-8 text-center">
        No open PRs in your repos.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      {STAGE_SECTIONS.map(({ key, label }) => {
        const rows = prs.filter((p) => (p.stage ?? '') === key)
        if (rows.length === 0) return null
        return (
          <section key={key}>
            <GroupHeader
              groupKey={`stage-${key}`}
              label={label}
              count={rows.length}
              collapsed={!!collapsed[key]}
              onToggle={() => toggle(key)}
            />
            {!collapsed[key] && (
              <ul className="mt-1 space-y-1">
                {rows.map((p) => {
                  const canFix =
                    !!p.session && p.checks === 'failing' && isActionablePrState(p.state)
                  return (
                    <li key={`${p.repo}#${p.number}`}>
                      <div
                        data-testid={`pipeline-row-${p.number}`}
                        role={p.session ? 'button' : undefined}
                        tabIndex={p.session ? 0 : undefined}
                        onClick={() => {
                          if (p.session) onOpenSession?.(p.session.id)
                          else window.open(p.url, '_blank', 'noopener,noreferrer')
                        }}
                        className="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-muted/60 cursor-pointer"
                      >
                        <PrPill pr={p} />
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {p.repo}
                        </span>
                        <span className="truncate text-sm">{p.title}</span>
                        {p.session && (
                          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                            {p.session.name}
                          </span>
                        )}
                        {canFix && p.session && (
                          <button
                            type="button"
                            data-testid={`pipeline-fix-${p.number}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              onFixPr?.(p.session!.id, p.number)
                            }}
                            aria-label={`Fix PR #${p.number}`}
                            className="ml-2 inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
                          >
                            <Wrench size={11} aria-hidden />
                            <span>Fix #{p.number}</span>
                          </button>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        )
      })}
    </div>
  )
}

export function PipelineView() {
  const navigate = useNavigate()
  const { data: prs = [] } = useAllPrs()
  const fixPr = useFixPr()
  return (
    <PipelineBoard
      prs={prs}
      onOpenSession={(id) => navigate(`/session/${id}`)}
      onFixPr={(id, number) => fixPr.mutate({ id, number })}
    />
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineBoard.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/PipelineView.tsx web/src/components/__tests__/PipelineBoard.test.tsx
git commit -m "feat(web): PipelineBoard + PipelineView (stage-grouped PR board)"
```

---

### Task 9: Surface the Pipeline tab (mobile Dashboard, desktop DesktopShell, route)

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (`TabId` line 411; `TAB_ORDER` line 509; tab button + pane)
- Modify: `web/src/components/DesktopShell.tsx` (local `TabId` line 60; tab strip line 336; content render ~line 429)
- Modify: `web/src/App.tsx` (`/pipeline` route)
- Test: `web/src/components/__tests__/PipelineTab.test.tsx`

**Interfaces:**
- Consumes: `PipelineView` (Task 8).
- Produces: a `'pipeline'` tab in both shells rendering `<PipelineView />`; `/pipeline` route on mobile.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/PipelineTab.test.tsx`. Mirror `DashboardTabs.test.tsx` for the hook-mock factory (copy its `vi.mock('@/api/hooks', ...)` block verbatim, then add `useAllPrs: vi.fn(() => ({ data: [] }))` to it — see memory: dashboard-hooks-partial-mocks):

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Dashboard } from '../Dashboard'

// COPY the full hook-mock factory from DashboardTabs.test.tsx here, then add:
//   useAllPrs: vi.fn(() => ({ data: [] })),

describe('Pipeline tab', () => {
  it('renders the Pipeline board when the pipeline tab is active', () => {
    render(
      <MemoryRouter>
        <Dashboard activeTab="pipeline" onTabChange={vi.fn()} onOpenSession={vi.fn()} />
      </MemoryRouter>,
    )
    // empty board → empty state copy from PipelineView
    expect(screen.getByText(/no open prs/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/PipelineTab.test.tsx`
Expected: FAIL — `'pipeline'` is not assignable to `TabId` (tsc) / nothing rendered.

- [ ] **Step 3: Wire the Dashboard tab**

In `web/src/components/Dashboard.tsx`:
- Line 411: `export type TabId = 'sessions' | 'accounts' | 'archived' | 'pipeline'`
- Line 509: `const TAB_ORDER: TabId[] = ['sessions', 'accounts', 'pipeline', 'archived']`
- Add a tab button mirroring the `accounts` button (lines 708–717): copy the `<button>` block, swap `'accounts'`→`'pipeline'`, label `Pipeline`, and add a `tabRefs` entry (`pipeline: useRef(...)`) matching the existing `tabRefs` map (line 504).
- Add the pane after the `archived` pane (after line ~882):

```tsx
      {activeTab === 'pipeline' && (
        <PipelineView />
      )}
```

- Add the import at the top: `import { PipelineView } from './PipelineView'`.

- [ ] **Step 4: Wire the DesktopShell tab**

In `web/src/components/DesktopShell.tsx`:
- Line 60: `type TabId = 'sessions' | 'accounts' | 'archived' | 'pipeline'`
- Line 336: `{(['sessions', 'accounts', 'pipeline', 'archived'] as TabId[]).map((t) => (`
- In the content render (the `if (tab === 'accounts') {...} else if (tab === 'archived') {...}` chain ~line 429), add a branch:

```tsx
  } else if (tab === 'pipeline') {
    content = <PipelineView />
```

(Adapt to the file's actual `content = ...` assignment style.)
- Add the import: `import { PipelineView } from './PipelineView'`.

- [ ] **Step 5: Add the route in App.tsx**

In `web/src/App.tsx`, inside the mobile `<Routes>` (after line 104):

```tsx
          <Route path="/pipeline" element={<DashboardRoute tab="pipeline" />} />
```

(`onTabChange` at line 31 already maps non-`sessions` tabs to `/${next}`, so `/pipeline` is produced automatically.)

- [ ] **Step 6: Run the focused test + full gates**

Run (in `web/`):
```bash
npx vitest run src/components/__tests__/PipelineTab.test.tsx
npx tsc -b --force
npx eslint . --max-warnings 0
npx vitest run
```
Expected: the new test PASSES; tsc clean; eslint 0 warnings; full vitest green (run the full suite — CI runs it and concurrently-merged fixtures can break subset-only checks; memory: stale-worktree-pr-and-full-vitest).

- [ ] **Step 7: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/App.tsx web/src/components/__tests__/PipelineTab.test.tsx
git commit -m "feat(web): surface the Pipeline tab (mobile + desktop + route)"
```

---

## Final verification (before opening the PR)

- [ ] Backend full suite:
  `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q`
  (the ~2 env-only failures noted in memory — remote_mode_wiring, x11 — are not regressions.)
- [ ] Backend lint (whole tree): `ruff check src/ server/` and `mypy` per the repo's CI config.
- [ ] Frontend (in `web/`): `npx tsc -b --force` · `npx eslint . --max-warnings 0` · `npx vitest run`.
- [ ] Open the PR per CLAUDE.md completion workflow: non-draft, `Fixes`/`Refs` as appropriate, then `gh pr merge <#> --squash --auto`.

---

## Self-Review notes (author)

- **Spec coverage:** §3.1 PrPopulationPoller → T3; §3.2 repo field → T1; §3.3 all_prs 5-layer wiring → T4 (in-process) + T5 (facade/remote/RPC); §3.4 route → T6; §4.1 hook → T7; §4.2 board → T8; §4.3 navigation → T9; §6 edges (per-repo isolation, empty board, retention) → T3/T8; §7 testing → every task is TDD. No spec section is unmapped.
- **Type consistency:** `PipelinePr` (`pr`, `session_id`, `session_name`; wire `session`) is defined once (T4 backend, T7 frontend) and consumed identically in T5/T6/T8. `all_prs()` returns `list[PipelinePr]` at every layer. Stage order map (`parked,ci,ready,queue,merged`) matches the frontend `STAGE_SECTIONS` order.
- **Reuse:** stage logic via `_to_info` only (T2/T3); `useFixPr`/`PrPill`/`GroupHeader`/`isActionablePrState` reused, not reimplemented (T8).
- **Known soft spots flagged inline for the implementer:** exact fixture names in `test_orchestrator_prs_for.py`, `test_orchestrator_rpc_breadth.py`, `test_sessions_pr_field.py`, and `DashboardTabs.test.tsx` must be copied from those files (test harness signatures vary across the suite); each such step carries a NOTE.
