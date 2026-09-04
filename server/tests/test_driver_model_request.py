"""Driver captures a self-directed model_request from a real TurnEnd and exposes it
via last_model_request — the call-site sibling of test_driver_pr_resolution.py.

Whole-branch review finding 2: driver.py:718 (`self._latch_model_request(e)` inside
the TurnEnd branch of `_drain`) is the SINGLE point where a parsed request reaches
the driver. Every other test that touches `_latch_model_request` calls it unbound
(`SessionDriver._latch_model_request(d, e)`) or sets `last_model_request` on a fake
driver — none drives a real TurnEnd through `_drain()`, so replacing the call site
with `pass` passed the entire `driver|model|translator|turn_end` test selection.
This mirrors test_driver_pr_resolution.py's proven pattern (a fake SDK client whose
receive_response() emits a real turn) for exactly that reason.
"""
from __future__ import annotations

import pytest
from claude_agent_sdk import TextBlock

from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import PerQueryScript

_MARKER = '<!-- ccswitch:model=haiku reason="trivial" -->'


class _ModelRequestTurnClient(PerQueryScript, FakeSDKClient):
    """A FakeSDKClient whose single turn includes a self-directed model request
    marker in the assistant text."""
    async def receive_response(self):
        text = f"work done\n{_MARKER}"
        yield f.stream_start(uuid="t1")
        yield f.stream_text(text, uuid="t1")
        yield f.assistant(TextBlock(text=text), uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


class _PlainTurnClient(PerQueryScript, FakeSDKClient):
    """A FakeSDKClient whose single turn has NO model-request marker."""
    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        yield f.stream_text("all done", uuid="t1")
        yield f.assistant(TextBlock(text="all done"), uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def _make_driver(client_cls=_ModelRequestTurnClient):
    def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                on_pre_compact=None):
        return client_cls([], on_can_use_tool=can_use_tool)
    return SessionDriver(
        "sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
        sdk_factory=factory, registry=PermissionRegistry(),
        identity="wstest@example.com",
    )


@pytest.mark.asyncio
async def test_driver_latches_model_request_from_a_real_turn_end():
    """A turn whose text carries the ccswitch:model marker must populate
    driver.last_model_request via the real _drain()/TurnEnd event path — not just
    the isolated unit-level _latch_model_request call other tests exercise."""
    d = _make_driver(_ModelRequestTurnClient)
    await d.start()
    await d.send_prompt("go")
    if d._pump:
        await d._pump
    await d.aclose()

    assert d.last_model_request == {
        "turn_id": "t1", "model": "haiku", "reason": "trivial"}


@pytest.mark.asyncio
async def test_driver_last_model_request_none_without_marker():
    """A turn without the marker leaves last_model_request as None."""
    d = _make_driver(_PlainTurnClient)
    await d.start()
    await d.send_prompt("go")
    if d._pump:
        await d._pump
    await d.aclose()

    assert d.last_model_request is None
