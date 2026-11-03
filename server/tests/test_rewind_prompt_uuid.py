"""Rewind (D-9) Task 1: file checkpointing at spawn, the rewind capability, and the CLI
prompt uuid of every user turn.

The prompt uuid is the one id ``rewind_files`` and ``rewind_conversation`` both take
(docs/superpowers/specs/2026-11-03-d9-rewind-verification.md, "Which id the UI sends").
On replay a user turn's id already IS that uuid. Live, the driver mints its own turn id,
so the CLI is asked to echo every prompt (``--replay-user-messages``) and the echoed
``UserMessage.uuid`` is mapped onto the pending user turn as ``UserPromptUuid``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import TextBlock, UserMessage

from server.codex.capabilities import CODEX_CAPABILITIES
from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import AgentEngine, capabilities_to_wire, rewind_supported
from server.sessions.permissions import PermissionRegistry
from server.sessions.translator import Translator
from server.sessions.types import ASSISTANT_KIND, PROMPT_UUID_CAP, SessionRecord
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


def _echo(text, uuid: str, pid: str | None = None) -> UserMessage:
    """The CLI's ``--replay-user-messages`` echo of a prompt (the SDK parser drops
    ``isReplay``, so it is an ordinary top-level UserMessage carrying the uuid)."""
    return UserMessage(content=text, uuid=uuid, parent_tool_use_id=pid)


def _turn(tr: Translator, msgs) -> list:
    out: list = []
    for m in msgs:
        out += tr.feed(m)
    return out


def _prompt_uuids(events) -> list[tuple[str, str]]:
    return [(e.turn_id, e.uuid) for e in events if isinstance(e, ev.UserPromptUuid)]


def _reply(uuid_prefix: str = "") -> list:
    return [f.stream_start(uuid=f"{uuid_prefix}se-1"),
            f.assistant(TextBlock(text="Done."), uuid=f"{uuid_prefix}a1"),
            f.result(uuid=f"{uuid_prefix}res")]


# ── Translator ───────────────────────────────────────────────────────────────

def test_the_echo_maps_its_uuid_onto_the_pending_user_turn_exactly_once():
    tr = Translator()
    tr.expect_prompt("live-p1", "hello")
    out = _turn(tr, [f.sys_init("s1"), _echo("hello", "U1"), _echo("hello", "U1-dup"),
                     *_reply()])
    assert _prompt_uuids(out) == [("live-p1", "U1")]
    # The echo is never a second user message: the driver already emitted the prompt.
    assert not any(isinstance(e, ev.UserMessage) for e in out)


def test_the_echo_leaves_turn_ids_and_anchors_untouched():
    plain = Translator()
    without = _turn(plain, [f.sys_init("s1"), *_reply()])
    tr = Translator()
    tr.expect_prompt("live-p1", "hello")
    with_echo = [e for e in _turn(tr, [f.sys_init("s1"), _echo("hello", "U1"), *_reply()])
                 if not isinstance(e, ev.UserPromptUuid)]
    assert [e.to_wire() for e in with_echo] == [e.to_wire() for e in without]
    assert [(a.turn_id, a.uuid) for a in with_echo if isinstance(a, ev.TurnAnchor)] == [
        ("se-1", "a1")]


def test_only_a_top_level_echo_of_the_sent_text_counts():
    tr = Translator()
    tr.expect_prompt("p1", "hello")
    out = _turn(tr, [
        f.user_tool_result("toolu_1", "ok", uuid="r1"),                  # a tool result
        _echo("hello", "sub-1", pid="toolu_task"),                       # a sub-agent prompt
        _echo("<local-command-stdout>Set model</local-command-stdout>", "lc-1"),
        _echo("hello", "U1"),
    ])
    assert _prompt_uuids(out) == [("p1", "U1")]


def test_an_attachment_prompt_matches_on_its_leading_text_block():
    tr = Translator()
    tr.expect_prompt("p1", "look at this")
    out = tr.feed(_echo([TextBlock(text="look at this"), TextBlock(text="[Attached: a.zip]")],
                        "U1"))
    assert _prompt_uuids(out) == [("p1", "U1")]
    tr.expect_prompt("p2", "")          # image-only: the parser drops the image blocks
    assert _prompt_uuids(tr.feed(_echo([], "U2"))) == [("p2", "U2")]


def test_nothing_is_mapped_without_a_pending_prompt_or_after_the_turn_ends():
    tr = Translator()
    assert _prompt_uuids(tr.feed(_echo("hello", "U0"))) == []
    tr.expect_prompt("p1", "hello")
    _turn(tr, _reply())                 # the turn ended with no echo (flag off)
    assert _prompt_uuids(tr.feed(_echo("hello", "U-late"))) == []


def test_user_prompt_uuid_wire_shape():
    assert ev.UserPromptUuid("t1", "u1").to_wire() == {
        "type": "userPromptUuid", "turnId": "t1", "uuid": "u1"}


# ── Driver ───────────────────────────────────────────────────────────────────

async def _settle(pred, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_driver_maps_the_echo_onto_the_user_turn_it_emitted():
    calls: list[tuple] = []
    script = [f.sys_init("s1"), _echo("hello", "U1"), *_reply()]

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        return FakeSDKClient(script, on_can_use_tool=can_use_tool, startup_frames=[])

    d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=make, registry=PermissionRegistry(), identity="me",
                          on_prompt_uuid=lambda sid, tid, u: calls.append((sid, tid, u)))
    seen: list = []

    async def collect():
        async for e in d.events():
            seen.append(e)

    await d.start()
    reader = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await d.send_prompt("hello")
    await _settle(lambda: any(isinstance(e, ev.TurnEnd) for e in seen))
    users = [e for e in seen if isinstance(e, ev.UserMessage)]
    assert len(users) == 1
    assert _prompt_uuids(seen) == [(users[0].turn_id, "U1")]
    assert calls == [("s1", users[0].turn_id, "U1")]
    await d.aclose()
    reader.cancel()


# ── Spawn options and the capability ─────────────────────────────────────────

class _Recorder:
    """A fake SDK factory DECLARING the checkpoint/extra-args options (as the real one
    does), so the orchestrator's signature-gated forwarding hands them over."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                 on_pre_compact=None, options_overlay=None, fork_session=False,
                 extra_args=None, enable_file_checkpointing=False):
        self.calls.append({"resume": resume, "fork_session": fork_session,
                           "extra_args": extra_args,
                           "enable_file_checkpointing": enable_file_checkpointing})
        return FakeSDKClient([])


