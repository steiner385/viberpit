# Per-Session PR Indicators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a compact, color-coded PR pill (number + state + CI checks) on each dashboard session card, kept current by a background GitHub poller, in the style of Claude Code web.

**Architecture:** A session's per-session worktree branch (`ccswitch/<branch>-<short>`) is the 1:1 join key to a GitHub PR's `headRefName`. We store that branch on the session, a background `PrStatusPoller` batch-queries GitHub via `gh api graphql` and caches `(repo, branch) → PullRequestInfo` in memory, and the existing `GET /api/sessions` route attaches each session's cached PR to its wire payload. The frontend renders a `PrPill` beside the existing `StatusPill`.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy(SQLite) backend; `gh` CLI for GitHub GraphQL; React + TypeScript + Tailwind + lucide-react frontend; pytest + vitest.

## Global Constraints

- Constitution: never fork/patch the Claude binary; verify against the real `gh`/GitHub, not assumptions; security-first; TDD + quality gates; single source of truth / no dead code.
- New SQLite columns MUST be **nullable** (dev `init_db` backfills nullable columns; never ALTERs). A NOT NULL column would need a real migration.
- GitHub reads go through **GraphQL** (`gh api graphql`), batched — not per-PR REST calls.
- Run `gh` through `ccswitch.gitrepos._run_gh` (it strips the shadowing `GITHUB_TOKEN`).
- PR status is **decorative**: poller/parse failures must never raise into an HTTP request; degrade to "no pill".
- Do NOT add a new `@/api/hooks` hook into `Dashboard.tsx` — PR data rides the existing session-list payload (see `dashboard-hooks-partial-mocks`). 
- Server tests: run with the main checkout's prebuilt env, e.g. `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/ff9edc26/.venv/bin/python` if present, else the main checkout's `.venv`. Web tests run in `web/` with `node_modules` symlinked from the main checkout.

---

### Task 1: PR status query + response parsing (pure functions)

**Files:**
- Create: `server/github/__init__.py` (empty)
- Create: `server/github/pr_status.py`
- Test: `server/tests/test_pr_status.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PullRequestInfo` with fields `number: int`, `url: str`, `state: str` (`'open'|'draft'|'merged'|'closed'`), `checks: str` (`'passing'|'failing'|'pending'|'none'`), `title: str`, and method `to_dict() -> dict` returning `{"number","url","state","checks","title"}`.
  - `build_pr_query(pairs: list[tuple[str, str]]) -> str` — GraphQL doc aliasing one `repository(...)` block per `(repo_name_with_owner, branch)` pair; returns `""` for an empty list.
  - `parse_pr_response(response: dict, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], PullRequestInfo]`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_status.py
from server.github.pr_status import (
    PullRequestInfo, build_pr_query, parse_pr_response,
)


def _resp(alias_to_nodes: dict) -> dict:
    return {"data": {a: {"pullRequests": {"nodes": n}} for a, n in alias_to_nodes.items()}}


