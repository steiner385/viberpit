"""Blocking account-store reads never run on the daemon's event loop (#2852).

On rocky the account store is an sshfs mount. A stalled mount parked the daemon loop
for 88 s inside ``open()`` (goal sweep -> ``helper_env`` -> manifest read), and every
RPC starved, ``creds_sync`` included. These tests pin the three halves of the fix:

* ``run_blocking_io`` keeps the loop ticking while a read is parked, gives up at its
  deadline, and caps how many parked reads can pile up;
* every async caller of the account-store helper funnels goes through
  ``account_store_io`` (an AST guard, proven able to fail);
* the ``transcript_path`` RPC (a stat + glob over the projects tree) runs off-loop.
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI

from server import aio
from server.aio import BlockingIOTimeout, run_blocking_io
from server.sessions import account_auth


async def _ticks_while(aw) -> tuple[object, int]:
    """Await *aw* while a 10 ms ticker runs; return (outcome, ticks seen)."""
    ticks = 0
    stop = asyncio.Event()

    async def _ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(_ticker())
    try:
        try:
            outcome: object = await aw
        except BaseException as exc:  # noqa: BLE001 — returned for the assertion
            outcome = exc
    finally:
        stop.set()
        await t
    return outcome, ticks


@pytest.mark.asyncio
async def test_parked_read_times_out_and_the_loop_keeps_ticking():
    release = threading.Event()
    try:
        started = time.monotonic()
        outcome, ticks = await _ticks_while(
            run_blocking_io(release.wait, timeout=0.3, phase="stalled-mount"))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert isinstance(outcome, BlockingIOTimeout)
    assert isinstance(outcome, TimeoutError)
    assert "stalled-mount" in str(outcome)
    assert elapsed < 2.0
    # Inline, the read would have frozen the loop for the whole wait: zero ticks.
    assert ticks >= 10


@pytest.mark.asyncio
async def test_result_exception_and_context_cross_the_thread():
    assert await run_blocking_io(lambda a, b: a + b, 2, 3, timeout=5, phase="ok") == 5

    def _boom():
        raise OSError(5, "Input/output error")

    with pytest.raises(OSError, match="Input/output"):
        await run_blocking_io(_boom, timeout=5, phase="eio")

    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")
    var.set("bound")
    assert await run_blocking_io(var.get, timeout=5, phase="ctx") == "bound"


@pytest.mark.asyncio
async def test_cap_fails_fast_instead_of_piling_up_parked_threads(monkeypatch):
    monkeypatch.setattr(aio, "_BLOCKING_IO_CAP", 2)
    monkeypatch.setattr(aio, "_blocking_io_slots", threading.BoundedSemaphore(2))
    release = threading.Event()
    try:
        parked = [asyncio.create_task(run_blocking_io(release.wait, timeout=5, phase="p"))
                  for _ in range(2)]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(BlockingIOTimeout, match="already in flight"):
            await run_blocking_io(lambda: None, timeout=5, phase="third")
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
    assert await asyncio.gather(*parked) == [True, True]
    # Slots come back once the parked reads finish.
    assert await run_blocking_io(lambda: "free", timeout=5, phase="after") == "free"


@pytest.mark.asyncio
async def test_parked_worker_thread_is_a_daemon():
    """Interpreter exit must not join a read stuck in the kernel (daemon shutdown)."""
    release = threading.Event()
    seen: list[bool] = []

    def _record():
        seen.append(threading.current_thread().daemon)
        release.wait()

    try:
        with pytest.raises(BlockingIOTimeout):
            await run_blocking_io(_record, timeout=0.1, phase="daemon")
    finally:
        release.set()
    assert seen == [True]


@pytest.mark.asyncio
async def test_account_store_io_default_vs_raise(monkeypatch):
    monkeypatch.setattr(account_auth, "ACCOUNT_STORE_IO_TIMEOUT_S", 0.1)
    release = threading.Event()
    try:
        assert await account_auth.account_store_io(release.wait, default=None) is None
        with pytest.raises(BlockingIOTimeout):
            await account_auth.account_store_io(release.wait)
    finally:
        release.set()
    seen: list[tuple] = []

    def _fake(config_dir, account=None, *, writes_back=False):
        seen.append((config_dir, account, writes_back, threading.current_thread().name))
        return {"CLAUDE_CONFIG_DIR": config_dir}

    assert await account_auth.account_store_io(_fake, "/cfg", "acct", writes_back=True) == {
        "CLAUDE_CONFIG_DIR": "/cfg"}
    assert seen[0][:3] == ("/cfg", "acct", True)
    assert seen[0][3].startswith("blocking-io:_fake")


# --- AST guard: async code must not call the account-store helper funnels inline ------

# The funnels a stall dump caught on the loop (#2852): each reads the account manifest,
# ledger and/or setup-tokens, which live on the sshfs mount on a session host.
_BLOCKING_FUNNELS = frozenset({"helper_env", "pick_helper_config_dir",
                               "_autopilot_helper_config_dir"})
_SERVER = Path(__file__).resolve().parents[1]


def _inline_funnel_calls(source: str, filename: str) -> list[str]:
    """``file:line name`` for each funnel called directly in an async def's own body.

    Nested defs and lambdas are skipped: they run wherever they are called, which
    ``account_store_io`` makes a worker thread. A nested async def is its own root.
    """
    hits: list[str] = []

    def _scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = f.id if isinstance(f, ast.Name) else (
                    f.attr if isinstance(f, ast.Attribute) else None)
                if name in _BLOCKING_FUNNELS:
                    hits.append(f"{filename}:{child.lineno} {name}")
            _scan(child)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef):
            _scan(node)
    return hits


def test_guard_flags_an_inline_call_and_passes_the_wrapped_form():
    bad = (
        "async def f(cfg):\n"
        "    env = helper_env(cfg)\n"
        "    d = self._autopilot_helper_config_dir(a)\n"
        "    return pick_helper_config_dir()\n"
    )
    assert _inline_funnel_calls(bad, "x.py") == [
        "x.py:2 helper_env", "x.py:3 _autopilot_helper_config_dir",
        "x.py:4 pick_helper_config_dir"]
    good = (
        "async def f(cfg):\n"
        "    env = await account_store_io(helper_env, cfg)\n"
        "    g = lambda: pick_helper_config_dir()\n"
        "    def sync_inner():\n"
        "        return helper_env(cfg)\n"
        "def sync_outer(cfg):\n"
        "    return helper_env(cfg)\n"
    )
    assert _inline_funnel_calls(good, "x.py") == []


def test_no_async_code_calls_an_account_store_funnel_inline():
    files = [p for p in _SERVER.rglob("*.py") if "tests" not in p.relative_to(_SERVER).parts]
    assert len(files) > 100  # the walk found the package, not an empty dir
    hits = [h for p in files
            for h in _inline_funnel_calls(p.read_text(encoding="utf-8"),
                                          str(p.relative_to(_SERVER.parent)))]
    assert hits == [], (
        "Account-store reads called inline from async code park the event loop on a "
        "stalled sshfs mount (#2852). Wrap them: `await account_store_io(fn, ...)`.\n"
        + "\n".join(hits))


# --- transcript_path RPC --------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcript_path_rpc_runs_off_the_event_loop():
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    class _Orch:
        def transcript_path(self, session_id):
            seen.append(threading.current_thread())
            return f"/projects/x/{session_id}.jsonl"

    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, _Orch())
    out = await app.state.rpc["transcript_path"]({"session_id": "abc"})
    assert out == "/projects/x/abc.jsonl"
    assert seen and seen[0] is not loop_thread


@pytest.mark.asyncio
async def test_transcript_path_rpc_still_serves_an_async_orchestrator_method():
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    class _Orch:
        async def transcript_path(self, session_id):
            return f"/p/{session_id}.jsonl"

    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, _Orch())
    assert await app.state.rpc["transcript_path"]({"session_id": "s"}) == "/p/s.jsonl"
