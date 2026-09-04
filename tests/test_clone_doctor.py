"""Tests for ccswitch.clone_doctor — the #1003 clone guard + worktree recovery.

These run against REAL git (constitution III): the whole point is that
``git worktree repair`` alone cannot recover a worktree whose clone was deleted,
so a mocked git would test the wrong thing.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from ccswitch import clone_doctor, gitrepos, worktrees


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


def _make_remote(path: Path) -> None:
    """A bare repo with one commit on ``main``, acting as the clone source."""
    work = path.parent / (path.name + "-work")
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", str(work), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=str(work))
    _git("config", "user.name", "Test", cwd=str(work))
    (work / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=str(work))
    _git("commit", "-m", "init", cwd=str(work))
    _git("remote", "add", "origin", str(path), cwd=str(work))
    _git("push", "origin", "main", cwd=str(work))


class CloneDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "GitHub"          # repo_root
        self.root.mkdir()
        self.remote = self.base / "remote.git"
        _make_remote(self.remote)
        self.url = str(self.remote)
        self.clone = Path(gitrepos.ensure_clone(str(self.root), "cairnea/KinDash", self.url))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _url_for(self, name_with_owner: str) -> str:
        """Re-clone from the local bare fixture, not github.com.

        Without this seam ``repair`` would reconstruct
        ``https://github.com/cairnea/KinDash.git`` and actually hit the network —
        passing only on a machine with ambient credentials for that private repo
        and failing in CI with ``could not read Username``.
        """
        return self.url

    def _add_worktree(self, session_id: str = "2a158cb3") -> Path:
        info = worktrees.create_worktree(str(self.clone), None, session_id, str(self.root))
        return Path(info.path)

    # -- link discovery ----------------------------------------------------

    def test_scan_links_resolves_worktree_to_its_clone(self) -> None:
        wt = self._add_worktree()
        links = clone_doctor.scan_worktree_links(str(self.root))
        self.assertEqual([link.path for link in links], [str(wt)])
        self.assertEqual(links[0].clone_dir, str(self.clone))
        self.assertTrue(links[0].record_present)

    def test_clone_dir_from_gitdir_rejects_foreign_layout(self) -> None:
        self.assertIsNone(clone_doctor.clone_dir_from_gitdir("/somewhere/.git"))
        self.assertEqual(
            clone_doctor.clone_dir_from_gitdir("/c/repo/.git/worktrees/ab12"), "/c/repo")

    def test_referencing_worktrees_lists_dependents(self) -> None:
        wt = self._add_worktree()
        self.assertEqual(
            clone_doctor.referencing_worktrees(str(self.root), self.clone), [str(wt)])

    # -- diagnosis ---------------------------------------------------------

    def test_diagnose_flags_missing_clone(self) -> None:
        wt = self._add_worktree()
        _rmtree(self.clone)

        reports = clone_doctor.diagnose(str(self.root))
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertFalse(report.exists)
        self.assertEqual(report.name_with_owner, "cairnea/KinDash")
        self.assertEqual([link.path for link in report.broken], [str(wt)])
        self.assertFalse(report.healthy)

    def test_diagnose_healthy_clone_has_no_broken_links(self) -> None:
        self._add_worktree()
        reports = clone_doctor.diagnose(str(self.root))
        self.assertTrue(reports[0].healthy)
        self.assertTrue(reports[0].exists)

    # -- recovery ----------------------------------------------------------

    def test_repair_relinks_worktree_and_keeps_uncommitted_work(self) -> None:
        """The #1003 incident, replayed: clone deleted under a live worktree."""
        wt = self._add_worktree()
        (wt / "README.md").write_text("edited by the session\n")
        (wt / "scratch.txt").write_text("untracked work\n")
        _rmtree(self.clone)

        # Precondition: every git call in the worktree is fatal.
        broken = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                capture_output=True, text=True)
        self.assertNotEqual(broken.returncode, 0)

        results = clone_doctor.repair(
            str(self.root), base_ref="origin/main", url_for=self._url_for)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "repaired")
        self.assertEqual(results[0]["results"], {str(wt): "repaired"})

        status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        # The edit reads as a modification (not a mass deletion from an empty
        # index) and the untracked file survives.
        self.assertIn(" M README.md", status.stdout)
        self.assertIn("?? scratch.txt", status.stdout)
        self.assertEqual((wt / "README.md").read_text(), "edited by the session\n")
        self.assertEqual((wt / "scratch.txt").read_text(), "untracked work\n")

    def test_repair_relocks_recovered_worktree(self) -> None:
        wt = self._add_worktree()
        _rmtree(self.clone)
        clone_doctor.repair(str(self.root), base_ref="origin/main", url_for=self._url_for)
        listing = _git("worktree", "list", "--porcelain", cwd=str(self.clone)).stdout
        self.assertIn("locked", listing)
        self.assertIn(str(wt), listing)

    def test_repair_is_idempotent(self) -> None:
        self._add_worktree()
        _rmtree(self.clone)
        clone_doctor.repair(str(self.root), base_ref="origin/main", url_for=self._url_for)
        self.assertEqual(clone_doctor.repair(str(self.root), url_for=self._url_for), [])

    def test_repair_dry_run_changes_nothing(self) -> None:
        wt = self._add_worktree()
        _rmtree(self.clone)
        results = clone_doctor.repair(str(self.root), dry_run=True)
        self.assertEqual(results[0]["action"], "would-repair")
        self.assertEqual(results[0]["worktrees"], [str(wt)])
        self.assertFalse(self.clone.exists())

    def test_repair_reports_a_failed_reclone_without_crashing(self) -> None:
        """No network / no credentials must degrade to a reported failure.

        The orphaned worktrees stay exactly as they were — still recoverable
        once the clone source is reachable again.
        """
        wt = self._add_worktree()
        _rmtree(self.clone)
        results = clone_doctor.repair(
            str(self.root), url_for=lambda repo: str(self.base / "no-such-remote.git"))
        self.assertEqual(results[0]["action"], "failed")
        self.assertIn("git clone failed", results[0]["error"])
        self.assertTrue(wt.exists())

    def test_default_clone_url_is_the_github_https_remote(self) -> None:
        self.assertEqual(
            clone_doctor.default_clone_url("cairnea/KinDash"),
            "https://github.com/cairnea/KinDash.git",
        )

    def test_ensure_clone_self_heals_orphans(self) -> None:
        """Re-cloning at the orphaned path re-links its worktrees automatically."""
        wt = self._add_worktree()
        _rmtree(self.clone)
        gitrepos.ensure_clone(str(self.root), "cairnea/KinDash", self.url)
        status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)

    # -- guarded removal ---------------------------------------------------

    def test_remove_clone_refuses_while_worktrees_reference_it(self) -> None:
        self._add_worktree()
        with self.assertRaises(clone_doctor.CloneInUseError):
            clone_doctor.remove_clone(str(self.root), self.clone, reason="test")
        self.assertTrue((self.clone / ".git").exists())

    def test_remove_clone_logs_the_block_with_counts(self) -> None:
        self._add_worktree()
        with self.assertLogs("ccswitch.clone_doctor", level="ERROR") as logs:
            with self.assertRaises(clone_doctor.CloneInUseError):
                clone_doctor.remove_clone(str(self.root), self.clone, reason="pytest")
        line = "\n".join(logs.output)
        self.assertIn("clone.remove_blocked", line)
        self.assertIn("worktrees=1", line)
        self.assertIn("reason=pytest", line)

    def test_remove_clone_allows_unreferenced_clone_and_audits_it(self) -> None:
        with self.assertLogs("ccswitch.clone_doctor", level="INFO") as logs:
            result = clone_doctor.remove_clone(
                str(self.root), self.clone, reason="operator cleanup")
        self.assertTrue(result["removed"])
        self.assertFalse(self.clone.exists())
        line = "\n".join(logs.output)
        self.assertIn("clone.remove ", line)
        self.assertIn("repo=cairnea/KinDash", line)

    def test_remove_clone_force_overrides_but_warns(self) -> None:
        self._add_worktree()
        with self.assertLogs("ccswitch.clone_doctor", level="WARNING") as logs:
            result = clone_doctor.remove_clone(
                str(self.root), self.clone, reason="test", force=True)
        self.assertTrue(result["removed"])
        self.assertEqual(result["worktrees"], 1)
        self.assertIn("clone.remove_forced", "\n".join(logs.output))

    def test_remove_clone_never_touches_a_path_outside_the_clones_base(self) -> None:
        outside = self.root / "my-real-checkout"
        outside.mkdir()
        with self.assertRaises(clone_doctor.CloneInUseError):
            clone_doctor.remove_clone(str(self.root), outside, reason="test", force=True)
        self.assertTrue(outside.exists())


def _rmtree(path: Path) -> None:
    """Delete a clone the way the incident did — straight rmtree, no guard."""
    import shutil

    shutil.rmtree(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
