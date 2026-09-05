"""The context chip at the driver seam (spec §4.5): what actually reaches the SDK.

The header decorates ONLY the copy of the text handed to `client.query(...)` — never
the `UserMessage` the transcript bubble renders — and only on a page CHANGE, so a
follow-up in the same view does not repeat it.

`test_driver.py` covers the once-per-change dedupe against the *route* attribute only
(`startswith('<ccswitch-context route=…')`). What lives here is the rendering of the
whole header — `session=` and especially `host=`, which no other test asserts.
"""
from __future__ import annotations

import asyncio

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient


def _factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


def _driver(sid: str) -> ClaudeAgentDriver:
    return ClaudeAgentDriver(sid, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=_factory, registry=PermissionRegistry(),
                             identity="me")


def _sent(d: ClaudeAgentDriver) -> list[str]:
    client = d._client
    assert isinstance(client, FakeSDKClient)
    return list(client.sent)


def test_context_header_renders_a_host_and_only_on_a_page_change(monkeypatch):
    """`host=` MUST actually render. The plan wired this to `local_host_stamp()`, which
    is an OWNERSHIP marker returning None on the primary by design — and assistants are
    pinned to the primary — so the attribute spec §4.5 shows was dead in every topology.
    It reads `self_host_name()` now, which is always a string.
    """
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")

    async def run():
        d = _driver("ctx1")
        await d.start()
        try:
            await d.send_prompt("what is this?", context={"route": "/sessions/abc-123",
                                                          "sessionId": "abc-123-def-456"})
            await asyncio.sleep(0.05)
            first = _sent(d)[0]
            assert first.startswith(
                '<ccswitch-context route="/sessions/abc-123" '
                'session="abc-123-def-456" host="dobby" />\n')
            assert first.endswith("what is this?")

            # Same page again → no repeat (the model already knows where we are).
            await d.send_prompt("and this?", context={"route": "/sessions/abc-123",
                                                      "sessionId": "abc-123-def-456"})
            await asyncio.sleep(0.05)
            assert _sent(d)[1] == "and this?"

            # A page CHANGE re-announces.
            await d.send_prompt("now?", context={"route": "/pipeline"})
            await asyncio.sleep(0.05)
            assert _sent(d)[2] == '<ccswitch-context route="/pipeline" host="dobby" />\nnow?'
        finally:
            await d.aclose()

    asyncio.run(run())


def test_a_coding_prompt_is_never_decorated():
    """No context (every coding session) → the SDK sees exactly what the user typed."""
    async def run():
        d = _driver("ctx2")
        await d.start()
        try:
            await d.send_prompt("run the tests")
            await asyncio.sleep(0.05)
            assert _sent(d) == ["run the tests"]
        finally:
            await d.aclose()

    asyncio.run(run())