def _pr(number, state, *, draft=False, rollup="SUCCESS", updated="2026-06-22T10:00:00Z"):
    commit = {"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}
    return {
        "number": number, "url": f"https://github.com/o/r/pull/{number}",
        "title": f"PR {number}", "state": state, "isDraft": draft,
        "updatedAt": updated, "commits": {"nodes": [commit]},
    }


def test_build_query_empty_is_blank():
    assert build_pr_query([]) == ""


def test_build_query_aliases_each_pair():
    q = build_pr_query([("steiner385/cc-account-switcher", "ccswitch/foo-abc")])
    assert "r0: repository(" in q
    assert '"steiner385"' in q and '"cc-account-switcher"' in q
    assert '"ccswitch/foo-abc"' in q
    assert "statusCheckRollup" in q


def test_parse_open_passing():
    pairs = [("o/r", "br")]
    out = parse_pr_response(_resp({"r0": [_pr(7, "OPEN", rollup="SUCCESS")]}), pairs)
    info = out[("o/r", "br")]
    assert info == PullRequestInfo(7, "https://github.com/o/r/pull/7", "open", "passing", "PR 7")


def test_parse_state_classification():
    pairs = [("o/r", "br")]
    draft = parse_pr_response(_resp({"r0": [_pr(1, "OPEN", draft=True)]}), pairs)[("o/r", "br")]
    merged = parse_pr_response(_resp({"r0": [_pr(2, "MERGED")]}), pairs)[("o/r", "br")]
    closed = parse_pr_response(_resp({"r0": [_pr(3, "CLOSED")]}), pairs)[("o/r", "br")]
    assert (draft.state, merged.state, closed.state) == ("draft", "merged", "closed")


def test_parse_checks_classification():
    pairs = [("o/r", "br")]
    def checks(rollup):
        return parse_pr_response(_resp({"r0": [_pr(1, "OPEN", rollup=rollup)]}), pairs)[("o/r", "br")].checks
    assert checks("SUCCESS") == "passing"
    assert checks("FAILURE") == "failing"
    assert checks("ERROR") == "failing"
    assert checks("PENDING") == "pending"
    assert checks(None) == "none"


def test_parse_picks_most_relevant_pr():
    # An open PR outranks a merged one even if the merged is newer.
    pairs = [("o/r", "br")]
    nodes = [
        _pr(2, "MERGED", updated="2026-06-22T12:00:00Z"),
        _pr(1, "OPEN", updated="2026-06-22T09:00:00Z"),
    ]
    out = parse_pr_response(_resp({"r0": nodes}), pairs)
    assert out[("o/r", "br")].number == 1


def test_parse_no_prs_omits_key():
    out = parse_pr_response(_resp({"r0": []}), [("o/r", "br")])
    assert ("o/r", "br") not in out


def test_parse_tolerates_missing_alias():
    out = parse_pr_response({"data": {}}, [("o/r", "br")])
    assert out == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_pr_status.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.github'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/github/__init__.py
```
(empty file)

```python
# server/github/pr_status.py
"""Pure helpers to query GitHub PRs by head branch and parse the response.

No I/O lives here — ``build_pr_query`` produces a GraphQL document and
``parse_pr_response`` maps a parsed response to PullRequestInfo, so both are
fully unit-testable without touching the network or the ``gh`` CLI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    state: str   # 'open' | 'draft' | 'merged' | 'closed'
    checks: str  # 'passing' | 'failing' | 'pending' | 'none'
    title: str

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "checks": self.checks,
            "title": self.title,
        }


def _alias(i: int) -> str:
    return f"r{i}"


def build_pr_query(pairs: list[tuple[str, str]]) -> str:
    """One GraphQL doc aliasing a repository(...).pullRequests block per pair.

    Strings are JSON-encoded so a branch/owner/name can never break out of the
    string literal. Returns "" for an empty list (caller skips the gh call).
    """
    if not pairs:
        return ""
    blocks = []
    for i, (name_with_owner, branch) in enumerate(pairs):
        owner, _, name = name_with_owner.partition("/")
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
            f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{\n"
            f"    nodes {{ number url title state isDraft updatedAt\n"
            f"      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}\n"
            f"    }}\n"
            f"  }}\n"
            f"}}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def _classify_state(gql_state: str, is_draft: bool) -> str:
    if gql_state == "MERGED":
        return "merged"
    if gql_state == "CLOSED":
        return "closed"
    return "draft" if is_draft else "open"


def _classify_checks(rollup_state: str | None) -> str:
    if rollup_state in ("SUCCESS",):
        return "passing"
    if rollup_state in ("FAILURE", "ERROR"):
        return "failing"
    if rollup_state in ("PENDING", "EXPECTED"):
        return "pending"
    return "none"


# Lower rank = more relevant. Tie-break by updatedAt (newest first).
_STATE_RANK = {"open": 0, "draft": 1, "merged": 2, "closed": 3}


def _rollup_state(pr: dict) -> str | None:
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    if not nodes:
        return None
    rollup = (nodes[0].get("commit") or {}).get("statusCheckRollup")
    return rollup.get("state") if rollup else None


def _to_info(pr: dict) -> PullRequestInfo:
    state = _classify_state(pr.get("state", ""), bool(pr.get("isDraft")))
    return PullRequestInfo(
        number=pr["number"],
        url=pr["url"],
        state=state,
        checks=_classify_checks(_rollup_state(pr)),
        title=pr.get("title", ""),
    )


def parse_pr_response(
    response: dict, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], PullRequestInfo]:
    data = (response or {}).get("data") or {}
    out: dict[tuple[str, str], PullRequestInfo] = {}
    for i, key in enumerate(pairs):
        node = data.get(_alias(i))
        if not node:
            continue
        prs = ((node.get("pullRequests") or {}).get("nodes")) or []
        best: PullRequestInfo | None = None
        best_sort: tuple | None = None
        for raw in prs:
            info = _to_info(raw)
            # Rank ascending, then newest updatedAt first (negate via reverse compare).
            sort_key = (_STATE_RANK.get(info.state, 99), _neg_iso(raw.get("updatedAt", "")))
            if best_sort is None or sort_key < best_sort:
                best, best_sort = info, sort_key
        if best is not None:
            out[key] = best
    return out


def _neg_iso(iso: str) -> str:
    # ISO-8601 strings sort lexicographically; invert each char so a *newer*
    # timestamp produces a *smaller* key (used as a min tie-break).
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in iso)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_pr_status.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add server/github/__init__.py server/github/pr_status.py server/tests/test_pr_status.py
git commit -m "feat(github): PR-by-branch GraphQL query + response parsing"
```

---

### Task 2: PrStatusPoller (background cache)

**Files:**
- Create: `server/github/pr_poller.py`
- Test: `server/tests/test_pr_poller.py`

**Interfaces:**
- Consumes: `build_pr_query`, `parse_pr_response`, `PullRequestInfo` from Task 1; `ccswitch.gitrepos._run_gh`, `ccswitch.gitrepos.GhError`.
- Produces: `class PrStatusPoller`:
  - `__init__(self, poll_interval: float = 75.0, query_fn=None, *, jitter_frac=0.1, rng=random.random, clock=time.monotonic, backoff_base=None, backoff_cap=900.0)` — `query_fn(pairs) -> dict` runs the GraphQL call and returns the parsed response; defaults to the real `gh` call.
  - `set_pairs_provider(self, fn: Callable[[], list[tuple[str, str]]]) -> None`.
  - `cached(self, repo: str | None, branch: str | None) -> PullRequestInfo | None`.
  - `async refresh_once(self, pairs: list[tuple[str, str]]) -> bool` — runs the query off-thread, updates cache for the queried pairs, returns True on success / False on failure (cache untouched on failure).
  - `async start(self) -> None` / `async stop(self) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_poller.py
import pytest

from server.github.pr_poller import PrStatusPoller
from server.github.pr_status import PullRequestInfo

PAIRS = [("o/r", "br")]


def _good_query(pairs):
    return {"data": {"r0": {"pullRequests": {"nodes": [{
        "number": 5, "url": "https://github.com/o/r/pull/5", "title": "x",
        "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-22T10:00:00Z",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }]}}}}


@pytest.mark.asyncio
async def test_refresh_populates_cache():
    p = PrStatusPoller(query_fn=_good_query)
    assert await p.refresh_once(PAIRS) is True
    info = p.cached("o/r", "br")
    assert isinstance(info, PullRequestInfo) and info.number == 5 and info.state == "open"


@pytest.mark.asyncio
async def test_cached_none_for_unknown_or_null():
    p = PrStatusPoller(query_fn=_good_query)
    assert p.cached("o/r", "br") is None        # nothing fetched yet
    assert p.cached(None, "br") is None
    assert p.cached("o/r", None) is None


@pytest.mark.asyncio
async def test_refresh_failure_keeps_cache():
    p = PrStatusPoller(query_fn=_good_query)
    await p.refresh_once(PAIRS)

    def boom(pairs):
        raise RuntimeError("gh down")

    p._query_fn = boom
    assert await p.refresh_once(PAIRS) is False
    # Last-known value survives a failed refresh.
    assert p.cached("o/r", "br").number == 5


@pytest.mark.asyncio
async def test_refresh_empty_pairs_is_noop_success():
    p = PrStatusPoller(query_fn=_good_query)
    assert await p.refresh_once([]) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_pr_poller.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.github.pr_poller'`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/github/pr_poller.py
"""PrStatusPoller — background cache of GitHub PR status keyed by (repo, branch).

Mirrors the shape of server.usage.poller.UsagePoller: a configurable interval,
jitter to avoid lockstep bursts, adaptive backoff on failure, and an in-memory
cache the HTTP path reads. Tests inject ``query_fn`` so no real gh call is made.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Callable

from ccswitch import gitrepos

from .pr_status import PullRequestInfo, build_pr_query, parse_pr_response

log = logging.getLogger(__name__)


def _default_query(pairs: list[tuple[str, str]]) -> dict:
    query = build_pr_query(pairs)
    if not query:
        return {"data": {}}
    proc = gitrepos._run_gh(["api", "graphql", "-f", f"query={query}"])
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api graphql failed")
    return json.loads(proc.stdout or "{}")


class PrStatusPoller:
    def __init__(
        self,
        poll_interval: float = 75.0,
        query_fn: Callable[[list[tuple[str, str]]], dict] | None = None,
        *,
        jitter_frac: float = 0.1,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float | None = None,
        backoff_cap: float = 900.0,
    ) -> None:
        self._poll_interval = poll_interval
        self._query_fn = query_fn if query_fn is not None else _default_query
        self._jitter_frac = jitter_frac
        self._rng = rng
        self._clock = clock
        self._backoff_base = backoff_base if backoff_base is not None else poll_interval
        self._backoff_cap = backoff_cap
        self._fail_count = 0
        self._cooldown_until = 0.0
        self._cache: dict[tuple[str, str], PullRequestInfo] = {}
        self._pairs_provider: Callable[[], list[tuple[str, str]]] | None = None
        self._task: asyncio.Task | None = None

    def set_pairs_provider(self, fn: Callable[[], list[tuple[str, str]]]) -> None:
        self._pairs_provider = fn

    def cached(self, repo: str | None, branch: str | None) -> PullRequestInfo | None:
        if not repo or not branch:
            return None
        return self._cache.get((repo, branch))

    async def refresh_once(self, pairs: list[tuple[str, str]]) -> bool:
        if not pairs:
            return True
        try:
            response = await asyncio.to_thread(self._query_fn, pairs)
            fresh = parse_pr_response(response, pairs)
        except Exception:
            log.warning("pr_poller.refresh_failed", exc_info=True)
            self._fail_count += 1
            self._cooldown_until = self._clock() + min(
                self._backoff_base * (2 ** (self._fail_count - 1)), self._backoff_cap
            )
            return False
        self._fail_count = 0
        self._cooldown_until = 0.0
        # Rebuild cache entries for the pairs we just queried: drop stale keys
        # (PR closed/branch gone) and refresh present ones.
        for key in pairs:
            self._cache.pop(key, None)
        self._cache.update(fresh)
        return True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="pr-status-poller")

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
            pairs = self._pairs_provider() if self._pairs_provider else []
            await self.refresh_once(pairs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_pr_poller.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_poller.py server/tests/test_pr_poller.py
git commit -m "feat(github): PrStatusPoller background PR-status cache"
```

---

### Task 3: Persist the session worktree branch

**Files:**
- Modify: `server/db/models.py` (add `branch` column to `Session`)
- Modify: `server/sessions/types.py` (add `branch` to `SessionRecord` + `to_dict`/`from_dict`)
- Modify: `server/sessions/orchestrator.py:~325-352` (set branch on DbSession row + SessionRecord)
- Test: `server/tests/test_session_branch_persist.py`

**Interfaces:**
- Consumes: `WorktreeInfo.branch` (already exists on `worktrees.create_worktree` result).
- Produces: `SessionRecord.branch: str | None`; DB column `sessions.branch`; wire key already covered by Task 4.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_branch_persist.py
from server.sessions.types import SessionRecord


def test_session_record_roundtrips_branch():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )
    assert rec.branch == "ccswitch/main-abcd"
    assert rec.to_dict()["branch"] == "ccswitch/main-abcd"
    assert SessionRecord.from_dict(rec.to_dict()).branch == "ccswitch/main-abcd"


def test_session_record_branch_defaults_none():
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt",
    )
    assert rec.branch is None
    assert rec.to_dict()["branch"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_session_branch_persist.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'branch'`.

- [ ] **Step 3: Write minimal implementation**

In `server/db/models.py`, inside `class Session`, after the `repo` column (around line 44) add:

```python
    # The session's per-session worktree branch (e.g. ccswitch/main-abcd), used
    # to join the session to its GitHub PR by headRefName. NULL for detached/
    # scratch sessions. Nullable so init_db backfills it without a migration.
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
```

In `server/sessions/types.py`, add the field after `repo` in the dataclass:

```python
    branch: str | None = None        # per-session worktree branch (joins to PR headRefName)
```

Add `"branch": self.branch,` to the `to_dict()` return, and `branch=d.get("branch"),` to the `from_dict()` constructor call.

In `server/sessions/orchestrator.py`, in `create_session` capture the branch right after the worktree block resolves `cwd` (just after the `if cwd is None:` scratch block, before `if account is None:`), then thread it into both the DB row and the record:

```python
        # Branch this session's worktree is on (None for detached/scratch) — the
        # join key to its GitHub PR. Surfaced as a PR pill on the dashboard.
        branch_name = worktree_info.branch if worktree_info is not None else None
```

In the `DbSession(...)` constructor add `branch=branch_name,` (next to `repo=repo,`).
In the `SessionRecord(...)` constructor add `branch=branch_name,` (next to `repo=repo,`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_session_branch_persist.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the broader session suites to confirm no serde regression**

Run: `python -m pytest server/tests/test_session_record_serde.py server/tests/test_session_repo.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/sessions/orchestrator.py server/tests/test_session_branch_persist.py
git commit -m "feat(sessions): persist per-session worktree branch (PR join key)"
```

---

### Task 4: Wire the poller into the orchestrator + attach `pr` to the sessions API

**Files:**
- Modify: `server/sessions/orchestrator.py` (own `PrStatusPoller`; start/stop; `pr_for`; pairs provider)
- Modify: `server/sessions/facade.py` (expose `pr_for` on the adapter)
- Modify: `server/api/sessions.py` (`_record_to_dict` gains `pr`; route looks it up)
- Test: `server/tests/test_sessions_pr_field.py`

**Interfaces:**
- Consumes: `PrStatusPoller` (Task 2); `SessionRecord.branch` (Task 3).
- Produces: wire key `pr` on each session dict — `null` or `{number,url,state,checks,title}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sessions_pr_field.py
from server.api.sessions import _record_to_dict
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )


def test_record_to_dict_includes_pr_when_given():
    pr = PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")
    d = _record_to_dict(_rec(), repo="o/r", pr=pr)
    assert d["pr"] == {
        "number": 9, "url": "https://github.com/o/r/pull/9",
        "state": "open", "checks": "passing", "title": "t",
    }


def test_record_to_dict_pr_null_by_default():
    assert _record_to_dict(_rec(), repo="o/r")["pr"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest server/tests/test_sessions_pr_field.py -q`
Expected: FAIL — `TypeError: _record_to_dict() got an unexpected keyword argument 'pr'`.

- [ ] **Step 3: Write minimal implementation**

In `server/api/sessions.py`, change the `_record_to_dict` signature and body:

```python
def _record_to_dict(record, repo: Any = _UNSET, pr: Any = None) -> dict[str, Any]:
```
and add to the returned dict (next to `"repo": ...`):
```python
        "pr": pr.to_dict() if pr is not None else None,
```

Update the `list_sessions` route to look up each record's cached PR and pass it through. Replace the route body's return section with:

```python
    orch = _get_orchestrator(request)
    records = await orch.list_sessions()
    repos = await asyncio.gather(*(_resolve_repo(r) for r in records))
    pr_for = getattr(orch, "pr_for", None)
    prs = [
        (pr_for(r.repo, getattr(r, "branch", None)) if pr_for else None)
        for r in records
    ]
    return [
        _record_to_dict(r, repo=repo, pr=pr)
        for r, repo, pr in zip(records, repos, prs)
    ]
```

In `server/sessions/orchestrator.py` `__init__`, right after the `self._poller = UsagePoller(...)` block, add:

```python
        from server.github.pr_poller import PrStatusPoller
        self._pr_poller: PrStatusPoller = PrStatusPoller()
        self._pr_poller.set_pairs_provider(self._pr_pairs)
```

Add the provider + accessor methods on the orchestrator (near `list_sessions`):

```python
    def _pr_pairs(self) -> list[tuple[str, str]]:
        """Distinct (repo, branch) pairs for live sessions that have both."""
        seen: set[tuple[str, str]] = set()
        for r in self._records.values():
            branch = getattr(r, "branch", None)
            if r.repo and branch:
                seen.add((r.repo, branch))
        return list(seen)

    def pr_for(self, repo, branch):
        """Cached PullRequestInfo for a session's (repo, branch), or None."""
        return self._pr_poller.cached(repo, branch)
```

In the orchestrator's `start()` add `await self._pr_poller.start()` after `await self._poller.start()`; in `stop()` add `await self._pr_poller.stop()` after `await self._poller.stop()`.

In `server/sessions/facade.py`, add to `InProcessOrchestratorAdapter` (a sync passthrough — the cache read is sync):

```python
    def pr_for(self, repo, branch): return self._orch.pr_for(repo, branch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest server/tests/test_sessions_pr_field.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the sessions API + orchestrator suites**

Run: `python -m pytest server/tests/test_session_repo.py server/tests/test_orchestrator_facade.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/sessions/facade.py server/api/sessions.py server/tests/test_sessions_pr_field.py
git commit -m "feat(sessions): attach cached PR status to the sessions API"
```

---

### Task 5: Frontend types + PrPill component

**Files:**
- Modify: `web/src/api/types.ts` (add `PullRequestInfo`, extend `Session`)
- Create: `web/src/components/PrPill.tsx`
- Test: `web/src/components/__tests__/PrPill.test.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces: `interface PullRequestInfo`; `Session.pr?: PullRequestInfo | null`; `<PrPill pr={...} />`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/PrPill.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { PrPill } from '../PrPill'
import type { PullRequestInfo } from '@/api/types'

const base: PullRequestInfo = {
  number: 42, url: 'https://github.com/o/r/pull/42',
  state: 'open', checks: 'passing', title: 'Add thing',
}

describe('PrPill', () => {
  it('shows the PR number and links to the PR in a new tab', () => {
    render(<PrPill pr={base} />)
    const link = screen.getByRole('link', { name: /#42/ })
    expect(link).toHaveAttribute('href', base.url)
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('does not bubble clicks to an ancestor handler', () => {
    const onParent = vi.fn()
    render(<div onClick={onParent}><PrPill pr={base} /></div>)
    screen.getByRole('link', { name: /#42/ }).click()
    expect(onParent).not.toHaveBeenCalled()
  })

  it('renders a state-specific label for each state', () => {
    const states = ['open', 'draft', 'merged', 'closed'] as const
    for (const state of states) {
      const { unmount } = render(<PrPill pr={{ ...base, state }} />)
      expect(screen.getByRole('link')).toHaveAttribute('data-state', state)
      unmount()
    }
  })

  it('reflects the checks rollup in a checks indicator', () => {
    const { rerender } = render(<PrPill pr={{ ...base, checks: 'failing' }} />)
    expect(screen.getByRole('link')).toHaveAttribute('data-checks', 'failing')
    rerender(<PrPill pr={{ ...base, checks: 'pending' }} />)
    expect(screen.getByRole('link')).toHaveAttribute('data-checks', 'pending')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/PrPill.test.tsx`
Expected: FAIL — cannot resolve `../PrPill`.

- [ ] **Step 3: Write minimal implementation**

Append to `web/src/api/types.ts`:

```ts
export interface PullRequestInfo {
  number: number
  url: string
  state: 'open' | 'draft' | 'merged' | 'closed'
  checks: 'passing' | 'failing' | 'pending' | 'none'
  title: string
}
```
and add to `interface Session` (after `model?`):
```ts
  /** The PR opened from this session's branch, or null when there is none. */
  pr?: PullRequestInfo | null
```

```tsx
// web/src/components/PrPill.tsx
import { GitPullRequest, GitMerge, GitPullRequestClosed, Check, X, Dot } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PullRequestInfo } from '@/api/types'

// Color per PR state — mirrors GitHub's semantics (open green, draft gray,
// merged purple, closed red).
const STATE_STYLES: Record<PullRequestInfo['state'], string> = {
  open: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  merged: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  closed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
}

const STATE_ICON: Record<PullRequestInfo['state'], typeof GitPullRequest> = {
  open: GitPullRequest,
  draft: GitPullRequest,
  merged: GitMerge,
  closed: GitPullRequestClosed,
}

function ChecksIcon({ checks }: { checks: PullRequestInfo['checks'] }) {
  if (checks === 'passing') return <Check size={11} aria-hidden className="text-green-600 dark:text-green-400" />
  if (checks === 'failing') return <X size={11} aria-hidden className="text-red-600 dark:text-red-400" />
  if (checks === 'pending') return <Dot size={11} aria-hidden className="text-amber-500" />
  return null
}

export function PrPill({ pr }: { pr: PullRequestInfo }) {
  const StateIcon = STATE_ICON[pr.state]
  const checksLabel = pr.checks === 'none' ? '' : `, checks ${pr.checks}`
  return (
    <a
      href={pr.url}
      target="_blank"
      rel="noopener noreferrer"
      data-state={pr.state}
      data-checks={pr.checks}
      onClick={(e) => e.stopPropagation()}
      title={`${pr.title} — ${pr.state}${checksLabel}`}
      aria-label={`Pull request #${pr.number}, ${pr.state}${checksLabel}`}
      className={cn(
        'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        STATE_STYLES[pr.state],
      )}
    >
      <StateIcon size={11} aria-hidden />
      <span className="tabular-nums">#{pr.number}</span>
      <ChecksIcon checks={pr.checks} />
    </a>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/PrPill.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/components/PrPill.tsx web/src/components/__tests__/PrPill.test.tsx
git commit -m "feat(web): PrPill component + PullRequestInfo type"
```

---

### Task 6: Render PrPill on the session card

**Files:**
- Modify: `web/src/components/SessionCard.tsx` (render `<PrPill>` beside `<StatusPill>`)
- Test: `web/src/components/__tests__/SessionCardPr.test.tsx`

**Interfaces:**
- Consumes: `PrPill` (Task 5); `Session.pr` (Task 5).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/SessionCardPr.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const session: Session = {
  id: 's1', name: 'My session', accountName: 'acct', repo: 'o/r',
  status: 'in_progress', createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
}

const noop = () => {}
const props = { account: undefined, onOpen: noop, onRename: noop, onClose: noop, onDelete: noop }

describe('SessionCard PR pill', () => {
  it('shows the PR pill when the session has a PR', () => {
    render(<SessionCard session={{ ...session, pr: {
      number: 7, url: 'https://github.com/o/r/pull/7', state: 'open',
      checks: 'passing', title: 't',
    } }} {...props} />)
    expect(screen.getByRole('link', { name: /#7/ })).toBeInTheDocument()
  })

  it('shows no PR pill when the session has none', () => {
    render(<SessionCard session={session} {...props} />)
    expect(screen.queryByRole('link', { name: /#/ })).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/SessionCardPr.test.tsx`
Expected: FAIL — no PR link rendered.

- [ ] **Step 3: Write minimal implementation**

In `web/src/components/SessionCard.tsx`, import the pill at the top:

```tsx
import { PrPill } from './PrPill'
```
and render it immediately after `<StatusPill status={session.status} />` (around line 129):

```tsx
          {session.pr && <PrPill pr={session.pr} />}
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/SessionCardPr.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full web checks**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src/components/PrPill.tsx src/components/SessionCard.tsx`
Expected: all PASS / no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCardPr.test.tsx
git commit -m "feat(web): render PR pill on the session card"
```

---

## Final verification (after all tasks)

- [ ] Server: `python -m pytest server/tests/test_pr_status.py server/tests/test_pr_poller.py server/tests/test_session_branch_persist.py server/tests/test_sessions_pr_field.py -q` — all PASS.
- [ ] Server regression: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests/ -q` — green (or no new failures vs. baseline).
- [ ] Web: in `web/`, `npx vitest run && npx tsc --noEmit && npx eslint .` — green.
- [ ] Open a PR per the completion workflow; mark ready; arm `--squash --auto`.
