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
