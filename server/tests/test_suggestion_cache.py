import asyncio
import types
from unittest.mock import AsyncMock

from server.sessions.driver import SessionDriver
from server.sessions.orchestrator import SessionOrchestrator


async def _run_caches():
    fake = types.SimpleNamespace(
        _suggester=types.SimpleNamespace(generate=AsyncMock(return_value=["Run the tests", "Commit it"])),
        cwd="/x", config_dir="/c", session_id="s1", account="acct",
        _emit=lambda e: None, _last_suggestions=[], _last_turn_id="",
        _last_suggest_sig=None,
    )
    await SessionDriver._run_suggestions(fake, "User: hi\n\nAssistant: done", "t1")
    return fake._last_suggestions


def test_run_suggestions_caches_items():
    assert asyncio.run(_run_caches()) == ["Run the tests", "Commit it"]


def test_orchestrator_latest_suggestions_reads_driver():
    orch = SessionOrchestrator()
    orch._drivers["s1"] = types.SimpleNamespace(_last_suggestions=["A", "B"])
    assert asyncio.run(orch.latest_suggestions("s1")) == ["A", "B"]
    assert asyncio.run(orch.latest_suggestions("missing")) == []
