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
    # Safety net independent of any external `timeout` wrapper: if the bound in
    # transcript_window regresses, fail fast here instead of hanging for the
    # stub's full 3600s. 5s is generous headroom over the 2s assertion below.
    try:
        out = await asyncio.wait_for(orch.transcript_window("sess-1"), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("transcript_window did not return within 5s — the internal "
                    "ceiling regressed and this test's own safety timeout fired")
    elapsed = loop.time() - started

    assert out is None, "a timed-out replay must degrade to None, not raise"
    assert elapsed < 2.0, f"transcript_window blocked for {elapsed:.1f}s"


async def _timed_out_window(monkeypatch, caplog, **kwargs):
    from server.sessions import orchestrator as orch_mod
    from server.sessions import replay as replay_mod

    async def never_finishes(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(replay_mod, "replay_window", never_finishes)
    monkeypatch.setattr(orch_mod, "_TRANSCRIPT_WINDOW_CEILING_S", 0.05)
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock())
    monkeypatch.setattr(orch, "resolve_transcript_path",
                        _async_const("/tmp/does-not-matter.jsonl"))
    with caplog.at_level("DEBUG", logger="server.sessions.orchestrator"):
        out = await orch.transcript_window("sess-1", **kwargs)
    assert out is None
    return [r for r in caplog.records if "session.transcript_window" in r.getMessage()]


@pytest.mark.asyncio
async def test_interactive_timeout_still_warns(monkeypatch, caplog):
    # A user-facing read (scrollback page, export) that comes back empty is a real
    # degradation — it keeps the WARNING the error sink files on.
    records = await _timed_out_window(monkeypatch, caplog)
    assert [r.levelname for r in records] == ["WARNING"]


@pytest.mark.asyncio
async def test_background_timeout_is_info_not_warning(monkeypatch, caplog):
    # #1712: the goal / autopilot / chain-ingest sweeps read the transcript
    # best-effort and already treat None as "skip this pass". A slow replay there is
    # a deferred sweep, not a fault, so it must not file an auto-error issue.
    records = await _timed_out_window(monkeypatch, caplog, background=True)
    assert [r.levelname for r in records] == ["INFO"]
