"""One self-reaping scratch root per server-suite run (#2993).

Everything a run writes to the temp dir — the durable-in-memory SQLite files,
the redirected ViberPit home, helper scratch, every stray ``tempfile.mkstemp``
/ ``mkdtemp`` / ``NamedTemporaryFile(delete=False)`` in a test, and pytest's
own ``tmp_path`` base — lands under ONE directory, ``<tmp>/vpt-XXXXXXXX``,
because ``install()`` points ``tempfile.tempdir`` and ``$TMPDIR`` at it.

That makes cleanup a single ``rmtree`` instead of thousands of per-file
``atexit`` callbacks, and — the actual bug — makes it survivable. ``atexit``
never runs when a run is SIGKILLed (OOM, a Bash-tool timeout, a daemon
restart), SIGTERMed, or ended by pytest-timeout's ``os._exit``; each such run
used to strand ~2,500 ``viberpit-test-db-*`` files until /tmp hit its quota and
every session on the host lost its Bash output.

So liveness is a kernel fact, not a promise: every process using a root holds
a shared ``flock`` on ``<root>/.alive``. The kernel drops it when the holder
exits for ANY reason, so the next run's ``sweep_stale()`` can take the lock
exclusively exactly when nobody is left, and removes the root. xdist workers
(and any pytest a test spawns) inherit ``VIBERPIT_TEST_RUN_ROOT`` and join the
controller's root instead of making their own, so ``tmp_path`` — which the
controller allocates — stays under every worker's ``tempfile.gettempdir()``.

Entries with no lock file are reaped by age alone (``LOCKLESS_GRACE_S``): the
loose files and ``viberpit-tests-*`` dirs written before this module existed
(and still written by older checkouts), and roots kept on purpose with
``VIBERPIT_TEST_KEEP_TMP=1`` for a post-mortem.

Every filesystem error fails open: a run must never fail because of tidying.
"""
from __future__ import annotations

import atexit
import fcntl
import os
import shutil
import tempfile
import time
from collections.abc import MutableMapping
from pathlib import Path

# Short on purpose: AF_UNIX paths cap at 108 bytes and tmp_path sits below this.
ROOT_PREFIX = "vpt-"
LOCK_NAME = ".alive"
ROOT_ENV = "VIBERPIT_TEST_RUN_ROOT"
KEEP_ENV = "VIBERPIT_TEST_KEEP_TMP"


def _system_tmp() -> Path:
    """The host's real temp dir, even in a process already pointed at a root.

    A child of a run (xdist worker, a pytest a test spawns) inherits
    ``TMPDIR=<root>``, and a re-import of this module runs after install()
    repointed ``tempfile.tempdir``; either way ``gettempdir()`` answers the
    root. Its parent is the real temp dir, and anchoring there is what lets the
    child adopt the root instead of nesting a new one inside it.
    """
    inherited = os.environ.get(ROOT_ENV)
    current = Path(tempfile.gettempdir()).resolve()
    if inherited and Path(inherited).resolve() == current:
        return current.parent
    return current


# Guards that mean "a caller's own throwaway location" compare against this,
# not against the (narrower) run root.
SYSTEM_TMP = _system_tmp()

# What the suite wrote straight into the temp dir before it had a run root.
LEGACY_PREFIXES = ("viberpit-tests-", "viberpit-test-db-")

# A lockless entry may belong to a live run of an older checkout, which can
# queue an hour for a test slot and then run for ~25 minutes.
LOCKLESS_GRACE_S = 6 * 3600

# The lock is held for the life of the process; the fd is never closed.
_held: dict[Path, int] = {}


def _hold_shared(root: Path, *, create: bool) -> bool:
    """Take a shared liveness lock on ``root``; False if the root is gone."""
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    try:
        fd = os.open(root / LOCK_NAME, flags, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        # A sweeper may have won the lock between our open and our flock and
        # removed the root; an unlinked lock file is how that shows.
        if os.fstat(fd).st_nlink == 0:
            raise FileNotFoundError(root)
    except OSError:
        os.close(fd)
        return False
    _held[root] = fd
    return True


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError:
        pass


def _is_dead_root(root: Path) -> bool | None:
    """True/False for a locked root; None when it has no lock file."""
    try:
        fd = os.open(root / LOCK_NAME, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    # Remove while still holding the lock, so a process adopting this root
    # sees an unlinked lock file rather than a half-deleted tree.
    shutil.rmtree(root, ignore_errors=True)
    os.close(fd)
    return True


def sweep_stale(tmp: Path = SYSTEM_TMP, *, now: float | None = None) -> int:
    """Remove dead runs' leftovers from ``tmp``; return how many entries went."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    for name in names:
        is_root = name.startswith(ROOT_PREFIX)
        if not is_root and not name.startswith(LEGACY_PREFIXES):
            continue
        path = tmp / name
        try:
            st = path.lstat()
        except OSError:
            continue
        if st.st_uid != os.getuid():
            continue
        if is_root and path.is_dir() and not path.is_symlink():
            dead = _is_dead_root(path)
            if dead is not None:
                removed += dead
                continue
        if now - st.st_mtime > LOCKLESS_GRACE_S:
            _remove(path)
            removed += 1
    return removed


def _adoptable(value: str | None, tmp: Path) -> Path | None:
    """The inherited root, if it really is a run root under ``tmp``."""
    if not value:
        return None
    root = Path(value)
    if root.parent != tmp or not root.name.startswith(ROOT_PREFIX) or not root.is_dir():
        return None
    return root


def _create(tmp: Path, *, keep: bool) -> Path:
    while True:
        root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX, dir=tmp))
        # A kept root takes no lock, so only LOCKLESS_GRACE_S ever reaps it.
        if keep or _hold_shared(root, create=True):
            return root


def install(
    env: MutableMapping[str, str] | None = None, *, tmp: Path = SYSTEM_TMP,
) -> Path:
    """Point this process's temp dir at the run's scratch root and return it.

    Joins the root named by ``VIBERPIT_TEST_RUN_ROOT`` when a parent run
    exported one; otherwise reaps dead runs, creates a root, and removes it at
    exit (unless ``VIBERPIT_TEST_KEEP_TMP=1``).
    """
    env = os.environ if env is None else env
    root = _adoptable(env.get(ROOT_ENV), tmp)
    if root is None or not _hold_shared(root, create=False):
        sweep_stale(tmp)
        keep = env.get(KEEP_ENV) == "1"
        root = _create(tmp, keep=keep)
        if not keep:
            atexit.register(shutil.rmtree, root, ignore_errors=True)
    env[ROOT_ENV] = str(root)
    env["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    return root
