"""Tests for per-session git worktrees (ccswitch.worktrees)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ccswitch import worktrees


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


def _make_repo(path: Path, branch: str = "main") -> None:
    subprocess.run(["git", "init", str(path), "-b", branch], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=str(path))
    _git("config", "user.name", "Test", cwd=str(path))
    (path / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-m", "init", cwd=str(path))


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


class WorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)            # acts as repo_root
        self.repo = self.root / "myrepo"
        _make_repo(self.repo, branch="main")
        # a second branch that is NOT checked out anywhere
        _git("branch", "feature", cwd=str(self.repo))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_branch_creates_detached_worktree(self) -> None:
        """branch=None → a detached per-session worktree (its own dir), so repo
        sessions without a selected branch still get isolated working directories."""
        info = worktrees.create_worktree(str(self.repo), None, "no00branch-2222", str(self.root))
        self.assertIsNone(info.branch)
        self.assertFalse(info.is_child)
        self.assertTrue(os.path.isdir(info.path))
        self.assertTrue(worktrees.is_managed_path(info.path, str(self.root)))
        # The worktree is on a DETACHED HEAD (symbolic-ref fails when detached).
        sym = subprocess.run(
            ["git", "-C", info.path, "symbolic-ref", "-q", "HEAD"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(sym.returncode, 0, "expected a detached HEAD in the worktree")

    def test_two_detached_worktrees_are_distinct(self) -> None:
        a = worktrees.create_worktree(str(self.repo), None, "aaaaaaaa-0001", str(self.root))
        b = worktrees.create_worktree(str(self.repo), None, "bbbbbbbb-0002", str(self.root))
        self.assertNotEqual(a.path, b.path)
        self.assertTrue(os.path.isdir(a.path) and os.path.isdir(b.path))

    def test_checks_out_selected_branch_directly(self) -> None:
        info = worktrees.create_worktree(str(self.repo), "feature", "abc12345-0000", str(self.root))
        self.assertFalse(info.is_child)
        self.assertEqual(info.branch, "feature")
        self.assertTrue(os.path.isdir(info.path))
        # the worktree is actually on `feature`
        head = subprocess.run(
            ["git", "-C", info.path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head, "feature")
        # and it lives under <root>/.ccswitch/worktrees/
        self.assertTrue(worktrees.is_managed_path(info.path, str(self.root)))

    def test_real_checkout_is_untouched(self) -> None:
        before = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        worktrees.create_worktree(str(self.repo), "feature", "abc12345-0000", str(self.root))
        after = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(before, after)  # still on main; never switched

    def test_busy_branch_falls_back_to_child(self) -> None:
        # `main` is checked out in the real repo, so it cannot be checked out again.
        info = worktrees.create_worktree(str(self.repo), "main", "def67890-1111", str(self.root))
        self.assertTrue(info.is_child)
        self.assertTrue(info.branch.startswith("ccswitch/main-"))
        self.assertTrue(os.path.isdir(info.path))

    def test_remove_clean_worktree_and_child_branch(self) -> None:
        info = worktrees.create_worktree(str(self.repo), "main", "def67890-1111", str(self.root))
        self.assertTrue(worktrees.remove_worktree(info.path))
        self.assertFalse(os.path.isdir(info.path))
        # the ccswitch/* child branch was deleted too
        branches = subprocess.run(
            ["git", "-C", str(self.repo), "branch", "--format=%(refname:short)"],
            capture_output=True, text=True,
        ).stdout
        self.assertNotIn(info.branch, branches.split())

    def test_remove_ignores_leftover_codex_attachment_staging(self) -> None:
        """#1291 — a Codex session may leave workspace-local staged attachment
        copies behind (server/codex/driver.py:_stage_in_workspace). Untracked,
        they must not read as "dirty" and block the worktree's own removal."""
        info = worktrees.create_worktree(str(self.repo), "main", "def67890-1111", str(self.root))
        from ccswitch import paths
        staging = Path(info.path) / paths.codex_attachment_staging_dirname()
        staging.mkdir()
        (staging / "deadbeef.png").write_bytes(b"staged")
        self.assertTrue(worktrees.remove_worktree(info.path))
        self.assertFalse(os.path.isdir(info.path))

    def test_remove_keeps_dirty_worktree(self) -> None:
        info = worktrees.create_worktree(str(self.repo), "feature", "abc12345-0000", str(self.root))
        (Path(info.path) / "scratch.txt").write_text("uncommitted work\n")
        self.assertFalse(worktrees.remove_worktree(info.path))  # left in place
        self.assertTrue(os.path.isdir(info.path))               # work preserved

    def test_non_git_dir_raises(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.create_worktree(str(plain), "main", "x-1", str(self.root))

    def test_dash_branch_raises_worktree_error(self) -> None:
        """Branch names starting with '-' are rejected before any git call (arg-injection guard)."""
        with self.assertRaises(worktrees.WorktreeError) as ctx:
            worktrees.create_worktree(str(self.repo), "-evil", "abc12345-0000", str(self.root))
        self.assertIn("invalid branch name", str(ctx.exception))

    def test_nonexistent_branch_raises_unresolvable(self) -> None:
        """A branch that resolves to no ref at all (deleted on merge between
        enqueue and spawn — issue #836) raises the distinct
        BranchRefUnresolvableError so the orchestrator can treat the race as
        benign. Still a WorktreeError subclass for existing catch-sites."""
        with self.assertRaises(worktrees.BranchRefUnresolvableError) as ctx:
            worktrees.create_worktree(
                str(self.repo), "gone-on-merge", "abc12345-0000", str(self.root))
        self.assertIsInstance(ctx.exception, worktrees.WorktreeError)
        self.assertIn("invalid reference", str(ctx.exception))


class WorktreeLockingTests(unittest.TestCase):
    """Regression tests for #177 — a sibling session's cleanup must never reap a
    live session's worktree admin record.

    Two mechanisms are exercised: (1) every session worktree is `git worktree
    lock`'d on create, so a global `git worktree prune` skips it; (2) create-time
    cleanup is scoped to the session's own `dest`, so it never touches a sibling.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "myrepo"
        _make_repo(self.repo, branch="main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _porcelain(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        ).stdout

    def _is_registered(self, path: str) -> bool:
        target = os.path.abspath(path)
        for line in self._porcelain().splitlines():
            if line.startswith("worktree "):
                if os.path.abspath(line[len("worktree "):].strip()) == target:
                    return True
        return False

    def test_created_worktree_is_locked(self) -> None:
        """Item 1: a freshly-created session worktree is locked so prune skips it."""
        info = worktrees.create_worktree(str(self.repo), None, "aaaaaaaa-0001", str(self.root))
        r = subprocess.run(
            ["git", "-C", info.path, "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        ).stdout
        # The block for our dest carries a `locked` marker.
        self.assertIn("locked", r, "expected the session worktree to be git-locked")

    def test_locked_worktree_survives_prune_after_dir_loss(self) -> None:
        """The exact #177 reap: a worktree whose dir vanished (SIGKILL'd restart)
        is normally prunable, but locking makes `git worktree prune` skip it."""
        info = worktrees.create_worktree(str(self.repo), None, "aaaaaaaa-0001", str(self.root))
        # Simulate the crash: the working dir is gone but the admin record remains.
        subprocess.run(["rm", "-rf", info.path], check=True)
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], check=True)
        self.assertTrue(
            self._is_registered(info.path),
            "a locked worktree must survive a global prune even with its dir deleted",
        )

    def test_creating_sibling_does_not_reap_live_worktree(self) -> None:
        """End-to-end #177: session A is live; creating session B must not drop A's
        admin registration (old code's global prune reaped a prunable A)."""
        a = worktrees.create_worktree(str(self.repo), None, "aaaaaaaa-0001", str(self.root))
        # Put A into the prunable state the SIGKILL'd restart produced.
        subprocess.run(["rm", "-rf", a.path], check=True)
        # A sibling session starts — its create must not touch A.
        worktrees.create_worktree(str(self.repo), None, "bbbbbbbb-0002", str(self.root))
        self.assertTrue(
            self._is_registered(a.path),
            "creating a sibling session reaped a live session's worktree (#177)",
        )

    def test_reuses_stale_dest_path(self) -> None:
        """Item 2: scoped cleanup clears a stale (locked, dir-gone) record at the
        session's OWN dest so the same short-session id can be reused."""
        a = worktrees.create_worktree(str(self.repo), None, "aaaaaaaa-0001", str(self.root))
        dest = a.path
        subprocess.run(["rm", "-rf", dest], check=True)   # stale locked record at dest
        # Same session id → same dest path; create must clear the stale record and succeed.
        b = worktrees.create_worktree(str(self.repo), None, "aaaaaaaa-0001", str(self.root))
        self.assertEqual(os.path.abspath(b.path), os.path.abspath(dest))
        self.assertTrue(os.path.isdir(b.path))

    def test_remove_unlocks_and_removes_locked_worktree(self) -> None:
        """Teardown must unlock the (now-locked) worktree before removing it."""
        info = worktrees.create_worktree(str(self.repo), "main", "def67890-1111", str(self.root))
        self.assertTrue(worktrees.remove_worktree(info.path))
        self.assertFalse(os.path.isdir(info.path))
        self.assertFalse(self._is_registered(info.path))


class WorktreeFreshFromOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "myrepo"
        self.local_sha, self.origin_sha = _make_repo_with_origin(self.repo, self.root)
        self.assertNotEqual(self.local_sha, self.origin_sha)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_branch_uses_remote_default_tip(self) -> None:
        """branch=None with origin → detached at origin/HEAD tip, not local HEAD."""
        info = worktrees.create_worktree(str(self.repo), None, "bbbbbbbb-0002", str(self.root))
        self.assertIsNone(info.branch)
        self.assertFalse(info.is_child)
        self.assertEqual(_rev(Path(info.path), "HEAD"), self.origin_sha)
        self.assertNotEqual(_rev(Path(info.path), "HEAD"), self.local_sha)

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

    def test_fetch_failure_falls_back_to_local(self) -> None:
        """A configured-but-unreachable origin does NOT abort the session: fetch is
        best-effort, so we fall back to a local-ref worktree and log a warning.

        Many real origins are not fetchable non-interactively (private repos, no
        cached credentials, the credential-less deploy box, offline) — refusing to
        start a session in those cases would be far worse than a slightly-stale base.
        """
        _git("remote", "set-url", "origin", str(self.root / "does-not-exist.git"),
             cwd=str(self.repo))
        with self.assertLogs("ccswitch.worktrees", level="WARNING") as cm:
            info = worktrees.create_worktree(str(self.repo), "main", "aaaaaaaa-0001", str(self.root))
        # Session started, based on the LOCAL ref (origin tip was never reachable).
        self.assertEqual(_rev(Path(info.path), "HEAD"), self.local_sha)
        self.assertNotEqual(_rev(Path(info.path), "HEAD"), self.origin_sha)
        # The operator is warned that the base could not be refreshed from origin.
        self.assertTrue(any("fetch" in line.lower() for line in cm.output))

    def test_local_only_branch_uses_local_base(self) -> None:
        """A branch that exists only locally (not on origin) is based off the local
        ref even though origin is configured — there is no remote tip to be fresh against."""
        _git("branch", "localfeature", cwd=str(self.repo))   # at local A, never pushed
        info = worktrees.create_worktree(str(self.repo), "localfeature", "eeeeeeee-0005", str(self.root))
        self.assertFalse(info.is_child)
        self.assertEqual(info.branch, "localfeature")
        self.assertEqual(_rev(Path(info.path), "HEAD"), self.local_sha)


class WorktreeConcurrencyTests(unittest.TestCase):
    """Concurrent create_worktree against ONE clone must not race on git's
    shared lockfiles. `worktree add -b <child> ... origin/<branch>` writes
    upstream-tracking keys into the clone's `.git/config`, guarded by
    `.git/config.lock`; git fails immediately (does NOT wait) when the lock is
    held, so without per-clone serialization a burst of spawns loses all-but-one
    to `could not lock config file .git/config: File exists` (pr-doctor
    multi-trigger spawn_failed, 2026-08-17)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "myrepo"
        _make_repo_with_origin(self.repo, self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_concurrent_child_creations_all_succeed(self) -> None:
        """N threads each creating a fresh child off origin/main must all get a
        distinct worktree — none lost to a config.lock collision."""
        import concurrent.futures

        n = 8

        def make(i: int) -> worktrees.WorktreeInfo:
            # Distinct session id per call → distinct ccswitch/main-<short> child
            # branch, but ALL write upstream config into the one shared clone.
            return worktrees.create_worktree(
                str(self.repo), "main", f"conc{i:04d}-{i:04d}", str(self.root))

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            infos = list(ex.map(make, range(n)))

        # Every call produced a usable child worktree on a unique path/branch.
        self.assertEqual(len(infos), n)
        self.assertTrue(all(info.is_child for info in infos))
        self.assertEqual(len({info.path for info in infos}), n)
        self.assertEqual(len({info.branch for info in infos}), n)
        for info in infos:
            self.assertTrue(os.path.isdir(info.path))


class CurrentBranchTests(unittest.TestCase):
    """worktrees.current_branch — read the branch checked out at a path (PR-join backfill)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_checked_out_branch(self) -> None:
        repo = self.root / "repo"
        _make_repo(repo, branch="feat/x")
        self.assertEqual(worktrees.current_branch(str(repo)), "feat/x")

    def test_returns_none_for_detached_head(self) -> None:
        repo = self.root / "repo"
        _make_repo(repo, branch="main")
        _git("checkout", _rev(repo, "HEAD"), cwd=str(repo))   # detach
        self.assertIsNone(worktrees.current_branch(str(repo)))

    def test_returns_none_for_non_repo(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(worktrees.current_branch(str(plain)))


class AuthoredBranchesTests(unittest.TestCase):
    """worktrees.authored_branches — feature branches from reflog + current branch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_remote_refs_from_reflog_are_excluded(self) -> None:
        # `git checkout origin/main` writes "checkout: moving from … to origin/main"
        # into the reflog; a PR headRefName never carries the remote prefix, so
        # remote-tracking names must not leak into authored_branches (they bloat
        # the PR poller's GraphQL pair list — observed as origin/* DB pollution).
        repo = self.root / "repo"
        _make_repo(repo, branch="main")
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=str(repo))
        _git("checkout", "--detach", "origin/main", cwd=str(repo))
        _git("checkout", "-b", "feat/x", cwd=str(repo))
        self.assertEqual(worktrees.authored_branches(str(repo)), ["feat/x"])

    def test_is_remote_ref(self) -> None:
        self.assertTrue(worktrees.is_remote_ref("origin/main"))
        self.assertTrue(worktrees.is_remote_ref("remotes/origin/feat/x"))
        self.assertFalse(worktrees.is_remote_ref("feat/origin-fix"))
        self.assertFalse(worktrees.is_remote_ref("main"))


class DiskHeadroomTests(unittest.TestCase):
    """Tests for check_disk_headroom preflight and DiskSpaceError."""

    class _FakeStatvfs:
        """Shape-compatible with os.statvfs_result for the fields we read."""
        def __init__(self, *, frsize=4096, bavail=10_000_000, files=1_000_000, favail=500_000):
            self.f_frsize = frsize
            self.f_bavail = bavail
            self.f_files = files
            self.f_favail = favail

    def test_headroom_ok_no_raise(self) -> None:
        """Default headroom (2 GiB bytes, 100k inodes) with plenty available."""
        original_statvfs = os.statvfs
        try:
            os.statvfs = lambda p: self._FakeStatvfs()
            worktrees.check_disk_headroom("/tmp")  # ~40 GB / 500k inodes free — fine
        finally:
            os.statvfs = original_statvfs

    def test_headroom_low_bytes_raises(self) -> None:
        """Insufficient bytes → DiskSpaceError."""
        original_statvfs = os.statvfs
        try:
            os.statvfs = lambda p: self._FakeStatvfs(bavail=100)  # ~400 KB free
            with self.assertRaises(worktrees.DiskSpaceError) as ctx:
                worktrees.check_disk_headroom("/tmp")
            self.assertIn("insufficient disk headroom", str(ctx.exception))
        finally:
            os.statvfs = original_statvfs

    def test_headroom_low_inodes_raises(self) -> None:
        """Insufficient inodes → DiskSpaceError."""
        original_statvfs = os.statvfs
        try:
            os.statvfs = lambda p: self._FakeStatvfs(favail=500)
            with self.assertRaises(worktrees.DiskSpaceError) as ctx:
                worktrees.check_disk_headroom("/tmp")
            self.assertIn("inode", str(ctx.exception))
        finally:
            os.statvfs = original_statvfs

    def test_headroom_inode_check_skipped_when_fs_reports_none(self) -> None:
        """btrfs-style: f_files == 0 means no inode accounting — must not raise."""
        original_statvfs = os.statvfs
        try:
            os.statvfs = lambda p: self._FakeStatvfs(files=0, favail=0)
            worktrees.check_disk_headroom("/tmp")
        finally:
            os.statvfs = original_statvfs

    def test_headroom_env_overrides(self) -> None:
        """CCSWITCH_MIN_FREE_BYTES env var overrides default threshold."""
        original_statvfs = os.statvfs
        original_env = os.environ.copy()
        try:
            os.statvfs = lambda p: self._FakeStatvfs(bavail=10_000_000)
            os.environ["CCSWITCH_MIN_FREE_BYTES"] = str(10_000_000 * 4096 + 1)
            with self.assertRaises(worktrees.DiskSpaceError):
                worktrees.check_disk_headroom("/tmp")
        finally:
            os.statvfs = original_statvfs
            os.environ.clear()
            os.environ.update(original_env)

    def test_disk_space_error_is_worktree_error(self) -> None:
        """DiskSpaceError is a WorktreeError so existing catch-sites work."""
        self.assertTrue(issubclass(worktrees.DiskSpaceError, worktrees.WorktreeError))
        self.assertIn("insufficient disk headroom", worktrees.DISK_EXHAUSTION_MARKERS)

    def test_create_worktree_preflights_before_git(self) -> None:
        """When headroom is too low, create_worktree raises DiskSpaceError WITHOUT
        running any git subprocess (fail-fast: no churn on a full disk)."""
        tmp = tempfile.TemporaryDirectory()
        try:
            tmp_path = Path(tmp.name)
            # Create a real repo for rev-parse to work on
            subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)

            original_statvfs = os.statvfs
            calls: list[list[str]] = []

            def _no_git(args):
                calls.append(args)
                raise AssertionError(f"git must not run when disk is exhausted: {args}")

            # rev-parse runs BEFORE the preflight (cheap, read-only) — allow only that.
            real_run = worktrees._run

            def _guarded(args):
                if "rev-parse" in args:
                    return real_run(args)
                return _no_git(args)

            try:
                os.statvfs = lambda p: self._FakeStatvfs(favail=10)
                worktrees._run = _guarded
                with self.assertRaises(worktrees.DiskSpaceError):
                    worktrees.create_worktree(
                        str(tmp_path / "repo"), None, "sess1234", str(tmp_path / "root")
                    )
            finally:
                os.statvfs = original_statvfs
                worktrees._run = real_run
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class WorktreeFetchCredentialsTests(unittest.TestCase):
    """The worktree base fetch must use the same App credential as ensure_clone.

    Same anti-pattern as gitrepos._run_git before issue #887: a NETWORK git call
    through a bare subprocess.run, silently unauthenticated for a private repo.
    Its own docstring used to accept that ("private repos with no cached
    credentials") — that is the bug, not a fact of life.
    """

    def setUp(self) -> None:
        from ccswitch import gitrepos
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "clone"
        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin",
             "https://github.com/cairnea/KinDash.git"],
            check=True, capture_output=True)
        self.addCleanup(gitrepos.set_token_provider, None)

    def test_fetch_carries_the_app_credential_env(self) -> None:
        from unittest import mock
        from ccswitch import gitrepos
        gitrepos.set_token_provider(lambda owner: "ghs_wt")
        seen: dict = {}

        real = subprocess.run

        def spy(args, **kw):
            if "fetch" in args:
                seen.update(kw.get("env") or {})
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real(args, **kw)

        with mock.patch.object(worktrees.subprocess, "run", spy):
            worktrees._try_fetch_origin(str(self.repo))

        self.assertEqual(seen.get("CCSWITCH_GIT_TOKEN"), "ghs_wt")

    def test_fetch_without_a_provider_inherits_ambient_env(self) -> None:
        from unittest import mock
        from ccswitch import gitrepos
        gitrepos.set_token_provider(None)
        seen: list = []

        real = subprocess.run

        def spy(args, **kw):
            if "fetch" in args:
                seen.append(kw.get("env"))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real(args, **kw)

        with mock.patch.object(worktrees.subprocess, "run", spy):
            worktrees._try_fetch_origin(str(self.repo))

        self.assertEqual(seen, [None])
