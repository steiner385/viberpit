"""transcript_window must never hang (RC2, defence in depth).

Opening a session is not optional. If replay is slow for ANY reason, the RPC
must return — degraded — rather than burn the caller's 30s ceiling and 503.
"""
import asyncio
from unittest import mock

import pytest

from server.sessions.orchestrator import SessionOrchestrator


def _async_const(value):
    async def _fn(*a, **k):
        return value
    return _fn


@pytest.mark.asyncio
async def test_slow_replay_returns_instead_of_hanging(monkeypatch):
    from server.sessions import orchestrator as orch_mod
    from server.sessions import replay as replay_mod

    async def never_finishes(*a, **k):
        await asyncio.sleep(3600)

    # transcript_window imports replay_window INSIDE the function body, so the
    # name is resolved on the replay module at call time — patch it there, not
    # on the orchestrator module (patching orch_mod would have no effect).
    monkeypatch.setattr(replay_mod, "replay_window", never_finishes)
    monkeypatch.setattr(orch_mod, "_TRANSCRIPT_WINDOW_CEILING_S", 0.3)

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock())
    monkeypatch.setattr(orch, "resolve_transcript_path",
                        _async_const("/tmp/does-not-matter.jsonl"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    out = await orch.transcript_window("sess-1")
    elapsed = loop.time() - started

    assert out is None, "a timed-out replay must degrade to None, not raise"
    assert elapsed < 2.0, f"transcript_window blocked for {elapsed:.1f}s"
