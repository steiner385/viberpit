"""A prompt typed in the Claude apps (Remote Control) renders in ViberPit's transcript.

With ``--replay-user-messages`` the CLI echoes every user prompt on the SDK stream as a
top-level plain-text ``UserMessage`` carrying its transcript ``uuid`` — ViberPit's own
prompts AND one a person typed into claude.ai / the desktop app, which then runs as a
CLI-initiated turn. ViberPit's own echoes must stay invisible (the driver already
emitted that bubble); a prompt it did NOT send becomes exactly one ``UserMessage``.
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import TextBlock, UserMessage

from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.translator import Translator
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


def _echo(content, uuid: str, pid: str | None = None) -> UserMessage:
    return UserMessage(content=content, uuid=uuid, parent_tool_use_id=pid)


def _bridged() -> Translator:
    """A translator whose CLI has an open Remote Control bridge."""
    tr = Translator()
    tr.remote_bridge_open = True
    return tr


def _feed(tr: Translator, msgs) -> list:
    out: list = []
    for m in msgs:
        out += tr.feed(m)
    return out


def _users(events) -> list[ev.UserMessage]:
    return [e for e in events if isinstance(e, ev.UserMessage)]


def _reply(p: str = "") -> list:
    return [f.stream_start(uuid=f"{p}se"), f.assistant(TextBlock(text="Done."), uuid=f"{p}a1"),
            f.result(uuid=f"{p}res")]


# ── Translator ───────────────────────────────────────────────────────────────

def test_own_prompt_echo_maps_its_uuid_and_renders_no_second_bubble():
    tr = _bridged()
    tr.expect_prompt("p1", "hello")
    out = _feed(tr, [_echo("hello", "U1"), *_reply()])
    assert [(e.turn_id, e.uuid) for e in out if isinstance(e, ev.UserPromptUuid)] == [
        ("p1", "U1")]
    assert _users(out) == []


def test_a_queued_second_prompt_echo_is_not_a_bubble():
    """Two prompts announced before either echo lands (the single pending slot holds
    only the newer one): the older echo is still recognized as ours."""
    tr = _bridged()
    tr.expect_prompt("p1", "first")
    tr.expect_prompt("p2", "second")
    out = _feed(tr, [_echo("first", "U1"), _echo("second", "U2"), *_reply()])
    assert _users(out) == []
    assert [(e.turn_id, e.uuid) for e in out if isinstance(e, ev.UserPromptUuid)] == [
        ("p2", "U2")]


def test_a_prompt_from_an_earlier_turn_is_still_ours():
    tr = _bridged()
    tr.expect_prompt("p1", "first")
    _feed(tr, _reply("t1-"))                  # the turn ended before the echo arrived
    tr.expect_prompt("p2", "second")
    out = _feed(tr, [_echo("first", "U1"), _echo("second", "U2"), *_reply("t2-")])
    assert _users(out) == []


def test_a_steer_echo_is_not_a_bubble():
    tr = _bridged()
    tr.note_sent("Drop the tangent.")
    assert _users(tr.feed(_echo("Drop the tangent.", "S1"))) == []


def test_a_foreign_prompt_renders_exactly_one_bubble_keyed_by_its_uuid():
    tr = _bridged()
    out = _feed(tr, [_echo("Use the Write tool to create a.txt", "R1"), *_reply()])
    users = _users(out)
    assert len(users) == 1
    u = users[0]
    assert (u.turn_id, u.src_uuid, u.text) == ("R1", "R1", "Use the Write tool to create a.txt")
    assert u.remote is True
    assert u.origin is None         # a person typed it — the same as a reload rebuilds it
    assert u.to_wire()["remote"] is True
    # The CLI re-echoing the same entry never doubles the bubble.
    assert _users(tr.feed(_echo("Use the Write tool to create a.txt", "R1"))) == []


def test_a_foreign_prompt_in_list_form_renders_its_text_blocks():
    tr = _bridged()
    out = tr.feed(_echo([TextBlock(text="look at "), TextBlock(text="this")], "R2"))
    assert [(u.turn_id, u.text) for u in _users(out)] == [("R2", "look at this")]


def test_a_foreign_prompt_next_to_a_pending_one_still_renders():
    tr = _bridged()
    tr.expect_prompt("p1", "mine")
    out = _feed(tr, [_echo("theirs", "R1"), _echo("mine", "U1")])
    assert [u.turn_id for u in _users(out)] == ["R1"]
    assert [(e.turn_id, e.uuid) for e in out if isinstance(e, ev.UserPromptUuid)] == [
        ("p1", "U1")]


def test_tool_results_sub_agents_and_plumbing_are_unchanged():
    tr = _bridged()
    out = _feed(tr, [
        f.user_tool_result("toolu_1", "ok", uuid="r1"),
        _echo("sub-agent prompt", "sub-1", pid="toolu_task"),
        _echo([TextBlock(text="sub-agent list prompt")], "sub-2", pid="toolu_task"),
        _echo("<local-command-stdout>Set model</local-command-stdout>", "lc-1"),
        _echo("<task-notification>done</task-notification>", "tn-1"),
        _echo("[Request interrupted by user]", "int-1"),
        _echo("   ", "blank-1"),
        _echo([], "empty-1"),
        _echo("no uuid", ""),
    ])
    assert _users(out) == []
    assert [e.tool_id for e in out if isinstance(e, ev.ToolResult)] == ["toolu_1"]


def test_no_bubble_without_an_open_bridge():
    """No bridge, no one else can type into this CLI: a foreign echo is the CLI's own."""
    tr = Translator()
    assert _users(tr.feed(_echo("Summarize the work so far", "M1"))) == []


