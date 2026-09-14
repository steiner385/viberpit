# Cross-Host Session Transfer, Phase 1 (primitive) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `POST /api/sessions/{id}/transfer` move a turn-boundary Claude session from one fleet host to another, carrying its worktree through git, with the session resumed on the target and the UI routed there.

**Architecture:** A pure git module in `src/ccswitch/worktree_transfer.py` moves worktree state through a hidden ref namespace on `origin`. Three daemon RPCs (`transfer_export`, `transfer_import`, `transfer_finalize`) wrap the existing driver-swap, lease and rehydrate primitives. An edge-side saga in `server/sessions/transfer.py` sequences them and owns rollback; the `FleetOrchestrator` learns to mark a session as moving and to self-heal a stale route.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (SQLite in tests), pytest + pytest-asyncio, unittest for the core suite, git CLI.

**Spec:** `docs/superpowers/specs/2026-09-14-cross-host-session-transfer-design.md` (PR #1696). Phase 2 (web) and Phase 3 (rebalance loop) get their own plans after this one lands.

## Global Constraints

- New env settings take the `VIBERPIT_` prefix: declare the pydantic field with the legacy `CCSWITCH_` alias (the import-time `mirror_legacy_env()` fills it from `VIBERPIT_*`), never add a new bare `CCSWITCH_*` reader.
- Never `func.now()`; datetime writes use `server/db/clock.py:db_utc_now()`. Columns are naive UTC.
- Never hand-roll `task.cancel()` + `await task`; use `server/aio.py:cancel_and_reap()`.
- New `sessions` columns are nullable so `init_db` reconcile adds them on SQLite and Postgres; still add an alembic revision chained off `0045_automation_ownership`.
- Brand ratchet: new identifiers, refs and env names spell `viberpit`, not `ccswitch`. Run `python3 scripts/brand_ratchet.py` before every commit; counts must not rise.
- Daemons never talk to each other. Every cross-host step is issued by the edge through `FleetRouter.get(host)`.
- Core-suite tests (`tests/`) run with `PYTHONPATH=$PWD/src` so they import this checkout's `ccswitch`, not the deploy worktree's.
- Test commands below assume `python3` is `/home/tony/GitHub/ccswitch/.venv/bin/python3` (or any venv with the server extras) and are run from the checkout root with `env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CLIENT_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. Write that prefix once as `$T` in your shell: `T='env -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CLIENT_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src python3 -m pytest'`.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Write commit messages to a file and pass `-F`.
- Work on one branch off `origin/master`: `git fetch && git checkout -b feat/session-transfer-phase1 origin/master`. Two PRs at the end: 1a (Tasks 1–4, core) and 1b (Tasks 5–17, server). Task 17 explains the split.

---

## File structure

| File | Responsibility |
|---|---|
| `src/ccswitch/worktree_transfer.py` (new) | Pure git plumbing: inspect a worktree, export it to transfer refs, import it on another clone, finalize the source, delete the refs. No orchestrator imports. |
| `tests/test_worktree_transfer.py` (new) | Core-suite tests against a temp bare origin with two clones. |
| `server/db/models.py` | Four nullable `sessions` columns. |
| `server/db/migrations/versions/0046_session_transfer.py` (new) | Reviewable record of the columns. |
| `server/sessions/types.py` | `SessionRecord` gains the four fields plus `to_dict`/`from_dict` keys. |
| `server/sessions/engine.py`, `server/sessions/claude_driver.py` | `AgentCapabilities.cross_host_transfer`, true for Claude. |
| `server/sessions/transfer_policy.py` (new) | Pure `movable()` predicate and `Verdict`. |
| `server/sessions/orchestrator.py` | `_normalize_row` / `_adopt_row` extraction; `transfer_export`, `transfer_import`, `transfer_finalize`; `moved_host_nudge`. |
| `server/ipc/orchestrator_rpc.py`, `server/sessions/remote.py`, `server/sessions/facade.py` | The three RPCs on the wire. |
| `server/sessions/fleet_orchestrator.py` | `begin_move` / `end_move` / `note_host`, `TransferInFlight`, stale-route self-heal. |
| `server/sessions/transfer.py` (new) | Edge saga: claim, export, restamp, import, finalize, rollback. |
| `server/bridge/ws.py` | `SESSION_MOVING` attach error with `retryAfterMs`. |
| `server/api/sessions.py`, `server/api/fleet.py` | `POST /{id}/transfer`, `POST /batch/transfer`, `POST /{id}/host-pin`, `POST /api/fleet/hosts/{name}/drain`. |
| `server/config.py` | `VIBERPIT_TRANSFER_ENABLED` kill switch. |
| `scripts/verify-cross-host-resume.sh` (new) | The Principle III drill from spec 002 Task 17. |

---

### Task 1: `worktree_transfer.inspect` and the data types

**Files:**
- Create: `src/ccswitch/worktree_transfer.py`
- Test: `tests/test_worktree_transfer.py`

**Interfaces:**
- Consumes: `ccswitch.worktrees._run`, `_origin_url`, `_short`, `worktrees_base`, `lock_worktree`, `clear_stale_registration`, `remove_worktree`, `current_branch`, `authored_branches`, `WorktreeInfo`, `_try_fetch_origin`; `ccswitch.gitrepos.git_env_for_remote(url) -> dict | None`.
- Produces: `TransferError(code, detail)` with `.code`; `WorktreeState(dirty, dirty_files, branch, head_sha, unpushed)`; `inspect(cwd) -> WorktreeState`; `TRANSFER_REF_NS`, `head_ref(session_id)`, `branch_ref(session_id, name)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worktree_transfer.py
"""Tests for ccswitch.worktree_transfer — carrying a worktree between hosts via git."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ccswitch import worktree_transfer as wt, worktrees


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_origin_and_clone(root: Path, name: str = "myrepo", branch: str = "main") -> Path:
    """A bare origin under root/origin.git plus a clone at root/<name> on <branch>."""
    origin = root / "origin.git"
    if not origin.exists():
        subprocess.run(["git", "init", "--bare", str(origin), "-b", branch],
                       check=True, capture_output=True)
        seed = root / "_seed"
        subprocess.run(["git", "init", str(seed), "-b", branch], check=True, capture_output=True)
        _git("config", "user.email", "t@example.com", cwd=str(seed))
        _git("config", "user.name", "Test", cwd=str(seed))
        (seed / "README.md").write_text("hello\n")
        _git("add", "-A", cwd=str(seed))
        _git("commit", "-q", "-m", "init", cwd=str(seed))
        _git("remote", "add", "origin", str(origin), cwd=str(seed))
        _git("push", "-q", "-u", "origin", branch, cwd=str(seed))
    clone = root / name
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=str(clone))
    _git("config", "user.name", "Test", cwd=str(clone))
    return clone


class InspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clone = _make_origin_and_clone(self.root)
        # a session worktree on its own branch, the shape create_worktree produces
        self.info = worktrees.create_worktree(str(self.clone), "main", "aaaa1111-0000", str(self.root))
        self.cwd = self.info.path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_pushed_worktree(self) -> None:
        st = wt.inspect(self.cwd)
        self.assertFalse(st.dirty)
        self.assertEqual(st.dirty_files, 0)
        self.assertEqual(st.head_sha, _git("rev-parse", "HEAD", cwd=self.cwd))
        # child branch ccswitch/main-aaaa1111 exists only locally → unpushed
        self.assertEqual(st.branch, self.info.branch)
        self.assertIn(self.info.branch, st.unpushed)

    def test_dirty_counts_untracked_and_modified(self) -> None:
        (Path(self.cwd) / "README.md").write_text("changed\n")
        (Path(self.cwd) / "new.txt").write_text("new\n")
        st = wt.inspect(self.cwd)
        self.assertTrue(st.dirty)
        self.assertEqual(st.dirty_files, 2)

    def test_pushed_branch_is_not_unpushed(self) -> None:
        _git("push", "-q", "-u", "origin", self.info.branch, cwd=self.cwd)
        st = wt.inspect(self.cwd)
        self.assertNotIn(self.info.branch, st.unpushed)

    def test_detached_head_has_no_branch(self) -> None:
        _git("checkout", "-q", "--detach", cwd=self.cwd)
        st = wt.inspect(self.cwd)
        self.assertIsNone(st.branch)

    def test_not_a_repo_raises(self) -> None:
        with self.assertRaises(wt.TransferError) as ctx:
            wt.inspect(str(self.root))
        self.assertEqual(ctx.exception.code, "not_git")

    def test_ref_names(self) -> None:
        self.assertEqual(wt.head_ref("s-1"), "refs/viberpit/transfer/s-1/head")
        self.assertEqual(wt.branch_ref("s-1", "feat/x"), "refs/viberpit/transfer/s-1/branches/feat/x")
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.worktree_transfer'`

- [ ] **Step 3: Write the module**

```python
# src/ccswitch/worktree_transfer.py
"""Carry a session's worktree between fleet hosts through git.

A session's worktree is the one host-local thing a cross-host transfer must move
(spec: docs/superpowers/specs/2026-09-14-cross-host-session-transfer-design.md).
The source pushes HEAD (plus unpushed authored branches) to a hidden namespace on
``origin``, ``refs/viberpit/transfer/<session>/…``; the target fetches it and
re-creates the worktree at the same path. Uncommitted edits ride inside a WIP
commit that the target unpacks with ``git reset --mixed`` so they arrive as
unstaged edits and the branch tip is exactly where the agent left it.

Pure git plumbing: no orchestrator imports, every function is synchronous and
safe to run under ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ccswitch.gitrepos import git_env_for_remote
from ccswitch.worktrees import (
    WorktreeInfo, _origin_url, _run, _short, _try_fetch_origin, authored_branches,
    clear_stale_registration, current_branch, lock_worktree, remove_worktree,
    worktrees_base,
)

log = logging.getLogger(__name__)

__all__ = ["TransferError", "WorktreeState", "Bundle", "TRANSFER_REF_NS", "head_ref",
           "branch_ref", "inspect", "export", "import_", "finalize", "delete_transfer_refs"]

#: Hidden ref namespace on origin. Not under refs/heads, so it never appears as a
#: branch, never triggers CI or PR automation, and is deleted after import.
TRANSFER_REF_NS = "refs/viberpit/transfer"
WIP_SUBJECT = "viberpit: transfer WIP for session {session_id}; not for merge"
_WIP_IDENTITY = ["-c", "user.name=ViberPit", "-c", "user.email=viberpit@localhost"]


class TransferError(Exception):
    """A step refused or failed. ``code`` is the stable machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorktreeState:
    dirty: bool
    dirty_files: int
    branch: str | None          # None when detached
    head_sha: str
    unpushed: tuple[str, ...]   # authored branches whose tip is not on origin at the same sha


def head_ref(session_id: str) -> str:
    return f"{TRANSFER_REF_NS}/{session_id}/head"


def branch_ref(session_id: str, name: str) -> str:
    return f"{TRANSFER_REF_NS}/{session_id}/branches/{name}"


def _rev(cwd: str, ref: str) -> str | None:
    r = _run(["git", "-C", cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _repo_top(cwd: str) -> str:
    """Toplevel of the clone that owns *cwd* (a worktree or the clone itself)."""
    r = _run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if r.returncode != 0:
        raise TransferError("not_git", f"{cwd} is not inside a git repository")
    return os.path.dirname(r.stdout.strip().rstrip("/"))


def inspect(cwd: str) -> WorktreeState:
    """What a transfer would have to carry from *cwd*. Read-only, no network."""
    head = _rev(cwd, "HEAD")
    if head is None:
        raise TransferError("not_git", f"{cwd} is not a git worktree")
    status = _run(["git", "-C", cwd, "status", "--porcelain"])
    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    branch = current_branch(cwd)
    candidates = set(authored_branches(cwd))
    if branch:
        candidates.add(branch)
    unpushed: list[str] = []
    for b in sorted(candidates):
        local = _rev(cwd, f"refs/heads/{b}")
        if local is None:
            continue
        if _rev(cwd, f"refs/remotes/origin/{b}") != local:
            unpushed.append(b)
    return WorktreeState(dirty=bool(lines), dirty_files=len(lines), branch=branch,
                         head_sha=head, unpushed=tuple(unpushed))
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktree_transfer.py tests/test_worktree_transfer.py
printf 'feat(core): worktree_transfer.inspect and transfer ref names\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -q -F /tmp/c1.txt
```

---

### Task 2: `worktree_transfer.export` and `Bundle`

**Files:**
- Modify: `src/ccswitch/worktree_transfer.py`
- Test: `tests/test_worktree_transfer.py`

**Interfaces:**
- Produces: `Bundle(session_id, branch, head_sha, base_sha, wip_commit, branches)` with `to_dict()` / `Bundle.from_dict(d)`; `export(cwd, session_id, *, carry_dirty) -> Bundle`, raising `TransferError` with code `dirty_worktree`, `no_origin`, `wip_commit_failed` or `push_failed`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worktree_transfer.py`:

```python
class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clone = _make_origin_and_clone(self.root)
        self.info = worktrees.create_worktree(str(self.clone), "main", "bbbb2222-0000", str(self.root))
        self.cwd = self.info.path
        self.origin = str(self.root / "origin.git")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _origin_ref(self, ref: str) -> str | None:
        r = subprocess.run(["git", "-C", self.origin, "rev-parse", "--verify", "--quiet", ref],
                           capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_clean_export_pushes_head_without_wip(self) -> None:
        head = _git("rev-parse", "HEAD", cwd=self.cwd)
        b = wt.export(self.cwd, "bbbb2222-0000", carry_dirty=False)
        self.assertFalse(b.wip_commit)
        self.assertEqual(b.head_sha, head)
        self.assertEqual(b.base_sha, head)
        self.assertEqual(b.branch, self.info.branch)
        self.assertEqual(self._origin_ref(wt.head_ref("bbbb2222-0000")), head)

    def test_dirty_without_consent_refuses_and_touches_nothing(self) -> None:
        (Path(self.cwd) / "x.txt").write_text("x\n")
        with self.assertRaises(wt.TransferError) as ctx:
            wt.export(self.cwd, "bbbb2222-0000", carry_dirty=False)
        self.assertEqual(ctx.exception.code, "dirty_worktree")
        self.assertIsNone(self._origin_ref(wt.head_ref("bbbb2222-0000")))
        self.assertTrue(wt.inspect(self.cwd).dirty)

    def test_dirty_with_consent_ships_a_wip_commit(self) -> None:
        base = _git("rev-parse", "HEAD", cwd=self.cwd)
        (Path(self.cwd) / "x.txt").write_text("x\n")
        b = wt.export(self.cwd, "bbbb2222-0000", carry_dirty=True)
        self.assertTrue(b.wip_commit)
        self.assertEqual(b.base_sha, base)
        self.assertNotEqual(b.head_sha, base)
        self.assertEqual(self._origin_ref(wt.head_ref("bbbb2222-0000")), b.head_sha)
        subj = subprocess.run(["git", "-C", self.origin, "log", "-1", "--format=%s", b.head_sha],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(subj, wt.WIP_SUBJECT.format(session_id="bbbb2222-0000"))
        parent = subprocess.run(["git", "-C", self.origin, "rev-parse", f"{b.head_sha}^"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(parent, base)

    def test_unpushed_authored_branch_is_carried(self) -> None:
        _git("checkout", "-q", "-b", "feat/extra", cwd=self.cwd)
        (Path(self.cwd) / "e.txt").write_text("e\n")
        _git("add", "-A", cwd=self.cwd)
        _git("commit", "-q", "-m", "extra", cwd=self.cwd)
        extra = _git("rev-parse", "HEAD", cwd=self.cwd)
        _git("checkout", "-q", self.info.branch, cwd=self.cwd)
        b = wt.export(self.cwd, "bbbb2222-0000", carry_dirty=False)
        self.assertEqual(b.branches, {"feat/extra": extra})
        self.assertEqual(self._origin_ref(wt.branch_ref("bbbb2222-0000", "feat/extra")), extra)

    def test_push_failure_undoes_the_wip_commit(self) -> None:
        base = _git("rev-parse", "HEAD", cwd=self.cwd)
        (Path(self.cwd) / "x.txt").write_text("x\n")
        _git("remote", "set-url", "origin", str(self.root / "does-not-exist.git"), cwd=self.cwd)
        with self.assertRaises(wt.TransferError) as ctx:
            wt.export(self.cwd, "bbbb2222-0000", carry_dirty=True)
        self.assertEqual(ctx.exception.code, "push_failed")
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.cwd), base)
        self.assertTrue(wt.inspect(self.cwd).dirty)

    def test_bundle_round_trips_through_dict(self) -> None:
        b = wt.Bundle("s", "br", "h", "b", True, {"feat/x": "sha"})
        self.assertEqual(wt.Bundle.from_dict(b.to_dict()), b)
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v -k Export`
Expected: FAIL with `AttributeError: module 'ccswitch.worktree_transfer' has no attribute 'export'`

- [ ] **Step 3: Implement**

Append to `src/ccswitch/worktree_transfer.py`:

```python
@dataclass(frozen=True)
class Bundle:
    """What the source pushed and the target must fetch. Crosses hosts as a dict
    inside the transfer manifest, so every field is a JSON scalar."""

    session_id: str
    branch: str | None
    head_sha: str        # the commit the target checks out (the WIP commit when wip_commit)
    base_sha: str        # the agent's real tip; == head_sha unless wip_commit
    wip_commit: bool
    branches: dict[str, str] = field(default_factory=dict)  # other unpushed branches → sha

    def to_dict(self) -> dict:
        return {"sessionId": self.session_id, "branch": self.branch, "headSha": self.head_sha,
                "baseSha": self.base_sha, "wipCommit": self.wip_commit,
                "branches": dict(self.branches)}

    @classmethod
    def from_dict(cls, d: dict) -> "Bundle":
        return cls(session_id=d["sessionId"], branch=d.get("branch"), head_sha=d["headSha"],
                   base_sha=d["baseSha"], wip_commit=bool(d.get("wipCommit", False)),
                   branches=dict(d.get("branches") or {}))


def export(cwd: str, session_id: str, *, carry_dirty: bool) -> Bundle:
    """Push *cwd*'s HEAD and unpushed authored branches to the transfer namespace.

    A dirty tree is refused unless ``carry_dirty`` (the user ticked the consent
    box; the rebalancer never passes True). With consent the edits become one WIP
    commit on top of HEAD, pushed as the transfer head; on a push failure that
    commit is undone (``reset --mixed``) so the worktree is exactly as found.
    """
    top = _repo_top(cwd)
    url = _origin_url(top)
    if not url:
        raise TransferError("no_origin", f"{top} has no origin remote")
    _try_fetch_origin(top)  # best-effort: makes `unpushed` accurate against a fresh origin
    state = inspect(cwd)
    if state.dirty and not carry_dirty:
        raise TransferError("dirty_worktree", f"{state.dirty_files} uncommitted change(s)")
    base_sha = state.head_sha
    head_sha = base_sha
    wip = False
    if state.dirty:
        add = _run(["git", "-C", cwd, "add", "-A"])
        commit = _run(["git", *_WIP_IDENTITY, "-C", cwd, "commit", "-q", "--no-verify",
                       "-m", WIP_SUBJECT.format(session_id=session_id)])
        if add.returncode != 0 or commit.returncode != 0:
            raise TransferError("wip_commit_failed", (add.stderr + commit.stderr).strip())
        head_sha = _rev(cwd, "HEAD") or base_sha
        wip = True
    refspecs = [f"+{head_sha}:{head_ref(session_id)}"]
    branches: dict[str, str] = {}
    for b in state.unpushed:
        if b == state.branch:
            continue
        sha = _rev(cwd, f"refs/heads/{b}")
        if sha is None:
            continue
        branches[b] = sha
        refspecs.append(f"+{sha}:{branch_ref(session_id, b)}")
    r = _run(["git", "-C", cwd, "push", "-q", "--no-verify", "origin", *refspecs],
             env=git_env_for_remote(url))
    if r.returncode != 0:
        if wip:
            _run(["git", "-C", cwd, "reset", "-q", "--mixed", base_sha])
        raise TransferError("push_failed", r.stderr.strip())
    log.info("transfer.export session=%s head=%s wip=%s branches=%d",
             session_id, head_sha[:12], wip, len(branches))
    return Bundle(session_id=session_id, branch=state.branch, head_sha=head_sha,
                  base_sha=base_sha, wip_commit=wip, branches=branches)
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktree_transfer.py tests/test_worktree_transfer.py
printf 'feat(core): worktree_transfer.export pushes HEAD and unpushed branches to transfer refs\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -q -F /tmp/c2.txt
```

---

### Task 3: `worktree_transfer.import_` and `delete_transfer_refs`

**Files:**
- Modify: `src/ccswitch/worktree_transfer.py`
- Test: `tests/test_worktree_transfer.py`

**Interfaces:**
- Produces: `import_(bundle, repo_path, repo_root) -> WorktreeInfo` raising `TransferError` codes `fetch_failed`, `dest_occupied`, `branch_busy_on_target`, `branch_failed`, `worktree_add_failed`, `wip_unpack_failed`; `delete_transfer_refs(repo_path, session_id) -> None`.

- [ ] **Step 1: Write the failing tests**

Append:

```python
class ImportTests(unittest.TestCase):
    """Two clones of one origin under two repo roots stand in for two hosts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src_root = self.root / "hostA"; self.src_root.mkdir()
        self.dst_root = self.root / "hostB"; self.dst_root.mkdir()
        self.src = _make_origin_and_clone(self.root, name="hostA/myrepo")
        self.dst = _make_origin_and_clone(self.root, name="hostB/myrepo")
        self.sid = "cccc3333-0000"
        self.info = worktrees.create_worktree(str(self.src), "main", self.sid, str(self.src_root))
        self.cwd = self.info.path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _dest(self) -> Path:
        return worktrees.worktrees_base(str(self.dst_root)) / "myrepo" / "cccc3333"

    def test_clean_import_recreates_worktree_on_branch(self) -> None:
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        info = wt.import_(b, str(self.dst), str(self.dst_root))
        self.assertEqual(Path(info.path), self._dest())
        self.assertEqual(_git("branch", "--show-current", cwd=info.path), self.info.branch)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=info.path), b.head_sha)
        self.assertFalse(wt.inspect(info.path).dirty)

    def test_wip_arrives_as_unstaged_edits_with_tip_at_base(self) -> None:
        (Path(self.cwd) / "x.txt").write_text("x\n")
        (Path(self.cwd) / "README.md").write_text("changed\n")
        b = wt.export(self.cwd, self.sid, carry_dirty=True)
        info = wt.import_(b, str(self.dst), str(self.dst_root))
        self.assertEqual(_git("rev-parse", "HEAD", cwd=info.path), b.base_sha)
        st = wt.inspect(info.path)
        self.assertTrue(st.dirty)
        self.assertEqual(st.dirty_files, 2)
        self.assertEqual((Path(info.path) / "x.txt").read_text(), "x\n")
        staged = _git("diff", "--cached", "--name-only", cwd=info.path)
        self.assertEqual(staged, "")

    def test_detached_head_imports_detached(self) -> None:
        _git("checkout", "-q", "--detach", cwd=self.cwd)
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        info = wt.import_(b, str(self.dst), str(self.dst_root))
        self.assertIsNone(info.branch)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=info.path), b.head_sha)

    def test_branch_busy_on_target_is_refused(self) -> None:
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        _git("fetch", "-q", "origin", f"+{wt.head_ref(self.sid)}:refs/tmp/h", cwd=str(self.dst))
        _git("checkout", "-q", "-b", self.info.branch, "refs/tmp/h", cwd=str(self.dst))
        with self.assertRaises(wt.TransferError) as ctx:
            wt.import_(b, str(self.dst), str(self.dst_root))
        self.assertEqual(ctx.exception.code, "branch_busy_on_target")
        self.assertFalse(self._dest().exists())

    def test_existing_dest_with_matching_head_is_reused(self) -> None:
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        first = wt.import_(b, str(self.dst), str(self.dst_root))
        second = wt.import_(b, str(self.dst), str(self.dst_root))
        self.assertEqual(first.path, second.path)

    def test_extra_branches_are_recreated_when_free(self) -> None:
        _git("checkout", "-q", "-b", "feat/extra", cwd=self.cwd)
        (Path(self.cwd) / "e.txt").write_text("e\n")
        _git("add", "-A", cwd=self.cwd)
        _git("commit", "-q", "-m", "extra", cwd=self.cwd)
        extra = _git("rev-parse", "HEAD", cwd=self.cwd)
        _git("checkout", "-q", self.info.branch, cwd=self.cwd)
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        wt.import_(b, str(self.dst), str(self.dst_root))
        self.assertEqual(_git("rev-parse", "refs/heads/feat/extra", cwd=str(self.dst)), extra)

    def test_delete_transfer_refs_clears_origin_and_local(self) -> None:
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        wt.import_(b, str(self.dst), str(self.dst_root))
        wt.delete_transfer_refs(str(self.dst), self.sid)
        origin = str(self.root / "origin.git")
        listed = subprocess.run(["git", "-C", origin, "for-each-ref", f"{wt.TRANSFER_REF_NS}/{self.sid}/"],
                                capture_output=True, text=True).stdout
        self.assertEqual(listed.strip(), "")
        local = _git("for-each-ref", f"{wt.TRANSFER_REF_NS}/{self.sid}/", cwd=str(self.dst))
        self.assertEqual(local, "")
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v -k Import`
Expected: FAIL with `AttributeError: ... has no attribute 'import_'`

- [ ] **Step 3: Implement**

Append:

```python
def _branch_checked_out_at(repo_top: str, branch: str) -> str | None:
    """Path of the worktree that has refs/heads/<branch> checked out, else None."""
    r = _run(["git", "-C", repo_top, "worktree", "list", "--porcelain"])
    path: str | None = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return None


