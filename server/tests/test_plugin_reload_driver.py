"""``ClaudeAgentDriver.reload_plugins``: the raw ``reload_plugins`` control request."""
from __future__ import annotations

import logging

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
