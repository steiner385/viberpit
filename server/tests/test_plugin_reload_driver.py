"""``ClaudeAgentDriver.reload_plugins``: the raw ``reload_plugins`` control request."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from server.sessions import plugin_reload as pr
from server.tests.test_fast_mode import FakeControl, _control_error, _driver, _no_warnings, \
    _timeout_error


@pytest.mark.asyncio
async def test_reload_sends_the_raw_control_request_and_reduces_the_reply() -> None:
    control = FakeControl({"commands": [], "agents": [], "plugins": [{"name": "hello", "path": "/p"}],
                           "mcpServers": [], "error_count": 0})
    d = _driver(control)
    await d.start()
    try:
        assert await d.reload_plugins(hold_on_cache_impact=True) == {
            "outcome": "applied", "plugins": ["hello"], "errorCount": 0}
    finally:
        await d.aclose()
    assert control.sent == [{"subtype": "reload_plugins", "hold_on_cache_impact": True}]
    assert control.timeouts == [pr.RELOAD_CONTROL_TIMEOUT_S]


@pytest.mark.asyncio
async def test_reload_reports_a_held_reply() -> None:
    impact = {"added": 2}
    control = FakeControl({"held": True, "cache_impact": impact, "plugins": [], "error_count": 1})
    d = _driver(control)
    await d.start()
    try:
        out = await d.reload_plugins(hold_on_cache_impact=False)
    finally:
        await d.aclose()
    assert out == {"outcome": "held", "plugins": [], "errorCount": 1, "cacheImpact": impact}
    assert control.sent == [{"subtype": "reload_plugins", "hold_on_cache_impact": False}]


@pytest.mark.asyncio
async def test_reload_refuses_while_busy(caplog: pytest.LogCaptureFixture) -> None:
    control = FakeControl()
    d = _driver(control)
    await d.start()
    try:
        d._turn_active = True
        with caplog.at_level(logging.INFO), pytest.raises(pr.ReloadError) as ei:
            await d.reload_plugins()
    finally:
        d._turn_active = False
        await d.aclose()
    assert ei.value.code == pr.RELOAD_BUSY
    assert control.sent == []
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_reload_refuses_when_not_live(caplog: pytest.LogCaptureFixture) -> None:
    d = _driver(FakeControl())
    with caplog.at_level(logging.INFO), pytest.raises(pr.ReloadError) as before:
        await d.reload_plugins()
    assert before.value.code == pr.RELOAD_UNAVAILABLE
    no_channel = _driver(None)
    await no_channel.start()
    try:
        with pytest.raises(pr.ReloadError) as ei:
            await no_channel.reload_plugins()
    finally:
        await no_channel.aclose()
    assert ei.value.code == pr.RELOAD_UNAVAILABLE
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_reload_maps_control_error_and_no_reply(caplog: pytest.LogCaptureFixture) -> None:
    control = FakeControl(_control_error("Plugin reload is not supported"), _timeout_error())
    d = _driver(control)
    await d.start()
    try:
        with caplog.at_level(logging.INFO):
            with pytest.raises(pr.ReloadError) as refused:
                await d.reload_plugins()
            with pytest.raises(pr.ReloadError) as failed:
                await d.reload_plugins()
    finally:
        await d.aclose()
    assert refused.value.code == pr.RELOAD_REFUSED
    assert failed.value.code == pr.RELOAD_FAILED
    assert _no_warnings(caplog) == []


def test_outcome_is_total_over_garbage() -> None:
    assert pr.outcome(None) == {"outcome": "applied", "plugins": [], "errorCount": 0}
    assert pr.outcome({"plugins": "x", "held": "yes", "cache_impact": 3}) == {
        "outcome": "applied", "plugins": [], "errorCount": 0}
    assert pr.outcome({"plugins": [{"name": 1}, {"name": "a"}, 5]})["plugins"] == ["a"]


async def _settle(d, client, n_sent: int) -> None:
    """Wait (bounded) until the client saw ``n_sent`` prompts and the turn settled."""
    for _ in range(200):
        if len(client.sent) >= n_sent and not d._turn_live():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"sent={client.sent!r} live={d._turn_live()}")


def test_prompt_sent_while_the_reload_is_in_flight_is_queued_then_started() -> None:
    """The reload runs inside the driver's idle claim: a prompt arriving during the
    control request must not start a turn under it; it starts once the reload returns."""
    control = FakeControl({"plugins": [], "error_count": 0})
    gate = control.gate = asyncio.Event()

    async def go() -> tuple[list, list, list, dict]:
        d = _driver(control)
        await d.start()
        client: Any = d._client
        try:
            reload = asyncio.create_task(d.reload_plugins())
            for _ in range(200):
                if control.in_flight:
                    break
                await asyncio.sleep(0.01)
            assert control.in_flight == 1 and d._rewind_held
            await d.send_prompt("typed during the reload")
            sent_during = list(client.sent)
            queued = [q[0] for q in d._queue]
            gate.set()
            out = await reload
            await _settle(d, client, 1)
            return sent_during, queued, list(client.sent), out
        finally:
            await d.aclose()

    sent_during, queued, sent_after, out = asyncio.run(go())
    assert sent_during == [] and queued == ["typed during the reload"]
    assert len(sent_after) == 1 and "typed during the reload" in sent_after[0]
    assert out["outcome"] == "applied"


def test_reload_during_an_active_hold_is_busy(caplog: pytest.LogCaptureFixture) -> None:
    control = FakeControl()

    async def go() -> str:
        d = _driver(control)
        await d.start()
        try:
            async with d.rewind_hold():
                with pytest.raises(pr.ReloadError) as ei:
                    await d.reload_plugins()
            return ei.value.code
        finally:
            await d.aclose()

    with caplog.at_level(logging.INFO):
        assert asyncio.run(go()) == pr.RELOAD_BUSY
    assert control.sent == []
    assert _no_warnings(caplog) == []
