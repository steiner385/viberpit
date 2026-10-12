"""The server suite keeps its temp files in one self-reaping run root (#2993).

Covers server/tests/_run_scratch.py against real directories and real flocks
under tmp_path: a dead run's root is reaped, a live one is not, lockless
leftovers go by age, and a child process joins its parent's root.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from server.tests import _run_scratch as rs

REPO_ROOT = Path(__file__).resolve().parents[2]
OLD = rs.LOCKLESS_GRACE_S + 60


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    """A private stand-in for the system temp dir; restores tempfile.tempdir."""
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(rs, "_held", {})
    return tmp_path


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then), follow_symlinks=False)


def _dead_root(tmp: Path, name: str = "vpt-dead") -> Path:
    """A root whose owner is gone: lock file present, nobody holding it."""
    root = tmp / name
    root.mkdir()
    (root / rs.LOCK_NAME).touch()
    (root / "viberpit-test-db-x.sqlite").write_text("x")
    return root


# ------------------------------------------------------------------ sweep_stale


def test_sweep_removes_a_root_whose_owner_died(tmp):
    root = _dead_root(tmp)
    assert rs.sweep_stale(tmp) == 1
    assert not root.exists()


def test_sweep_spares_a_root_with_a_live_holder(tmp):
    root = _dead_root(tmp, "vpt-live")
    fd = os.open(root / rs.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        _age(root, OLD)  # age is irrelevant once a lock file exists
        assert rs.sweep_stale(tmp) == 0
        assert (root / "viberpit-test-db-x.sqlite").exists()
    finally:
        os.close(fd)
    assert rs.sweep_stale(tmp) == 1  # the holder is gone now


def test_sweep_reaps_a_killed_process_root(tmp):
    """The real failure: SIGKILL skips atexit, the kernel still drops the lock."""
    script = textwrap.dedent(
        """
        import os, signal, sys
        from pathlib import Path
        from server.tests import _run_scratch as rs
        root = rs.install({}, tmp=Path(sys.argv[1]))
        (root / "leak.sqlite").write_text("x")
        print(root, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    root = Path(proc.stdout.strip())
    assert proc.returncode < 0 and (root / "leak.sqlite").exists(), proc.stderr
    assert rs.sweep_stale(tmp) == 1
    assert not root.exists()


def test_sweep_reaps_lockless_entries_only_past_the_grace(tmp):
    old_db = tmp / "viberpit-test-db-old.sqlite"
    new_db = tmp / "viberpit-test-db-new.sqlite"
    old_dir = tmp / "viberpit-tests-old"
    kept = tmp / "vpt-kept"  # VIBERPIT_TEST_KEEP_TMP root: no lock file
    for f in (old_db, new_db):
        f.write_text("x")
    for d in (old_dir, kept):
        d.mkdir()
        (d / "f").write_text("x")
    for p in (old_db, old_dir):
        _age(p, OLD)

    assert rs.sweep_stale(tmp) == 2
    assert not old_db.exists() and not old_dir.exists()
    assert new_db.exists() and kept.exists()

    _age(kept, OLD)
    assert rs.sweep_stale(tmp) == 1
    assert not kept.exists()


def test_sweep_ignores_unrelated_and_symlinked_entries(tmp):
    other = tmp / "someone-elses-file"
    other.write_text("x")
    target = tmp / "precious"
    target.mkdir()
    (target / "f").write_text("x")
    link = tmp / "vpt-link"
    link.symlink_to(target)
    for p in (other, link):
        _age(p, OLD)

    rs.sweep_stale(tmp)
    assert other.exists()
    assert (target / "f").exists()  # the link went, never its target
    assert not link.is_symlink()


def test_sweep_of_a_missing_dir_is_a_noop(tmp):
    assert rs.sweep_stale(tmp / "nope") == 0


# ---------------------------------------------------------------------- install


def test_install_creates_a_locked_root_and_repoints_tempfile(tmp):
    env: dict[str, str] = {}
    root = rs.install(env, tmp=tmp)

    assert root.parent == tmp and root.name.startswith(rs.ROOT_PREFIX)
    assert env[rs.ROOT_ENV] == env["TMPDIR"] == str(root)
    assert tempfile.gettempdir() == str(root)
    fd, path = tempfile.mkstemp()
    os.close(fd)
    assert Path(path).parent == root
    # Held by this process, so a concurrent run's sweep leaves it alone.
    assert rs.sweep_stale(tmp) == 0 and root.exists()


def test_install_reaps_dead_roots_first(tmp):
    dead = _dead_root(tmp)
    rs.install({}, tmp=tmp)
    assert not dead.exists()


def test_install_adopts_the_inherited_root(tmp):
    parent = rs.install({}, tmp=tmp)
    other_dead = _dead_root(tmp)

    child = rs.install({rs.ROOT_ENV: str(parent)}, tmp=tmp)

    assert child == parent
    assert other_dead.exists()  # only the run's creator sweeps


@pytest.mark.parametrize("value", ["", "/etc", "{tmp}/not-a-root", "{tmp}/vpt-gone"])
def test_install_refuses_an_inherited_root_it_cannot_trust(tmp, value):
    (tmp / "not-a-root").mkdir()
    root = rs.install({rs.ROOT_ENV: value.format(tmp=tmp)}, tmp=tmp)
    assert root.parent == tmp and root.name.startswith(rs.ROOT_PREFIX)
    assert root.name != "vpt-gone" and (root / rs.LOCK_NAME).exists()


def test_install_does_not_adopt_a_root_being_reaped(tmp):
    """A sweeper holding the exclusive lock means the root is going away."""
    doomed = _dead_root(tmp, "vpt-doomed")
    fd = os.open(doomed / rs.LOCK_NAME, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.unlink(doomed / rs.LOCK_NAME)  # what the sweeper's rmtree does next
    try:
        root = rs.install({rs.ROOT_ENV: str(doomed)}, tmp=tmp)
    finally:
        os.close(fd)
    assert root != doomed


def test_a_child_inheriting_tmpdir_joins_the_root_instead_of_nesting(tmp):
    """An xdist worker / spawned pytest sees TMPDIR=<root> before importing us."""
    parent = rs.install({}, tmp=tmp)
    script = textwrap.dedent(
        """
        from server.tests import _run_scratch as rs
        print(rs.SYSTEM_TMP)
        print(rs.install())
        """
    )
    env = {**os.environ, rs.ROOT_ENV: str(parent), "TMPDIR": str(parent)}
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.split()
    assert out == [str(tmp), str(parent)]
    assert [p.name for p in parent.iterdir() if p.name.startswith(rs.ROOT_PREFIX)] == []


def test_keep_tmp_root_is_lockless_and_survives_exit(tmp):
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from server.tests import _run_scratch as rs
        print(rs.install({rs.KEEP_ENV: "1"}, tmp=Path(sys.argv[1])))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script, str(tmp)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    root = Path(out)
    assert root.is_dir() and not (root / rs.LOCK_NAME).exists()
    assert rs.sweep_stale(tmp) == 0  # kept until the grace period lapses


def test_root_is_removed_on_a_clean_exit(tmp):
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from server.tests import _run_scratch as rs
        print(rs.install({}, tmp=Path(sys.argv[1])))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script, str(tmp)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    assert not Path(out).exists()


# ------------------------------------------------------------------- the suite


def test_this_run_keeps_its_temp_files_in_a_run_root():
    """conftest installed the root: nothing here writes loose into /tmp."""
    here = Path(tempfile.gettempdir())
    assert here.parent == rs.SYSTEM_TMP and here.name.startswith(rs.ROOT_PREFIX)
    assert os.environ[rs.ROOT_ENV] == os.environ["TMPDIR"] == str(here)


def test_tmp_path_lives_under_the_run_root(tmp_path):
    assert tmp_path.is_relative_to(tempfile.gettempdir())
