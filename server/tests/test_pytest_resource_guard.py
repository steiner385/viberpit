"""The server suite caps its own xdist worker count and test-slot usage (#821/#822).

Covers server/tests/_resource_guard.py: the pure memory clamp, the host-local
flock test-slot semaphore (real lock files under tmp_path), and the controller-only
pytest_configure/unconfigure wiring (fake config — no real xdist session).
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from server.tests import _resource_guard as rg

MiB = 1024 * 1024
GiB = 1024 * MiB


# ---------------------------------------------------------------- clamp_workers


def test_clamp_unreadable_meminfo_keeps_requested():
    assert rg.clamp_workers(6, None, per_worker_bytes=GiB, reserve_bytes=GiB) == 6


def test_clamp_plenty_of_memory_keeps_requested():
    assert rg.clamp_workers(4, 64 * GiB, per_worker_bytes=GiB, reserve_bytes=3 * GiB) == 4


def test_clamp_reduces_to_what_fits():
    # (10 - 3) // 2 = 3
    assert rg.clamp_workers(6, 10 * GiB, per_worker_bytes=2 * GiB, reserve_bytes=3 * GiB) == 3


def test_clamp_never_below_one():
    assert rg.clamp_workers(6, GiB, per_worker_bytes=2 * GiB, reserve_bytes=3 * GiB) == 1


def test_clamp_ci_shape_is_not_clamped():
    # CI: 16 GB runner, 4 cores, ~14.5 GB available at configure time.
    avail = int(14.5 * GiB)
    assert rg.clamp_workers(
        4, avail, per_worker_bytes=rg.DEFAULT_WORKER_MB * MiB,
        reserve_bytes=rg.DEFAULT_RESERVE_MB * MiB) == 4


# ---------------------------------------------------------- read_mem_available


def test_read_mem_available_parses_kib(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       20000000 kB\nMemAvailable:    1000 kB\n")
    assert rg.read_mem_available(p) == 1000 * 1024


def test_read_mem_available_missing_file_is_none(tmp_path):
    assert rg.read_mem_available(tmp_path / "nope") is None


def test_read_mem_available_missing_field_is_none(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1 kB\n")
    assert rg.read_mem_available(p) is None


# ------------------------------------------------------------ SlotSemaphore


def _sem(tmp_path, cap=1, *, logs=None, sleeps=None, clock=None, wait_s=3600):
    return rg.SlotSemaphore(
        tmp_path / "slots", cap, wait_s=wait_s,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        clock=clock or (lambda: 0.0),
        log=(logs.append if logs is not None else (lambda m: None)))


def test_slot_acquire_creates_dir_and_stamps_pid(tmp_path):
    s = _sem(tmp_path)
    assert s.acquire() is True
    assert (tmp_path / "slots" / "slot-0.lock").read_text().strip() == str(os.getpid())
    s.release()
    assert (tmp_path / "slots" / "slot-0.lock").read_text() == ""


def test_slot_cap_two_admits_two_holders(tmp_path):
    a, b = _sem(tmp_path, 2), _sem(tmp_path, 2)
    assert a.acquire() and b.acquire()
    assert {p.name for p in (tmp_path / "slots").iterdir()} == {"slot-0.lock", "slot-1.lock"}
    a.release()
    b.release()


def test_slot_waits_while_held_then_acquires(tmp_path):
    holder = _sem(tmp_path)
    assert holder.acquire()
    logs: list[str] = []
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            holder.release()  # the other run finishes while we wait

    waiter = rg.SlotSemaphore(tmp_path / "slots", 1, sleep=sleep,
                              clock=lambda: 0.0, log=logs.append)
    assert waiter.acquire() is True
    assert sleeps == [rg.POLL_S, rg.POLL_S]
    assert any(f"waiting for test slot (cap 1, held by pid {os.getpid()}" in m for m in logs)
    waiter.release()


def test_slot_release_frees_for_next(tmp_path):
    a = _sem(tmp_path)
    assert a.acquire()
    a.release()
    a.release()  # idempotent
    b = _sem(tmp_path)
    assert b.acquire() is True
    b.release()


def test_slot_wait_timeout_proceeds_without_slot(tmp_path):
    holder = _sem(tmp_path)
    assert holder.acquire()
    now = [0.0]

    def sleep(s):
        now[0] += s

    logs: list[str] = []
    waiter = rg.SlotSemaphore(tmp_path / "slots", 1, wait_s=25, sleep=sleep,
                              clock=lambda: now[0], log=logs.append)
    assert waiter.acquire() is False
    assert any("proceeding without a slot" in m for m in logs)
    waiter.release()  # nothing held: no-op
    holder.release()


def test_slot_unusable_dir_fails_open(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    logs: list[str] = []
    s = rg.SlotSemaphore(blocker / "slots", 1, sleep=lambda s: None, log=logs.append)
    assert s.acquire() is False
    assert any("fail-open" in m for m in logs)


def test_default_slot_dir_env_override(tmp_path):
    assert rg.default_slot_dir({"VIBERPIT_TEST_SLOT_DIR": str(tmp_path)}) == tmp_path
    d = rg.default_slot_dir({})
    assert d.parts[-3:] == (".cache", "viberpit", "test-slots")


# ------------------------------------------------- configure / unconfigure


def _config(numprocesses, *, tx=None, worker=False, maxprocesses=None):
    option = SimpleNamespace(
        numprocesses=numprocesses, maxprocesses=maxprocesses,
        tx=tx if tx is not None else (["popen"] * numprocesses if numprocesses else []),
    )
    cfg = SimpleNamespace(option=option)
    if worker:
        cfg.workerinput = {"workerid": "gw0"}
    return cfg


class Recorder:
    def __init__(self, acquire_result=True):
        self.acquire_result = acquire_result
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.acquired += 1
        return self.acquire_result

    def release(self):
        self.released += 1


def _run(cfg, env, *, mem=None, client=None, registered=None):
    rec = client or Recorder()
    rg.configure(
        cfg, env=env, mem_available=lambda: mem, log=lambda m: None,
        make_slot=lambda e: rec,
        register_atexit=(registered.append if registered is not None else lambda f: None))
    return rec


def test_configure_clamps_numprocesses_and_tx():
    cfg = _config(6)
    env: dict[str, str] = {"VIBERPIT_TEST_SLOT": "0"}
    _run(cfg, env, mem=int(3 * GiB + 2 * 1536 * MiB))
    assert cfg.option.numprocesses == 2
    assert cfg.option.tx == ["popen", "popen"]


def test_configure_respects_maxprocesses_as_requested():
    cfg = _config(6, maxprocesses=2)
    cfg.option.tx = ["popen"] * 2  # what xdist derives from -n 6 --maxprocesses 2
    _run(cfg, {"VIBERPIT_TEST_SLOT": "0"}, mem=64 * GiB)
    assert cfg.option.tx == ["popen", "popen"]


def test_configure_clamp_knobs_from_env():
    cfg = _config(6)
    env = {"VIBERPIT_TEST_SLOT": "0", "VIBERPIT_PYTEST_WORKER_MB": "1024",
           "VIBERPIT_PYTEST_MEM_RESERVE_MB": "0"}
    _run(cfg, env, mem=3 * GiB)
    assert cfg.option.numprocesses == 3


def test_configure_clamp_disabled():
    cfg = _config(6)
    _run(cfg, {"VIBERPIT_TEST_SLOT": "0", "VIBERPIT_PYTEST_MEM_CLAMP": "0"}, mem=1 * GiB)
    assert cfg.option.numprocesses == 6
    assert len(cfg.option.tx) == 6


def test_configure_bad_knob_falls_back_to_default():
    cfg = _config(2)
    _run(cfg, {"VIBERPIT_TEST_SLOT": "0", "VIBERPIT_PYTEST_WORKER_MB": "lots"}, mem=64 * GiB)
    assert cfg.option.numprocesses == 2


def test_configure_leaves_custom_tx_alone():
    cfg = _config(2, tx=["ssh=otherhost//python=python3"] * 2)
    _run(cfg, {"VIBERPIT_TEST_SLOT": "0"}, mem=1 * GiB)
    assert cfg.option.tx == ["ssh=otherhost//python=python3"] * 2


def test_configure_skips_workers_entirely():
    cfg = _config(6, worker=True)
    rec = _run(cfg, {}, mem=1 * GiB)
    assert cfg.option.numprocesses == 6
    assert rec.acquired == 0


@pytest.mark.parametrize("n", [None, 0])
def test_configure_single_process_never_acquires(n):
    cfg = _config(n)
    rec = _run(cfg, {}, mem=64 * GiB)
    assert rec.acquired == 0


def test_configure_clamped_to_one_never_acquires():
    cfg = _config(6)
    rec = _run(cfg, {}, mem=1 * GiB)
    assert cfg.option.numprocesses == 1
    assert rec.acquired == 0


def test_configure_multi_worker_acquires_and_registers_release():
    cfg = _config(2)
    env: dict[str, str] = {}
    registered: list = []
    rec = _run(cfg, env, mem=64 * GiB, registered=registered)
    assert rec.acquired == 1
    assert registered == [rec.release]
    # A nested pytest (subprocess or in-process) inherits this and must not
    # queue behind its own parent's slot.
    assert env[rg.HELD_ENV] == "1"
    rg.unconfigure(cfg, env=env)
    assert rec.released == 1
    assert rg.HELD_ENV not in env


def test_configure_opt_out():
    cfg = _config(2)
    rec = _run(cfg, {"VIBERPIT_TEST_SLOT": "0"}, mem=64 * GiB)
    assert rec.acquired == 0


def test_configure_nested_run_does_not_acquire():
    cfg = _config(2)
    rec = _run(cfg, {rg.HELD_ENV: "1"}, mem=64 * GiB)
    assert rec.acquired == 0


def test_configure_fail_open_does_not_mark_held():
    cfg = _config(2)
    env: dict[str, str] = {}
    rec = _run(cfg, env, mem=64 * GiB, client=Recorder(acquire_result=False))
    assert rec.acquired == 1
    assert rg.HELD_ENV not in env
    rg.unconfigure(cfg, env=env)  # nothing held; must not raise


def test_unconfigure_without_configure_is_noop():
    rg.unconfigure(_config(2), env={})


def test_configure_default_slot_uses_env_cap_and_dir(tmp_path):
    cfg = _config(2)
    env = {"VIBERPIT_TEST_SLOT_DIR": str(tmp_path), "VIBERPIT_TEST_SLOT_CAP": "3"}
    rg.configure(cfg, env=env, mem_available=lambda: 64 * GiB, log=lambda m: None,
                 register_atexit=lambda f: None)
    assert (tmp_path / "slot-0.lock").exists()
    rg.unconfigure(cfg, env=env)
    assert rg.HELD_ENV not in env
