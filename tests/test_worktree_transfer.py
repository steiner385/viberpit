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