def import_(bundle: Bundle, repo_path: str, repo_root: str) -> WorktreeInfo:
    """Fetch *bundle*'s transfer refs into the clone at *repo_path* and re-create the
    session worktree at its canonical path under *repo_root*.

    Idempotent for the restore-to-source case: a worktree already at the path whose
    HEAD is the bundle's head or base is reused untouched. A branch already checked
    out elsewhere on this clone is refused rather than renamed — ``create_worktree``'s
    child-branch fallback would silently change the branch the agent remembers.
    """
    top = _repo_top(repo_path)
    repo_name = os.path.basename(top.rstrip("/"))
    dest = worktrees_base(repo_root) / repo_name / _short(bundle.session_id)
    url = _origin_url(top)
    ns = f"{TRANSFER_REF_NS}/{bundle.session_id}"
    r = _run(["git", "-C", top, "fetch", "-q", "origin", f"+{ns}/*:{ns}/*"],
             env=git_env_for_remote(url))
    if r.returncode != 0:
        raise TransferError("fetch_failed", r.stderr.strip())
    if _rev(top, bundle.head_sha) is None:
        raise TransferError("fetch_failed", f"{bundle.head_sha} absent after fetch")

    if dest.is_dir():
        if _rev(str(dest), "HEAD") in (bundle.head_sha, bundle.base_sha):
            log.info("transfer.import reuse path=%s", dest)
            return WorktreeInfo(str(dest), bundle.branch, is_child=False, repo=top)
        raise TransferError("dest_occupied", f"{dest} exists with a different HEAD")

    dest.parent.mkdir(parents=True, exist_ok=True)
    clear_stale_registration(top, dest)
    if bundle.branch is None:
        add = _run(["git", "-C", top, "worktree", "add", "-q", "--detach", str(dest),
                    "--", bundle.head_sha])
    else:
        busy = _branch_checked_out_at(top, bundle.branch)
        if busy:
            raise TransferError("branch_busy_on_target",
                                f"{bundle.branch} is checked out at {busy}")
        br = _run(["git", "-C", top, "branch", "-f", bundle.branch, bundle.head_sha])
        if br.returncode != 0:
            raise TransferError("branch_failed", br.stderr.strip())
        add = _run(["git", "-C", top, "worktree", "add", "-q", str(dest), "--", bundle.branch])
    if add.returncode != 0:
        raise TransferError("worktree_add_failed", add.stderr.strip())
    if bundle.wip_commit:
        rs = _run(["git", "-C", str(dest), "reset", "-q", "--mixed", bundle.base_sha])
        if rs.returncode != 0:
            raise TransferError("wip_unpack_failed", rs.stderr.strip())
    for name, sha in bundle.branches.items():
        if _rev(top, f"refs/heads/{name}") is None:
            _run(["git", "-C", top, "branch", name, sha])
    lock_worktree(top, dest)
    log.info("transfer.import session=%s path=%s branch=%s wip=%s",
             bundle.session_id, dest, bundle.branch, bundle.wip_commit)
    return WorktreeInfo(str(dest), bundle.branch, is_child=False, repo=top)


def delete_transfer_refs(repo_path: str, session_id: str) -> None:
    """Drop the session's transfer namespace locally and on origin. Best-effort."""
    top = _repo_top(repo_path)
    ns = f"{TRANSFER_REF_NS}/{session_id}"
    listed = _run(["git", "-C", top, "for-each-ref", "--format=%(refname)", f"{ns}/"])
    for ref in (ln.strip() for ln in listed.stdout.splitlines() if ln.strip()):
        _run(["git", "-C", top, "update-ref", "-d", ref])
    url = _origin_url(top)
    if not url:
        return
    env = git_env_for_remote(url)
    remote = _run(["git", "-C", top, "ls-remote", "--refs", "origin", f"{ns}/*"], env=env)
    refs = [ln.split("\t", 1)[1] for ln in remote.stdout.splitlines() if "\t" in ln]
    if refs:
        r = _run(["git", "-C", top, "push", "-q", "--no-verify", "origin",
                  *[f":{ref}" for ref in refs]], env=env)
        if r.returncode != 0:
            log.warning("transfer.refs_delete_failed session=%s: %s", session_id, r.stderr.strip())
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v`
Expected: 19 passed

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktree_transfer.py tests/test_worktree_transfer.py
printf 'feat(core): worktree_transfer.import_ recreates the worktree from transfer refs\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -q -F /tmp/c3.txt
```

---

### Task 4: `worktree_transfer.finalize`

**Files:**
- Modify: `src/ccswitch/worktree_transfer.py`
- Test: `tests/test_worktree_transfer.py`

**Interfaces:**
- Produces: `finalize(bundle, worktree_path) -> bool` (True when the source worktree was removed).

- [ ] **Step 1: Write the failing tests**

Append:

```python
class FinalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clone = _make_origin_and_clone(self.root)
        self.sid = "dddd4444-0000"
        self.info = worktrees.create_worktree(str(self.clone), "main", self.sid, str(self.root))
        self.cwd = self.info.path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finalize_after_wip_export_restores_branch_tip_and_removes_dir(self) -> None:
        base = _git("rev-parse", "HEAD", cwd=self.cwd)
        (Path(self.cwd) / "x.txt").write_text("x\n")
        b = wt.export(self.cwd, self.sid, carry_dirty=True)
        self.assertTrue(wt.finalize(b, self.cwd))
        self.assertFalse(os.path.isdir(self.cwd))
        # the child branch was deleted by remove_worktree; no ref may still point at the WIP
        r = subprocess.run(["git", "-C", str(self.clone), "branch", "--contains", b.head_sha],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIsNotNone(base)

    def test_finalize_clean_removes_dir(self) -> None:
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        self.assertTrue(wt.finalize(b, self.cwd))
        self.assertFalse(os.path.isdir(self.cwd))

    def test_finalize_is_idempotent(self) -> None:
        b = wt.export(self.cwd, self.sid, carry_dirty=False)
        self.assertTrue(wt.finalize(b, self.cwd))
        self.assertFalse(wt.finalize(b, self.cwd))
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py -v -k Finalize`
Expected: FAIL with `AttributeError: ... has no attribute 'finalize'`

- [ ] **Step 3: Implement**

Append:

```python
def finalize(bundle: Bundle, worktree_path: str) -> bool:
    """Source-side cleanup after a successful import elsewhere.

    The WIP commit was a transport artifact: put the branch tip back at the base
    (``reset --soft`` keeps the edits staged so nothing is lost if the removal below
    is refused), then remove the worktree with force — its contents now live on
    the target. Returns True iff the directory was removed. Safe to call twice.
    """
    if not os.path.isdir(worktree_path):
        return False
    if bundle.wip_commit and _rev(worktree_path, "HEAD") == bundle.head_sha:
        _run(["git", "-C", worktree_path, "reset", "-q", "--soft", bundle.base_sha])
    removed = remove_worktree(worktree_path, force=True)
    log.info("transfer.finalize session=%s removed=%s path=%s",
             bundle.session_id, removed, worktree_path)
    return removed
```

