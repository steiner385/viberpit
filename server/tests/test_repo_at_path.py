"""Tests for gitrepos.repo_at_path — deriving owner/name from a working dir's git remote.

The sessions screen's repo indicator is read live from each session's working
directory rather than a stored field, so this helper must turn a real on-disk
checkout into a GitHub nameWithOwner ("owner/name") and degrade gracefully when
the directory is not a git repo / has no GitHub origin.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccswitch import gitrepos


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path, origin: str | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    if origin is not None:
        _git(path, "remote", "add", "origin", origin)


def test_https_origin_yields_owner_name(tmp_path):
    repo = tmp_path / "work"
    _init_repo(repo, "https://github.com/acme/widget.git")
    assert gitrepos.repo_at_path(str(repo)) == "acme/widget"


def test_ssh_origin_yields_owner_name(tmp_path):
    repo = tmp_path / "work"
    _init_repo(repo, "git@github.com:cairnea/KinDash.git")
    assert gitrepos.repo_at_path(str(repo)) == "cairnea/KinDash"


def test_non_git_dir_returns_none(tmp_path):
    plain = tmp_path / "scratch"
    plain.mkdir()
    assert gitrepos.repo_at_path(str(plain)) is None


def test_git_dir_without_origin_returns_none(tmp_path):
    repo = tmp_path / "work"
    _init_repo(repo, origin=None)
    assert gitrepos.repo_at_path(str(repo)) is None


def test_empty_or_missing_path_returns_none(tmp_path):
    assert gitrepos.repo_at_path("") is None
    assert gitrepos.repo_at_path(str(tmp_path / "does-not-exist")) is None
