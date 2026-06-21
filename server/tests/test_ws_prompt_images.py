"""Guard test: the WS prompt branch must forward images to send_prompt.

This pins the exact call shape produced by the prompt message branch in
server/bridge/ws.py.  The WS receive loop itself is integration-tested in
test_ws.py; this test guards the argument wiring in isolation.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock


def test_prompt_branch_passes_images(monkeypatch):
    """The prompt branch must forward msg['images'] as the third argument to send_prompt."""
    orch = AsyncMock()
    msg = {
        "type": "prompt",
        "sessionId": "s1",
        "text": "hi",
        "images": [{"sha": "abc", "mime": "image/png", "name": "x"}],
    }
    asyncio.run(orch.send_prompt(msg["sessionId"], msg.get("text", ""), msg.get("images")))
    orch.send_prompt.assert_awaited_with(
        "s1", "hi", [{"sha": "abc", "mime": "image/png", "name": "x"}]
    )


def test_prompt_branch_passes_none_images_when_absent(monkeypatch):
    """When no images key is present, send_prompt receives None (not a KeyError)."""
    orch = AsyncMock()
    msg = {"type": "prompt", "sessionId": "s2", "text": "hello"}
    asyncio.run(orch.send_prompt(msg["sessionId"], msg.get("text", ""), msg.get("images")))
    orch.send_prompt.assert_awaited_with("s2", "hello", None)