- [ ] **Step 4: Run the whole core suite**

Run: `PYTHONPATH=$PWD/src python3 -m pytest tests/test_worktree_transfer.py tests/test_worktrees.py -v`
Expected: all passed (22 new + existing). Then `python3 -m ruff check src/ tests/test_worktree_transfer.py` → clean, and `python3 scripts/brand_ratchet.py` → counts unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/worktree_transfer.py tests/test_worktree_transfer.py
printf 'feat(core): worktree_transfer.finalize cleans the source after import\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -q -F /tmp/c4.txt
```

---

### Task 5: Schema columns, `SessionRecord` fields, wire keys, migration

**Files:**
- Modify: `server/db/models.py:199-203` (after `host`)
- Modify: `server/sessions/types.py` (`SessionRecord` fields after `display_mode`; `to_dict` after `"displayMode"`; `from_dict` after `display_mode=`)
- Modify: `server/sessions/orchestrator.py:11290` (`_record_from_row` field map, after `display_mode=`)
- Modify: `server/api/sessions.py:237-239` (`_record_to_dict`, after `"host"`)
- Create: `server/db/migrations/versions/0046_session_transfer.py`
- Test: `server/tests/test_session_transfer_columns.py`

**Interfaces:**
- Produces: `Session.moving_to: str | None`, `Session.moved_from: str | None`, `Session.moved_at: datetime | None`, `Session.host_pinned: bool | None`; `SessionRecord.moving_to`, `.moved_from`, `.moved_at`, `.host_pinned: bool`; wire keys `movingTo`, `movedFrom`, `movedAt`, `hostPinned` on both the RPC codec and the HTTP dict.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_transfer_columns.py
"""The four transfer columns reconcile onto SQLite, rehydrate into SessionRecord,
survive the RPC codec, and reach the HTTP dict."""
import asyncio
from datetime import datetime

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def test_columns_round_trip_through_rehydrate_codec_and_http(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"
    at = datetime(2026, 9, 14, 19, 52, 0)

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", engine_session_id="c",
                             status="idle", cwd="/w", host="rocky", moving_to="dobby",
                             moved_from="dobby", moved_at=at, host_pinned=True))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return orch._records["s1"]

    rec = asyncio.run(run())
    assert (rec.moving_to, rec.moved_from, rec.moved_at, rec.host_pinned) == ("dobby", "dobby", at, True)
    wire = rec.to_dict()
    assert wire["movingTo"] == "dobby" and wire["movedFrom"] == "dobby" and wire["hostPinned"] is True
    assert wire["movedAt"].startswith("2026-09-14T19:52:00")
    back = SessionRecord.from_dict(wire)
    assert (back.moving_to, back.moved_from, back.moved_at, back.host_pinned) == ("dobby", "dobby", at, True)
    from server.api.sessions import _record_to_dict
    d = _record_to_dict(rec)
    assert d["movingTo"] == "dobby" and d["movedFrom"] == "dobby" and d["hostPinned"] is True
    assert d["movedAt"].startswith("2026-09-14T19:52:00")


def test_defaults_are_null_and_unpinned(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'y.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s2", name="n", account_name="a", engine_session_id="c",
                             status="idle", cwd="/w"))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return orch._records["s2"]

    rec = asyncio.run(run())
    assert rec.moving_to is None and rec.moved_from is None and rec.moved_at is None
    assert rec.host_pinned is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_session_transfer_columns.py -v`
Expected: FAIL with `TypeError: 'moving_to' is an invalid keyword argument for Session`

- [ ] **Step 3: Add the columns, fields, codec keys and migration**

`server/db/models.py`, directly after the `host` column:

```python
    # Cross-host transfer (docs/superpowers/specs/2026-09-14-cross-host-session-transfer-
    # design.md). `moving_to` is the in-flight marker: the target host name while the
    # edge saga runs, NULL otherwise — set with a conditional UPDATE so two moves of one
    # session serialize. `moved_from`/`moved_at` record the last completed move (the
    # rebalancer's cooldown reads moved_at). `host_pinned` opts a session out of
    # automatic rebalancing. All nullable so init_db reconciles them on both dialects.
    moving_to: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    moved_from: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    moved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    host_pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
```

`server/sessions/types.py`, `SessionRecord` after `display_mode: str | None = None`:

```python
    moving_to: str | None = None      # target host while a cross-host transfer is in flight
    moved_from: str | None = None     # source host of the last completed transfer
    moved_at: datetime | None = None  # when that transfer completed (rebalance cooldown)
    host_pinned: bool = False         # opted out of automatic rebalancing
```

`to_dict`, after `"displayMode": self.display_mode,`:

```python
            "movingTo": self.moving_to,
            "movedFrom": self.moved_from,
            "movedAt": iso_utc(self.moved_at),
            "hostPinned": self.host_pinned,
```

`from_dict`, after `display_mode=d.get("displayMode"),`:

```python
            moving_to=d.get("movingTo"),
            moved_from=d.get("movedFrom"),
            moved_at=(datetime.fromisoformat(d["movedAt"]) if d.get("movedAt") else None),
            host_pinned=bool(d.get("hostPinned", False)),
```

`server/sessions/orchestrator.py` `_record_from_row`, after `display_mode=getattr(row, "display_mode", None),`:

```python
            moving_to=getattr(row, "moving_to", None),
            moved_from=getattr(row, "moved_from", None),
            moved_at=getattr(row, "moved_at", None),
            host_pinned=bool(getattr(row, "host_pinned", False) or False),
```

`server/api/sessions.py` `_record_to_dict`, after the `"host"` entry (import `iso_utc` is already in scope there):

```python
        # Cross-host transfer state (spec 2026-09-14). movingTo is non-null only while a
        # move is in flight; the card renders "moving to <host>…" from it.
        "movingTo": getattr(record, "moving_to", None),
        "movedFrom": getattr(record, "moved_from", None),
        "movedAt": iso_utc(getattr(record, "moved_at", None)),
        "hostPinned": bool(getattr(record, "host_pinned", False)),
```

`server/db/migrations/versions/0046_session_transfer.py`:

```python
"""session_transfer: moving_to, moved_from, moved_at, host_pinned on sessions

Revision ID: 0046_session_transfer
Revises: 0045_automation_ownership

Reviewable record (spec 2026-09-14-cross-host-session-transfer-design, Schema). Nothing
runs `alembic upgrade` at deploy time: init_db's reconcile adds the nullable columns.
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_session_transfer"
down_revision = "0045_automation_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("moving_to", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("moved_from", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("moved_at", sa.DateTime(), nullable=True))
    op.add_column("sessions", sa.Column("host_pinned", sa.Boolean(), nullable=True))


def downgrade() -> None:
    for col in ("host_pinned", "moved_at", "moved_from", "moving_to"):
        op.drop_column("sessions", col)
```

- [ ] **Step 4: Run to verify it passes, plus the neighbours**

Run: `$T server/tests/test_session_transfer_columns.py server/tests/test_orchestrator_rehydrate.py server/tests/test_schema_reconcile_coverage.py server/tests/test_migrations_chain.py -v` (if the last file does not exist, run `ls server/tests | grep -i migrat` and use what is there).
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/sessions/orchestrator.py server/api/sessions.py server/db/migrations/versions/0046_session_transfer.py server/tests/test_session_transfer_columns.py
printf 'feat(db): session transfer columns moving_to/moved_from/moved_at/host_pinned\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -q -F /tmp/c5.txt
```

---

### Task 6: `AgentCapabilities.cross_host_transfer`

**Files:**
- Modify: `server/sessions/engine.py:24-87` (add the field after `library_management`)
- Modify: `server/sessions/claude_driver.py:159-182` (set it true)
- Test: `server/tests/test_engine_capabilities_transfer.py`

**Interfaces:**
- Produces: `AgentCapabilities.cross_host_transfer: bool` (Claude true, Codex false via the default); wire key `crossHostTransfer` from `capabilities_to_wire`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_engine_capabilities_transfer.py
from server.codex.capabilities import CODEX_CAPABILITIES
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentCapabilities, capabilities_to_wire


def test_claude_declares_cross_host_transfer_and_codex_does_not():
    assert ClaudeAgentDriver.capabilities.cross_host_transfer is True
    assert CODEX_CAPABILITIES.cross_host_transfer is False
    assert AgentCapabilities().cross_host_transfer is False


def test_wire_key_is_camel_cased():
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["crossHostTransfer"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_engine_capabilities_transfer.py -v`
Expected: FAIL with `AttributeError: 'AgentCapabilities' object has no attribute 'cross_host_transfer'`

- [ ] **Step 3: Add the field**

`server/sessions/engine.py`, after `library_management: bool = False`:

```python
    #: A live session can be moved to ANOTHER fleet host and resumed there by
    #: conversation id (docs/superpowers/specs/2026-09-14-cross-host-session-
    #: transfer-design.md). Two prerequisites: the conversation history lives on
    #: the shared tree every host mounts (Claude: ~/.claude/projects over sshfs),
    #: and a resume needs nothing host-local beyond the worktree, which the
    #: transfer re-creates. Codex fails the first — its history lives inside a
    #: host-local app-server under CODEX_HOME — so it stays False.
    cross_host_transfer: bool = False
```

`server/sessions/claude_driver.py`, inside the `capabilities = AgentCapabilities(` call after `durable_tool_surface=True,`:

```python
        # Transcript on the shared ~/.claude tree + resume by conversation id: the
        # #1432 hand recovery already proved a Claude session resumes on the other
        # host at the same path.
        cross_host_transfer=True,
```

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_engine_capabilities_transfer.py server/tests/test_api_config.py -v` (the config route serializes every capability; if that test file has another name, `grep -rl capabilities_to_wire server/tests` and run those).
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/engine.py server/sessions/claude_driver.py server/tests/test_engine_capabilities_transfer.py
printf 'feat(engine): declare cross_host_transfer capability (Claude only)\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git commit -q -F /tmp/c6.txt
```

---

### Task 7: `transfer_policy.movable` (pure predicate)

**Files:**
- Create: `server/sessions/transfer_policy.py`
- Test: `server/tests/test_transfer_policy.py`

**Interfaces:**
- Consumes: `server.sessions.fleet.HostCandidate`, `_dispatchable`, `engine_rejection`; `server.sessions.status.MID_TURN_STATUSES`; `server.db.models.INACTIVE_SESSION_STATUSES`; `server.sessions.types.is_assistant_kind`; `server.sessions.display_policy.DISPLAY_NATIVE`; `ccswitch.worktrees.is_managed_path`.
- Produces: `Verdict(ok, reason, detail)`; `movable(record, *, target, candidates, capabilities, repo_root) -> Verdict`; `REASON_*` constants; `MOVABLE_REASONS` tuple for the API docs.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_transfer_policy.py
import pytest

from server.sessions.engine import AgentCapabilities, AgentEngine
from server.sessions.fleet import HostCandidate
from server.sessions.transfer_policy import movable
from server.sessions.types import SessionRecord

CAPS = AgentCapabilities(cross_host_transfer=True)
ROOT = "/home/tony/GitHub"


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="c", cols=80, rows=24,
                cwd=f"{ROOT}/.ccswitch/worktrees/viberpit/s1", host="dobby", status="idle",
                display_mode="virtual")
    base.update(kw)
    return SessionRecord(**base)


def _host(name, **kw):
    base = dict(name=name, enabled=True, healthy=True, weight=100, active_sessions=1,
                max_sessions=None, load_avg=1.0, mem_available_mb=8000)
    base.update(kw)
    return HostCandidate(**base)


HOSTS = [_host("dobby"), _host("rocky")]


def test_happy_path():
    v = movable(_rec(), target="rocky", candidates=HOSTS, capabilities=CAPS, repo_root=ROOT)
    assert v.ok and v.reason is None


@pytest.mark.parametrize("kw,reason", [
    (dict(engine=AgentEngine.CODEX), "engine"),
    (dict(kind="assistant"), "assistant"),
    (dict(display_mode="native"), "native_display"),
    (dict(status="in_progress"), "mid_turn"),
    (dict(status="condensing"), "mid_turn"),
    (dict(status="archived"), "inactive"),
    (dict(moving_to="rocky"), "transfer_in_flight"),
    (dict(cwd="/tmp/scratch"), "non_repo"),
    (dict(host="rocky"), "same_host"),
])
def test_record_refusals(kw, reason):
    caps = AgentCapabilities() if kw.get("engine") is AgentEngine.CODEX else CAPS
    v = movable(_rec(**kw), target="rocky", candidates=HOSTS, capabilities=caps, repo_root=ROOT)
    assert not v.ok and v.reason == reason


@pytest.mark.parametrize("hosts,reason", [
    ([_host("dobby")], "unknown_host"),
    ([_host("dobby"), _host("rocky", enabled=False)], "target_not_dispatchable"),
    ([_host("dobby"), _host("rocky", healthy=None)], "target_not_dispatchable"),
    ([_host("dobby"), _host("rocky", active_sessions=4, max_sessions=4)], "target_not_dispatchable"),
    ([_host("dobby"), _host("rocky", engines={})], "target_engine"),
])
def test_target_refusals(hosts, reason):
    v = movable(_rec(), target="rocky", candidates=hosts, capabilities=CAPS, repo_root=ROOT)
    assert not v.ok and v.reason == reason


def test_null_host_means_primary_so_moving_to_primary_is_same_host():
    v = movable(_rec(host=None), target="dobby", candidates=HOSTS, capabilities=CAPS,
                repo_root=ROOT, primary="dobby")
    assert not v.ok and v.reason == "same_host"
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_transfer_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.transfer_policy'`

- [ ] **Step 3: Implement**

```python
# server/sessions/transfer_policy.py
"""Is this session movable to that host? One pure answer shared by the API, the
Move dialog's `transferable` field and (Phase 3) the rebalancer, so the three can
never disagree (spec 2026-09-14-cross-host-session-transfer-design, "Movable
predicate")."""
from __future__ import annotations

from dataclasses import dataclass

from ccswitch.worktrees import is_managed_path
from server.db.models import INACTIVE_SESSION_STATUSES
from server.sessions.display_policy import DISPLAY_NATIVE
from server.sessions.engine import AgentCapabilities
from server.sessions.fleet import HostCandidate, _dispatchable, engine_rejection
from server.sessions.status import MID_TURN_STATUSES
from server.sessions.types import SessionRecord, is_assistant_kind

__all__ = ["Verdict", "movable", "MOVABLE_REASONS"]

REASON_ENGINE = "engine"
REASON_ASSISTANT = "assistant"
REASON_NATIVE_DISPLAY = "native_display"
REASON_MID_TURN = "mid_turn"
REASON_INACTIVE = "inactive"
REASON_IN_FLIGHT = "transfer_in_flight"
REASON_NON_REPO = "non_repo"
REASON_SAME_HOST = "same_host"
REASON_UNKNOWN_HOST = "unknown_host"
REASON_TARGET_DOWN = "target_not_dispatchable"
REASON_TARGET_ENGINE = "target_engine"

MOVABLE_REASONS: tuple[str, ...] = (
    REASON_ENGINE, REASON_ASSISTANT, REASON_NATIVE_DISPLAY, REASON_MID_TURN, REASON_INACTIVE,
    REASON_IN_FLIGHT, REASON_NON_REPO, REASON_SAME_HOST, REASON_UNKNOWN_HOST,
    REASON_TARGET_DOWN, REASON_TARGET_ENGINE,
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None
    detail: str | None = None


def _no(reason: str, detail: str | None = None) -> Verdict:
    return Verdict(False, reason, detail)


def movable(record: SessionRecord, *, target: str, candidates: list[HostCandidate],
            capabilities: AgentCapabilities, repo_root: str,
            primary: str | None = None) -> Verdict:
    """Every line must hold. Order is cheapest-first and record-before-target so the
    dialog can explain the session's own state even when no target is chosen yet.

    ``record.host is None`` means "the primary" (pre-fleet rows and the primary's own
    automation spawns), which is why *primary* is threaded in for the same-host check.
    """
    if not capabilities.cross_host_transfer:
        return _no(REASON_ENGINE, f"{record.engine.value} cannot be resumed on another host")
    if is_assistant_kind(getattr(record, "kind", None)):
        return _no(REASON_ASSISTANT, "assistant threads are pinned to the primary")
    if getattr(record, "display_mode", None) == DISPLAY_NATIVE:
        return _no(REASON_NATIVE_DISPLAY, "a native-display session is bound to its host's desktop")
    if record.status in MID_TURN_STATUSES:
        return _no(REASON_MID_TURN, "wait for the turn to finish or interrupt first")
    if record.status in INACTIVE_SESSION_STATUSES:
        return _no(REASON_INACTIVE, record.status)
    if getattr(record, "moving_to", None):
        return _no(REASON_IN_FLIGHT, f"already moving to {record.moving_to}")
    if not record.cwd or not is_managed_path(record.cwd, repo_root):
        return _no(REASON_NON_REPO, "only repo sessions with a managed worktree can move")
    current = record.host or primary
    if current == target:
        return _no(REASON_SAME_HOST, f"already on {target}")
    cand = next((c for c in candidates if c.name == target), None)
    if cand is None:
        return _no(REASON_UNKNOWN_HOST, target)
    if not _dispatchable(cand):
        return _no(REASON_TARGET_DOWN, f"{target} is disabled, unhealthy or at capacity")
    rejection = engine_rejection(cand, record.engine)
    if rejection is not None:
        return _no(REASON_TARGET_ENGINE, f"{target}: {rejection}")
    return Verdict(True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_transfer_policy.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add server/sessions/transfer_policy.py server/tests/test_transfer_policy.py
printf 'feat(sessions): pure movable() predicate for cross-host transfer\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c7.txt
git commit -q -F /tmp/c7.txt
```