def _spawning_orch(rec: SessionRecord, base) -> object:
    from server.sessions.orchestrator import SessionOrchestrator
    o = object.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._sdk_factory = base
    o._display_env = lambda session_id: {}   # type: ignore[method-assign]
    o._oat_env = lambda session_id: {}       # type: ignore[method-assign]
    return o


def _spawn_all(o, sid: str) -> None:
    factory = o._session_factory(sid)
    factory("/w", "/cfg", None, "interactive", None, on_pre_compact=None)            # new
    factory("/w", "/cfg", None, "interactive", "sdk-1", on_pre_compact=None)         # resume
    factory("/w", "/cfg", None, "interactive", "src-sdk", on_pre_compact=None,       # fork
            fork_session=True, extra_args={"resume-session-at": "a1"})


def test_every_coding_spawn_turns_checkpointing_and_prompt_echo_on():
    rec = _rec()
    base = _Recorder()
    _spawn_all(_spawning_orch(rec, base), rec.id)
    assert [c["enable_file_checkpointing"] for c in base.calls] == [True, True, True]
    assert [c["extra_args"] for c in base.calls] == [
        {"replay-user-messages": None},
        {"replay-user-messages": None},
        {"resume-session-at": "a1", "replay-user-messages": None},
    ]
    assert base.calls[2]["fork_session"] is True


def test_kill_switch_off_passes_neither_and_drops_the_capability():
    rec = _rec()
    base = _Recorder()
    assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["rewind"] is True
    with patch.dict(os.environ, {"VIBERPIT_REWIND_ENABLED": "false"}):
        _spawn_all(_spawning_orch(rec, base), rec.id)
        assert capabilities_to_wire(ClaudeAgentDriver.capabilities)["rewind"] is False
        assert rewind_supported(ClaudeAgentDriver.capabilities) is False
    assert [c["enable_file_checkpointing"] for c in base.calls] == [False, False, False]
    assert [c["extra_args"] for c in base.calls] == [
        None, None, {"resume-session-at": "a1"}]


def test_codex_and_unknown_engines_never_rewind():
    assert CODEX_CAPABILITIES.rewind is False
    assert capabilities_to_wire(CODEX_CAPABILITIES)["rewind"] is False
    assert rewind_supported(None) is False


def test_the_assistant_session_is_not_a_coding_spawn():
    rec = _rec()
    rec.kind = ASSISTANT_KIND
    base = _Recorder()
    _spawn_all(_spawning_orch(rec, base), rec.id)
    assert [c["enable_file_checkpointing"] for c in base.calls] == [False, False, False]


