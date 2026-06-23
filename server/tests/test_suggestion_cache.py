import asyncio
import types
from unittest.mock import AsyncMock

from server.sessions.driver import SessionDriver
from server.sessions.orchestrator import SessionOrchestrator


async def _run_caches():
    fake = types.SimpleNamespace(
        _suggester=types.SimpleNamespace(generate=AsyncMock(return_value=["Run the tests", "Commit it"])),
        cwd="/x", config_dir="/c", session_id="s1",
        _out=asyncio.Queue(), _last_suggestions=[],
    )
    await SessionDriver._run_suggestions(fake, "User: hi\n\nAssistant: done", "t1")
    return fake._last_suggestions


def test_run_suggestions_caches_items():
    assert asyncio.run(_run_caches()) == ["Run the tests", "Commit it"]


def test_orchestrator_latest_suggestions_reads_driver():
    orch = SessionOrchestrator()
    orch._drivers["s1"] = types.SimpleNamespace(_last_suggestions=["A", "B"])
    assert orch.latest_suggestions("s1") == ["A", "B"]
    assert orch.latest_suggestions("missing") == []


def test_orchestrator_request_suggestions_calls_driver():
    orch = SessionOrchestrator()
    calls = []
    orch._drivers["s1"] = types.SimpleNamespace(request_suggestions=lambda: calls.append("s1"))
    asyncio.run(orch.request_suggestions("s1"))
    assert calls == ["s1"]
    # Unknown session is a no-op, not an error.
    asyncio.run(orch.request_suggestions("missing"))
    assert calls == ["s1"]