---

### Task 8: Extract `_normalize_row` / `_adopt_row` from `rehydrate`

**Files:**
- Modify: `server/sessions/orchestrator.py:11350-11470` (`rehydrate`)
- Test: `server/tests/test_orchestrator_adopt_row.py`

**Interfaces:**
- Produces: `async def _normalize_row(self, row) -> tuple[SessionRecord, str | None, str | None]` (record, backfilled branch, healed status); `async def _adopt_row(self, row) -> SessionRecord` (normalizes, installs into `_records` replacing any mirror, persists backfill/heal immediately). `rehydrate` calls `_normalize_row` and keeps its batched persistence. This is the "shared rehydrate normalization (extracted, not copied)" #949 Task 18 asks for.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_orchestrator_adopt_row.py
"""_adopt_row: one row in, one normalized record installed — the unit rehydrate loops over."""
import asyncio

from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _row(sid):
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


def test_adopt_row_normalizes_status_and_installs(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", engine_session_id="c",
                             status="in_progress", cwd="/w", host="rocky"))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        rec = await orch._adopt_row(await _row("s1"))
        return rec, orch._records.get("s1")

    rec, installed = asyncio.run(run())
    assert installed is rec
    assert rec.status == "idle" and rec.interrupted_by_restart is True
    assert rec.interrupted_from_status == "in_progress"


def test_adopt_row_replaces_an_existing_mirror(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", engine_session_id="c",
                             status="idle", cwd="/w", host="rocky"))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        stale = orch._records["s1"]
        stale.name = "stale mirror"
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            row.name = "fresh"
        rec = await orch._adopt_row(await _row("s1"))
        return rec.name, orch._records["s1"].name

    assert asyncio.run(run()) == ("fresh", "fresh")


def test_rehydrate_still_batches_and_matches_adopt(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", engine_session_id="c",
                             status="waiting_input", cwd="/w", pid=None))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        n = await orch.rehydrate()
        return n, orch._records["s1"].status, (await _row("s1")).status

    assert asyncio.run(run()) == (1, "idle", "idle")   # healed and persisted, as before
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_orchestrator_adopt_row.py -v`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute '_adopt_row'`

- [ ] **Step 3: Extract**

In `server/sessions/orchestrator.py`, replace the body of the `for row in rows:` loop inside `rehydrate` (from `if row.id in self._records: continue` through the `waiting_external` heal block) with:

```python
        for row in rows:
            if row.id in self._records:
                continue
            record, backfilled_branch, healed_status = await self._normalize_row(row)
            self._records[row.id] = record
            added += 1
            if backfilled_branch is not None:
                backfilled[row.id] = backfilled_branch
            if healed_status is not None:
                healed[row.id] = healed_status
            log_event(log, logging.INFO, "session.reattach",
                      session_id=row.id,
                      **{"from": row.status, "to": record.status, "reason": "rehydrate"})
```

and add these two methods directly above `rehydrate`, moving the existing comments (the #434 / #789 / #177 explanations) with the code they explain:

```python
    async def _normalize_row(self, row) -> tuple[SessionRecord, str | None, str | None]:
        """Build the in-memory record for one non-terminal DB row the way a daemon
        restart does. Returns ``(record, backfilled_branch, healed_status)`` so the
        caller decides how to persist the two side effects: ``rehydrate`` batches them,
        ``_adopt_row`` writes them at once. Shared by restart, cross-host transfer and
        (spec 002 Phase 4) dead-host adoption, so normalization has one definition."""
        was_interrupted = row.status in _RESTART_TRANSIENT_STATUSES
        status = "idle" if was_interrupted else row.status
        healed: str | None = None
        if was_interrupted and row.status == "waiting_input":
            pid = getattr(row, "pid", None)
            if pid is None or not _adopt._pid_alive(pid):
                healed = "idle"
        branch = getattr(row, "branch", None)
        backfilled: str | None = None
        if branch is None and row.cwd and worktrees.is_git_repo(row.cwd):
            derived = await asyncio.to_thread(worktrees.current_branch, row.cwd)
            if derived:
                branch = derived
                backfilled = derived
        record = self._record_from_row(
            row, status=status, branch=branch,
            interrupted_by_restart=was_interrupted,
            interrupted_from_status=(row.status if was_interrupted else None))
        if status == "waiting_external" and not await self._session_is_parked(row.id):
            record.status = "done"
            healed = "done"
            log_event(log, logging.INFO, "session.unpark", session_id=row.id,
                      **{"from": "waiting_external", "to": "done", "reason": "bg_tasks_cleared"})
        return record, backfilled, healed

    async def _adopt_row(self, row) -> SessionRecord:
        """Install one DB row as this daemon's record, replacing any dashboard mirror
        the primary may hold for it, and persist normalization side effects now."""
        record, backfilled, healed = await self._normalize_row(row)
        self._records[row.id] = record
        if backfilled is not None or healed is not None:
            from sqlalchemy import update as sa_update
            values: dict = {}
            if backfilled is not None:
                values["branch"] = backfilled
            if healed is not None:
                values["status"] = healed
            async with db_session() as db:
                await db.execute(sa_update(DbSession).where(DbSession.id == row.id).values(**values))
        log_event(log, logging.INFO, "session.reattach", session_id=row.id,
                  **{"from": row.status, "to": record.status, "reason": "adopt_row"})
        return record
```

Note the `_session_is_parked` heal in the original ran after `self._records[row.id]` was set; check its body (`grep -n "def _session_is_parked" server/sessions/orchestrator.py`) reads only `self._bg_tasks`/engine state, not `_records`, before moving it. If it does read `_records`, keep the heal in the caller instead and return `healed=None` from `_normalize_row` for that case.

- [ ] **Step 4: Run to verify it passes, with every rehydrate test**

Run: `$T server/tests/test_orchestrator_adopt_row.py server/tests/test_orchestrator_rehydrate.py server/tests/test_api_rehydrate_list.py server/tests/test_app_rehydrate_on_startup.py server/tests/test_rehydrate_persists_model_pin.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_adopt_row.py
printf 'refactor(sessions): extract _normalize_row/_adopt_row from rehydrate\n\nOne normalization for restart, transfer and (#949) adoption.\n\nRefs #1696, #949\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c8.txt
git commit -q -F /tmp/c8.txt
```

---

### Task 9: `SessionOrchestrator.transfer_export`

**Files:**
- Modify: `server/sessions/orchestrator.py` (new method next to `reload_session`, ~line 13131; new import `from ccswitch import worktree_transfer`)
- Test: `server/tests/test_transfer_export.py`

**Interfaces:**
- Consumes: `self._swapping`, `self._teardown_driver_state(sid) -> AgentDriver | None`, `bounded_wait`, `_DRIVER_CONTROL_TIMEOUT_S`, `self._release_session_lease(sid)`, `self._records`, `self._worktrees`, `self._last_prompt`, `self._engine_supports(engine, cap)`, `MID_TURN_STATUSES` (already imported as `_status_mod.MID_TURN_STATUSES`), `worktree_transfer.inspect/export`.
- Produces: `async def transfer_export(self, session_id: str, *, carry_dirty: bool = False) -> dict` returning `{"ok": True, "manifest": {...}}` or `{"ok": False, "reason": <code>, "detail": str}`. Manifest keys: `sessionId`, `engineSessionId`, `cwd`, `permissionMode`, `model`, `status`, `bundle` (from `Bundle.to_dict()`).

Ordering inside the method is the whole design: the tree is snapshotted and pushed **before** the driver is closed. At a turn boundary the CLI is not writing files, and this order means a push failure leaves the session exactly as it was (the WIP commit is undone by `export` itself), so no restore-resume is needed on the source.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_transfer_export.py
"""transfer_export: refuse mid-turn / dirty-without-consent; on success push, close,
release the lease and forget the session; on push failure change nothing."""
import asyncio
from unittest import mock

import pytest

from ccswitch import worktree_transfer as wt
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


class _Driver:
    def __init__(self):
        self.closed = False; self.interrupted = False
        self.engine_session_id = "csid"; self.cwd = "/w"; self.mode = "interactive"; self.identity = "u"
    async def interrupt(self): self.interrupted = True
    async def aclose(self): self.closed = True


def _rec(status="idle"):
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="csid", cols=80, rows=24, cwd="/w", status=status,
                         permission_mode="full-auto", model="opus")


def _orch(rec, *, with_driver=True):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._drivers = {rec.id: _Driver()} if with_driver else {}
    o._swapping = {}
    o._worktrees = {rec.id: object()}
    o._last_prompt = {rec.id: "last"}
    o._clock = lambda: 0.0
    o.released = []
    o._engine_supports = lambda engine, cap: cap == "cross_host_transfer"
    async def teardown(sid): return o._drivers.pop(sid, None)
    async def release(sid): o.released.append(sid)
    o._teardown_driver_state = teardown
    o._release_session_lease = release
    return o


BUNDLE = wt.Bundle("s1", "br", "h", "h", False, {})


@pytest.mark.asyncio
async def test_mid_turn_is_refused_untouched():
    rec = _rec("in_progress"); o = _orch(rec)
    out = await o.transfer_export("s1")
    assert out == {"ok": False, "reason": "mid_turn", "detail": "in_progress"}
    assert "s1" in o._records and "s1" in o._drivers


@pytest.mark.asyncio
async def test_unknown_session():
    o = _orch(_rec()); o._records.clear()
    assert (await o.transfer_export("s1"))["reason"] == "not_found"


@pytest.mark.asyncio
async def test_engine_without_capability_is_refused():
    o = _orch(_rec()); o._engine_supports = lambda e, c: False
    assert (await o.transfer_export("s1"))["reason"] == "engine"


@pytest.mark.asyncio
async def test_dirty_without_consent_refused_before_driver_is_touched():
    o = _orch(_rec())
    state = wt.WorktreeState(True, 3, "br", "h", ())
    with mock.patch.object(wt, "inspect", return_value=state), \
         mock.patch.object(wt, "export", side_effect=AssertionError("must not export")):
        out = await o.transfer_export("s1", carry_dirty=False)
    assert out["reason"] == "dirty_worktree" and out["detail"] == "3 uncommitted change(s)"
    assert "s1" in o._drivers and not o._drivers["s1"].closed


@pytest.mark.asyncio
async def test_success_pushes_then_closes_releases_and_forgets():
    rec = _rec("done"); o = _orch(rec); drv = o._drivers["s1"]
    order = []
    def fake_export(cwd, sid, *, carry_dirty):
        order.append("export"); assert not drv.closed; return BUNDLE
    with mock.patch.object(wt, "inspect", return_value=wt.WorktreeState(False, 0, "br", "h", ())), \
         mock.patch.object(wt, "export", side_effect=fake_export):
        out = await o.transfer_export("s1")
    assert out["ok"] is True
    m = out["manifest"]
    assert m["sessionId"] == "s1" and m["engineSessionId"] == "csid" and m["cwd"] == "/w"
    assert m["permissionMode"] == "full-auto" and m["model"] == "opus" and m["status"] == "done"
    assert m["bundle"] == BUNDLE.to_dict()
    assert drv.interrupted and drv.closed
    assert o.released == ["s1"]
    assert "s1" not in o._records and "s1" not in o._drivers and "s1" not in o._worktrees
    assert "s1" not in o._last_prompt and "s1" not in o._swapping


@pytest.mark.asyncio
async def test_driverless_session_exports_from_record():
    o = _orch(_rec("idle"), with_driver=False)
    with mock.patch.object(wt, "inspect", return_value=wt.WorktreeState(False, 0, "br", "h", ())), \
         mock.patch.object(wt, "export", return_value=BUNDLE):
        out = await o.transfer_export("s1")
    assert out["ok"] is True and o.released == ["s1"]


@pytest.mark.asyncio
async def test_push_failure_leaves_session_in_place():
    o = _orch(_rec())
    with mock.patch.object(wt, "inspect", return_value=wt.WorktreeState(False, 0, "br", "h", ())), \
         mock.patch.object(wt, "export", side_effect=wt.TransferError("push_failed", "boom")):
        out = await o.transfer_export("s1")
    assert out == {"ok": False, "reason": "push_failed", "detail": "boom"}
    assert "s1" in o._records and "s1" in o._drivers and not o._drivers["s1"].closed
    assert o.released == []


@pytest.mark.asyncio
async def test_waits_for_an_in_flight_swap_then_rechecks():
    o = _orch(_rec("idle"))
    fut = asyncio.get_running_loop().create_future(); o._swapping["s1"] = fut
    async def flip():
        await asyncio.sleep(0.01); o._records["s1"].status = "in_progress"; fut.set_result(None)
    asyncio.create_task(flip())
    out = await o.transfer_export("s1")
    assert out["reason"] == "mid_turn"
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_transfer_export.py -v`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute 'transfer_export'`

- [ ] **Step 3: Implement**

Add `from ccswitch import worktree_transfer` to the orchestrator's `ccswitch` imports, then add after `reload_session`:

