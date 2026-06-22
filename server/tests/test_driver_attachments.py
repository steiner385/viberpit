# server/tests/test_driver_attachments.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import base64
import pytest
from server.tests.fakes.fake_sdk import FakeSDKClient, asst_text, result
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry


def _factory(messages):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(messages, on_can_use_tool=can_use_tool)
    return make


@pytest.mark.asyncio
async def test_send_with_attachments_builds_structured_message(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    client_holder = {}
    def make(cwd, config_dir, can_use_tool, mode, resume):
        c = FakeSDKClient([asst_text("ok"), result()], on_can_use_tool=can_use_tool)
        client_holder["c"] = c
        return c
    d = SessionDriver("s1", "acct", str(tmp_path), str(tmp_path), mode="default",
                      sdk_factory=make, registry=PermissionRegistry(), identity="u")
    await d.start()
    atts = [{"id": "a1", "media_type": "image/png", "filename": "a.png", "path": str(img)}]
    # collect echoed events
    seen = []
    async def collect():
        async for e in d.events():
            seen.append(e)
            if getattr(e, "state", None) == "idle":
                return
    import asyncio
    task = asyncio.create_task(collect())
    await d.send_prompt("look", attachments=atts)
    await asyncio.wait_for(task, timeout=2)

    c = client_holder["c"]
    assert c.sent_structured, "expected a structured query"
    content = c.sent_structured[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == base64.b64encode(b"\x89PNG\r\n\x1a\nDATA").decode()

    users = [e for e in seen if e.__class__.__name__ == "UserMessage"]
    assert users and users[0].attachments[0]["id"] == "a1"
    assert users[0].attachments[0]["mediaType"] == "image/png"
