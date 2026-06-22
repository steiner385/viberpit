# Fresh-from-origin Session Worktrees Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `create_worktree` fetch-first and remote-anchored so every new session starts from the true `origin` tip, never a stale local ref.

**Architecture:** Refactor the single function `create_worktree` in `src/ccswitch/worktrees.py`. Before resolving a base, detect whether `origin` is configured. If it is, `git fetch origin` (hard-fail on error) and base the worktree on remote refs — detached at `origin/HEAD` for no-branch sessions, or an isolated child branch `ccswitch/<branch>-<short>` based at `origin/<branch>` for branch sessions. Repos with no `origin` keep today's local-ref behavior. The user's local refs are never moved.

**Tech Stack:** Python 3, `subprocess` + git CLI, `unittest` (run via `pytest`).

## Global Constraints

- Never mutate the user's local refs or working tree (module philosophy, `worktrees.py:1-20`).
- Keep the `-`-prefixed branch-name injection guard and the `--` option/commit-ish separator on every `worktree add`.
- New behavior applies only when an `origin` remote is configured; remoteless repos are unchanged (offline-capable).
- A `git fetch` failure against a configured `origin` is fatal (`WorktreeError`); the session must not start.
- Tests run against the main checkout's prebuilt env, but that env's `ccswitch` resolves to the **deploy** checkout, so you MUST prepend `PYTHONPATH=<this-worktree>/src` to force-import the worktree's source. **Exact command (use verbatim, every pytest step below is shorthand for this):** `PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/e15be2a4/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …`. Verified: this runs the 9 existing `tests/test_worktrees.py` green against the worktree source.
- Branch: `feat/worktree-fresh-from-origin` (already created off `origin/master`).

---

## File Structure

- **Modify** `src/ccswitch/worktrees.py` — add four private helpers (`_has_origin`, `_fetch_origin`, `_remote_default_ref`, `_branch_on_origin`) and rewrite the body of `create_worktree`. No new files; the module stays the single home for worktree logic.
- **Modify** `tests/test_worktrees.py` — add module-level fixture helpers (`_rev`, `_make_repo_with_origin`) and a new `WorktreeFreshFromOriginTests` class. The existing `WorktreeTests` class (remoteless) is untouched and must stay green.

---

### Task 1: Fetch-first plumbing + hard-fail on fetch failure

**Files:**
- Modify: `src/ccswitch/worktrees.py` (add `_has_origin`, `_fetch_origin`; call them in `create_worktree` after the `worktree prune`)
- Test: `tests/test_worktrees.py` (add `_rev`, `_make_repo_with_origin`, new test class with the fetch-failure test)

**Interfaces:**
- Consumes: existing `_run`, `WorktreeError`, `worktrees_base`.
- Produces: `_has_origin(repo_top: str) -> bool`; `_fetch_origin(repo_top: str) -> None` (raises `WorktreeError`). `create_worktree` now performs a fetch when `origin` exists, before base resolution. Signature unchanged: `create_worktree(repo_path: str, branch: str | None, session_id: str, repo_root: str) -> WorktreeInfo`.

- [ ] **Step 1: Add test fixture helpers and the fetch-failure test**

Add these module-level helpers near the top of `tests/test_worktrees.py` (after the existing `_make_repo`):

```python
def _rev(path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", ref],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_repo_with_origin(local: Path, root: Path, branch: str = "main") -> tuple[str, str]:
    """Local repo whose `origin` is one commit AHEAD of local `branch`.

    Returns (local_sha, origin_sha): local tip (A) vs origin tip (B), B != A.
    """
    origin = root / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin), "-b", branch],
                   check=True, capture_output=True)
    _make_repo(local, branch)                                  # commit A
    _git("remote", "add", "origin", str(origin), cwd=str(local))
    _git("push", "-u", "origin", branch, cwd=str(local))       # origin @ A
    (local / "ahead.txt").write_text("remote is ahead\n")
    _git("add", "-A", cwd=str(local))
    _git("commit", "-m", "B (remote-only)", cwd=str(local))    # commit B
    origin_sha = _rev(local, "HEAD")
    _git("push", "origin", branch, cwd=str(local))             # origin @ B
    _git("reset", "--hard", "HEAD~1", cwd=str(local))          # local back to A
    _git("remote", "set-head", "origin", "--auto", cwd=str(local))
    return _rev(local, "HEAD"), origin_sha
```