```python
    # ------------------------------------------------------------------ transfer
    # Cross-host transfer (docs/superpowers/specs/2026-09-14-cross-host-session-
    # transfer-design.md). The edge drives the saga; this daemon only ever plays one
    # side of it per call: export (we are the source) or import/finalize (we are the
    # target / the source after a successful import).

    async def transfer_export(self, session_id: str, *, carry_dirty: bool = False) -> dict:
        """Hand the session off: snapshot + push the worktree, close the driver, release
        the lease, forget the record. Returns the manifest the target imports.

        Push BEFORE close, on purpose: at a turn boundary the CLI is not writing files,
        so the snapshot is complete, and a push failure then changes nothing here — the
        session keeps running on this host and the caller just reports the refusal.
        """
        record = self._records.get(session_id)
        if record is None:
            return {"ok": False, "reason": "not_found", "detail": session_id}
        if not self._engine_supports(record.engine, "cross_host_transfer"):
            return {"ok": False, "reason": "engine", "detail": record.engine.value}
        prior_swap = self._swapping.get(session_id)
        if prior_swap is not None:
            with contextlib.suppress(Exception):
                await prior_swap
            record = self._records.get(session_id, record)
        if record.status in _status_mod.MID_TURN_STATUSES:
            return {"ok": False, "reason": "mid_turn", "detail": record.status}
        swap_done: asyncio.Future = asyncio.get_running_loop().create_future()
        self._swapping[session_id] = swap_done
        try:
            cwd = record.cwd
            try:
                state = await asyncio.to_thread(worktree_transfer.inspect, cwd)
                if state.dirty and not carry_dirty:
                    return {"ok": False, "reason": "dirty_worktree",
                            "detail": f"{state.dirty_files} uncommitted change(s)"}
                bundle = await asyncio.to_thread(
                    worktree_transfer.export, cwd, session_id, carry_dirty=carry_dirty)
            except worktree_transfer.TransferError as exc:
                log.warning("transfer.export refused session=%s code=%s: %s",
                            session_id, exc.code, exc.detail)
                return {"ok": False, "reason": exc.code, "detail": exc.detail}
            driver = await self._teardown_driver_state(session_id)
            if driver is not None:
                # Same bounded courtesy as _reassign_locked: a wedged CLI must not
                # block the hand-off; aclose() reaps it regardless (#846).
                await bounded_wait(driver.interrupt(), timeout=_DRIVER_CONTROL_TIMEOUT_S,
                                   phase=f"transfer.interrupt:{session_id}")
                await driver.aclose()
            await self._release_session_lease(session_id)
            self._records.pop(session_id, None)
            self._worktrees.pop(session_id, None)
            self._last_prompt.pop(session_id, None)
            log_event(log, logging.INFO, "session.transfer_export", session_id=session_id,
                      wip=bundle.wip_commit, branches=len(bundle.branches))
            return {"ok": True, "manifest": {
                "sessionId": session_id,
                "engineSessionId": record.engine_session_id,
                "cwd": cwd,
                "permissionMode": record.permission_mode,
                "model": record.model,
                "status": record.status,
                "bundle": bundle.to_dict(),
            }}
        finally:
            if self._swapping.get(session_id) is swap_done:
                del self._swapping[session_id]
            if not swap_done.done():
                swap_done.set_result(None)
```

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_transfer_export.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_transfer_export.py
printf 'feat(sessions): transfer_export hands a session off its host\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c9.txt
git commit -q -F /tmp/c9.txt
```

---

### Task 10: `transfer_import`, `transfer_finalize` and the moved-host nudge

**Files:**
- Modify: `server/sessions/orchestrator.py` (after `transfer_export`; module-level `moved_host_nudge` next to `RESUME_NUDGE` at line 197)
- Test: `server/tests/test_transfer_import.py`

**Interfaces:**
- Consumes: `self._claim_session(sid, why=)`, `self._release_session_lease`, `self._adopt_row(row)`, `self._effective_repo_root()`, `self._worktrees`, `self.ensure_driver(sid)`, `self.send_prompt(sid, text)`, `worktree_transfer.import_/finalize/delete_transfer_refs`, `gitrepos.clone_path(repo_root, name_with_owner)`.
- Produces: `moved_host_nudge(from_host, to_host, at, *, wip) -> str`; `async def transfer_import(self, session_id, manifest, *, from_host: str | None = None) -> dict` → `{"ok": True}` / `{"ok": False, "reason": ...}` with reasons `lease_held`, `not_found`, `no_repo`, any `TransferError.code`, `resume_failed`; `async def transfer_finalize(self, session_id, manifest) -> dict` → `{"ok": bool, "removed": bool}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_transfer_import.py
"""transfer_import: claim lease → adopt row → recreate worktree → resume → nudge;
each failure releases what it took. transfer_finalize is source-side cleanup."""
import asyncio
from datetime import datetime
from unittest import mock

import pytest

from ccswitch import worktree_transfer as wt
from ccswitch.worktrees import WorktreeInfo
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator, moved_host_nudge
from server.sessions.types import SessionRecord

MANIFEST = {"sessionId": "s1", "engineSessionId": "csid", "cwd": "/root/.ccswitch/worktrees/r/s1",
            "permissionMode": "full-auto", "model": None, "status": "idle",
            "bundle": wt.Bundle("s1", "br", "h", "h", False, {}).to_dict()}


def _orch():
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {}; o._drivers = {}; o._worktrees = {}; o._swapping = {}
    o.calls = []
    async def claim(sid, *, why): o.calls.append(("claim", why)); return o.claim_ok
    async def release(sid): o.calls.append(("release", sid))
    async def adopt(row):
        rec = SessionRecord(id=row.id, name=row.name, account=row.account_name,
                            engine=AgentEngine.CLAUDE_CODE, engine_session_id=row.engine_session_id or "",
                            cols=80, rows=24, cwd=row.cwd, status=row.status, repo=row.repo)
        o._records[row.id] = rec; o.calls.append(("adopt", row.id)); return rec
    async def repo_root(): return "/root"
    async def ensure(sid, identity="system"):
        o.calls.append(("ensure", sid)); return o.driver
    async def send(sid, text, *a, **k): o.calls.append(("prompt", text)); return "queued"
    o.claim_ok = True; o.driver = object()
    o._claim_session = claim; o._release_session_lease = release; o._adopt_row = adopt
    o._effective_repo_root = repo_root; o.ensure_driver = ensure; o.send_prompt = send
    return o


async def _seed(tmp_path, status="idle", repo="o/r"):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="a", engine_session_id="csid",
                         status=status, cwd=MANIFEST["cwd"], repo=repo, host="rocky"))


@pytest.mark.asyncio
async def test_import_success_resumes_and_nudges(tmp_path):
    await _seed(tmp_path); o = _orch()
    info = WorktreeInfo(MANIFEST["cwd"], "br", is_child=False, repo="/root/.ccswitch/clones/o/r")
    with mock.patch.object(wt, "import_", return_value=info) as imp, \
         mock.patch.object(wt, "delete_transfer_refs") as rm, \
         mock.patch("os.path.isdir", return_value=True):
        out = await o.transfer_import("s1", MANIFEST, from_host="dobby")
    assert out == {"ok": True}
    assert [c[0] for c in o.calls] == ["claim", "adopt", "ensure", "prompt"]
    assert o._worktrees["s1"] is info
    assert imp.call_args.args[1] == "/root/.ccswitch/clones/o/r"
    rm.assert_called_once_with("/root/.ccswitch/clones/o/r", "s1")
    assert "moved from **dobby**" in o.calls[-1][1]


@pytest.mark.asyncio
async def test_complete_session_is_not_nudged(tmp_path):
    await _seed(tmp_path, status="complete"); o = _orch()
    info = WorktreeInfo(MANIFEST["cwd"], "br", is_child=False, repo="/x")
    m = dict(MANIFEST, status="complete")
    with mock.patch.object(wt, "import_", return_value=info), \
         mock.patch.object(wt, "delete_transfer_refs"), mock.patch("os.path.isdir", return_value=True):
        assert (await o.transfer_import("s1", m))["ok"] is True
    assert "prompt" not in [c[0] for c in o.calls]


@pytest.mark.asyncio
async def test_lease_held_refuses(tmp_path):
    await _seed(tmp_path); o = _orch(); o.claim_ok = False
    assert await o.transfer_import("s1", MANIFEST) == {"ok": False, "reason": "lease_held"}
    assert o.calls == [("claim", "transfer_import")]


@pytest.mark.asyncio
async def test_missing_row_releases(tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"); o = _orch()
    out = await o.transfer_import("s1", MANIFEST)
    assert out["reason"] == "not_found" and ("release", "s1") in o.calls


@pytest.mark.asyncio
async def test_worktree_failure_releases_and_forgets(tmp_path):
    await _seed(tmp_path); o = _orch()
    with mock.patch.object(wt, "import_", side_effect=wt.TransferError("branch_busy_on_target", "x")), \
         mock.patch("os.path.isdir", return_value=True):
        out = await o.transfer_import("s1", MANIFEST)
    assert out == {"ok": False, "reason": "branch_busy_on_target", "detail": "x"}
    assert "s1" not in o._records and ("release", "s1") in o.calls


@pytest.mark.asyncio
async def test_resume_failure_releases_and_forgets(tmp_path):
    await _seed(tmp_path); o = _orch(); o.driver = None
    info = WorktreeInfo(MANIFEST["cwd"], "br", is_child=False, repo="/x")
    with mock.patch.object(wt, "import_", return_value=info), mock.patch("os.path.isdir", return_value=True):
        out = await o.transfer_import("s1", MANIFEST)
    assert out["reason"] == "resume_failed"
    assert "s1" not in o._records and ("release", "s1") in o.calls


@pytest.mark.asyncio
async def test_finalize_delegates(tmp_path):
    o = _orch()
    with mock.patch.object(wt, "finalize", return_value=True) as fin:
        assert await o.transfer_finalize("s1", MANIFEST) == {"ok": True, "removed": True}
    assert fin.call_args.args[1] == MANIFEST["cwd"]
    with mock.patch.object(wt, "finalize", side_effect=RuntimeError("disk")):
        assert await o.transfer_finalize("s1", MANIFEST) == {"ok": False, "removed": False}


def test_nudge_text():
    t = moved_host_nudge("dobby", "rocky", datetime(2026, 9, 14, 19, 52), wip=True)
    assert t.startswith("This session was moved from **dobby** to **rocky** at 2026-09-14 19:52 UTC.")
    assert "carried over as unstaged edits" in t and "git status" in t
    assert "carried over" not in moved_host_nudge("a", "b", datetime(2026, 1, 1), wip=False)
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_transfer_import.py -v`
Expected: FAIL with `ImportError: cannot import name 'moved_host_nudge'`

- [ ] **Step 3: Implement**

Next to `RESUME_NUDGE` (module level):

```python
def moved_host_nudge(from_host: str, to_host: str, at: datetime, *, wip: bool) -> str:
    """The agent's first message after a cross-host transfer (spec 002 Task 19 wording).
    Delivered as a prompt so the agent re-reads its git state before acting."""
    carried = (" Uncommitted changes were carried over as unstaged edits." if wip
               else " The tree was clean.")
    return (f"This session was moved from **{from_host}** to **{to_host}** at "
            f"{at.strftime('%Y-%m-%d %H:%M')} UTC. Your worktree was re-created at the same "
            f"path.{carried} Run `git status` before continuing. Anything left unpushed on "
            f"{from_host} that was not on your current branch or a branch you checked out "
            "here may not be present.")


#: Statuses whose objective is finished: a moved session in one of these gets no nudge
#: (a prompt would start a turn on a session nobody is waiting on).
_NO_NUDGE_STATUSES = frozenset({"complete"})
```

After `transfer_export` in the class:

```python
    async def transfer_import(self, session_id: str, manifest: dict, *,
                              from_host: str | None = None) -> dict:
        """Take a session over from *manifest*: lease → row → worktree → resume → nudge.
        Every failure releases the lease and drops the record so this daemon holds
        nothing for a session it is not running."""
        if not await self._claim_session(session_id, why="transfer_import"):
            return {"ok": False, "reason": "lease_held"}
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == session_id))).scalar_one_or_none()
        if row is None:
            await self._release_session_lease(session_id)
            return {"ok": False, "reason": "not_found"}
        record = await self._adopt_row(row)

        async def _abandon(reason: str, detail: str = "") -> dict:
            self._records.pop(session_id, None)
            self._worktrees.pop(session_id, None)
            await self._release_session_lease(session_id)
            log.warning("transfer.import failed session=%s reason=%s %s", session_id, reason, detail)
            return {"ok": False, "reason": reason, "detail": detail}

        bundle = worktree_transfer.Bundle.from_dict(manifest["bundle"])
        repo_root = await self._effective_repo_root()
        repo_path = self._clone_path_for(record, repo_root)
        if repo_path is None:
            return await _abandon("no_repo", "session has no repo to clone from")
        try:
            info = await asyncio.to_thread(worktree_transfer.import_, bundle, repo_path, repo_root)
        except worktree_transfer.TransferError as exc:
            return await _abandon(exc.code, exc.detail)
        self._worktrees[session_id] = info
        record.cwd = info.path
        try:
            driver = await self.ensure_driver(session_id)
        except Exception as exc:  # noqa: BLE001 — a failed resume must land as a reason
            return await _abandon("resume_failed", str(exc))
        if driver is None:
            return await _abandon("resume_failed", "ensure_driver returned no driver")
        with contextlib.suppress(Exception):
            await asyncio.to_thread(worktree_transfer.delete_transfer_refs, repo_path, session_id)
        if manifest.get("status") not in _NO_NUDGE_STATUSES:
            await self.send_prompt(session_id, moved_host_nudge(
                from_host or "another host", self_host_name(), datetime.utcnow(),
                wip=bundle.wip_commit))
        log_event(log, logging.INFO, "session.transfer_import", session_id=session_id,
                  **{"from": from_host, "wip": bundle.wip_commit})
        return {"ok": True}

    def _clone_path_for(self, record: SessionRecord, repo_root: str) -> str | None:
        """The clone the target re-creates the worktree from. Both hosts keep the same
        layout, so the source's clone path is tried first; the canonical clone path
        for the session's repo is the fallback."""
        candidates = []
        if record.cwd:
            candidates.append(os.path.dirname(os.path.dirname(record.cwd)))  # <root>/.ccswitch/worktrees/<repo>
        if record.repo:
            candidates.append(str(gitrepos.clone_path(repo_root, record.repo)))
        for c in candidates:
            if os.path.isdir(os.path.join(c, ".git")):
                return c
        return candidates[-1] if record.repo else None

    async def transfer_finalize(self, session_id: str, manifest: dict) -> dict:
        """Source-side cleanup once the target confirmed its import."""
        bundle = worktree_transfer.Bundle.from_dict(manifest["bundle"])
        try:
            removed = await asyncio.to_thread(worktree_transfer.finalize, bundle, manifest["cwd"])
        except Exception:  # noqa: BLE001 — cleanup never fails the transfer
            log.exception("transfer.finalize failed session=%s", session_id)
            return {"ok": False, "removed": False}
        return {"ok": True, "removed": removed}
```

`_clone_path_for`'s first candidate is wrong on purpose in the test above (a worktree's grandparent is not a clone); the test mocks `os.path.isdir` True so the first candidate wins only when it is a real clone. Replace that first candidate with the real derivation: `worktree_transfer._repo_top(record.cwd)` wrapped in `try/except TransferError` when the cwd exists on this host, else skip. Write it as:

```python
        candidates = []
        if record.cwd and os.path.isdir(record.cwd):
            with contextlib.suppress(worktree_transfer.TransferError):
                candidates.append(worktree_transfer._repo_top(record.cwd))
        if record.repo:
            candidates.append(str(gitrepos.clone_path(repo_root, record.repo)))
        return candidates[0] if candidates else None
```

and change the success test's `isdir` patch to `mock.patch("os.path.isdir", return_value=False)` so the clone path fallback is what is asserted. `gitrepos` is already imported in the orchestrator as `from ccswitch import gitrepos` (check with `grep -n "^from ccswitch import" server/sessions/orchestrator.py`; add it if not).

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_transfer_import.py server/tests/test_transfer_export.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_transfer_import.py
printf 'feat(sessions): transfer_import/transfer_finalize and the moved-host nudge\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c10.txt
git commit -q -F /tmp/c10.txt
```

---

### Task 11: The three RPCs on the wire

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py:314-354` (`_SIMPLE`)
- Modify: `server/sessions/remote.py:763` (after `rotate_to_best`)
- Modify: `server/sessions/facade.py:167` (after `rotate_to_best`)
- Test: `server/tests/test_transfer_rpc_surface.py`

**Interfaces:**
- Produces: `RemoteOrchestrator.transfer_export(session_id, *, carry_dirty=False) -> dict`, `.transfer_import(session_id, manifest, *, from_host=None) -> dict`, `.transfer_finalize(session_id, manifest) -> dict`; same names on the in-process facade.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_transfer_rpc_surface.py
import pytest

from server.ipc import orchestrator_rpc
from server.sessions.remote import RemoteOrchestrator


class _Client:
    def __init__(self): self.calls = []
    async def call(self, method, **params): self.calls.append((method, params)); return {"ok": True}


def test_simple_allowlist_carries_the_three_transfer_rpcs():
    src = open(orchestrator_rpc.__file__).read()
    for name in ("transfer_export", "transfer_import", "transfer_finalize"):
        assert f'"{name}"' in src


@pytest.mark.asyncio
async def test_remote_mirrors_forward_keyword_args():
    c = _Client(); r = RemoteOrchestrator(c)
    await r.transfer_export("s1", carry_dirty=True)
    await r.transfer_import("s1", {"bundle": {}}, from_host="dobby")
    await r.transfer_finalize("s1", {"bundle": {}})
    assert c.calls == [
        ("transfer_export", {"session_id": "s1", "carry_dirty": True}),
        ("transfer_import", {"session_id": "s1", "manifest": {"bundle": {}}, "from_host": "dobby"}),
        ("transfer_finalize", {"session_id": "s1", "manifest": {"bundle": {}}}),
    ]


@pytest.mark.asyncio
async def test_facade_passes_through():
    from server.sessions.facade import InProcessOrchestratorAdapter
    class _O:
        async def transfer_export(self, sid, *, carry_dirty=False): return {"ok": True, "sid": sid}
        async def transfer_import(self, sid, manifest, *, from_host=None): return {"from": from_host}
        async def transfer_finalize(self, sid, manifest): return {"ok": True}
    f = InProcessOrchestratorAdapter(_O())
    assert (await f.transfer_export("s1"))["sid"] == "s1"
    assert (await f.transfer_import("s1", {}, from_host="x"))["from"] == "x"
    assert (await f.transfer_finalize("s1", {}))["ok"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_transfer_rpc_surface.py -v`
