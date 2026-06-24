# Multiple PRs per Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track every PR a session has authored and surface them all on the session — a single inline pill for one PR, a count chip + popover for two or more.

**Architecture:** A new git helper derives a session's authored branches from its worktree reflog + live branch. The orchestrator maintains and persists that set per session (new `authored_branches` column), the PR poller queries the union of all sessions' branches, and a new `prs_for(session_id)` returns the deduped PR list (exposed over the daemon RPC for remote mode). The API emits `prs` instead of `pr`; the frontend renders a `PrList` (pill for 1, chip+popover for 2+).

**Tech Stack:** Python (FastAPI, SQLAlchemy/SQLite, pytest) · React + TypeScript + Tailwind + lucide-react (vitest) · git CLI via `ccswitch.worktrees`.

## Global Constraints

- **Branch discovery:** union of the worktree's reflog checkout targets + live current branch, filtering the session's scratch branch, `master`, `main`, `HEAD`, and bare commit SHAs. The GitHub PR query filters to branches that actually have a PR.
- **Persistence:** new `DbSession.authored_branches` column MUST be `JSON`, nullable, default `None` (CLAUDE.md: nullable so `init_db` backfills without a migration). `SessionRecord.authored_branches` defaults to `[]`.
- **Ordering / dedup:** `prs_for` dedupes by PR number and orders open/draft first, then merged/closed, newest `updatedAt` first (reuse `pr_status._STATE_RANK` + `_neg_iso`).
- **API shape:** `_record_to_dict` emits `"prs": [...]` and no longer emits `"pr"`. Front + back deploy together.
- **UI:** 0 PRs → render nothing; 1 → existing `<PrPill>`; 2+ → chip `🔀 {n} PRs · {m} 🚀` + popover of `<PrPill>` rows. Reuse `PrPill` — no parallel pill.
- **Remote parity:** anything the API calls on the orchestrator must exist on `RemoteOrchestrator` (remote mode is the live serving path).
- **Backend test env (worktree):** `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …`. For tests that hit the API/DB/auth, prefix `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1`.
- **Frontend test env:** run in `web/` with `./node_modules/.bin/vitest`; real type gate is `npx tsc -b --force`.

---

### Task 1: `worktrees.authored_branches` git helper

**Files:**
- Modify: `src/ccswitch/worktrees.py`
- Test: `src/ccswitch/tests/test_worktrees_authored.py` (create)

**Interfaces:**
- Consumes: existing `_run` and `current_branch` in `worktrees.py`.
- Produces: `authored_branches(cwd: str, scratch_branch: str | None = None) -> list[str]` — sorted, deduped list of feature branches the worktree has been on.

- [ ] **Step 1: Write the failing test**

Create `src/ccswitch/tests/test_worktrees_authored.py`:

```python
import subprocess
from ccswitch import worktrees


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    p = str(path)
    _git(p, "init", "-q", "-b", "master")
    _git(p, "config", "user.email", "t@t.t")
    _git(p, "config", "user.name", "t")
    (path / "f").write_text("x")
    _git(p, "add", ".")
    _git(p, "commit", "-q", "-m", "init")
    return p


def test_collects_feature_branches_filters_scratch_and_default(tmp_path):
    p = _init_repo(tmp_path / "repo")
    # Simulate a session: scratch branch, then two feature branches.
    _git(p, "checkout", "-q", "-b", "ccswitch/master-abcd1234")
    _git(p, "checkout", "-q", "-b", "feat/one")
    _git(p, "checkout", "-q", "-b", "feat/two")
    got = worktrees.authored_branches(p, scratch_branch="ccswitch/master-abcd1234")
    assert got == ["feat/one", "feat/two"]  # sorted; scratch + master filtered


def test_non_git_dir_returns_empty(tmp_path):
    assert worktrees.authored_branches(str(tmp_path)) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest src/ccswitch/tests/test_worktrees_authored.py -v`
Expected: FAIL with `AttributeError: module 'ccswitch.worktrees' has no attribute 'authored_branches'`.

