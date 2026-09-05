"""On resume, the driver heals a transcript orphaned by an interrupt mid-tool-call
BEFORE handing it to the SDK, so the CLI doesn't dead-end the turn with
``error_during_execution`` / ``[ede_diagnostic] ... stop_reason=tool_use``.
"""
import asyncio
import json
import os

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


def _orphan_transcript(config_dir: str, session_id: str) -> str:
    proj = os.path.join(config_dir, "projects", "-repo")
    os.makedirs(proj)
    path = os.path.join(proj, f"{session_id}.jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps({
            "type": "assistant", "uuid": "a1", "isSidechain": False,
            "sessionId": session_id, "cwd": "/repo",
            "message": {"role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": "toolu_x",
                                     "name": "Agent", "input": {}}]},
        }) + "\n")
    return path


def test_start_heals_orphaned_tool_use_on_resume(tmp_path):
    cfg = str(tmp_path)
    path = _orphan_transcript(cfg, "sess-1")

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([f.sys_init("sess-1"), f.result(session_id="sess-1", uuid="t1")],
                             on_can_use_tool=can_use_tool)

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/repo", cfg, mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(),
                          identity="me", resume="sess-1")
        await d.start()
        await d.aclose()

    asyncio.run(run())

    lines = [json.loads(ln) for ln in open(path) if ln.strip()]
    assert len(lines) == 2
    healed = lines[-1]["message"]["content"][0]
    assert healed["type"] == "tool_result"
    assert healed["tool_use_id"] == "toolu_x"
    assert healed["is_error"] is True


def test_start_without_resume_does_not_touch_disk(tmp_path):
    # A fresh (non-resumed) session has nothing to heal; start() must not glob/repair.
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([f.sys_init("sess-1")], on_can_use_tool=can_use_tool)

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/repo", str(tmp_path), mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.aclose()

    asyncio.run(run())  # no exception, no projects dir created
    assert not os.path.exists(os.path.join(tmp_path, "projects"))