Expected: FAIL (allowlist assertion; `AttributeError: 'RemoteOrchestrator' object has no attribute 'transfer_export'`)

- [ ] **Step 3: Implement**

`server/ipc/orchestrator_rpc.py`, inside `_SIMPLE` after `"list_adoptable_candidates", "migrate_archive_home", "cloned_repos",`:

```python
        # Cross-host transfer (spec 2026-09-14): dict in, dict out. The EDGE picks the
        # host for each — export on the source, import/finalize on the target — so
        # none of these is in FleetOrchestrator._PER_SESSION.
        "transfer_export", "transfer_import", "transfer_finalize",
```

`server/sessions/remote.py`, after `rotate_to_best`:

```python
    async def transfer_export(self, session_id: str, *, carry_dirty: bool = False) -> dict:
        """Source side of a cross-host transfer: returns {"ok", "manifest"|"reason"}."""
        return await self._client.call(
            "transfer_export", session_id=session_id, carry_dirty=carry_dirty)

    async def transfer_import(self, session_id: str, manifest: dict, *,
                              from_host: str | None = None) -> dict:
        """Target side: take the session over from *manifest*."""
        return await self._client.call(
            "transfer_import", session_id=session_id, manifest=manifest, from_host=from_host)

    async def transfer_finalize(self, session_id: str, manifest: dict) -> dict:
        """Source-side cleanup after the target confirmed its import."""
        return await self._client.call(
            "transfer_finalize", session_id=session_id, manifest=manifest)
```

`server/sessions/facade.py`, after `rotate_to_best`:

```python
    async def transfer_export(self, *a, **k): return await _maybe_await(self._orch.transfer_export(*a, **k))
    async def transfer_import(self, *a, **k): return await _maybe_await(self._orch.transfer_import(*a, **k))
    async def transfer_finalize(self, *a, **k): return await _maybe_await(self._orch.transfer_finalize(*a, **k))
```

If `InProcessOrchestratorAdapter` is not the facade class name, use the one `grep -n "^class" server/sessions/facade.py` prints and fix the test import.

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_transfer_rpc_surface.py server/tests/test_fleet_orchestrator.py -v`
Expected: all passed (the `_PER_SESSION` completeness test must still pass; the three names are deliberately absent from that set).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/sessions/remote.py server/sessions/facade.py server/tests/test_transfer_rpc_surface.py
printf 'feat(ipc): transfer_export/import/finalize RPCs\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c11.txt
git commit -q -F /tmp/c11.txt
```

---

### Task 12: `FleetOrchestrator` move markers and stale-route self-heal

**Files:**
- Modify: `server/sessions/fleet_orchestrator.py:166-222` (`__init__`, `_host_for`, `_make_per_session`)
- Test: `server/tests/test_fleet_transfer_routing.py`

**Interfaces:**
- Produces: `class TransferInFlight(Exception)` with `.session_id`, `.target`; `FleetOrchestrator.begin_move(session_id, target)`, `.end_move(session_id)`, `.moving_to(session_id) -> str | None`, `.note_host(session_id, host)`; per-session calls raise `TransferInFlight` while a move is marked; a `DaemonError` whose `err_type == "KeyError"` and message starts with `"No session with id"` drops the cache entry, re-reads the row and retries once on the new host.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_fleet_transfer_routing.py
import pytest

from server.ipc.client import DaemonError
from server.sessions.fleet_orchestrator import FleetOrchestrator, TransferInFlight


class _Orch:
    def __init__(self, name, known=()):
        self.name = name; self.known = set(known); self.calls = []
    async def get_session(self, sid):
        self.calls.append(sid)
        if sid not in self.known:
            raise DaemonError("KeyError", f"No session with id {sid!r}")
        return {"host": self.name}


class _Router:
    def __init__(self, orchs): self._o = orchs
    def get(self, name): return self._o[name]
    def names(self): return sorted(self._o)


def _fleet(dobby, rocky):
    return FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")


@pytest.mark.asyncio
async def test_begin_move_blocks_per_session_calls_and_end_move_unblocks(monkeypatch):
    dobby, rocky = _Orch("dobby", {"s1"}), _Orch("rocky")
    fo = _fleet(dobby, rocky); fo.note_host("s1", "dobby")
    fo.begin_move("s1", "rocky")
    assert fo.moving_to("s1") == "rocky"
    with pytest.raises(TransferInFlight) as exc:
        await fo.get_session("s1")
    assert exc.value.target == "rocky" and dobby.calls == []
    fo.end_move("s1"); fo.note_host("s1", "dobby")
    assert await fo.get_session("s1") == {"host": "dobby"}


@pytest.mark.asyncio
async def test_stale_route_self_heals_on_no_session(monkeypatch):
    dobby, rocky = _Orch("dobby"), _Orch("rocky", {"s1"})
    fo = _fleet(dobby, rocky); fo.note_host("s1", "dobby")   # stale: the row now says rocky
    async def db_host(sid): return "rocky"
    monkeypatch.setattr(fo, "_read_host", db_host)
    assert await fo.get_session("s1") == {"host": "rocky"}
    assert dobby.calls == ["s1"] and rocky.calls == ["s1"]
    assert fo._host_cache["s1"] == "rocky"


@pytest.mark.asyncio
async def test_no_session_on_the_true_host_propagates(monkeypatch):
    dobby, rocky = _Orch("dobby"), _Orch("rocky")
    fo = _fleet(dobby, rocky); fo.note_host("s1", "dobby")
    async def db_host(sid): return "dobby"
    monkeypatch.setattr(fo, "_read_host", db_host)
    with pytest.raises(DaemonError):
        await fo.get_session("s1")
    assert rocky.calls == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_fleet_transfer_routing.py -v`
Expected: FAIL with `ImportError: cannot import name 'TransferInFlight'`

- [ ] **Step 3: Implement**

In `server/sessions/fleet_orchestrator.py`:

```python
class TransferInFlight(Exception):
    """A per-session call arrived while the edge is moving that session between
    hosts. Neither host owns a live driver for it right now; the caller retries."""

    def __init__(self, session_id: str, target: str) -> None:
        super().__init__(f"session {session_id!r} is moving to {target!r}")
        self.session_id = session_id
        self.target = target


def _is_no_session(exc: BaseException) -> bool:
    from server.ipc.client import DaemonError
    return (isinstance(exc, DaemonError) and exc.err_type == "KeyError"
            and exc.message.startswith("No session with id"))
```

`__init__` gains `self._moving: dict[str, str] = {}  # session_id -> target while a transfer runs`.

Replace `_host_for` with a cache layer over a separate DB read, and add the markers:

```python
    async def _read_host(self, session_id: str) -> str | None:
        from sqlalchemy import select as _sel
        from server.db import db_session
        from server.db.models import Session as _S
        async with db_session() as db:
            return (await db.execute(
                _sel(_S.host).where(_S.id == session_id))).scalar_one_or_none()

    async def _host_for(self, session_id: str) -> str | None:
        cached = self._host_cache.get(session_id)
        if cached is not None:
            return cached
        host = await self._read_host(session_id)
        if host:
            self._host_cache[session_id] = host
        return host

    # -- cross-host transfer markers (spec 2026-09-14) -------------------------------
    def note_host(self, session_id: str, host: str) -> None:
        """The saga restamped the row; route there from now on."""
        self._host_cache[session_id] = host

    def begin_move(self, session_id: str, target: str) -> None:
        self._moving[session_id] = target
        self._host_cache.pop(session_id, None)

    def end_move(self, session_id: str) -> None:
        self._moving.pop(session_id, None)

    def moving_to(self, session_id: str) -> str | None:
        return self._moving.get(session_id)
```

Replace `_make_per_session`'s inner `_call`:

```python
        async def _call(session_id, *a, **k):
            target = self._moving.get(session_id)
            if target is not None:
                raise TransferInFlight(session_id, target)
            host = await self._host_for(session_id)
            try:
                orch = self._router.get(host) if host else self._primary_orch()
            except Exception:
                orch = self._primary_orch()
            try:
                return await getattr(orch, name)(session_id, *a, **k)
            except Exception as exc:
                if not _is_no_session(exc):
                    raise
                # Stale route (#1432): the row may have been restamped by a transfer
                # or an adoption since this process cached it. Re-read once and retry
                # on the new host; the same answer from the true owner propagates.
                self._host_cache.pop(session_id, None)
                fresh = await self._read_host(session_id)
                if not fresh or fresh == host:
                    raise
                self._host_cache[session_id] = fresh
                try:
                    orch2 = self._router.get(fresh)
                except Exception:
                    raise exc from None
                log.info("fleet.route_healed session=%s %s -> %s", session_id, host, fresh)
                return await getattr(orch2, name)(session_id, *a, **k)
```

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_fleet_transfer_routing.py server/tests/test_fleet_orchestrator.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add server/sessions/fleet_orchestrator.py server/tests/test_fleet_transfer_routing.py
printf 'feat(fleet): move markers and stale-route self-heal on no-session\n\nRefs #1696, #1432\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c12.txt
git commit -q -F /tmp/c12.txt
```

---

### Task 13: The edge saga, `server/sessions/transfer.py`

**Files:**
- Create: `server/sessions/transfer.py`
- Test: `server/tests/test_transfer_saga.py`

**Interfaces:**
- Consumes: `FleetOrchestrator` (`_router.get(host)`, `begin_move`, `end_move`, `note_host`, `_primary`), `db_utc_now`, `record_audit`, `DbSession.moving_to/host/moved_from/moved_at`.
- Produces: `@dataclass TransferOutcome(session_id, source, target, outcome, reason=None, detail=None)` with `outcome in {"moved","restored","failed","refused"}`; `async def transfer(sessions, session_id, target, *, carry_dirty, initiator, identity, reason=None) -> TransferOutcome`. `sessions` is the `FleetOrchestrator`. Audit rows: `session.transfer` (moved/restored/failed) and `session.transfer.finalize_failed`.

The saga owns the `moving_to` claim (conditional UPDATE), the restamp, rollback and the `finally` that clears the marker. It calls daemons only through `sessions._router.get(host)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_transfer_saga.py
"""The edge saga against two fake daemons and a real SQLite row.

One test per failure point; each asserts the terminal host/moving_to state,
what each daemon was asked, and the audit row."""
import asyncio
from unittest import mock

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import AuditEvent, Session as DbSession
from server.sessions import transfer as tx
from server.sessions.fleet_orchestrator import FleetOrchestrator

MANIFEST = {"sessionId": "s1", "engineSessionId": "c", "cwd": "/w", "permissionMode": "i",
            "model": None, "status": "idle", "bundle": {"sessionId": "s1", "branch": "b",
            "headSha": "h", "baseSha": "h", "wipCommit": False, "branches": {}}}


class _Daemon:
    def __init__(self, name):
        self.name = name; self.calls = []
        self.export_result = {"ok": True, "manifest": MANIFEST}
        self.import_result = {"ok": True}
        self.finalize_result = {"ok": True, "removed": True}
    async def transfer_export(self, sid, *, carry_dirty=False):
        self.calls.append(("export", sid, carry_dirty)); return self.export_result
    async def transfer_import(self, sid, manifest, *, from_host=None):
        self.calls.append(("import", sid, from_host)); return self.import_result
    async def transfer_finalize(self, sid, manifest):
        self.calls.append(("finalize", sid)); return self.finalize_result


class _Router:
    def __init__(self, orchs): self._o = orchs
    def get(self, name): return self._o[name]
    def names(self): return sorted(self._o)


@pytest.fixture
def fleet(tmp_path):
    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", engine_session_id="c",
                             status="idle", cwd="/w", host="dobby"))
    asyncio.get_event_loop().run_until_complete(go()) if False else asyncio.run(go())
    dobby, rocky = _Daemon("dobby"), _Daemon("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")
    fo.note_host("s1", "dobby")
    return fo, dobby, rocky


async def _row():
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()


async def _audits():
    async with db_session() as db:
        return [(a.type, a.detail) for a in
                (await db.execute(select(AuditEvent).order_by(AuditEvent.at))).scalars().all()]


@pytest.mark.asyncio
async def test_happy_path_moves(fleet):
    fo, dobby, rocky = fleet
    out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "moved" and out.source == "dobby" and out.target == "rocky"
    assert dobby.calls == [("export", "s1", False), ("finalize", "s1")]
    assert rocky.calls == [("import", "s1", "dobby")]
    row = await _row()
    assert (row.host, row.moving_to, row.moved_from) == ("rocky", None, "dobby")
    assert row.moved_at is not None
    assert fo._host_cache["s1"] == "rocky" and fo.moving_to("s1") is None
    types = [t for t, _ in await _audits()]
    assert types == ["session.transfer"]
    assert (await _audits())[0][1]["outcome"] == "moved"


@pytest.mark.asyncio
async def test_export_refusal_changes_nothing(fleet):
    fo, dobby, rocky = fleet
    dobby.export_result = {"ok": False, "reason": "mid_turn", "detail": "in_progress"}
    out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "refused" and out.reason == "mid_turn"
    row = await _row()
    assert (row.host, row.moving_to) == ("dobby", None)
    assert rocky.calls == [] and fo.moving_to("s1") is None
    assert await _audits() == []


@pytest.mark.asyncio
async def test_import_failure_restores_on_source(fleet):
    fo, dobby, rocky = fleet
    rocky.import_result = {"ok": False, "reason": "branch_busy_on_target", "detail": "x"}
    out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "restored" and out.reason == "branch_busy_on_target"
    assert dobby.calls == [("export", "s1", False), ("import", "s1", "rocky")]
    row = await _row()
    assert (row.host, row.moving_to, row.moved_from) == ("dobby", None, None)
    assert fo._host_cache["s1"] == "dobby"
    assert (await _audits())[0][1]["outcome"] == "restored"


@pytest.mark.asyncio
async def test_double_failure_parks_on_source(fleet):
    fo, dobby, rocky = fleet
    rocky.import_result = {"ok": False, "reason": "fetch_failed", "detail": "x"}
    dobby.import_result = {"ok": False, "reason": "resume_failed", "detail": "y"}
    out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "failed"
    row = await _row()
    assert (row.host, row.moving_to, row.status, row.paused_reason) == \
        ("dobby", None, "needs_attention", "transfer_failed")


@pytest.mark.asyncio
async def test_finalize_failure_is_audited_not_fatal(fleet):
    fo, dobby, rocky = fleet
    dobby.finalize_result = {"ok": False, "removed": False}
    with mock.patch.object(tx, "_FINALIZE_BACKOFF_S", 0.0):
        out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "moved"
    assert dobby.calls.count(("finalize", "s1")) == 3
    assert [t for t, _ in await _audits()] == ["session.transfer", "session.transfer.finalize_failed"]


@pytest.mark.asyncio
async def test_concurrent_move_is_refused_by_the_claim(fleet):
    fo, dobby, rocky = fleet
    async with db_session() as db:
        row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
        row.moving_to = "rocky"
    out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "refused" and out.reason == "transfer_in_flight"
    assert dobby.calls == []


