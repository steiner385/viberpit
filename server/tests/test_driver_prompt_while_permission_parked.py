"""A prompt sent while the turn is parked on a permission must reach the model.

A turn parked inside ``_can_use_tool`` on a permission is ``_turn_live()``, so the
plain-send path used to append the prompt to the FIFO queue — a queue that only drains
when the turn ENDS, which needs the very permission the prompt was sent to unstick.
The prompt waited on a turn that waited on the prompt: a deadlock that silently
swallowed the message while ``send_prompt``'s caller was told ``"sent"``.

Observed in production on 2026-09-09: three ``Write``/``Edit`` calls parked for ~1800s
(every other tool call in that session finished in under 9s), two nudges vanished, and
the only escape — ``interrupt()`` — denied the parked work with a bare "denied by user",
so the model retried the same writes and parked again. Three cycles, no progress.

``send_prompt`` already had exactly this fix for a parked AskUserQuestion; the
permission registry never got the twin. These tests pin the twin.
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.events import UserMessage
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


class _StuckTurn(FakeSDKClient):
    """The first query opens a turn that never closes on its own — the CLI sitting
    inside a tool call. Later queries complete at once."""

    async def query(self, prompt: str):
        self.sent.append(prompt)
        if len(self.sent) == 1:
            self._deliver([f.stream_start(uuid="t1")])
        else:
            self._deliver([f.result(uuid=f"r{len(self.sent)}")])


def _make(sdk, reg):
    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return sdk

    return ClaudeAgentDriver("pperm", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=factory, registry=reg, identity="me")


async def _park_permission(d: ClaudeAgentDriver, reg: PermissionRegistry,
                           tool: str = "Write", **inp):
    """Start a can_use_tool call and wait until it is registered as pending."""
    task = asyncio.create_task(d._can_use_tool(tool, inp or {"file_path": "/repo/SKILL.md"}))
    for _ in range(200):
        if reg.pending(d.session_id):
            return task
        await asyncio.sleep(0.005)
    task.cancel()
    raise AssertionError("permission never registered as pending")


def test_prompt_while_permission_parked_reaches_the_model():
    """The user's words are delivered to the parked tool call as the deny reason —
    not queued behind a turn that can never end."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()
    seen: list = []

    async def run():
        d = _make(sdk, reg)
        q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(q)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)                      # let the pump open the turn
        task = await _park_permission(d, reg)

        await d.send_prompt("Drop the tangent and write the skill file instead.")

        result = await asyncio.wait_for(task, timeout=2.0)
        while not q.empty():
            seen.append(q.get_nowait())
        await d.aclose()
        return result

    result = asyncio.run(run())

    # The tool was NOT run — a typed message can never approve a tool use.
    assert isinstance(result, PermissionResultDeny)
    # ...but it carries the user's actual instruction, so the model acts on it rather
    # than on a bare "denied by user" that reads as "stop what you are doing".
    assert "Drop the tangent and write the skill file instead." in result.message
    assert result.message != "denied by user"
    # The prompt is visible in the conversation, not silently absorbed.
    assert any(isinstance(e, UserMessage)
               and e.text == "Drop the tangent and write the skill file instead."
               for e in seen)


def test_prompt_while_permission_parked_is_not_queued():
    """The regression itself: the prompt must not sit in the FIFO queue waiting for a
    turn end that the parked permission makes impossible."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d = _make(sdk, reg)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        task = await _park_permission(d, reg)

        await d.send_prompt("unstick please")
        queued = d.queued()

        await asyncio.wait_for(task, timeout=2.0)
        await d.aclose()
        return queued

    assert asyncio.run(run()) == []


def test_prompt_resolves_every_parked_permission_of_the_session():
    """Concurrent tool calls park several permissions at once (the production case had
    three). One message steers all of them, and each gets the user's words — not one
    real reason and two bare "denied by user"s."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d = _make(sdk, reg)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        t1 = await _park_permission(d, reg, file_path="/repo/a.md")
        t2 = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/repo/b.sh"}))
        t3 = asyncio.create_task(d._can_use_tool("Edit", {"file_path": "/repo/ci.yml"}))
        for _ in range(200):
            if len(reg.pending(d.session_id)) == 3:
                break
            await asyncio.sleep(0.005)
        assert len(reg.pending(d.session_id)) == 3

        await d.send_prompt("write them one at a time and commit each")

        out = await asyncio.wait_for(asyncio.gather(t1, t2, t3), timeout=2.0)
        await d.aclose()
        return out

    for result in asyncio.run(run()):
        assert isinstance(result, PermissionResultDeny)
        assert "write them one at a time and commit each" in result.message


def test_attachment_only_prompt_also_unparks():
    """The same deadlock with no words in it: an attachment sent at a parked turn must
    still resolve the park, and the image itself rides the queue that can now drain."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d = _make(sdk, reg)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        task = await _park_permission(d, reg)

        await d.send_prompt("", [{"url": "/u/1.png", "mime": "image/png",
                                  "name": "1.png", "path": "/tmp/1.png"}])

        result = await asyncio.wait_for(task, timeout=2.0)
        queued = d.queued()
        await d.aclose()
        return result, queued

    result, queued = asyncio.run(run())
    assert isinstance(result, PermissionResultDeny)
    assert result.message != "denied by user"
    # The attachment is not lost — it waits for the turn it just unblocked.
    assert queued == [""]


def test_card_deny_still_reads_denied_by_user():
    """A permission the user actively denies on the card is unchanged: no stale reason
    from an earlier prompt-steer leaks into it."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d = _make(sdk, reg)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        task = await _park_permission(d, reg)
        perm_id = reg.pending(d.session_id)[0]["permId"]
        reg.resolve(perm_id, "deny", "me")
        result = await asyncio.wait_for(task, timeout=2.0)
        await d.aclose()
        return result

    result = asyncio.run(run())
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "denied by user"


def test_interrupt_still_denies_without_a_reason():
    """interrupt() keeps its generic deny — it is a Stop, not user guidance."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d = _make(sdk, reg)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        task = await _park_permission(d, reg)
        await d.interrupt()
        result = await asyncio.wait_for(task, timeout=2.0)
        await d.aclose()
        return result

    result = asyncio.run(run())
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "denied by user"


def test_approval_path_untouched():
    """An approved permission still returns Allow — the reason plumbing must not
    change the happy path."""
    sdk = _StuckTurn([])
    reg = PermissionRegistry()

    async def run():
        d = _make(sdk, reg)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.02)
        task = await _park_permission(d, reg)
        perm_id = reg.pending(d.session_id)[0]["permId"]
        reg.resolve(perm_id, "allow", "me")
        result = await asyncio.wait_for(task, timeout=2.0)
        await d.aclose()
        return result

    assert isinstance(asyncio.run(run()), PermissionResultAllow)
