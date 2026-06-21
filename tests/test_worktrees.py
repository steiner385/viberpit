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


if __name__ == "__main__":
    unittest.main()