def test_a_factory_that_does_not_declare_the_options_never_receives_them():
    calls: list[dict] = []

    def legacy(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        calls.append({"resume": resume})
        return FakeSDKClient([])

    rec = _rec()
    factory = _spawning_orch(rec, legacy)._session_factory(rec.id)
    factory("/w", "/cfg", None, "interactive", None, on_pre_compact=None)
    assert calls == [{"resume": None}]


@pytest.mark.asyncio
async def test_real_sdk_factory_passes_checkpointing_through(tmp_path) -> None:
    from server.sessions import claude_sdk_factory as sf
    captured: dict = {}

    class Opts:
        def __init__(self, **kw):
            captured.update(kw)

    class Client:
        def __init__(self, options):
            self.options = options

    with patch.object(sf, "_import_sdk", return_value=(Client, Opts)), \
            patch.object(sf, "_cli_path_kwargs", return_value={}):
        await sf.real_sdk_factory(str(tmp_path), str(tmp_path), None, "interactive", None,
                                  enable_file_checkpointing=True,
                                  extra_args={"replay-user-messages": None})
        assert captured["enable_file_checkpointing"] is True
        assert captured["extra_args"] == {"replay-user-messages": None}
        await sf.real_sdk_factory(str(tmp_path), str(tmp_path), None, "interactive", None)
        assert captured["enable_file_checkpointing"] is False


# ── Record and orchestrator resolution ───────────────────────────────────────

def _rec() -> SessionRecord:
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="e", cols=80, rows=24, cwd="/tmp")


def test_record_keeps_prompt_uuids_bounded_and_daemon_local():
    rec = _rec()
    for i in range(PROMPT_UUID_CAP + 5):
        rec.record_prompt_uuid(f"t{i}", f"u{i}")
    assert len(rec.prompt_uuids) == PROMPT_UUID_CAP == 500
    assert "t4" not in rec.prompt_uuids
    assert rec.prompt_uuids["t5"] == "u5"
    assert "prompt_uuids" not in rec.to_dict()
    assert SessionRecord.from_dict(rec.to_dict()).prompt_uuids == {}


def _jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return str(path)


def _transcript_rows() -> list[dict]:
    return [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "message": {"model": "claude", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]}},
        {"type": "user", "uuid": "r1", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
        {"type": "assistant", "uuid": "a2", "message": {"model": "claude", "content": [
            {"type": "text", "text": "Done."}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "again"}},
        {"type": "assistant", "uuid": "b1", "message": {"model": "claude", "content": [
            {"type": "text", "text": "Again."}]}},
    ]


def _orch(rec: SessionRecord, path: str | None):
    from server.sessions.orchestrator import SessionOrchestrator
    o = object.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o.transcript_path = lambda session_id: path   # type: ignore[method-assign]
    return o


def test_orchestrator_callback_records_the_prompt_uuid():
    rec = _rec()
    o = _orch(rec, None)
    o._record_prompt_uuid("s1", "live-p1", "U1")
    o._record_prompt_uuid("unknown-session", "x", "y")     # no record: ignored
    assert rec.prompt_uuids == {"live-p1": "U1"}


def test_resolve_hits_the_record_live_then_rebuilds_from_the_transcript(tmp_path):
    rec = _rec()
    o = _orch(rec, _jsonl(tmp_path / "s.jsonl", _transcript_rows()))
    rec.record_prompt_uuid("live-p9", "U9")

    async def run():
        assert await o.resolve_prompt_uuid("s1", "live-p9") == "U9"
        # A replayed user turn id (reload / daemon restart): the id IS the uuid.
        assert await o.resolve_prompt_uuid("s1", "u2") == "u2"
        assert rec.prompt_uuids["u1"] == "u1"            # the rebuild was merged in
        assert rec.prompt_uuids["live-p9"] == "U9"       # without dropping live ones
        assert await o.resolve_prompt_uuid("s1", "r1") is None   # a tool result
        assert await o.resolve_prompt_uuid("s1", "a2") is None   # an assistant entry
        assert await o.resolve_prompt_uuid("nope", "u1") is None
    asyncio.run(run())


def test_resolve_after_a_restart_with_an_empty_record(tmp_path):
    rec = _rec()
    o = _orch(rec, _jsonl(tmp_path / "s.jsonl", _transcript_rows()))
    assert asyncio.run(o.resolve_prompt_uuid("s1", "u1")) == "u1"


def test_resolve_without_a_transcript_is_none():
    rec = _rec()
    assert asyncio.run(_orch(rec, None).resolve_prompt_uuid("s1", "u1")) is None


def test_resolve_finds_a_prompt_older_than_the_record_bound(tmp_path):
    rows: list[dict] = []
    for i in range(PROMPT_UUID_CAP + 3):
        rows.append({"type": "user", "uuid": f"u{i}", "message": {"role": "user",
                                                                  "content": f"p{i}"}})
        rows.append({"type": "assistant", "uuid": f"a{i}", "message": {
            "model": "claude", "content": [{"type": "text", "text": f"r{i}"}]}})
    rec = _rec()
    o = _orch(rec, _jsonl(tmp_path / "s.jsonl", rows))
    assert asyncio.run(o.resolve_prompt_uuid("s1", "u0")) == "u0"
    assert len(rec.prompt_uuids) == PROMPT_UUID_CAP
