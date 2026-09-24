"""Async callers must resolve ``transcript_path`` off the event loop (#1913).

``transcript_path`` stats and globs the account's projects tree, which is a network
mount on the fleet hosts. Run on the loop, a slow mount blocks the whole daemon: the
loop-lag stack dumps on rocky caught ``_titling_sweep`` → ``_maybe_apply_auto_name``
→ ``transcript_path`` → ``glob`` holding the loop for >10s, three times.
``resolve_transcript_path`` already offloads it (#826); these are the siblings that
did not — including ``_session_is_parked``, which *looked* offloaded but evaluated
``self.transcript_path(...)`` as the argument to ``to_thread``, i.e. on the loop.
"""
from __future__ import annotations

import asyncio
import threading

from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _orch_recording_lookup_threads():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    threads: list[threading.Thread] = []

    def fake_transcript_path(session_id):
        threads.append(threading.current_thread())
        return None

    orch.transcript_path = fake_transcript_path
    return orch, threads


def _assert_resolved_off_loop(call) -> None:
    async def run():
        orch, threads = _orch_recording_lookup_threads()
        await call(orch)
        return threads, threading.current_thread()

    threads, loop_thread = asyncio.run(run())
    assert threads, "the lookup must still happen"
    assert all(t is not loop_thread for t in threads), (
        "transcript_path ran on the event-loop thread — a slow mount stalls the daemon"
    )


def test_auto_name_resolves_the_transcript_off_the_loop():
    async def call(orch):
        rec = SessionRecord(
            id="s1", name="s1", account="acct", engine=AgentEngine.CLAUDE_CODE,
            engine_session_id="c1",
            cols=80, rows=24, cwd="/repo", status="idle", name_is_auto=True,
        )
        orch._records = {rec.id: rec}
        await orch._maybe_apply_auto_name(rec.id)

    _assert_resolved_off_loop(call)


def test_parked_check_resolves_the_transcript_off_the_loop():
    _assert_resolved_off_loop(lambda orch: orch._session_is_parked("s1"))


def test_background_work_snapshot_resolves_the_transcript_off_the_loop():
    _assert_resolved_off_loop(lambda orch: orch._open_background_work("s1"))