Add the new test class at the end of the file (before the `__main__` guard):

```python
class WorktreeFreshFromOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "myrepo"
        self.local_sha, self.origin_sha = _make_repo_with_origin(self.repo, self.root)
        self.assertNotEqual(self.local_sha, self.origin_sha)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fetch_failure_is_fatal(self) -> None:
        """A configured-but-unreachable origin aborts session start; no worktree left."""
        _git("remote", "set-url", "origin", str(self.root / "does-not-exist.git"),
             cwd=str(self.repo))
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.create_worktree(str(self.repo), "main", "aaaaaaaa-0001", str(self.root))
        listing = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "list"],
            capture_output=True, text=True,
        ).stdout
        self.assertEqual(len(listing.strip().splitlines()), 1, "no extra worktree expected")
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_worktrees.py::WorktreeFreshFromOriginTests::test_fetch_failure_is_fatal -v`
Expected: FAIL — today `create_worktree` never fetches, so it succeeds and creates a child worktree instead of raising.

- [ ] **Step 3: Add the helpers and wire the fetch into `create_worktree`**

Add after the existing `_run` definition in `src/ccswitch/worktrees.py`:

```python
def _has_origin(repo_top: str) -> bool:
    """True if the repo has an `origin` remote configured."""
    return _run(["git", "-C", repo_top, "remote", "get-url", "origin"]).returncode == 0


def _fetch_origin(repo_top: str) -> None:
    """Fetch `origin`; raise WorktreeError if it fails.

    A configured-but-unreachable origin is fatal: we refuse to silently base a
    session on a stale local ref.
    """
    r = _run(["git", "-C", repo_top, "fetch", "origin"])
    if r.returncode != 0:
        raise WorktreeError(f"git fetch origin failed: {r.stderr.strip()}")
```

In `create_worktree`, immediately after the `worktree prune` line (`worktrees.py:105`), insert:

```python
    # Fetch-first: a session must start from the true remote tip. If origin is
    # configured we fetch and (below) base on remote refs; a fetch failure is
    # fatal — never silently fall back to a stale local ref. A repo with NO
    # origin has nothing to be stale against and keeps the local-ref behavior.
    has_origin = _has_origin(repo_top)
    if has_origin:
        _fetch_origin(repo_top)
```

(Leave the base-resolution branches unchanged in this task — they still read local refs; later tasks switch them to remote refs.)