- [ ] **Step 3: Implement the helper**

In `src/ccswitch/worktrees.py`, add `import re` at the top if not present, and add after `current_branch`:

```python
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REFLOG_CHECKOUT_RE = re.compile(r"^checkout: moving from (\S+) to (\S+)$")
# Branches that are never a session's authored PR branch.
_NON_AUTHORED = {"master", "main", "HEAD"}


def authored_branches(cwd: str, scratch_branch: str | None = None) -> list[str]:
    """Sorted, deduped feature branches this worktree has checked out.

    Unions the live current branch with the worktree's reflog checkout history
    (``checkout: moving from A to B``). Filters the session's scratch branch, the
    repo default (master/main), HEAD, and bare commit SHAs (detached checkouts).
    Returns [] for a non-git dir. The caller's PR query keeps only branches that
    actually have a PR, so listing a no-PR feature branch here is harmless.
    """
    branches: set[str] = set()
    cur = current_branch(cwd)
    if cur:
        branches.add(cur)
    r = _run(["git", "-C", cwd, "reflog", "show", "--format=%gs", "HEAD"])
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            m = _REFLOG_CHECKOUT_RE.match(line.strip())
            if m:
                branches.add(m.group(1))
                branches.add(m.group(2))
    skip = _NON_AUTHORED | {scratch_branch}
    return sorted(
        b for b in branches
        if b and b not in skip and not _SHA_RE.match(b)
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest src/ccswitch/tests/test_worktrees_authored.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktrees.py src/ccswitch/tests/test_worktrees_authored.py
git commit -m "feat(worktrees): authored_branches — feature branches from reflog + live HEAD"
```

---

### Task 2: persist `authored_branches` (DB column + record field)

**Files:**
- Modify: `server/db/models.py`
- Modify: `server/sessions/types.py`
- Test: `server/tests/test_session_record_authored.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `DbSession.authored_branches` (JSON, nullable); `SessionRecord.authored_branches: list[str] = []` carried through `to_dict`/`from_dict` under key `"authoredBranches"`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_session_record_authored.py`:

```python
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_authored_branches_defaults_empty():
    assert _rec().authored_branches == []


def test_authored_branches_roundtrips_through_dict():
    rec = _rec(authored_branches=["feat/one", "feat/two"])
    again = SessionRecord.from_dict(rec.to_dict())
    assert again.authored_branches == ["feat/one", "feat/two"]


def test_from_dict_tolerates_missing_authored_branches():
    d = _rec().to_dict()
    del d["authoredBranches"]
    assert SessionRecord.from_dict(d).authored_branches == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_session_record_authored.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'authored_branches'`.

- [ ] **Step 3: Add the DB column**

In `server/db/models.py`, inside the `Session` model, add after the `branch` column (around line 48):

```python
    # Feature branches this session has authored (for multi-PR badges). JSON list,
    # nullable so init_db backfills it on existing rows without a migration.
    authored_branches: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
```

