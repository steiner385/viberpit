# Issue #591 — Orphaned Worktree Janitor: Implementation Plan

**Date:** 2026-07-16  
**Issue:** steiner385/ccswitch#591  
**Trigger:** 2026-07-16 inode-exhaustion incident — 292 KinDash worktrees, 3 live sessions, ~115 dirs with no DB row.

> **For agentic workers:** implement task-by-task using `superpowers:subagent-driven-development`.
> Steps use checkbox (`- [ ]`) syntax for tracking.

---

## 1. Why Orphans Accumulate Silently

`_worktrees` (the orchestrator's in-memory map of `session_id → WorktreeInfo`) is only
populated for sessions that have **live drivers**. A session rehydrated on daemon restart
appears in `_records` but NOT in `_worktrees` — no driver was rebuilt, so the slot was never
re-populated. This means "is this path live?" cannot be answered by consulting `_worktrees`
alone; the check must union `_records` (for rehydrated idle sessions) and `_worktrees` (for
newly-spawned sessions not yet persisted, or sessions whose driver is still active).

`remove_worktree()` in `src/ccswitch/worktrees.py` already handles the safe-removal
protocol — unlock, scoped `git worktree remove`, child-branch cleanup — and never destroys
uncommitted work. It also never runs a repo-global `git worktree prune` (issue #177), which
previously reaped a *sibling* session's live worktree. The janitor's job is simply to call
`remove_worktree()` at the right time, with the right liveness filter.

---

## 2. Root Cause Analysis

### Three independent orphan sources

**Source 1 — Spawn crash after `git worktree add`, before DB row**

`create_worktree()` (in `src/ccswitch/worktrees.py`) succeeds and the dir is created. Then
`create_session()` in the orchestrator raises (bad branch name, fetch failure resolved late,
SQLAlchemy error on `Session` commit, etc.) before the row lands in the DB. The worktree dir
exists on disk with no session row anywhere — no cleanup path will ever reach it.

**Source 2 — `archive_session` `skipped:nospace` / exception**

`archive_session()` calls `move_into_archive()` from `server/sessions/archive_move.py`.
That function performs a size check before a cross-device copy; if free space is insufficient
it returns `"skipped:nospace"` without removing the source dir. The session is already
marked `status='archived'` in the DB (popped from `_records`) before the move, so the
worktree dir is abandoned at its original path with no retry mechanism.

The same happens on any uncaught exception in the move logic — the DB row is already
`archived`, the dir is not.

**Source 3 — Daemon killed mid-teardown**

`close_session()` and `archive_session()` both clean up the worktree dir near the END of
their flows. A SIGKILL mid-flight (deploy restart, OOM, watchdog) leaves the dir in place.
The next boot's `rehydrate()` rebuilds `_records` from DB rows, but `_worktrees` is NOT
reconstructed — only live-driver sessions populate it — so a killed session's worktree has
no representation in memory and no cleanup trigger.

### Why the existing sweeps don't cover this

- `_stuck_session_sweep()` — retries `_rotate_away` for `needs_attention` sessions; it only
  touches sessions still in `_records`. Orphan dirs by definition have no `_records` entry.
- `close_session()` / `archive_session()` — cleanup happens inline; a crash before that
  point means cleanup never runs.
- `git worktree prune` is deliberately banned (issue #177) as a global operation.

---

## 3. Step-by-Step Implementation Plan

### Step 1 — Create `src/ccswitch/worktrees_janitor.py`

- [ ] New pure module; no asyncio, no orchestrator imports. All blocking git calls via
  `subprocess.run` (the same `_run` pattern as `worktrees.py`).

**Public API:**

```python
def sweep_orphaned_worktrees(
    repo_root: str,
    live_cwds: frozenset[str],   # absolute paths of all live session cwds
    now: float | None = None,    # injectable for testing
    min_age: float = 86400.0,    # 24 h default
) -> dict[str, int]:
    """Walk <repo_root>/.ccswitch/worktrees/<repo>/<short-id>/ and remove
    dirs that are orphaned, old, clean, and fully pushed.

    Returns count dict with keys:
      scanned, removed, kept_live, kept_young, kept_dirty, kept_unpushed, kept_error
    """
```

**Internal helpers:**

```python
def _candidate_worktree_dirs(repo_root: str) -> Iterator[Path]:
    """Yield every <repo_root>/.ccswitch/worktrees/<repo>/<short-id>/ dir."""
    base = Path(repo_root) / ".ccswitch" / "worktrees"
    if not base.is_dir():
        return
    for repo_dir in base.iterdir():
        if repo_dir.is_dir():
            for wt_dir in repo_dir.iterdir():
                if wt_dir.is_dir():
                    yield wt_dir

def _is_clean(wt: str) -> bool:
    """True iff git status --porcelain returns no output."""
    r = _run(["git", "-C", wt, "status", "--porcelain"])
    return r.returncode == 0 and r.stdout.strip() == ""

def _upstream_gone(wt: str) -> bool:
    """True iff the current branch's remote tracking ref has been deleted.

    git branch -vv shows [gone] when the upstream ref was deleted — this is
    the squash-merge signature: the branch was merged, GitHub deleted it on
    the remote, but the local child branch still exists.
    """
    r = _run(["git", "-C", wt, "branch", "-vv"])
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if line.startswith("* ") and "[gone]" in line:
            return True
    return False

def _is_fully_pushed(wt: str) -> bool:
    """True iff all HEAD commits have been pushed to a remote, OR upstream is gone.

    Two cases:
    1. Upstream [gone]: squash-merge landed, remote branch deleted. Commits are
       in the remote default branch, not reachable via --remotes in the child
       branch's own refspec — but the work is safely merged, so we can reclaim.
    2. rev-list HEAD --not --remotes empty: all commits reachable from any remote.

    Guard: a repo with no remotes configured makes --remotes return nothing, which
    would make rev-list vacuously empty. Check for remotes first.
    """
    if _upstream_gone(wt):
        return True
    if _run(["git", "-C", wt, "remote"]).stdout.strip() == "":
        return False          # no remotes → unverifiable → keep
    r = _run(["git", "-C", wt, "rev-list", "HEAD", "--not", "--remotes"])
    if r.returncode != 0:
        return False          # git error → unverifiable → keep
    return r.stdout.strip() == ""
```

**Decision matrix inside `sweep_orphaned_worktrees`:**

| Check | Fail path | Log level |
|---|---|---|
| `abspath(wt)` not in `live_cwds` | `kept_live += 1; continue` | DEBUG |
| `now - mtime >= min_age` | `kept_young += 1; continue` | DEBUG |
| `_is_clean(wt)` | `kept_dirty += 1; continue` | INFO (operator-visible) |
| `_is_fully_pushed(wt)` | `kept_unpushed += 1; continue` | INFO (operator-visible) |
| `remove_worktree(wt)` returns True | `kept_error += 1` on False | WARNING on False |

Each dir is wrapped in `try/except Exception` — a single broken dir must not abort the sweep.

---

### Step 2 — Wire the janitor into `server/sessions/orchestrator.py`

- [ ] Add `self._janitor_task: asyncio.Task | None = None` in `__init__` near the other
  task handles (~line 473, after `self._sweep_task`).

- [ ] Add `"_janitor_task"` to the `stop()` cancel-and-await tuple (~line 646):
  ```python
  for attr in ("...", "_cicd_task", "_archaudit_task", "_sweep_task",
               "_janitor_task", "_drain_timer"):
  ```

- [ ] Wire in `start()` after the `_sweep_task` block (~line 594):
  ```python
  # Orphan-worktree janitor: fires immediately on startup to clear accumulated
  # orphans, then every 24 h. Uses scoped git worktree remove only — never a
  # repo-global prune (issue #177). Runs in a thread (blocking git calls).
  self._janitor_task = asyncio.create_task(
      self._worktree_janitor_loop(interval=86400.0),
      name="worktree-janitor-loop",
  )
  ```

- [ ] Add loop method:
  ```python
  async def _worktree_janitor_loop(self, interval: float) -> None:
      while True:
          if not self._shutting_down:
              try:
                  await asyncio.to_thread(self._janitor_sweep_once)
              except Exception:
                  log.exception("worktree-janitor: sweep failed")
          await asyncio.sleep(interval)
  ```
  No leading `sleep` — the loop fires immediately on first iteration so startup catches
  orphans from the previous crash/incident.

- [ ] Add the sweep method (synchronous — runs inside `asyncio.to_thread`):
  ```python
  def _janitor_sweep_once(self) -> None:
      import time as _time
      from ccswitch.worktrees_janitor import sweep_orphaned_worktrees
      live_cwds: frozenset[str] = frozenset(
          os.path.abspath(rec.cwd)
          for rec in self._records.values()
          if rec.cwd
      ) | frozenset(
          os.path.abspath(wt.path)
          for wt in self._worktrees.values()
      )
      counts = sweep_orphaned_worktrees(self._repo_root, live_cwds, now=_time.time())
      log.info(
          "worktree-janitor: scanned=%d removed=%d kept_live=%d "
          "kept_young=%d kept_dirty=%d kept_unpushed=%d kept_error=%d",
          counts["scanned"], counts["removed"], counts["kept_live"],
          counts["kept_young"], counts["kept_dirty"],
          counts["kept_unpushed"], counts["kept_error"],
      )
  ```

---

### Step 3 — Tests

- [ ] **`server/tests/test_worktree_janitor.py`** — unit tests for the pure sweep logic.
  All tests use `tmp_path` for real dirs and patch subprocess calls; no real git repos.

| Test name | What it proves |
|---|---|
| `test_skips_live_cwd` | Path in `live_cwds` → `kept_live=1`, no git calls made |
| `test_skips_young_dir` | `mtime` < 24 h ago → `kept_young=1` |
| `test_keeps_dirty` | `git status` returns `M file.py` → `kept_dirty=1`, logged INFO |
| `test_keeps_unpushed` | `rev-list` returns `abc123` → `kept_unpushed=1`, logged INFO |
| `test_removes_clean_pushed` | Clean + rev-list empty + old → `removed=1`, `remove_worktree` called |
| `test_removes_upstream_gone` | `branch -vv` shows `[gone]` + clean + old → `removed=1` |
| `test_no_worktrees_dir` | `.ccswitch/worktrees/` absent → `scanned=0`, no crash |
| `test_no_remotes_kept` | `git remote` returns empty → treated as unverifiable → `kept_unpushed=1` |
| `test_per_dir_error_continues` | One dir raises in `remove_worktree` → `kept_error=1`, others processed |

- [ ] **Orchestrator integration tests** — add to `server/tests/test_stuck_session_sweep.py`
  (same pattern) or a new `test_worktree_janitor_orch.py`:

| Test name | What it proves |
|---|---|
| `test_janitor_loop_skips_during_shutdown` | `_shutting_down=True` → `_janitor_sweep_once` never called |
| `test_janitor_sweep_once_collects_records_and_worktrees` | Both `_records` and `_worktrees` contribute to `live_cwds` |
| `test_janitor_sweep_once_uses_abspath` | Relative paths are resolved before comparison |

---

## 4. Files Affected

| File | Change |
|---|---|
| `src/ccswitch/worktrees_janitor.py` | **CREATE** — pure sweep logic |
| `server/sessions/orchestrator.py` | **MODIFY** — `__init__`, `start()`, `stop()`, two new methods |
| `server/tests/test_worktree_janitor.py` | **CREATE** — unit tests for sweep logic |
| `server/tests/test_worktree_janitor_orch.py` | **CREATE** — orchestrator integration tests |

No DB schema changes. No new columns. No Alembic migration.

---

## 5. Constraints and Invariants to Preserve

- **Issue #177** — never `git worktree prune` globally. The janitor calls `remove_worktree()`
  which uses scoped `git worktree remove` + `git worktree unlock` on exactly one path.
- **Dirty-work safety** — `_is_clean()` runs BEFORE `remove_worktree()` (which re-checks
  internally); two independent dirty guards.
- **Unverifiable = keep** — any subprocess failure, missing dir between scan and check,
  or no-remote repo → treat conservatively and increment `kept_error`.
- **`_records`-only liveness** — `_worktrees` is supplementary. `_records` is the single
  source of truth for live sessions (it tracks all non-inactive sessions including rehydrated
  idle ones that have no active driver).
- **asyncio.to_thread** — the loop offloads the synchronous sweep to a thread pool so
  blocking git subprocesses don't stall the event loop.

---

## 6. Out of Scope (this PR)

- Retry mechanism for `skipped:nospace` archive moves (a separate issue).
- Surfacing janitor counts in the UI dashboard.
- Configurable `min_age` via the Automation config framework (daily fixed cadence is enough
  for v1; the interval constant is a named variable for easy future override).
