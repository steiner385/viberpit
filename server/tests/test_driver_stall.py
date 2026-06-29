"""Tests for the stall watchdog and ApiRetry progress-counting behaviour.

`_note_progress` must advance `_last_progress_at` for ApiRetry events (so the
300-s stall watchdog leaves a 529-backoff turn alone) while continuing to ignore
empty-text ThinkingDelta events (wedged-turn guard stays effective).

Also covers the retrying→resume status transition: when a real event arrives
after one or more ApiRetry events, the driver must immediately emit a Status
with state != "retrying" (not wait until TurnEnd).
"""
from __future__ import annotations
import asyncio

import pytest
from claude_agent_sdk import SystemMessage

from server.sessions.driver import SessionDriver
from server.sessions import events as ev
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def _make_driver(messages=None):
    """Construct a minimal SessionDriver with no real SDK interaction needed."""
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(messages or [], on_can_use_tool=can_use_tool)

    return SessionDriver(
        "s1", "primary", "/tmp/r", "/tmp/cfg", mode="default",
        sdk_factory=factory, registry=PermissionRegistry(),
        identity="test@example.com",
    )


def test_apiretry_counts_as_progress():
    d = _make_driver()
    t0 = d.last_progress_at
    d._clock = lambda: t0 + 100          # advance the injected clock
    d._note_progress(ev.ApiRetry(turn_id="t", status=529, attempt=2, max_retries=20))
    assert d.last_progress_at == t0 + 100


@pytest.mark.asyncio
async def test_drain_alive_tracks_pump_lifecycle():
    """drain_alive is the watchdog's 'interrupt didn't land' signal: it stays True
    while the turn's drain task runs, so a turn whose _turn_active was flipped False by
    an interrupt but whose drain never completed (wedged SDK subprocess) is detectable."""
    d = _make_driver()
    assert d.drain_alive is False                  # no turn started → no pump
    gate = asyncio.Event()
    async def _runner():
        await gate.wait()
    d._pump = asyncio.create_task(_runner())
    await asyncio.sleep(0)
    assert d.drain_alive is True                    # drain running
    gate.set()
    await d._pump
    assert d.drain_alive is False                   # drain completed


def test_empty_thinking_delta_still_not_progress():
    d = _make_driver()
    t0 = d.last_progress_at
    d._clock = lambda: t0 + 100
    d._note_progress(ev.ThinkingDelta(turn_id="t", text=""))
    assert d.last_progress_at == t0      # unchanged — wedged-turn guard intact


def _api_error_msg(status: int = 529, attempt: int = 1, max_retries: int = 20) -> SystemMessage:
    """Build a SystemMessage(subtype='api_error') as the translator expects."""
    return SystemMessage(
        subtype="api_error",
        data={
            "error": {"status": status},
            "retryAttempt": attempt,
            "maxRetries": max_retries,
        },
    )


@pytest.mark.asyncio
async def test_retrying_clears_on_real_event_with_status_emit():
    """After ApiRetry sets _retrying=True, the first real event must immediately
    emit a Status with state != 'retrying' — not wait until TurnEnd."""
    messages = [
        _api_error_msg(529, attempt=1),          # → ApiRetry → _retrying = True
        f.stream_start(uuid="t1"),               # first real event → _retrying → False + emit
        f.stream_text("hello", uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]
    d = _make_driver(messages)
    await d.start()
    await d.send_prompt("go")

    statuses: list[str] = []
    saw_turn_end = False
    async def collect():
        nonlocal saw_turn_end
        async for e in d.events():
            if isinstance(e, ev.Status):
                statuses.append(e.state)
            if isinstance(e, ev.TurnEnd):
                saw_turn_end = True
                break

    await asyncio.wait_for(collect(), timeout=5.0)
    await d.aclose()

    assert saw_turn_end, "turn did not complete"
    # At least one 'retrying' status must have been emitted (from the ApiRetry branch).
    assert "retrying" in statuses, f"expected 'retrying' in {statuses}"
    # After the first real event, a non-retrying status must appear before TurnEnd.
    # Find the last 'retrying' and verify a non-retrying follows it.
    last_retrying_idx = max(i for i, s in enumerate(statuses) if s == "retrying")
    post_resume = statuses[last_retrying_idx + 1:]
    assert post_resume, "no status emitted after retry→resume transition"
    assert any(s != "retrying" for s in post_resume), (
        f"status never left 'retrying' after real event; statuses={statuses}"
    )