(`JSON` is already imported in this file — it's used by `tool_allowlist`.)

- [ ] **Step 4: Add the record field + serialization**

In `server/sessions/types.py`, add the field to the dataclass after `active_goal` (line 36):

```python
    active_goal: str | None = None    # active /goal condition; None = no goal
    authored_branches: list[str] = field(default_factory=list)  # feature branches → multi-PR badges
```

Add the import at the top (line 8 currently `from dataclasses import dataclass`):

```python
from dataclasses import dataclass, field
```

In `to_dict`, add the key (after `"activeGoal"`):

```python
            "activeGoal": self.active_goal,
            "authoredBranches": list(self.authored_branches),
```

In `from_dict`, add the kwarg (after `active_goal=d.get("activeGoal")`):

```python
            active_goal=d.get("activeGoal"),
            authored_branches=list(d.get("authoredBranches") or []),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_session_record_authored.py -v`
Expected: PASS (all three).

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/tests/test_session_record_authored.py
git commit -m "feat(sessions): persist authored_branches on the session record + DB"
```

---

### Task 3: orchestrator — maintain set, query union, `prs_for`

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_orchestrator_prs_for.py` (create)

**Interfaces:**
- Consumes: `worktrees.authored_branches(cwd, scratch_branch)` (Task 1); `SessionRecord.authored_branches` (Task 2); existing `self._pr_poller`, `self._records`, `worktrees`, `db_session`, `DbSession`.
- Produces:
  - `prs_for(session_id: str) -> list[PullRequestInfo]` — deduped, ordered PR list for the session.
  - `refresh_authored_branches()` (async) — unions reflog+live into each record's `authored_branches` and persists changes; seeds `_pr_pairs`.
  - `_pr_pairs()` now unions over every record's `authored_branches`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_prs_for.py`:

```python
from unittest import mock

from server.github.pr_status import PullRequestInfo
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch():
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)


def _pr(n, state="open"):
    return PullRequestInfo(n, f"https://github.com/o/r/pull/{n}", state, "passing", f"PR {n}")


def test_prs_for_gathers_dedups_and_orders():
    orch = _orch()
    orch._records["s1"] = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a", "feat/b", "feat/c"],
    )
    cache = {
        ("o/r", "feat/a"): _pr(2, "merged"),
        ("o/r", "feat/b"): _pr(1, "open"),
        ("o/r", "feat/c"): _pr(1, "open"),   # duplicate PR #1 via another branch
    }
    orch._pr_poller.cached = lambda repo, br: cache.get((repo, br))
    out = orch.prs_for("s1")
    assert [p.number for p in out] == [1, 2]  # open before merged; #1 deduped


def test_prs_for_empty_when_no_repo_or_cache():
    orch = _orch()
    orch._records["s1"] = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp", repo=None, authored_branches=["feat/a"],
    )
    assert orch.prs_for("s1") == []


def test_pr_pairs_unions_authored_branches():
    orch = _orch()
    orch._records["s1"] = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a", "feat/b"],
    )
    assert set(orch._pr_pairs()) == {("o/r", "feat/a"), ("o/r", "feat/b")}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_prs_for.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'prs_for'` and the `_pr_pairs` union test fails (still keyed on `r.branch`).

- [ ] **Step 3: Rewrite `_pr_pairs` and add `prs_for`**

In `server/sessions/orchestrator.py`, replace `_pr_pairs` and `pr_for` (lines 717-728) with:

```python
    def _pr_pairs(self) -> list[tuple[str, str]]:
        """Distinct (repo, branch) pairs across every session's authored branches."""
        seen: set[tuple[str, str]] = set()
        for r in self._records.values():
            if not r.repo:
                continue
            for br in getattr(r, "authored_branches", None) or []:
                seen.add((r.repo, br))
        return list(seen)

    def pr_for(self, repo, branch):
        """Cached PullRequestInfo for a (repo, branch), or None."""
        return self._pr_poller.cached(repo, branch)

    def prs_for(self, session_id: str) -> list["PullRequestInfo"]:
        """Deduped, ordered PR list for a session's authored branches.

        Open/draft first, then merged/closed, newest updatedAt first — matches the
        single-PR ranking. Deduped by PR number (the same PR can surface via more
        than one branch ref).
        """
        from server.github.pr_status import _STATE_RANK
        rec = self._records.get(session_id)
        if rec is None or not rec.repo:
            return []
        by_number: dict[int, "PullRequestInfo"] = {}
        for br in getattr(rec, "authored_branches", None) or []:
            info = self._pr_poller.cached(rec.repo, br)
            if info is not None:
                by_number.setdefault(info.number, info)
        return sorted(
            by_number.values(),
            key=lambda p: (_STATE_RANK.get(p.state, 99), -p.number),
        )
```

Add the import near the top of `orchestrator.py` (with the other `server.github` / typing imports), if not already present:

```python
from server.github.pr_status import PullRequestInfo
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_prs_for.py -v`
Expected: PASS (all three). (Ordering: open #1 then merged #2; `-p.number` breaks ties newest-first without needing `updatedAt`, which `PullRequestInfo` doesn't carry.)

- [ ] **Step 5: Add `refresh_authored_branches` + seed it on a monitor**

Still in `server/sessions/orchestrator.py`, add the maintenance method (place it next to `prs_for`):

```python
    async def refresh_authored_branches(self) -> None:
        """Union each live session's reflog+live branches into its record, persist changes.

        Runs git off the event loop. Best-effort: a session whose cwd is gone or not a
        git repo simply keeps its existing set. Persists only changed rows so the set
        survives reflog GC and restarts.
        """
        changed: dict[str, list[str]] = {}
        for rec in list(self._records.values()):
            if not rec.cwd:
                continue
            fresh = await asyncio.to_thread(
                worktrees.authored_branches, rec.cwd, getattr(rec, "branch", None)
            )
            if not fresh:
                continue
            merged = sorted(set(getattr(rec, "authored_branches", None) or []) | set(fresh))
            if merged != (rec.authored_branches or []):
                rec.authored_branches = merged
                changed[rec.id] = merged
        if changed:
            from sqlalchemy import update as sa_update
            async with db_session() as db:
                for sid, brs in changed.items():
                    await db.execute(
                        sa_update(DbSession).where(DbSession.id == sid).values(authored_branches=brs)
                    )

    async def _pr_branch_monitor(self) -> None:
        """Periodically refresh authored branches so new feature branches get badges."""
        while True:
            try:
                await self.refresh_authored_branches()
            except Exception:
                log.warning("pr_branch_monitor.refresh_failed", exc_info=True)
            await asyncio.sleep(60)
```

In `start()` (after line 205 `await self._pr_poller.start()`), add:

```python
        self._pr_branch_task = asyncio.create_task(
            self._pr_branch_monitor(), name="pr-branch-monitor"
        )
```

In `__init__` (near the other task handles, around line 161-162) initialize the handle:

```python
        self._pr_branch_task: asyncio.Task | None = None
```

In `stop()` (near `await self._pr_poller.stop()`, line 264), cancel it:

```python
        if self._pr_branch_task is not None:
            self._pr_branch_task.cancel()
```

Seed the set on rehydrate so restarts don't start empty: in `rehydrate` where the `SessionRecord(...)` is built (around line 695, the `branch=branch,` line), add:

```python
                branch=branch,
                authored_branches=list(getattr(row, "authored_branches", None) or []),
```

- [ ] **Step 6: Write the refresh test**

Append to `server/tests/test_orchestrator_prs_for.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_refresh_authored_branches_unions_and_keeps_existing(monkeypatch):
    orch = _orch()
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/master-s1",
        authored_branches=["feat/old"],
    )
    orch._records["s1"] = rec
    monkeypatch.setattr(
        "server.sessions.orchestrator.worktrees.authored_branches",
        lambda cwd, scratch: ["feat/new"],
    )
    # No DB write should be required to update the in-memory set; stub db_session
    # so the persistence branch is exercised without a real engine.
    import contextlib

    class _FakeDb:
        async def execute(self, *a, **k):
            return None

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _FakeDb()

    monkeypatch.setattr("server.sessions.orchestrator.db_session", _fake_session)
    await orch.refresh_authored_branches()
    assert rec.authored_branches == ["feat/new", "feat/old"]  # union, sorted
```

- [ ] **Step 7: Run the full task test file**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_prs_for.py -v`
Expected: PASS (all four).

- [ ] **Step 8: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_prs_for.py
git commit -m "feat(sessions): maintain authored-branch set and resolve prs_for(session)"
```

---

### Task 4: daemon RPC `prs_for` + `RemoteOrchestrator.prs_for`

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py`
- Modify: `server/sessions/remote.py`
- Test: `server/tests/test_remote_orchestrator.py` (extend)

**Interfaces:**
- Consumes: `orch.prs_for(session_id)` (Task 3); `PullRequestInfo.from_dict` (already present).
- Produces: daemon RPC `prs_for` returning `{"prs": [info.to_dict(), ...]}`; `RemoteOrchestrator.prs_for(session_id) -> list[PullRequestInfo]` (async).

- [ ] **Step 1: Write the failing test**

In `server/tests/test_remote_orchestrator.py`, add after `test_pr_for_over_uds`:

```python
@pytest.mark.asyncio
async def test_prs_for_over_uds():
    """The edge resolves a session's full PR list through the daemon."""
    from server.github.pr_status import PullRequestInfo
    from server.sessions.types import SessionRecord
    app, orch = _build_app()
    orch._records["s1"] = SessionRecord(
        id="s1", name="S1", account="acct", claude_session_id="cs", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a", "feat/b"],
    )
    cache = {
        ("o/r", "feat/a"): PullRequestInfo(1, "u1", "open", "passing", "A"),
        ("o/r", "feat/b"): PullRequestInfo(2, "u2", "merged", "passing", "B"),
    }
    orch._pr_poller.cached = lambda repo, br: cache.get((repo, br))
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            got = await ro.prs_for("s1")
            empty = await ro.prs_for("nope")
        finally:
            await ro.aclose()
    assert [p.number for p in got] == [1, 2]
    assert empty == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_orchestrator.py::test_prs_for_over_uds -v`
Expected: FAIL — `RemoteOrchestrator` has no `prs_for` (AttributeError), or the RPC method is unknown.

- [ ] **Step 3: Register the daemon RPC**

In `server/ipc/orchestrator_rpc.py`, add after the `pr_for` handler (around line 56):

```python
    async def prs_for(params: dict) -> dict:
        infos = orch.prs_for(params["session_id"])
        return {"prs": [i.to_dict() for i in infos]}
```

And register it after `register_rpc(app, "pr_for", pr_for)`:

```python
    register_rpc(app, "prs_for", prs_for)
```

- [ ] **Step 4: Add `RemoteOrchestrator.prs_for`**

In `server/sessions/remote.py`, add after the `pr_for` method:

```python
    async def prs_for(self, session_id) -> "list[PullRequestInfo]":
        """Resolve a session's full PR list via the daemon (empty if none)."""
        r = await self._client.call("prs_for", session_id=session_id)
        return [PullRequestInfo.from_dict(d) for d in (r.get("prs") or [])]
```

(`PullRequestInfo` is already imported in `remote.py`.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_orchestrator.py -v`
Expected: PASS (new test + existing ones).

- [ ] **Step 6: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/sessions/remote.py server/tests/test_remote_orchestrator.py
git commit -m "feat(remote): expose prs_for over the daemon RPC"
```

---

### Task 5: API — emit `prs`

**Files:**
- Modify: `server/api/sessions.py`
- Test: `server/tests/test_sessions_pr_field.py` (rewrite the PR assertions)

**Interfaces:**
- Consumes: `orch.prs_for(session_id)` (Tasks 3/4).
- Produces: `_resolve_prs(orch, record) -> list[PullRequestInfo]`; `_record_to_dict(..., prs=[...])` emits `"prs": [...]` (no `"pr"`).

- [ ] **Step 1: Rewrite the field tests**

Replace the body of `server/tests/test_sessions_pr_field.py` with:

```python
import pytest

from server.api.sessions import _record_to_dict, _resolve_prs
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )


def test_record_to_dict_includes_prs_when_given():
    prs = [PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")]
    d = _record_to_dict(_rec(), repo="o/r", prs=prs)
    assert d["prs"] == [{
        "number": 9, "url": "https://github.com/o/r/pull/9",
        "state": "open", "checks": "passing", "title": "t", "deployed": False,
    }]
    assert "pr" not in d


def test_record_to_dict_prs_empty_by_default():
    assert _record_to_dict(_rec(), repo="o/r")["prs"] == []


class _SyncOrch:
    def prs_for(self, sid):
        return [PullRequestInfo(1, "u", "open", "none", "t")]


class _AsyncOrch:
    async def prs_for(self, sid):
        return [PullRequestInfo(2, "u", "merged", "passing", "t")]


class _NoOrch:
    pass


@pytest.mark.asyncio
async def test_resolve_prs_handles_sync_async_and_missing():
    rec = _rec()
    assert [p.number for p in await _resolve_prs(_SyncOrch(), rec)] == [1]
    assert [p.number for p in await _resolve_prs(_AsyncOrch(), rec)] == [2]
    assert await _resolve_prs(_NoOrch(), rec) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_sessions_pr_field.py -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_prs'` / `_record_to_dict` has no `prs` kwarg.

- [ ] **Step 3: Add `_resolve_prs` and update `_record_to_dict`**

In `server/api/sessions.py`, replace `_resolve_pr` (lines 157-170) with:

```python
async def _resolve_prs(orch, record):
    """Resolve a record's PR list via ``orch.prs_for``, sync (in-process) or async (remote).

    Returns ``list[PullRequestInfo]``; an orchestrator with no ``prs_for`` (older shim)
    yields ``[]`` rather than raising.
    """
    fn = getattr(orch, "prs_for", None)
    if fn is None:
        return []
    res = fn(record.id)
    if inspect.isawaitable(res):
        res = await res
    return res or []
```

Change `_record_to_dict`'s signature (line 84-86) — replace `pr: Any = None` with `prs: list | None = None`:

```python
def _record_to_dict(
    record, repo: Any = _UNSET, suggestions: list[str] | None = None, prs: list | None = None
) -> dict[str, Any]:
```

Replace the `"pr": ...` line (line 123) with:

```python
        "prs": [p.to_dict() for p in (prs or [])],
```

Update the docstring's `pr` paragraph (lines 97-98) to read:

```python
    ``prs`` is the list of cached PullRequestInfo for the session's authored
    branches (see :func:`_resolve_prs`).  Serialised to a list of dicts on the wire.
```

In the `list_sessions` handler (lines 194-198), replace the `prs`/return block with:

```python
    prs = await asyncio.gather(*(_resolve_prs(orch, r) for r in records))
    return [
        _record_to_dict(r, repo=repo, suggestions=s, prs=pr_list)
        for r, repo, s, pr_list in zip(records, repos, suggs, prs)
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_sessions_pr_field.py -v`
Expected: PASS (all).

- [ ] **Step 5: Run the broader API/session suite for regressions**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_sessions_pr_field.py server/tests/test_api_rehydrate_list.py -v`
Expected: PASS. (`test_remote_mode_wiring::test_flag_unset_is_in_process` is a known pre-existing environmental failure — do not run it here.)

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_pr_field.py
git commit -m "feat(api): emit prs list per session (replaces single pr)"
```

---

### Task 6: frontend — `prs` type + `PrList` (chip + popover)

**Files:**
- Modify: `web/src/api/types.ts:69`
- Create: `web/src/components/PrList.tsx`
- Modify: `web/src/components/SessionCard.tsx:162`
- Test: `web/src/components/__tests__/PrList.test.tsx` (create)

**Interfaces:**
- Consumes: API `Session.prs: PullRequestInfo[]`; existing `PrPill`.
- Produces: `<PrList prs={…} />`.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/PrList.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { PrList } from '../PrList'
import type { PullRequestInfo } from '@/api/types'

const pr = (number: number, over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
  number, url: `https://github.com/o/r/pull/${number}`,
  state: 'open', checks: 'passing', title: `PR ${number}`, deployed: false, ...over,
})

describe('PrList', () => {
  it('renders nothing for an empty list', () => {
    const { container } = render(<PrList prs={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a single pill for one PR (no chip)', () => {
    render(<PrList prs={[pr(42)]} />)
    expect(screen.getByRole('link', { name: /#42/ })).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders a count chip with deployed tally for 2+ PRs', () => {
    render(<PrList prs={[pr(1, { state: 'merged', deployed: true }), pr(2)]} />)
    const chip = screen.getByRole('button', { name: /2 PRs/i })
    expect(chip.textContent).toMatch(/2 PRs/)
    expect(chip.textContent).toMatch(/1/)  // one deployed
  })

  it('reveals all PRs in the popover when the chip is clicked', () => {
    render(<PrList prs={[pr(1, { state: 'merged' }), pr(2)]} />)
    screen.getByRole('button', { name: /2 PRs/i }).click()
    expect(screen.getByRole('link', { name: /#1/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /#2/ })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/PrList.test.tsx`
Expected: FAIL — cannot resolve `../PrList`.

- [ ] **Step 3: Add the `prs` type**

In `web/src/api/types.ts`, replace line 69 (`pr?: PullRequestInfo | null`) with:

```ts
  prs: PullRequestInfo[]
```

- [ ] **Step 4: Create `PrList`**

Create `web/src/components/PrList.tsx`:

```tsx
import { useState } from 'react'
import { GitPullRequest, Rocket } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PullRequestInfo } from '@/api/types'
import { PrPill } from './PrPill'

export function PrList({ prs }: { prs: PullRequestInfo[] }) {
  const [open, setOpen] = useState(false)
  if (prs.length === 0) return null
  if (prs.length === 1) return <PrPill pr={prs[0]} />

  const deployed = prs.filter((p) => p.deployed).length
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
        aria-expanded={open}
        aria-label={`${prs.length} PRs${deployed ? `, ${deployed} deployed` : ''}`}
        className={cn(
          'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
          'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        )}
      >
        <GitPullRequest size={11} aria-hidden />
        <span className="tabular-nums">{prs.length} PRs</span>
        {deployed > 0 && (
          <span className="inline-flex items-center gap-0.5 text-emerald-600 dark:text-emerald-400">
            <Rocket size={11} aria-hidden /><span className="tabular-nums">{deployed}</span>
          </span>
        )}
      </button>
      {open && (
        <div
          className={cn(
            'absolute z-10 mt-1 left-0 flex flex-col gap-1 p-2 rounded-md',
            'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-lg',
          )}
          onClick={(e) => e.stopPropagation()}
        >
          {prs.map((p) => <PrPill key={p.number} pr={p} />)}
        </div>
      )}
    </span>
  )
}
```

- [ ] **Step 5: Swap the SessionCard usage**

In `web/src/components/SessionCard.tsx`: change the import (line 8) from `import { PrPill } from './PrPill'` to:

```tsx
import { PrList } from './PrList'
```

And replace line 162 (`{session.pr && <PrPill pr={session.pr} />}`) with:

```tsx
          <PrList prs={session.prs} />
```

- [ ] **Step 6: Run the test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/PrList.test.tsx`
Expected: PASS (all four).

- [ ] **Step 7: Typecheck + check for other `session.pr` consumers**

Run (in `web/`): `npx tsc -b --force`
Expected: no errors. If a test fixture or component still references `session.pr` or builds a `Session` without `prs`, fix it (add `prs: []` to the fixture, or swap `.pr` for `.prs`). Re-run until clean. Also run the SessionCard test:

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/SessionCardPr.test.tsx`
Expected: PASS (update its fixture from `pr:` to `prs: [...]` if it fails).

- [ ] **Step 8: Commit**

```bash
git add web/src/api/types.ts web/src/components/PrList.tsx web/src/components/SessionCard.tsx web/src/components/__tests__/PrList.test.tsx web/src/components/__tests__/SessionCardPr.test.tsx
git commit -m "feat(web): PrList — single pill for one PR, count chip + popover for many"
```

---

## Manual verification (after all tasks land + deploy)

1. A session that authored 2+ PRs (e.g. one on `feat/x` and `feat/y`) shows a `🔀 N PRs` chip; clicking it lists each PR with state + deployed rocket.
2. A session on a single PR branch still shows the plain pill.
3. A session on only its scratch branch shows no badge.
4. `curl -H "Tailscale-User-Login: <allowed>" http://<host>:8787/api/sessions` returns `prs: [...]` per session.