def _flagged(content, uuid: str, **flags) -> UserMessage:
    m = _echo(content, uuid)
    m.cli_flags = flags  # type: ignore[attr-defined]
    return m


def test_synthetic_and_meta_echoes_never_render():
    tr = _bridged()
    out = _feed(tr, [
        _flagged("Continue from where you left off.", "S1", isSynthetic=True, isReplay=True),
        _flagged("a note", "S2", isMeta=True),
        _echo("/compact", "C1"),
        _echo("  /review the diff", "C2"),
        _echo("<scheduled-wakeup>check CI</scheduled-wakeup>", "W1"),
        _echo('<system-reminder kind="note">x</system-reminder>', "N1"),
    ])
    assert _users(out) == []
    # A real app prompt that merely mentions a tag or a slash still renders.
    assert [u.turn_id for u in _users(tr.feed(_echo("why is a/b <slow>?", "R9")))] == ["R9"]


def test_an_attachment_only_prompt_echo_is_ours():
    """The driver sends an empty prompt text plus the attachment's text block; the echo
    carries the attachment text first."""
    tr = _bridged()
    tr.expect_prompt("p1", "contents of notes.txt")
    out = tr.feed(_echo([TextBlock(text="contents of notes.txt")], "U1"))
    assert _users(out) == []
    assert [(e.turn_id, e.uuid) for e in out if isinstance(e, ev.UserPromptUuid)] == [
        ("p1", "U1")]


def test_the_driver_records_every_text_block_it_sends(tmp_path):
    """An attachment-only prompt: sdk_text is empty, the text file rides as a TextBlock."""
    note = tmp_path / "notes.txt"
    note.write_text("contents of notes.txt")
    client = FakeSDKClient([])

    async def run():
        d = ClaudeAgentDriver("s-att", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                              sdk_factory=lambda *a, **k: client,
                              registry=PermissionRegistry(), identity="me")
        await d.start()
        d._tr.remote_bridge_open = True
        client._messages = [*_reply("a-")]
        await d.send_prompt("", images=[{"path": str(note), "mime": "text/plain", "url": "u",
                                         "name": "notes.txt"}])
        await _wait_until(lambda: not d.turn_active)
        out = d._tr.feed(_echo([TextBlock(text="contents of notes.txt")], "U9"))
        await d.aclose()
        return out

    assert _users(asyncio.run(run())) == []


def test_raw_cli_flags_survive_the_sdk_parser():
    from types import SimpleNamespace
    from claude_agent_sdk import ClaudeSDKClient
    from server.sessions.claude_driver import _with_cli_flags

    async def raw():
        yield {"type": "user", "uuid": "S1", "isReplay": True, "isSynthetic": True,
               "parent_tool_use_id": None, "session_id": "x",
               "message": {"role": "user", "content": "Continue."}}
        yield {"type": "user", "uuid": "R1", "isReplay": True, "parent_tool_use_id": None,
               "session_id": "x", "message": {"role": "user", "content": "hi"}}

    client = ClaudeSDKClient()
    client._query = SimpleNamespace(receive_messages=raw)  # type: ignore[assignment]

    async def run():
        return [m async for m in _with_cli_flags(client)]

    msgs = asyncio.run(run())
    assert [m.uuid for m in msgs] == ["S1", "R1"]
    assert msgs[0].cli_flags == {"isSynthetic": True, "isReplay": True}
    tr = _bridged()
    assert [u.turn_id for u in _users(_feed(tr, msgs))] == ["R1"]


def test_user_message_wire_shape_omits_remote_unless_set():
    assert "remote" not in ev.UserMessage("t", "hi").to_wire()


# ── Driver ───────────────────────────────────────────────────────────────────

async def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


def test_driver_renders_an_app_typed_prompt_on_its_cli_initiated_turn():
    client = FakeSDKClient([])

    async def run():
        d = ClaudeAgentDriver("s-rc", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                              sdk_factory=lambda *a, **k: client,
                              registry=PermissionRegistry(), identity="me")
        seen: list = []

        async def collect():
            async for e in d.events():
                seen.append(e)
        await d.start()
        d._tr.remote_bridge_open = True   # the CLI answered an enable
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.01)
        client.inject([_echo("from the app", "R1"), *_reply()])
        await _wait_until(lambda: any(isinstance(e, ev.TurnEnd) for e in seen))
        await _wait_until(lambda: not d.turn_active)
        # Own prompt afterwards: one bubble (the driver's), never a second for its echo.
        client._messages = [_echo("from viberpit", "U2"), *_reply("v-")]
        await d.send_prompt("from viberpit")
        await _wait_until(lambda: sum(isinstance(e, ev.TurnEnd) for e in seen) >= 2)
        await d.aclose()
        task.cancel()
        return seen

    seen = asyncio.run(run())
    users = _users(seen)
    assert [(u.text, u.remote) for u in users] == [("from the app", True),
                                                   ("from viberpit", False)]
    assert users[0].turn_id == "R1"


def test_driver_steer_records_the_text_as_sent():
    client = FakeSDKClient([])

    async def run():
        reg = PermissionRegistry()
        d = ClaudeAgentDriver("s-steer", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                              sdk_factory=lambda *a, **k: client, registry=reg, identity="me")
        await d.start()
        task = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/r/a"}))
        await _wait_until(lambda: bool(reg.pending(d.session_id)))
        await d.send_prompt("steer this way")
        await asyncio.wait_for(task, timeout=2.0)
        out = d._tr.feed(_echo("steer this way", "S1"))
        await d.aclose()
        return out

    assert _users(asyncio.run(run())) == []