- [ ] **Step 4: Run the test and the full suite to verify green**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_worktrees.py -v`
Expected: PASS — `test_fetch_failure_is_fatal` passes; all existing `WorktreeTests` (remoteless) still pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktrees.py tests/test_worktrees.py
git commit -m "feat(worktrees): fetch origin before session worktree, hard-fail on failure

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: No-branch sessions detach at the remote default tip

**Files:**
- Modify: `src/ccswitch/worktrees.py` (add `_remote_default_ref`; change the `branch is None` path)
- Test: `tests/test_worktrees.py` (`WorktreeFreshFromOriginTests`)

**Interfaces:**
- Consumes: `_has_origin`, `_fetch_origin` (Task 1), `_run`, `WorktreeError`.
- Produces: `_remote_default_ref(repo_top: str) -> str` returning e.g. `"origin/master"` (raises `WorktreeError` if unresolvable). No-branch sessions on a repo with origin become detached at that tip; `WorktreeInfo.branch is None`, `is_child is False` (unchanged shape).

- [ ] **Step 1: Write the failing test**

Add to `WorktreeFreshFromOriginTests`:

```python
    def test_no_branch_uses_remote_default_tip(self) -> None:
        """branch=None with origin → detached at origin/HEAD tip, not local HEAD."""
        info = worktrees.create_worktree(str(self.repo), None, "bbbbbbbb-0002", str(self.root))
        self.assertIsNone(info.branch)
        self.assertFalse(info.is_child)
        self.assertEqual(_rev(Path(info.path), "HEAD"), self.origin_sha)
        self.assertNotEqual(_rev(Path(info.path), "HEAD"), self.local_sha)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_worktrees.py::WorktreeFreshFromOriginTests::test_no_branch_uses_remote_default_tip -v`
Expected: FAIL — worktree HEAD equals `local_sha` (detached off local HEAD), not `origin_sha`.

- [ ] **Step 3: Add `_remote_default_ref` and update the no-branch path**

Add helper to `src/ccswitch/worktrees.py` (next to the Task 1 helpers):

```python
def _remote_default_ref(repo_top: str) -> str:
    """Return the remote default branch ref (e.g. 'origin/master').

    Repairs origin/HEAD with `set-head --auto` first in case the clone never
    recorded it. Raises WorktreeError if it cannot be resolved.
    """
    _run(["git", "-C", repo_top, "remote", "set-head", "origin", "--auto"])
    r = _run(["git", "-C", repo_top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if r.returncode != 0 or not r.stdout.strip():
        raise WorktreeError("could not resolve origin/HEAD (remote default branch)")
    return r.stdout.strip()
```

Replace the `if branch is None:` block (`worktrees.py:110-115`) with:

```python
    # 0) No branch selected → detached worktree. With origin, detach at the remote
    # default tip (origin/HEAD); without origin, off the local current HEAD.
    if branch is None:
        if has_origin:
            base = _remote_default_ref(repo_top)
            rd = _run(["git", "-C", repo_top, "worktree", "add", "--detach",
                       str(dest), "--", base])
        else:
            rd = _run(["git", "-C", repo_top, "worktree", "add", "--detach", str(dest)])
        if rd.returncode == 0:
            log.info("worktree.create_detached repo=%s path=%s", repo_name, dest)
            return WorktreeInfo(str(dest), None, is_child=False, repo=repo_top)
        raise WorktreeError(f"git worktree add --detach failed: {rd.stderr.strip()}")
```

- [ ] **Step 4: Run tests to verify green**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_worktrees.py -v`
Expected: PASS — new test passes; `test_no_branch_creates_detached_worktree` (remoteless) still passes.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktrees.py tests/test_worktrees.py
git commit -m "feat(worktrees): detach no-branch sessions at remote default tip

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Branch sessions get a fresh child branch at `origin/<branch>`

**Files:**
- Modify: `src/ccswitch/worktrees.py` (add `_branch_on_origin`; insert remote-backed branch path before the local path)
- Test: `tests/test_worktrees.py` (`WorktreeFreshFromOriginTests`)

**Interfaces:**
- Consumes: `_has_origin`, `_branch_on_origin`, `_short`, `WorktreeInfo`.
- Produces: `_branch_on_origin(repo_top: str, branch: str) -> bool`. For a branch that exists on origin, `create_worktree` returns `WorktreeInfo(path, "ccswitch/<branch>-<short>", is_child=True, repo=...)` based at `origin/<branch>`, without moving the user's local `<branch>`.

- [ ] **Step 1: Write the failing tests**

Add to `WorktreeFreshFromOriginTests`:

```python
    def test_remote_branch_is_fresh_child(self) -> None:
        """branch on origin → isolated child branch based at origin/<branch> tip."""
        info = worktrees.create_worktree(str(self.repo), "main", "cccccccc-0003", str(self.root))
        self.assertTrue(info.is_child)
        self.assertTrue(info.branch.startswith("ccswitch/main-"))
        self.assertEqual(_rev(Path(info.path), "HEAD"), self.origin_sha)

    def test_user_local_refs_unchanged(self) -> None:
        """Creating a session never moves the user's local branch or tracking refs."""
        worktrees.create_worktree(str(self.repo), "main", "dddddddd-0004", str(self.root))
        self.assertEqual(_rev(self.repo, "main"), self.local_sha)
        self.assertEqual(_rev(self.repo, "refs/remotes/origin/main"), self.origin_sha)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest "tests/test_worktrees.py::WorktreeFreshFromOriginTests::test_remote_branch_is_fresh_child" "tests/test_worktrees.py::WorktreeFreshFromOriginTests::test_user_local_refs_unchanged" -v`
Expected: FAIL — `test_remote_branch_is_fresh_child` sees HEAD == `local_sha` (child based off local `main`), not `origin_sha`.

- [ ] **Step 3: Add `_branch_on_origin` and the remote-backed branch path**

Add helper to `src/ccswitch/worktrees.py`:

```python
def _branch_on_origin(repo_top: str, branch: str) -> bool:
    """True if `branch` exists on origin (refs/remotes/origin/<branch>)."""
    r = _run(["git", "-C", repo_top, "rev-parse", "--verify", "--quiet",
              f"refs/remotes/origin/{branch}"])
    return r.returncode == 0
```

Insert this block **between** the no-branch block and the existing `# 1) Try to check out the selected branch directly.` block (i.e. right before `worktrees.py:117`):

```python
    # 1) Remote-backed branch → isolated per-session child branch based at the
    # remote tip origin/<branch>. This is the normal path (not just a fallback):
    # a fresh, writable, isolated branch without moving the user's local <branch>.
    if has_origin and _branch_on_origin(repo_top, branch):
        child = f"ccswitch/{branch}-{_short(session_id)}"
        r = _run(["git", "-C", repo_top, "worktree", "add", "-b", child,
                  str(dest), "--", f"origin/{branch}"])
        if r.returncode == 0:
            log.info("worktree.create_child repo=%s base=origin/%s child=%s path=%s",
                     repo_name, branch, child, dest)
            return WorktreeInfo(str(dest), child, is_child=True, repo=repo_top)
        raise WorktreeError(
            f"git worktree add failed for origin/{branch}: {r.stderr.strip()}")
```

Renumber the trailing comment `# 1) Try to check out the selected branch directly.` to `# 2) Local-only branch (or no origin) → check out the local branch directly;` and `# 2) Branch is busy ...` to `# 3) ...` for clarity (comment-only edit). The code of those two blocks stays as-is.

- [ ] **Step 4: Run the full suite to verify green**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_worktrees.py -v`
Expected: PASS — both new tests pass; all remoteless `WorktreeTests` still pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktrees.py tests/test_worktrees.py
git commit -m "feat(worktrees): base session branch on origin/<branch>, not local ref

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Regression guard — local-only branch keeps local base

**Files:**
- Test: `tests/test_worktrees.py` (`WorktreeFreshFromOriginTests`)

**Interfaces:**
- Consumes: the full `create_worktree` behavior from Tasks 1–3. No production change; this task proves the local-only fallback (spec rule 3) holds when `origin` is present.

- [ ] **Step 1: Write the test**

Add to `WorktreeFreshFromOriginTests`:

```python
    def test_local_only_branch_uses_local_base(self) -> None:
        """A branch that exists only locally (not on origin) is based off the local
        ref even though origin is configured — there is no remote tip to be fresh against."""
        _git("branch", "localfeature", cwd=str(self.repo))   # at local A, never pushed
        info = worktrees.create_worktree(str(self.repo), "localfeature", "eeeeeeee-0005", str(self.root))
        self.assertFalse(info.is_child)
        self.assertEqual(info.branch, "localfeature")
        self.assertEqual(_rev(Path(info.path), "HEAD"), self.local_sha)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest "tests/test_worktrees.py::WorktreeFreshFromOriginTests::test_local_only_branch_uses_local_base" -v`
Expected: PASS — `localfeature` is not on origin, so `_branch_on_origin` is False and the local-checkout path (block 2) runs. (If it fails, the remote-backed path is wrongly matching local-only branches — fix `_branch_on_origin`.)

- [ ] **Step 3: Run the full project test suites for no regressions**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest tests/test_worktrees.py server/tests/test_orchestrator_worktree.py server/tests/test_security_hardening.py -v`
Expected: PASS — worktree behavior unchanged for remoteless repos (orchestrator + security-hardening tests use repos without origin).

- [ ] **Step 4: Commit**

```bash
git add tests/test_worktrees.py
git commit -m "test(worktrees): guard local-only branch keeps local base with origin present

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Completion

After Task 4, follow the project completion workflow (CLAUDE.md): push `feat/worktree-fresh-from-origin`, open a PR via `gh pr create` (Claude Code trailer in the body), and merge with `gh pr merge --merge --delete-branch` once green and mergeable.