@pytest.mark.asyncio
async def test_export_exception_clears_marker(fleet):
    fo, dobby, rocky = fleet
    async def boom(sid, *, carry_dirty=False): raise RuntimeError("daemon down")
    dobby.transfer_export = boom
    out = await tx.transfer(fo, "s1", "rocky", carry_dirty=False, initiator="user", identity="tony")
    assert out.outcome == "refused" and out.reason == "export_error"
    assert (await _row()).moving_to is None and fo.moving_to("s1") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_transfer_saga.py -v`
Expected: FAIL with `ImportError: cannot import name 'transfer'` (module missing)

- [ ] **Step 3: Implement**

```python
# server/sessions/transfer.py
"""Edge-driven cross-host transfer saga.

Spec: docs/superpowers/specs/2026-09-14-cross-host-session-transfer-design.md, "The
transfer saga". The edge sequences export → restamp → import → finalize through the
fleet router and owns every rollback; daemons never address each other. The
``moving_to`` column is the cross-process claim (one conditional UPDATE), the
FleetOrchestrator's in-memory marker is what makes per-session RPCs refuse fast.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import update as sa_update

from server.audit import record_audit
from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import Session as DbSession

log = logging.getLogger(__name__)

__all__ = ["TransferOutcome", "transfer"]

_FINALIZE_ATTEMPTS = 3
_FINALIZE_BACKOFF_S = 2.0


@dataclass(frozen=True)
class TransferOutcome:
    session_id: str
    source: str | None
    target: str
    outcome: str            # moved | restored | failed | refused
    reason: str | None = None
    detail: str | None = None


async def _claim(session_id: str, target: str) -> str | None:
    """Set moving_to iff it is NULL. Returns the current host on success, None if
    another move holds the claim (or the row is gone)."""
    async with db_session() as db:
        res = await db.execute(
            sa_update(DbSession)
            .where(DbSession.id == session_id, DbSession.moving_to.is_(None))
            .values(moving_to=target))
        if res.rowcount != 1:  # type: ignore[attr-defined]
            return None
        from sqlalchemy import select as sa_select
        host = (await db.execute(
            sa_select(DbSession.host).where(DbSession.id == session_id))).scalar_one()
    return host


async def _set(session_id: str, **values) -> None:
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id).values(**values))


async def _audit(type_: str, identity: str, detail: dict) -> None:
    async with db_session() as db:
        await record_audit(db, type_, identity, detail, via="transfer")


async def transfer(sessions, session_id: str, target: str, *, carry_dirty: bool,
                   initiator: str, identity: str, reason: str | None = None) -> TransferOutcome:
    """Move *session_id* to *target*. ``sessions`` is the FleetOrchestrator."""
    router = sessions._router
    source = await _claim(session_id, target)
    if source is None:
        return TransferOutcome(session_id, None, target, "refused", "transfer_in_flight")
    source = source or sessions._primary
    sessions.begin_move(session_id, target)
    base = {"sessionId": session_id, "from": source, "to": target, "initiator": initiator,
            "reason": reason, "carryDirty": carry_dirty}
    try:
        # 2. export on the source
        try:
            exported = await router.get(source).transfer_export(session_id, carry_dirty=carry_dirty)
        except Exception as exc:  # noqa: BLE001 — a dead source is a refusal, not a crash
            log.warning("transfer.export_error session=%s source=%s: %s", session_id, source, exc)
            return TransferOutcome(session_id, source, target, "refused", "export_error", str(exc))
        if not exported.get("ok"):
            return TransferOutcome(session_id, source, target, "refused",
                                   exported.get("reason"), exported.get("detail"))
        manifest = exported["manifest"]

        # 3. restamp
        await _set(session_id, host=target, moved_from=source, moved_at=db_utc_now())
        sessions.note_host(session_id, target)

        # 4. import on the target
        imported = await _safe_import(router, target, session_id, manifest, from_host=source)
        if imported.get("ok"):
            await _audit("session.transfer", identity, {**base, "outcome": "moved"})
            await _finalize(router, source, session_id, manifest, identity, base)
            return TransferOutcome(session_id, source, target, "moved")

        # rollback: back to the source, which still has the worktree on disk
        why, detail = imported.get("reason"), imported.get("detail")
        await _set(session_id, host=source, moved_from=None, moved_at=None)
        sessions.note_host(session_id, source)
        restored = await _safe_import(router, source, session_id, manifest, from_host=target)
        if restored.get("ok"):
            await _audit("session.transfer", identity,
                         {**base, "outcome": "restored", "reason": why, "detail": detail})
            return TransferOutcome(session_id, source, target, "restored", why, detail)
        await _set(session_id, status="needs_attention", paused_reason="transfer_failed")
        await _audit("session.transfer", identity,
                     {**base, "outcome": "failed", "reason": why, "detail": detail,
                      "restoreReason": restored.get("reason")})
        log.error("transfer.failed session=%s source=%s target=%s import=%s restore=%s",
                  session_id, source, target, why, restored.get("reason"))
        return TransferOutcome(session_id, source, target, "failed", why, detail)
    finally:
        sessions.end_move(session_id)
        await _set(session_id, moving_to=None)


async def _safe_import(router, host: str, session_id: str, manifest: dict, *, from_host: str) -> dict:
    try:
        return await router.get(host).transfer_import(session_id, manifest, from_host=from_host)
    except Exception as exc:  # noqa: BLE001 — surfaced as a reason, the saga decides
        log.warning("transfer.import_error session=%s host=%s: %s", session_id, host, exc)
        return {"ok": False, "reason": "import_error", "detail": str(exc)}


async def _finalize(router, source: str, session_id: str, manifest: dict,
                    identity: str, base: dict) -> None:
    """Best-effort with bounded retries; never changes the outcome."""
    last: dict = {}
    for attempt in range(_FINALIZE_ATTEMPTS):
        try:
            last = await router.get(source).transfer_finalize(session_id, manifest)
        except Exception as exc:  # noqa: BLE001
            last = {"ok": False, "detail": str(exc)}
        if last.get("ok"):
            return
        if attempt < _FINALIZE_ATTEMPTS - 1:
            await asyncio.sleep(_FINALIZE_BACKOFF_S)
    await _audit("session.transfer.finalize_failed", identity, {**base, "last": last})
    log.warning("transfer.finalize_failed session=%s source=%s: %s", session_id, source, last)
```

Check `record_audit`'s `via` parameter exists with that name (`server/audit.py:110-116`); it does. The fixture's odd `asyncio.get_event_loop()... if False else asyncio.run(go())` line is a leftover: replace it with plain `asyncio.run(go())`.

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_transfer_saga.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add server/sessions/transfer.py server/tests/test_transfer_saga.py
printf 'feat(sessions): edge-driven cross-host transfer saga with rollback\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c13.txt
git commit -q -F /tmp/c13.txt
```

---

### Task 14: Bridge `SESSION_MOVING` attach error

**Files:**
- Modify: `server/bridge/ws.py:660-744` (`_attach_failed` gains `extra`; the `ensure_driver` try gets a `TransferInFlight` branch before the generic `except Exception`)
- Test: `server/tests/test_ws_session_moving.py`

**Interfaces:**
- Produces: error frame `{"type": "error", "code": "SESSION_MOVING", "message": "...", "sessionId": ..., "retryAfterMs": 2000, "movingTo": <host>}`.

- [ ] **Step 1: Write the failing test**

Find the existing attach-error test to copy its socket fixture: `grep -ln "SESSION_NOT_FOUND" server/tests/*.py` (expect `test_ws_attach_errors.py` or similar). Add to a new file, reusing that file's client/fixture imports verbatim:

```python
# server/tests/test_ws_session_moving.py
"""An attach during a cross-host move gets SESSION_MOVING with a retry hint, not
SESSION_NOT_FOUND (#1432)."""
from server.sessions.fleet_orchestrator import TransferInFlight
# ... copy the imports and the `_client`/`_ws` fixture from the SESSION_NOT_FOUND test file ...


def test_attach_during_move_reports_session_moving(ws_fixture):
    client, fake = ws_fixture  # the fake orchestrator the existing test installs on app.state.sessions

    async def ensure_driver(sid, identity="system"):
        raise TransferInFlight(sid, "rocky")
    fake.ensure_driver = ensure_driver

    with client.websocket_connect("/ws", headers=_H) as ws:
        ws.send_json({"type": "attach", "sessionId": "s1"})
        frames = [ws.receive_json() for _ in range(2)]
    err = next(f for f in frames if f.get("type") == "error")
    assert err["code"] == "SESSION_MOVING"
    assert err["movingTo"] == "rocky" and err["retryAfterMs"] == 2000
```

- [ ] **Step 2: Run to verify it fails**

Run: `$T server/tests/test_ws_session_moving.py -v`
Expected: FAIL — the frame's `code` is `SESSION_START_FAILED` (the generic branch).

- [ ] **Step 3: Implement**

`_attach_failed` signature becomes `async def _attach_failed(session_id, code, message, auto, extra: dict | None = None)` and the send line becomes `await _send({"type": "error", "code": code, "message": message, "sessionId": session_id, **(extra or {})})`.

In the `ensure_driver` try block, add before `except Exception:`:

```python
            except TransferInFlight as exc:
                # The edge is moving this session between hosts; neither daemon owns a
                # driver for a few seconds. Tell the client to wait and re-attach — the
                # same detach→attach it already does for gap-healing — instead of the
                # SESSION_NOT_FOUND that #1432 showed to be misleading here.
                await _attach_failed(
                    session_id, "SESSION_MOVING",
                    f"Session is moving to {exc.target}; retrying shortly.", auto,
                    extra={"retryAfterMs": SESSION_MOVING_RETRY_MS, "movingTo": exc.target})
                return None
```

with `from server.sessions.fleet_orchestrator import TransferInFlight` and module constant `SESSION_MOVING_RETRY_MS = 2000` near the top of `server/bridge/ws.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `$T server/tests/test_ws_session_moving.py server/tests/test_ws_attach_errors.py -v` (substitute the real file name found in Step 1).
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws_session_moving.py
printf 'feat(ws): SESSION_MOVING attach error with retry hint during a transfer\n\nRefs #1696, #1432\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c14.txt
git commit -q -F /tmp/c14.txt
```

---

### Task 15: API endpoints and the kill switch

**Files:**
- Modify: `server/config.py` (pydantic field + dataclass fallback)
- Modify: `server/api/sessions.py` (after `rotate_session`, ~line 1699; batch after `batch_rotate`; `host-pin` after `account-pin`)
- Modify: `server/api/fleet.py` (after `upgrade_host`)
- Test: `server/tests/test_sessions_transfer_routes.py`, `server/tests/test_fleet_drain_route.py`

**Interfaces:**
- Produces: `POST /api/sessions/{id}/transfer` body `{host, carryDirty?, reason?}` → 200 `{sessionId, from, to, outcome}`; 404 `not_found`; 409 with `code` ∈ movable reasons ∪ `{transfer_in_flight, dirty_worktree, mid_turn, export_error, <TransferError codes>}` and `outcome` for `restored`/`failed`; 403 `transfer_disabled` when the switch is off. `POST /api/sessions/batch/transfer` `{ids, host, carryDirty?}` → `{succeeded: [...], failed: [{id, error}]}`. `POST /api/sessions/{id}/host-pin` `{pinned}` → session dict. `POST /api/fleet/hosts/{name}/drain` `{to?}` → `{moved: [...], left: [{id, reason}]}`. Setting `transfer_enabled: bool` (`VIBERPIT_TRANSFER_ENABLED`, default true).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_sessions_transfer_routes.py
"""POST /api/sessions/{id}/transfer, /batch/transfer, /{id}/host-pin."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.engine import AgentEngine
from server.sessions.fleet import HostCandidate
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.transfer import TransferOutcome
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_REPO_ROOT": "/root"}


def _host(name, **kw):
    base = dict(name=name, enabled=True, healthy=True, weight=100, active_sessions=0,
                max_sessions=None, load_avg=0.5, mem_available_mb=8000)
    base.update(kw); return HostCandidate(**base)


class _Router:
    async def candidates(self): return [_host("dobby"), _host("rocky")]
    def names(self): return ["dobby", "rocky"]


class _FakeSessions:
    def __init__(self, record):
        self._record = record; self._router = _Router(); self._primary = "dobby"; self.pins = []
    async def get_session(self, sid): return self._record if self._record and self._record.id == sid else None
    async def latest_suggestions(self, sid): return []
    async def set_host_pin(self, sid, pinned): self.pins.append((sid, pinned)); self._record.host_pinned = pinned; return True


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                cols=80, rows=24, cwd="/root/.ccswitch/worktrees/r/s1", host="dobby", status="idle",
                display_mode="virtual")
    base.update(kw); return SessionRecord(**base)


def _client(record, env=None):
    with patch.dict(os.environ, {**_ENV, **(env or {})}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(record); app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def _moved(*a, **k):
    async def go(sessions, sid, target, **kw): return TransferOutcome(sid, "dobby", target, "moved")
    return go


def test_transfer_moves_and_returns_outcome():
    client, fake = _client(_rec())
    with patch.dict(os.environ, _ENV), client, patch("server.api.sessions.run_transfer", _moved()):
        r = client.post("/api/sessions/s1/transfer", json={"host": "rocky"}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"sessionId": "s1", "from": "dobby", "to": "rocky", "outcome": "moved"}


def test_transfer_refuses_by_predicate_before_calling_saga():
    client, fake = _client(_rec(status="in_progress"))
    with patch.dict(os.environ, _ENV), client, \
         patch("server.api.sessions.run_transfer", side_effect=AssertionError("must not run")):
        r = client.post("/api/sessions/s1/transfer", json={"host": "rocky"}, headers=_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "mid_turn"


def test_transfer_unknown_target_is_400():
    client, _ = _client(_rec())
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/transfer", json={"host": "mars"}, headers=_H)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unknown_host"


def test_transfer_restored_is_409_with_outcome():
    async def go(sessions, sid, target, **kw):
        return TransferOutcome(sid, "dobby", target, "restored", "branch_busy_on_target", "x")
    client, _ = _client(_rec())
    with patch.dict(os.environ, _ENV), client, patch("server.api.sessions.run_transfer", go):
        r = client.post("/api/sessions/s1/transfer", json={"host": "rocky"}, headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "branch_busy_on_target", "outcome": "restored",
                                  "message": "x"}


def test_transfer_disabled_is_403():
    client, _ = _client(_rec(), env={"VIBERPIT_TRANSFER_ENABLED": "false"})
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_TRANSFER_ENABLED": "false"}), client:
        r = client.post("/api/sessions/s1/transfer", json={"host": "rocky"}, headers=_H)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "transfer_disabled"


def test_unknown_session_is_404():
    client, _ = _client(None)
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/nope/transfer", json={"host": "rocky"}, headers=_H)
    assert r.status_code == 404


def test_batch_transfer_reports_per_id():
    calls = []
    async def go(sessions, sid, target, **kw):
        calls.append(sid)
        return TransferOutcome(sid, "dobby", target, "moved" if sid == "s1" else "refused", "mid_turn")
    client, fake = _client(_rec())
    with patch.dict(os.environ, _ENV), client, patch("server.api.sessions.run_transfer", go):
        r = client.post("/api/sessions/batch/transfer", json={"ids": ["s1", "s1"], "host": "rocky"}, headers=_H)
    assert r.status_code == 200 and calls == ["s1"]
    assert r.json() == {"succeeded": ["s1"], "failed": []}


def test_host_pin_round_trips():
    client, fake = _client(_rec())
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/host-pin", json={"pinned": True}, headers=_H)
    assert r.status_code == 200 and r.json()["hostPinned"] is True and fake.pins == [("s1", True)]
```

```python
# server/tests/test_fleet_drain_route.py
"""POST /api/fleet/hosts/{name}/drain moves every movable session off a host."""
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.engine import AgentEngine
from server.sessions.fleet import HostCandidate
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.transfer import TransferOutcome
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_REPO_ROOT": "/root"}


def _host(name):
    return HostCandidate(name=name, enabled=True, healthy=True, weight=100, active_sessions=0,
                         max_sessions=None, load_avg=0.5, mem_available_mb=8000)


class _Router:
    async def candidates(self): return [_host("dobby"), _host("rocky")]
    def names(self): return ["dobby", "rocky"]
    async def pick(self, *, prefer=None, engine=None, expected_contract_sha256=None):
        return ["rocky", "dobby"]


def _rec(sid, **kw):
    base = dict(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                cols=80, rows=24, cwd=f"/root/.ccswitch/worktrees/r/{sid}", host="dobby",
                status="idle", display_mode="virtual")
    base.update(kw); return SessionRecord(**base)


class _FakeSessions:
    def __init__(self, records):
        self._records = records; self._router = _Router(); self._primary = "dobby"
    async def list_sessions(self): return list(self._records)
    async def get_session(self, sid): return next((r for r in self._records if r.id == sid), None)
    async def latest_suggestions(self, sid): return []


def test_drain_moves_movable_and_reports_the_rest():
    recs = [_rec("s1"), _rec("s2", status="in_progress"), _rec("s3", host="rocky")]
    moved = []
    async def go(sessions, sid, target, **kw):
        moved.append((sid, target)); return TransferOutcome(sid, "dobby", target, "moved")
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    app.state.sessions = _FakeSessions(recs)
    client = TestClient(app, raise_server_exceptions=False)
    with patch.dict(os.environ, _ENV), client, patch("server.api.fleet.run_transfer", go):
        r = client.post("/api/fleet/hosts/dobby/drain", json={}, headers=_H)
    assert r.status_code == 200, r.text
    assert moved == [("s1", "rocky")]
    assert r.json() == {"moved": ["s1"], "left": [{"id": "s2", "reason": "mid_turn"}]}
```

- [ ] **Step 2: Run to verify they fail**

Run: `$T server/tests/test_sessions_transfer_routes.py server/tests/test_fleet_drain_route.py -v`
Expected: FAIL (404 on the routes; `AttributeError` on `run_transfer`).

- [ ] **Step 3: Implement**

`server/config.py`, pydantic settings (next to `assistant_watch_max_per_identity`):

```python
        # Cross-host session transfer kill switch (spec 2026-09-14). Off = the transfer
        # API and (Phase 3) the rebalance loop both refuse; nothing else changes.
        transfer_enabled: bool = Field(default=True, alias="CCSWITCH_TRANSFER_ENABLED")
```

and the dataclass fallback:

```python
        transfer_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_TRANSFER_ENABLED", "true").lower()
            not in ("0", "false", "no"))
```

(`mirror_legacy_env()` fills `CCSWITCH_TRANSFER_ENABLED` from `VIBERPIT_TRANSFER_ENABLED`; the test sets the `VIBERPIT_` name and, because `get_settings()` may be cached, the route reads `os.environ` through `get_settings()` fresh: check how other routes read a flag — `grep -n "get_settings()" server/api/sessions.py | head -3` — and follow that. If settings are cached per process, read the flag with a tiny helper `_transfer_enabled() -> bool` that calls `get_settings.cache_clear()` only in tests via the `temp_env()` fixture the assistant-watches test uses.)

`server/api/sessions.py` — imports and helpers:

```python
from server.sessions.transfer import transfer as run_transfer
from server.sessions.transfer_policy import movable
from server.sessions.claude_driver import ClaudeAgentDriver
from server.codex.capabilities import CODEX_CAPABILITIES, NO_CAPABILITIES


def _capabilities_of(engine: AgentEngine):
    """Same source `server/api/config.py:_capabilities_for` reads; never restated."""
    if engine is AgentEngine.CLAUDE_CODE:
        return ClaudeAgentDriver.capabilities
    if engine is AgentEngine.CODEX:
        return CODEX_CAPABILITIES
    return NO_CAPABILITIES


class TransferRequest(BaseModel):
    host: str
    carryDirty: bool = False
    reason: str | None = None


class BatchTransferRequest(BaseModel):
    ids: list[str]
    host: str
    carryDirty: bool = False


class HostPinRequest(BaseModel):
    pinned: bool


async def _transfer_preflight(request: Request, record, target: str):
    """Shared by the single and batch routes: kill switch, then the pure predicate."""
    if not get_settings().transfer_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "transfer_disabled",
                                    "message": "cross-host transfer is disabled on this edge"})
    orch = _get_orchestrator(request)
    router = getattr(orch, "_router", None)
    candidates = await router.candidates() if router is not None else []
    verdict = movable(record, target=target, candidates=candidates,
                      capabilities=_capabilities_of(record.engine),
                      repo_root=get_settings().repo_root,
                      primary=getattr(orch, "_primary", None))
    if not verdict.ok:
        code = status.HTTP_400_BAD_REQUEST if verdict.reason == "unknown_host" else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code,
                            detail={"code": verdict.reason, "message": verdict.detail or verdict.reason})
```

Routes (after `rotate_session`):

```python
@router.post("/{session_id}/transfer")
async def transfer_session(
    session_id: str, body: TransferRequest, request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Move the session to another fleet host (spec 2026-09-14). Synchronous: returns
    the saga's outcome. 409 codes are the movable() reasons or the saga's refusal
    reason; a `restored`/`failed` outcome is also 409 with `outcome` set, since the
    session did not end up where the caller asked."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": f"Session {session_id!r} not found"})
    await _transfer_preflight(request, record, body.host)
    out = await run_transfer(orch, session_id, body.host, carry_dirty=body.carryDirty,
                             initiator="user", identity=identity, reason=body.reason)
    if out.outcome != "moved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"code": out.reason or out.outcome, "outcome": out.outcome,
                                    "message": out.detail or out.reason or out.outcome})
    return {"sessionId": session_id, "from": out.source, "to": out.target, "outcome": out.outcome}


@router.post("/batch/transfer")
async def batch_transfer(
    body: BatchTransferRequest, request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Sequential on purpose: two pushes from one clone contend on its lock anyway."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):
        record = await orch.get_session(session_id)
        if record is None:
            failed.append({"id": session_id, "error": "not_found"}); continue
        try:
            await _transfer_preflight(request, record, body.host)
        except HTTPException as exc:
            failed.append({"id": session_id, "error": exc.detail["code"]}); continue
        out = await run_transfer(orch, session_id, body.host, carry_dirty=body.carryDirty,
                                 initiator="user", identity=identity)
        if out.outcome == "moved":
            succeeded.append(session_id)
        else:
            failed.append({"id": session_id, "error": out.reason or out.outcome})
    return {"succeeded": succeeded, "failed": failed}


@router.post("/{session_id}/host-pin")
async def set_host_pin(
    session_id: str, body: HostPinRequest, request: Request,
    identity: str = Depends(current_identity), db=Depends(get_db),
) -> dict:
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": f"Session {session_id!r} not found"})
    await orch.set_host_pin(session_id, body.pinned)
    await record_audit(db, "session.host_pin", identity, {"sessionId": session_id, "pinned": body.pinned})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))
```

The `batch/transfer` route must be registered **before** `/{session_id}/transfer` in the file if FastAPI would otherwise match `batch` as a session id; the existing `batch/rotate` is placed above the `/{session_id}/...` routes for that reason — put `batch/transfer` next to it.

`set_host_pin` on the orchestrator: mirror `set_account_pin` (`grep -n "async def set_account_pin" -A 12 server/sessions/orchestrator.py`) — persist `host_pinned` on the row and the record, return True; add `"set_host_pin"` to `_SIMPLE`, `_PER_SESSION`, `RemoteOrchestrator` and the facade exactly the way `set_account_pin` appears in each.

`server/api/fleet.py` (after `upgrade_host`):

```python
from server.sessions.transfer import transfer as run_transfer
from server.sessions.transfer_policy import movable
from server.api.sessions import _capabilities_of


class DrainBody(BaseModel):
    to: str | None = None


@router.post("/hosts/{name}/drain")
async def drain_host(name: str, request: Request, body: DrainBody | None = None,
                     identity: str = Depends(current_identity)) -> dict:
    """Move every movable, clean session off *name* (the maintenance operation the
    rocky runbook's rollback section does by hand). Target per session: the explicit
    `to`, else the router's best pick excluding *name*. Returns what moved and what
    stayed with its reason."""
    from server.config import get_settings
    orch = request.app.state.sessions
    router = getattr(orch, "_router", None)
    if router is None or name not in router.names():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "unknown_host", "message": name})
    candidates = await router.candidates()
    moved: list[str] = []
    left: list[dict] = []
    for rec in await orch.list_sessions():
        if (rec.host or getattr(orch, "_primary", None)) != name:
            continue
        target = (body.to if body and body.to else
                  next((h for h in await router.pick(engine=rec.engine) if h != name), None))
        if target is None:
            left.append({"id": rec.id, "reason": "no_host_available"}); continue
        verdict = movable(rec, target=target, candidates=candidates,
                          capabilities=_capabilities_of(rec.engine),
                          repo_root=get_settings().repo_root,
                          primary=getattr(orch, "_primary", None))
        if not verdict.ok:
            left.append({"id": rec.id, "reason": verdict.reason}); continue
        out = await run_transfer(orch, rec.id, target, carry_dirty=False,
                                 initiator="drain", identity=identity)
        if out.outcome == "moved":
            moved.append(rec.id)
        else:
            left.append({"id": rec.id, "reason": out.reason or out.outcome})
    return {"moved": moved, "left": left}
```

`fleet.py` needs `Request` from fastapi in its import line. Importing `_capabilities_of` from `server.api.sessions` is acceptable only if it does not create an import cycle; if it does, move `_capabilities_of` into `server/api/config.py` next to `_capabilities_for` (make that one call it) and import from there in both routers.

- [ ] **Step 4: Run to verify they pass, plus every sessions/fleet route test**

Run: `$T server/tests/test_sessions_transfer_routes.py server/tests/test_fleet_drain_route.py server/tests/test_sessions_reload_route.py server/tests/test_sessions_rotate_routes.py server/tests/test_api_fleet.py -v` (substitute the real fleet API test name from `ls server/tests | grep fleet`).
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/api/sessions.py server/api/fleet.py server/sessions/orchestrator.py server/ipc/orchestrator_rpc.py server/sessions/remote.py server/sessions/facade.py server/sessions/fleet_orchestrator.py server/tests/test_sessions_transfer_routes.py server/tests/test_fleet_drain_route.py
printf 'feat(api): session transfer, batch transfer, host pin and fleet drain routes\n\nRefs #1696\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c15.txt
git commit -q -F /tmp/c15.txt
```

---

### Task 16: The Principle III drill script

**Files:**
- Create: `scripts/verify-cross-host-resume.sh`
- Modify: `specs/002-session-continuity/research.md` (append a "Cross-host resume drill" section with the run's findings; create the file if absent)

This is the drill spec 002 Task 17 specified and never ran. It is run by a human on a runner host against an **archived** session so it can never twin a live CLI. No unit test: the deliverable is the script plus the recorded findings.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Verify that a Claude conversation started on another fleet host resumes HERE
# from the shared transcript at the same worktree path (spec 2026-09-14 cross-host
# transfer, "Verification gate"; spec 002 Task 17).
#
# Usage: scripts/verify-cross-host-resume.sh <session-id> <account>
#   <session-id>  an ARCHIVED ViberPit session that last ran on the other host
#   <account>     the account name whose config dir composes the CLAUDE_CONFIG_DIR
#
# Prints PASS/FAIL. Never run against a live session: the lease guard lives in the
# orchestrator, not the CLI, so a hand-run `claude --resume` on a live conversation
# IS the twin bug (#1379).
set -euo pipefail
sid="${1:?session id}"; account="${2:?account name}"
edge="${VIBERPIT_EDGE_URL:-http://127.0.0.1:8787}"
row="$(curl -sf "$edge/api/sessions/$sid" -H 'Tailscale-User-Login: drill@localhost')" \
  || { echo "FAIL: cannot read session $sid from $edge"; exit 1; }
status="$(printf '%s' "$row" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
[ "$status" = "archived" ] || { echo "FAIL: session status is $status, drill needs archived"; exit 1; }
csid="$(printf '%s' "$row" | python3 -c 'import json,sys; print(json.load(sys.stdin)["engineSessionId"])')"
cwd="$(printf '%s' "$row" | python3 -c 'import json,sys; print(json.load(sys.stdin)["cwd"])')"
cfg="$HOME/.claude-accounts/cfg/$account"
[ -d "$cfg" ] || { echo "FAIL: no config dir $cfg"; exit 1; }
transcript="$(find "$HOME/.claude/projects" -maxdepth 2 -name "$csid.jsonl" | head -1)"
[ -n "$transcript" ] || { echo "FAIL: transcript $csid.jsonl not on the shared tree"; exit 1; }
echo "transcript: $transcript"
mkdir -p "$cwd"; cd "$cwd"
out="$(CLAUDE_CONFIG_DIR="$cfg" timeout 120 claude -p --resume "$csid" \
        'Reply with exactly the word PONG and nothing else.' 2>&1 || true)"
echo "$out" | tail -5
if printf '%s' "$out" | grep -q 'PONG'; then echo "PASS: resumed $csid on $(hostname -s)"; else echo "FAIL"; exit 1; fi
```

- [ ] **Step 2: Run it once on rocky against an archived dobby session**

Pick one: `curl -s http://127.0.0.1:8787/api/sessions/archived -H 'Tailscale-User-Login: drill@localhost' | python3 -c 'import json,sys; [print(r["id"], r["host"], r["accountName"]) for r in json.load(sys.stdin)["sessions"] if r.get("host")=="dobby"][:3]'` (via `ssh dobby` if the edge is not reachable from the sandbox; see the memory note "Reaching the edge from rocky"). Then `bash scripts/verify-cross-host-resume.sh <id> <account> | tee /tmp/cross-host-drill.log`.
Expected: `PASS: resumed <csid> on rocky`. Record the session id, date and outcome in `specs/002-session-continuity/research.md`. If it FAILS, stop the plan here and hand back: the spec's central assumption is wrong and FR-204 must become fresh-restart-only.

- [ ] **Step 3: Commit**

```bash
chmod +x scripts/verify-cross-host-resume.sh
git add scripts/verify-cross-host-resume.sh specs/002-session-continuity/research.md
printf 'chore(scripts): cross-host resume verification drill (spec 002 Task 17)\n\nRefs #1696, #949\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c16.txt
git commit -q -F /tmp/c16.txt
```

---

### Task 17: Full gate, two PRs

**Files:** none new.

- [ ] **Step 1: Run the four-step gate plus the web build sanity**

```bash
python3 -m ruff check src/ server/ 2>&1 | tail -3
python3 -m mypy server/ --ignore-missing-imports 2>&1 | tail -3
PYTHONPATH=$PWD/src python3 -m pytest tests/ -q 2>&1 | tail -3
python3 scripts/brand_ratchet.py | tail -8
```

Then the server suite with the test-slot cap, in ONE shell (acquire loop + pytest + trap together, as `.agents/instructions/core.md` shows), output to `/tmp/transfer-phase1-server.log`. Expected: ruff clean, mypy clean, core green, ratchet counts unchanged, server suite green except the known rocky-only `-n auto` flakes listed in the memory note "Rocky full-suite-only test failures" (re-run those alone).

- [ ] **Step 2: Split into the two PRs**

PR 1a carries Tasks 1–4 only (pure `src/ccswitch` + `tests/`), so the core module can land and be reviewed on its own:

```bash
git fetch origin
git checkout -b feat/worktree-transfer-core origin/master
git cherry-pick <sha of c1> <sha of c2> <sha of c3> <sha of c4>
git push -u origin feat/worktree-transfer-core
gh pr create --title "feat(core): worktree_transfer — carry a session worktree through git transfer refs" --body-file /tmp/pr1a.md --base master
gh pr merge <#> --squash --auto
```

PR 1b is the branch with all seventeen commits; open it against master with `Depends on #<1a>` in the body. Once 1a merges, rebase 1b onto `origin/master` (the four core commits drop out as empty) and push with `--force-with-lease`. Both PRs: non-draft, `--auto` armed, body ends with the Claude Code trailer, `Refs #1696` and `Refs #1432` (the route self-heal is a partial fix, not a close).

- [ ] **Step 3: Check for in-flight PRs touching the same files before opening**

```bash
gh api graphql -f query='{repository(owner:"steiner385",name:"viberpit"){
  pullRequests(states:OPEN,first:40,orderBy:{field:UPDATED_AT,direction:DESC}){
    nodes{number title headRefName files(first:60){nodes{path}}}}}}' \
  --jq '.data.repository.pullRequests.nodes[] | select([.files.nodes[].path] | any(test("fleet_orchestrator|orchestrator_rpc|remote.py|facade.py|bridge/ws.py|api/sessions.py|api/fleet.py|worktrees.py"))) | "\(.number)\t\(.headRefName)\t\(.title)"'
```

Rebase onto anything that lands first rather than racing it.

---

## Self-review notes (already applied)

- Spec coverage: saga steps 1–5 → Tasks 9, 10, 13; worktree carry-over → Tasks 1–4; routing/attach → Tasks 12, 14; nudge → Task 10; RPC surface → Task 11; edge API incl. batch, drain, host pin → Task 15; schema → Task 5; capability → Task 6; predicate → Task 7; `_adopt_row` for #949 → Task 8; verification gate → Task 16; kill switch → Task 15. Queued-session restamp ("movable by restamp alone") is deliberately deferred to Phase 3 with the rebalancer, since a queued session has no worktree and no driver: the predicate refuses it via `non_repo`/`inactive` today; note it in the PR body.
- Not in this plan (by spec): the web dialog/chip/drain button (Phase 2 plan), `cpus` in `/info` and the rebalance loop (Phase 3 plan).
- Type consistency: `Bundle.to_dict()` keys are camelCase and `Bundle.from_dict` reads the same keys in Tasks 2, 3, 10, 13; `TransferOutcome.outcome` values are the four strings used in Tasks 13 and 15; `TransferInFlight(session_id, target)` matches Tasks 12 and 14; `transfer_import(session_id, manifest, *, from_host)` matches Tasks 10, 11, 13.
