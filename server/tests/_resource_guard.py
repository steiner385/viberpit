"""Host-resource guard for the server test suite (dobby OOMs, 2026-10-09; #821/#822).

Wired from ``server/tests/conftest.py:pytest_configure`` and run in the xdist
CONTROLLER only, so no caller has to remember a recipe:

1. **Memory clamp** — shrink the worker count to what ``MemAvailable`` can
   hold (``clamp_workers``). Each server-suite worker peaks at ~1.3–2 GB; six
   of them on top of a desktop OOM-killed the browser on a 19 GB host.
2. **Host test slot** — a multi-worker run takes one of
   ``VIBERPIT_TEST_SLOT_CAP`` host-local ``flock`` slots (``SlotSemaphore``)
   before any worker is spawned, waiting while all are taken. The kernel drops
   an flock when its holder exits for ANY reason (SIGKILL included), so a dead
   run can never wedge the host — no reaper, no daemon, works on any host.

Hook ordering this relies on (verified against the installed pytest-xdist):
xdist's ``pytest_cmdline_main`` (``tryfirst``) resolves ``-n auto`` to an int
and derives ``config.option.tx = ["popen"] * n`` BEFORE pytest's own
``pytest_cmdline_main`` runs ``pytest_configure``; workers are spawned later,
from ``DSession.pytest_sessionstart`` via ``parse_tx_spec_config``. So the
clamp must rewrite ``option.tx`` (the field node setup reads), not only
``option.numprocesses``.

Everything here fails open: an unreadable ``/proc/meminfo`` means no clamp,
and any ``OSError`` on the lock directory means "run without a slot". A
test run must never be blocked by its own guard breaking.
"""
from __future__ import annotations

import atexit
import fcntl
import os
import sys
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

DEFAULT_WORKER_MB = 1536
DEFAULT_RESERVE_MB = 3072
DEFAULT_SLOT_CAP = 1  # VIBERPIT_TEST_SLOT_CAP
DEFAULT_WAIT_S = 3600
POLL_S = 10.0

# Exported while this process holds a slot. A nested pytest (a test that runs
# the suite in a subprocess or in-process) inherits it and skips acquiring:
# its parent already holds the slot, and waiting on it would deadlock.
HELD_ENV = "VIBERPIT_TEST_SLOT_HELD"

_STATE_ATTR = "_viberpit_test_slot"


def clamp_workers(
    requested: int,
    mem_available_bytes: int | None,
    *,
    per_worker_bytes: int,
    reserve_bytes: int,
) -> int:
    """Largest worker count <= ``requested`` that fits in available memory.

    ``None`` (memory unknown) leaves ``requested`` unchanged; never below 1.
    """
    if mem_available_bytes is None:
        return requested
    fits = (mem_available_bytes - reserve_bytes) // max(per_worker_bytes, 1)
    return max(1, min(requested, fits))


