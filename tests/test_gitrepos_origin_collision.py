"""A clone dir whose origin is a different repo is never reused (moved repos)."""
import hashlib
import logging
import subprocess
from pathlib import Path

import pytest

from viberpit import gitrepos


def _init(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "GitHub")


def _old_dir(root: str, origin: str | None, name: str = "acme/widget") -> Path:
    return _init(gitrepos.clones_base(root) / name, origin)


def test_collision_path_is_stable_hash_sibling(tmp_path: Path) -> None:
    p = gitrepos.collision_clone_path(tmp_path, "Acme/Widget")
    digest = hashlib.sha1(b"acme/widget").hexdigest()[:8]
    assert p == tmp_path / "Acme" / f"Widget@{digest}"
    assert gitrepos.collision_clone_path(tmp_path, "Acme/Widget") == p


def test_origin_matches_variants(tmp_path: Path) -> None:
    d = _init(tmp_path / "d", "git@github.com:Acme/Widget.git")
    assert gitrepos.clone_origin_matches(d, "acme/widget")
    assert not gitrepos.clone_origin_matches(d, "acme/other")


def test_no_git_dir_matches(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    assert gitrepos.clone_origin_matches(tmp_path / "x", "a/b")


def test_missing_origin_with_git_is_mismatch(tmp_path: Path) -> None:
    d = _init(tmp_path / "d", None)
    assert not gitrepos.clone_origin_matches(d, "a/b")


def test_matching_origin_reused(root: str) -> None:
    old = _old_dir(root, "https://github.com/acme/widget.git")
    assert gitrepos.clone_path(root, "acme/widget") == old


def test_case_variant_still_reused(root: str) -> None:
    old = _old_dir(root, "https://github.com/Acme/Widget.git", "Acme/Widget")
    assert gitrepos.clone_path(root, "acme/widget") == old


def test_mismatched_origin_gets_collision_path(root: str) -> None:
    old = _old_dir(root, "https://github.com/acme/elsewhere.git")
    p = gitrepos.clone_path(root, "acme/widget")
    assert p != old
    assert p == gitrepos.collision_clone_path(gitrepos.clones_base(root), "acme/widget")
    assert p == gitrepos.clone_path(root, "acme/widget")  # stable
    assert old.exists()


def test_collision_clone_reused_once_it_exists(root: str) -> None:
    _old_dir(root, "https://github.com/acme/elsewhere.git")
    coll = gitrepos.collision_clone_path(gitrepos.clones_base(root), "acme/widget")
    _init(coll, "https://github.com/acme/widget.git")
    assert gitrepos.clone_path(root, "acme/widget") == coll


def test_ensure_clone_new_sibling_old_untouched(
    root: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _init(work, None)
    for k, v in (("user.email", "t@e.com"), ("user.name", "T")):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
    (work / "f").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "i"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote)], check=True)

    old = _old_dir(root, "https://github.com/acme/elsewhere.git")
    caplog.set_level(logging.INFO)
    out = gitrepos.ensure_clone(root, "acme/widget", str(remote))
    assert Path(out) == gitrepos.collision_clone_path(gitrepos.clones_base(root), "acme/widget")
    assert (Path(out) / ".git").exists()
    url = subprocess.run(["git", "-C", str(old), "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    assert url == "https://github.com/acme/elsewhere.git"
    recs = [r for r in caplog.records if "clone.origin_mismatch" in r.getMessage()]
    assert recs and recs[0].levelno == logging.INFO
    # second call reuses the sibling
    assert gitrepos.ensure_clone(root, "acme/widget", str(remote)) == out


def test_doctor_maps_collision_dir_to_repo(root: str) -> None:
    from viberpit import clone_doctor
    coll = gitrepos.collision_clone_path(gitrepos.clones_base(root), "acme/widget")
    assert clone_doctor.name_with_owner_for(root, coll) == "acme/widget"


def test_relink_ignores_worktrees_of_the_old_clone(root: str, tmp_path: Path) -> None:
    """Old clone's worktrees keep their records and point at the old dir: untouched."""
    from unittest import mock

    from viberpit import clone_doctor
    old = _old_dir(root, "https://github.com/acme/elsewhere.git")
    new = gitrepos.collision_clone_path(gitrepos.clones_base(root), "acme/widget")
    link = mock.Mock(path="/wt/a", clone_dir=str(old), record_present=True)
    with mock.patch.object(clone_doctor, "scan_worktree_links", return_value=[link]), \
            mock.patch.object(clone_doctor, "repair_worktree_links") as repair:
        gitrepos._relink_orphaned_worktrees(root, str(new))
    repair.assert_not_called()


def test_non_github_origin_is_accepted(root: str) -> None:
    old = _old_dir(root, "/srv/git/anything.git")
    assert gitrepos.clone_path(root, "acme/widget") == old


def test_two_bases_rejected_in_one_sibling_in_other(root: str) -> None:
    from viberpit import brand
    if len(gitrepos.clones_bases(root)) < 2:
        pytest.skip(f"single namespace: {brand.REPO_NAMESPACES}")
    first, second = gitrepos.clones_bases(root)[:2]
    _init(first / "acme" / "widget", "https://github.com/acme/elsewhere.git")
    sib = _init(gitrepos.collision_clone_path(second, "acme/widget"),
                "https://github.com/acme/widget.git")
    assert gitrepos.clone_path(root, "acme/widget") == sib


def test_unreadable_vs_other_repo_log_reason(
    root: str, caplog: pytest.LogCaptureFixture
) -> None:
    _old_dir(root, None)
    caplog.set_level(logging.INFO)
    dest, rejected = gitrepos._resolve_clone(root, "acme/widget")
    assert rejected is not None and gitrepos._origin_url(rejected) is None
