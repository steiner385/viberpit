"""The per-repo namespace resolver and its accept-both readers (#1373).

New repo roots get the brand dot-dir; a root that already has ONLY the legacy one
keeps it forever, because live worktrees hold absolute ``gitdir:`` pointers into it.
"""
import tempfile
import unittest
from pathlib import Path

from viberpit import brand, gitrepos, paths, worktrees

NEW, OLD = brand.REPO_NAMESPACE, brand.LEGACY_REPO_NAMESPACE


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_spellings(self) -> None:
        self.assertEqual(brand.REPO_NAMESPACES, (".viberpit", ".ccswitch"))
        self.assertEqual(brand.CODEX_ATTACHMENTS_DIRNAMES,
                         (".viberpit-codex-attachments", ".ccswitch-codex-attachments"))

    def test_fresh_root_gets_the_brand_namespace(self) -> None:
        self.assertEqual(paths.repo_namespace(self.root), NEW)
        self.assertEqual(paths.repo_namespace(self.root / "not-yet-created"), NEW)
        self.assertEqual(gitrepos.clones_base(str(self.root)), self.root / NEW / "clones")
        self.assertEqual(worktrees.worktrees_base(str(self.root)), self.root / NEW / "worktrees")
        self.assertEqual(worktrees.scratch_base(str(self.root)), self.root / NEW / "scratch")

    def test_legacy_only_root_keeps_legacy_and_nothing_moves(self) -> None:
        wt = self.root / OLD / "worktrees" / "KinDash" / "9be6e554"
        wt.mkdir(parents=True)
        self.assertEqual(paths.repo_namespace(self.root), OLD)
        self.assertEqual(worktrees.worktrees_base(str(self.root)), self.root / OLD / "worktrees")
        self.assertEqual(gitrepos.clones_base(str(self.root)), self.root / OLD / "clones")
        self.assertTrue(wt.is_dir())
        self.assertFalse((self.root / NEW).exists())

    def test_both_present_resolves_to_brand_deterministically(self) -> None:
        (self.root / OLD).mkdir()
        (self.root / NEW).mkdir()
        for _ in range(3):
            self.assertEqual(paths.repo_namespace(self.root), NEW)
        self.assertEqual(paths.repo_namespace_dirs(self.root),
                         (self.root / NEW, self.root / OLD))

    def test_readers_list_the_resolved_namespace_first(self) -> None:
        (self.root / OLD).mkdir()
        self.assertEqual(paths.repo_namespace_dirs(self.root),
                         (self.root / OLD, self.root / NEW))

    def test_legacy_file_is_not_a_namespace(self) -> None:
        (self.root / OLD).write_text("not a dir")
        self.assertEqual(paths.repo_namespace(self.root), NEW)

    def test_codex_staging_follows_the_same_rule(self) -> None:
        ws = self.root / "ws"
        ws.mkdir()
        self.assertEqual(paths.codex_attachment_staging_dirname(ws),
                         brand.CODEX_ATTACHMENTS_DIRNAME)
        (ws / brand.LEGACY_CODEX_ATTACHMENTS_DIRNAME).mkdir()
        self.assertEqual(paths.codex_attachment_staging_dirname(ws),
                         brand.LEGACY_CODEX_ATTACHMENTS_DIRNAME)
        (ws / brand.CODEX_ATTACHMENTS_DIRNAME).mkdir()
        self.assertEqual(paths.codex_attachment_staging_dirname(ws),
                         brand.CODEX_ATTACHMENTS_DIRNAME)


class AcceptBothTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_in_repo_namespace_matches_either_segment_only(self) -> None:
        for ns in brand.REPO_NAMESPACES:
            self.assertTrue(brand.in_repo_namespace(Path(f"/h/GitHub/{ns}/worktrees/r/1").parts))
        for lookalike in ("/h/GitHub/viberpit/x", "/h/.viberpit-fake/x",
                          "/h/.ccswitch-codex-attachments/x", "/h/GitHub/KinDash"):
            self.assertFalse(brand.in_repo_namespace(Path(lookalike).parts), lookalike)

    def test_is_clone_path_matches_both_spellings(self) -> None:
        for ns in brand.REPO_NAMESPACES:
            self.assertTrue(gitrepos.is_clone_path(f"/h/GitHub/{ns}/clones/o/n"))
            self.assertFalse(gitrepos.is_clone_path(f"/h/GitHub/{ns}/worktrees/clones/x"))

    def test_clone_path_reuses_a_legacy_clone_when_both_namespaces_exist(self) -> None:
        # A root that gained the brand dot-dir out-of-band must never re-clone a
        # repo its live worktrees already point into.
        legacy = self.root / OLD / "clones" / "cairnea" / "KinDash"
        legacy.mkdir(parents=True)
        (self.root / NEW).mkdir()
        self.assertEqual(gitrepos.clone_path(str(self.root), "cairnea/kindash"), legacy)
        self.assertEqual(gitrepos.clone_path(str(self.root), "acme/widget"),
                         self.root / NEW / "clones" / "acme" / "widget")

    def test_managed_and_scratch_paths_in_both_namespaces(self) -> None:
        (self.root / NEW).mkdir()
        for ns in brand.REPO_NAMESPACES:
            wt = self.root / ns / "worktrees" / "KinDash" / "abc"
            scratch = self.root / ns / "scratch" / "sid"
            wt.mkdir(parents=True)
            scratch.mkdir(parents=True)
            self.assertTrue(worktrees.is_managed_path(str(wt), str(self.root)), ns)
            self.assertTrue(worktrees.is_scratch_path(str(scratch), str(self.root)), ns)
            self.assertFalse(worktrees.is_scratch_path(str(wt), str(self.root)), ns)
        self.assertFalse(worktrees.is_managed_path(str(self.root / "KinDash"), str(self.root)))


if __name__ == "__main__":
    unittest.main()