def read_mem_available(path: str | os.PathLike[str] = "/proc/meminfo") -> int | None:
    """``MemAvailable`` in bytes, or ``None`` when it cannot be read."""
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_slot_dir(env: MutableMapping[str, str]) -> Path:
    """Lock directory. Deliberately NOT /tmp or XDG_RUNTIME_DIR: those differ
    between the daemon service, interactive shells and sandboxes, which would
    give each its own private cap."""
    override = env.get("VIBERPIT_TEST_SLOT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "viberpit" / "test-slots"


class SlotSemaphore:
    """Counting semaphore of ``cap`` flock'd files in ``directory``."""

    def __init__(
        self,
        directory: Path,
        cap: int,
        *,
        wait_s: float = DEFAULT_WAIT_S,
        poll_s: float = POLL_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self.directory = directory
        self.cap = max(1, cap)
        self.wait_s = wait_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self._log = log
        self._fd: int | None = None

    def _path(self, i: int) -> Path:
        return self.directory / f"slot-{i}.lock"

    def _try_once(self) -> int | None:
        for i in range(self.cap):
            fd = os.open(self._path(i), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            except OSError:
                os.close(fd)
                raise
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            return fd
        return None

    def _holders(self) -> str:
        pids = []
        for i in range(self.cap):
            try:
                pid = self._path(i).read_text().strip()
            except OSError:
                pid = ""
            pids.append(pid or "?")
        return ", ".join(pids)

    def acquire(self) -> bool:
        """Block until a slot is held (True). False = proceeding without one
        (lock dir unusable, or ``wait_s`` exceeded)."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            deadline = self._clock() + self.wait_s
            while True:
                fd = self._try_once()
                if fd is not None:
                    self._fd = fd
                    return True
                if self._clock() >= deadline:
                    self._log(
                        f"test slot: still busy after {self.wait_s:.0f}s "
                        f"(VIBERPIT_TEST_SLOT_WAIT_S) — proceeding without a slot")
                    return False
                self._log(
                    f"waiting for test slot (cap {self.cap}, held by pid "
                    f"{self._holders()}) — lock dir {self.directory}")
                self._sleep(self.poll_s)
        except OSError as exc:
            self._log(f"test slot: lock dir unusable ({exc}) — fail-open, proceeding")
            return False

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            os.close(fd)  # closing the only fd drops the flock
        except OSError:
            pass


def _env_int(env: MutableMapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _log(msg: str) -> None:
    print(f"[viberpit pytest guard] {msg}", file=sys.stderr, flush=True)


def _requested_popen_workers(option: Any) -> int | None:
    """Worker count xdist will spawn, or None when this is not a plain local
    ``-n`` run (single process, or a custom ``--tx`` we must not rewrite)."""
    if not isinstance(getattr(option, "numprocesses", None), int):
        return None
    tx = list(getattr(option, "tx", None) or [])
    if not tx or any(spec != "popen" for spec in tx):
        return None
    return len(tx)


def configure(
    config: Any,
    *,
    env: MutableMapping[str, str] | None = None,
    mem_available: Callable[[], int | None] = read_mem_available,
    make_slot: Callable[[MutableMapping[str, str]], Any] | None = None,
    register_atexit: Callable[[Callable[[], None]], Any] = atexit.register,
    log: Callable[[str], None] = _log,
) -> None:
    """Clamp workers and take a host test slot. Controller-only; no-op on
    xdist workers and on single-process runs."""
    if hasattr(config, "workerinput"):
        return
    env = os.environ if env is None else env
    option = config.option
    requested = _requested_popen_workers(option)
    if requested is None:
        return

    workers = requested
    if env.get("VIBERPIT_PYTEST_MEM_CLAMP", "1") != "0":
        avail = mem_available()
        per_worker = _env_int(env, "VIBERPIT_PYTEST_WORKER_MB", DEFAULT_WORKER_MB) * 1024 * 1024
        reserve = _env_int(env, "VIBERPIT_PYTEST_MEM_RESERVE_MB", DEFAULT_RESERVE_MB) * 1024 * 1024
        workers = clamp_workers(requested, avail, per_worker_bytes=per_worker, reserve_bytes=reserve)
        if workers < requested:
            assert avail is not None
            log(f"clamping xdist workers {requested} -> {workers} "
                f"(MemAvailable {avail // (1024 * 1024)} MiB; "
                f"VIBERPIT_PYTEST_WORKER_MB / VIBERPIT_PYTEST_MEM_RESERVE_MB, "
                f"VIBERPIT_PYTEST_MEM_CLAMP=0 to disable)")
            option.numprocesses = workers
            option.tx = ["popen"] * workers

    if workers <= 1 or env.get("VIBERPIT_TEST_SLOT", "1") == "0" or env.get(HELD_ENV):
        return

    if make_slot is None:
        def make_slot(e: MutableMapping[str, str]) -> SlotSemaphore:
            return SlotSemaphore(
                default_slot_dir(e),
                _env_int(e, "VIBERPIT_TEST_SLOT_CAP", DEFAULT_SLOT_CAP),
                wait_s=_env_int(e, "VIBERPIT_TEST_SLOT_WAIT_S", DEFAULT_WAIT_S),
                log=log)

    slot = make_slot(env)
    if slot.acquire():
        setattr(config, _STATE_ATTR, slot)
        env[HELD_ENV] = "1"
        # Belt-and-braces: process exit closes the fd (and drops the lock)
        # anyway; atexit just clears the pid stamp if unconfigure never ran.
        register_atexit(slot.release)


def unconfigure(config: Any, *, env: MutableMapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    slot = getattr(config, _STATE_ATTR, None)
    if slot is None:
        return
    setattr(config, _STATE_ATTR, None)
    slot.release()
    env.pop(HELD_ENV, None)
