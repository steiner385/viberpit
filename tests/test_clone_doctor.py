"""Tests for viberpit.clone_doctor — the #1003 clone guard + worktree recovery.

These run against REAL git (constitution III): the whole point is that
``git worktree repair`` alone cannot recover a worktree whose clone was deleted,
so a mocked git would test the wrong thing.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from viberpit import clone_doctor, gitrepos, worktrees


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
            str(self.root), base_ref="origin/main", clone_url_for=self._url_for)
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
        clone_doctor.repair(str(self.root), base_ref="origin/main", clone_url_for=self._url_for)
        listing = _git("worktree", "list", "--porcelain", cwd=str(self.clone)).stdout
        self.assertIn("locked", listing)
        self.assertIn(str(wt), listing)

    def test_repair_is_idempotent(self) -> None:
        self._add_worktree()
        _rmtree(self.clone)
        clone_doctor.repair(str(self.root), base_ref="origin/main", clone_url_for=self._url_for)
        self.assertEqual(clone_doctor.repair(str(self.root), clone_url_for=self._url_for), [])

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
            str(self.root), clone_url_for=lambda repo: str(self.base / "no-such-remote.git"))
        self.assertEqual(results[0]["action"], "failed")
        self.assertIn("git clone failed", results[0]["error"])
        self.assertTrue(wt.exists())

    def test_repair_relinks_onto_the_clone_ensure_clone_resolved_to(self) -> None:
        """#1992: a case-variant clone name must not defeat recovery.

        ``clone_path`` deliberately collapses ``cairnea/kindash`` onto an
        existing ``cairnea/KinDash`` (#1003), so ``ensure_clone`` is a no-op and
        returns the OTHER spelling.  Re-linking against the diagnosed (still
        missing) path made every worktree fail with "clone has no resolvable
        base commit", so ``clones repair`` — the remedy the CRITICAL names —
        could never clear the alert.
        """
        wt = self._add_worktree("0badcafe")
        (wt / "uncommitted.txt").write_text("keep me\n")
        lower = self.clone.parent / "kindash"
        (wt / ".git").write_text(f"gitdir: {lower}/.git/worktrees/0badcafe\n")

        report = clone_doctor.diagnose(str(self.root))[0]
        self.assertEqual(report.clone_dir, str(lower))
        self.assertFalse(report.exists)

        results = clone_doctor.repair(
            str(self.root), base_ref="origin/main", clone_url_for=self._url_for)
        self.assertEqual(results[0]["action"], "repaired")
        self.assertEqual(results[0]["results"], {str(wt): "repaired"})
        # The clone actually used is reported, not just the missing one asked for.
        self.assertEqual(results[0]["clone"], str(lower))
        self.assertEqual(results[0]["resolvedClone"], str(self.clone))
        # No second clone was created under the lowercase spelling.
        self.assertFalse(lower.exists())
        # The condition is cleared, and uncommitted work survived.
        self.assertEqual(clone_doctor.diagnose(str(self.root))[0].broken, [])
        self.assertEqual((wt / "uncommitted.txt").read_text(), "keep me\n")

    def test_repair_action_reflects_the_per_worktree_outcomes(self) -> None:
        """``repaired`` must never be printed for a run that repaired nothing.

        ``action`` used to be assigned before ``repair_worktree_links`` ran, so
        ``ccswitch clones repair`` reported success for a run in which every
        worktree errored.
        """
        wt = self._add_worktree()
        _rmtree(self.clone)
        results = clone_doctor.repair(
            str(self.root), base_ref="refs/heads/no-such-branch",
            clone_url_for=self._url_for)
        self.assertEqual(results[0]["results"],
                         {str(wt): "error: clone has no resolvable base commit"})
        self.assertEqual(results[0]["action"], "failed")

    def test_repair_names_the_operator_step_for_an_unmanaged_clone_path(self) -> None:
        """A clone path outside the clones base can never be re-cloned here.

        ``repair`` has no ``<owner>/<name>`` to clone from, so it must say what
        a human has to do instead of reporting a bare "skipped" that reads like
        "nothing was wrong".
        """
        wt = self._add_worktree()
        outside = self.root / "my-real-checkout"
        (wt / ".git").write_text(f"gitdir: {outside}/.git/worktrees/2a158cb3\n")

        results = clone_doctor.repair(str(self.root), clone_url_for=self._url_for)
        self.assertEqual(results[0]["action"], "unrepairable")
        self.assertIn("outside", results[0]["error"])
        self.assertIn("salvage", results[0]["error"])
        self.assertEqual(results[0]["worktrees"], [str(wt)])
        self.assertTrue(wt.exists())
        # A dry run must not promise a repair the real run refuses.
        self.assertEqual(
            clone_doctor.repair(str(self.root), dry_run=True)[0]["action"], "unrepairable")

    def test_repair_recovers_a_clone_that_only_lost_its_admin_records(self) -> None:
        """A present clone outside the clones base is still repairable (#1992).

        Seen on dobby: the clone is on disk, only ``.git/worktrees/<id>`` died,
        so ``repair_worktree_links`` has a real clone dir to rebuild from — the
        janitor must not file this as the unrecoverable shape.
        """
        wt = self._add_worktree()
        record = self.clone / ".git" / "worktrees" / wt.name
        shutil.rmtree(record)

        self.assertTrue(clone_doctor.clone_is_repairable(str(self.root), self.clone))
        results = clone_doctor.repair(
            str(self.root), base_ref="origin/main", clone_url_for=self._url_for)
        self.assertEqual(results[0]["action"], "repaired")
        self.assertEqual(results[0]["results"], {str(wt): "repaired"})

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
        with self.assertLogs("viberpit.clone_doctor", level="ERROR") as logs:
            with self.assertRaises(clone_doctor.CloneInUseError):
                clone_doctor.remove_clone(str(self.root), self.clone, reason="pytest")
        line = "\n".join(logs.output)
        self.assertIn("clone.remove_blocked", line)
        self.assertIn("worktrees=1", line)
        self.assertIn("reason=pytest", line)

    def test_remove_clone_allows_unreferenced_clone_and_audits_it(self) -> None:
        with self.assertLogs("viberpit.clone_doctor", level="INFO") as logs:
            result = clone_doctor.remove_clone(
                str(self.root), self.clone, reason="operator cleanup")
        self.assertTrue(result["removed"])
        self.assertFalse(self.clone.exists())
        line = "\n".join(logs.output)
        self.assertIn("clone.remove ", line)
        self.assertIn("repo=cairnea/KinDash", line)

    def test_remove_clone_force_overrides_but_warns(self) -> None:
        self._add_worktree()
        with self.assertLogs("viberpit.clone_doctor", level="WARNING") as logs:
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


class LegacyNamespaceTests(unittest.TestCase):
    """#1373: a repo root that already had the legacy dot-dir keeps its clones there,
    and the doctor must still see the tree after the brand dot-dir appears beside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "GitHub"
        (self.root / ".ccswitch").mkdir(parents=True)
        self.remote = self.base / "remote.git"
        _make_remote(self.remote)
        self.clone = Path(gitrepos.ensure_clone(str(self.root), "cairnea/KinDash", str(self.remote)))
        info = worktrees.create_worktree(str(self.clone), None, "2a158cb3", str(self.root))
        self.wt = Path(info.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_legacy_tree_is_created_and_scanned_in_place(self) -> None:
        self.assertEqual(self.clone, self.root / ".ccswitch" / "clones" / "cairnea" / "KinDash")
        # The clone stays in the legacy dot-dir; the new worktree goes to the
        # non-hidden session dir (#2934) and is still linked to that clone.
        self.assertTrue(str(self.wt).startswith(str(self.root / "viberpit-sessions" / "worktrees")))
        (self.root / ".viberpit").mkdir()  # the brand dot-dir appears out-of-band
        links = clone_doctor.scan_worktree_links(str(self.root))
        self.assertEqual([link.path for link in links], [str(self.wt)])
        self.assertEqual(
            clone_doctor.name_with_owner_for(str(self.root), self.clone), "cairnea/KinDash")
        self.assertEqual(
            clone_doctor.referencing_worktrees(str(self.root), self.clone), [str(self.wt)])
        with self.assertRaises(clone_doctor.CloneInUseError):
            clone_doctor.remove_clone(str(self.root), self.clone, reason="test")
        self.assertTrue(self.clone.is_dir())


def _rmtree(path: Path) -> None:
    """Delete a clone the way the incident did — straight rmtree, no guard."""
    import shutil

    shutil.rmtree(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
