"""Shutdown-path awaits must be bounded (issue #846).

CI stalled ~11 minutes inside TestClient.__exit__ → app lifespan shutdown twice in
one day: the faulthandler dump showed the portal loop idle in select() with every
worker thread parked — i.e. some shutdown await was blocked on something that was
never going to complete, until the job's wall-clock `timeout` SIGKILLed pytest.

The shutdown chain (lifespan branch, orchestrator.stop(), driver.aclose()) had many
unbounded awaits; any one of them wedging reproduces the incident. These tests wedge
each layer deliberately and assert teardown still completes within its budget.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ccswitch.selector import Picked
from server.sessions.driver import SessionDriver
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import PerQueryScript

REPO_ROOT = Path(__file__).parent.parent.parent


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone

    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


# ---------------------------------------------------------------------------
# Driver layer
# ---------------------------------------------------------------------------

class _HangingInterruptClient(PerQueryScript, f.FakeSDKClient):
    """Stream stays open (turn active) and interrupt() never returns."""

    def __init__(self):
        super().__init__([])
        self._gate = asyncio.Event()

    async def receive_response(self):
        yield f.sys_init("sess-1")
        yield f.stream_start(uuid="t1")
        await self._gate.wait()  # never set — turn never ends

    async def interrupt(self):
        await self._gate.wait()  # never set — wedged control channel


class _CancelSwallowingClient(PerQueryScript, f.FakeSDKClient):
    """Stream swallows the first CancelledError, so the pump ignores one cancel."""

    def __init__(self):
        super().__init__([])
        self._gate = asyncio.Event()

    async def receive_response(self):
        yield f.sys_init("sess-1")
        yield f.stream_start(uuid="t1")
        try:
            await self._gate.wait()
        except asyncio.CancelledError:
            # Swallow the first cancel and park again; the loop's final
            # _cancel_all_tasks sends a second one that we let through.
            await self._gate.wait()


def _driver(client) -> SessionDriver:
    def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                on_pre_compact=None):
        return client

    return SessionDriver("sb", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=factory, registry=PermissionRegistry(),
                         identity="me")


async def _start_turn(d: SessionDriver) -> None:
    await d.start()
    await d.send_prompt("go")
    # Wait until the pump has consumed the stream head so the turn is active.
    deadline = time.monotonic() + 5.0
    while not d._turn_active and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert d._turn_active, "turn never became active — fixture bug"


def test_driver_aclose_bounded_when_interrupt_hangs():
    async def run():
        d = _driver(_HangingInterruptClient())
        d._close_grace = 0.2
        await _start_turn(d)
        start = time.monotonic()
        await asyncio.wait_for(d.aclose(), timeout=10.0)
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"aclose took {elapsed:.1f}s with a wedged interrupt()"


def test_driver_aclose_bounded_when_pump_swallows_cancel():
    async def run():
        d = _driver(_CancelSwallowingClient())
        d._close_grace = 0.2
        await _start_turn(d)
        start = time.monotonic()
        await asyncio.wait_for(d.aclose(), timeout=10.0)
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"aclose took {elapsed:.1f}s with a cancel-immune pump"


# ---------------------------------------------------------------------------
# Orchestrator layer
# ---------------------------------------------------------------------------

class _WedgedDriver:
    """Driver stand-in whose aclose() blocks until cancelled."""

    account = "primary"

    async def aclose(self):
        await asyncio.Event().wait()


def _orch() -> SessionOrchestrator:
    def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                on_pre_compact=None):  # pragma: no cover — never spawned here
        raise AssertionError("no sessions in this test")

    return SessionOrchestrator(sdk_factory=factory, work_dir="/tmp/r",
                               poll_interval=9999.0, usage_fn=_fake_usage)


def test_orchestrator_stop_bounded_with_wedged_driver(monkeypatch):
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_STOP_DRIVER_CLOSE_TIMEOUT_S", 0.2)

    async def run():
        o = _orch()
        o._drivers["wedged"] = _WedgedDriver()
        start = time.monotonic()
        await asyncio.wait_for(o.stop(), timeout=10.0)
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"stop() took {elapsed:.1f}s with a wedged driver.aclose"


def test_orchestrator_stop_bounded_with_cancel_swallowing_bg_task(monkeypatch):
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_STOP_TASK_TIMEOUT_S", 0.2)

    async def run():
        o = _orch()
        gate = asyncio.Event()

        async def stubborn():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()  # ignore the first cancel; loop teardown re-cancels

        o._spawn(stubborn(), name="stubborn-bg")
        start = time.monotonic()
        await asyncio.wait_for(o.stop(), timeout=10.0)
        gate.set()  # let the orphan finish so the loop can close cleanly
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"stop() took {elapsed:.1f}s with a cancel-immune bg task"


# ---------------------------------------------------------------------------
# App lifespan layer
# ---------------------------------------------------------------------------

def test_lifespan_shutdown_bounded_when_orchestrator_stop_wedges(tmp_path, monkeypatch):
    """A wedged orchestrator.stop() must cost its phase budget, not the CI job."""
    import server.app as app_mod
    from server.app import create_app

    monkeypatch.setattr(app_mod, "_SHUTDOWN_STOP_TIMEOUT_S", 0.3)

    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()

    env = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    usage = patch("ccswitch.accounts.usage_for", side_effect=_fake_usage)
    select = patch("server.sessions.orchestrator.select_account",
                   return_value=Picked("alpha"))

    with env, usage, select:
        orch = _orch()

        async def wedged_stop():
            await asyncio.Event().wait()

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        monkeypatch.setattr(orch, "stop", wedged_stop)
        start = time.monotonic()
        client.__exit__(None, None, None)
        elapsed = time.monotonic() - start

    assert elapsed < 10.0, f"lifespan shutdown took {elapsed:.1f}s with wedged stop()"
