"""Turn anchors (D-8 "Fork from here"): the CLI transcript uuid each turn can be forked from.

The anchor of a turn is the uuid of its LAST top-level assistant transcript entry
(docs/superpowers/specs/2026-11-02-d8-fork-verification.md §3-4). Live, that is the
consolidated ``AssistantMessage.uuid`` (stream-event uuids never reach the transcript);
on replay it is the JSONL ``uuid``. Sub-agent entries never anchor a turn.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from claude_agent_sdk import TextBlock, ThinkingBlock

from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.replay import _parse, replay_window, transcript_anchor_facts
from server.sessions.translator import Translator
from server.sessions.types import TURN_ANCHOR_CAP, SessionRecord
from server.sessions.engine import AgentEngine
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


def _feed(msgs):
    tr = Translator()
    out: list = []
    for m in msgs:
        out += tr.feed(m)
    return out


def _anchors(events):
    return [e for e in events if isinstance(e, ev.TurnAnchor)]


# One logical turn: thinking entry, tool_use entry, tool result, a second API call
# whose message_start carries a stream-only uuid, the final text entry — then a
# background sub-agent's own assistant message arrives AFTER the final top-level one.
LIVE_TURN = [
    f.sys_init("s1"),
    f.stream_start(uuid="se-1"),
    f.stream_thinking("hmm", uuid="se-1"),
    f.assistant(ThinkingBlock(thinking="hmm", signature="sig"), uuid="a1"),
    f.tool_use("toolu_task", "Task", {"description": "look"}, uuid="a2"),
    f.user_tool_result("toolu_task", "sub done", uuid="r1"),
    f.stream_start(uuid="se-2"),
    f.stream_text("Done.", uuid="se-2"),
    f.assistant(TextBlock(text="Done."), uuid="a3"),
    f.assistant(TextBlock(text="sub-agent chatter"), uuid="sub-1",
                parent_tool_use_id="toolu_task"),
    f.result(session_id="s1", uuid="res-1"),
]


def test_live_turn_emits_exactly_one_anchor_with_the_last_top_level_assistant_uuid():
    out = _feed(LIVE_TURN)
    anchors = _anchors(out)
    assert len(anchors) == 1
    turn_id = next(e for e in out if isinstance(e, ev.TurnStart)).turn_id
    assert anchors[0].turn_id == turn_id == "se-1"
    assert anchors[0].uuid == "a3"
    # Emitted inside the turn: after all content, immediately before its TurnEnd.
    kinds = [type(e).__name__ for e in out]
    assert kinds[-2:] == ["TurnAnchor", "TurnEnd"]


def test_sub_agent_entries_never_anchor_a_turn():
    out = _feed([f.stream_start(uuid="se-1"),
                 f.assistant(TextBlock(text="top"), uuid="a1"),
                 f.assistant(TextBlock(text="sub"), uuid="sub-1", parent_tool_use_id="toolu_x"),
                 f.result(uuid="res")])
    assert [a.uuid for a in _anchors(out)] == ["a1"]


def test_interrupted_turn_with_no_assistant_entry_emits_no_anchor():
    out = _feed([f.stream_start(uuid="se-1"), f.stream_text("par", uuid="se-1"),
                 f.result(uuid="res", subtype="error_during_execution", is_error=True)])
    assert _anchors(out) == []
    assert any(isinstance(e, ev.TurnEnd) for e in out)


def test_anchor_is_turn_scoped_and_does_not_leak_into_the_next_turn():
    out = _feed([f.stream_start(uuid="se-1"), f.assistant(TextBlock(text="one"), uuid="a1"),
                 f.result(uuid="r1"),
                 f.stream_start(uuid="se-2"), f.result(uuid="r2")])
    assert [(a.turn_id, a.uuid) for a in _anchors(out)] == [("se-1", "a1")]


def test_turn_anchor_wire_shape():
    assert ev.TurnAnchor("t1", "u1").to_wire() == {
        "type": "turnAnchor", "turnId": "t1", "uuid": "u1"}


# ── Replay ───────────────────────────────────────────────────────────────────

def _jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return str(path)


def _transcript_rows() -> list[dict]:
    """The transcript LIVE_TURN writes, then a second turn, then an interrupted prompt."""
    return [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "message": {"model": "claude", "content": [
            {"type": "thinking", "thinking": "hmm"}]}},
        {"type": "assistant", "uuid": "a2", "message": {"model": "claude", "content": [
            {"type": "tool_use", "id": "toolu_task", "name": "Task", "input": {}}]}},
        {"type": "assistant", "uuid": "sub-1", "isSidechain": True, "message": {
            "model": "claude", "content": [{"type": "text", "text": "sub-agent chatter"}]}},
        {"type": "user", "uuid": "r1", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_task", "content": "sub done"}]}},
        {"type": "assistant", "uuid": "a3", "message": {"model": "claude", "content": [
            {"type": "text", "text": "Done."}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "again"}},
        {"type": "assistant", "uuid": "b1", "message": {"model": "claude", "content": [
            {"type": "text", "text": "Again."}]}},
        {"type": "user", "uuid": "u3", "message": {"role": "user", "content": "interrupted"}},
    ]


def test_replay_yields_the_same_anchor_uuids_as_live(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", _transcript_rows())
    events = _parse(path, None)
    anchors = _anchors(events)
    # Turn ids on replay are the entry uuids, so the anchored web turn is the entry itself.
    assert [(a.turn_id, a.uuid) for a in anchors] == [("a3", "a3"), ("b1", "b1")]
    assert _anchors(_feed(LIVE_TURN))[0].uuid == anchors[0].uuid
    # Each anchor sits inside its entry's turn: directly before that entry's TurnEnd.
    for a in anchors:
        i = events.index(a)
        nxt = events[i + 1]
        assert isinstance(nxt, ev.TurnEnd) and nxt.turn_id == a.turn_id


def test_transcript_anchor_facts_maps_turn_ids_in_order(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", _transcript_rows())
    assert list(transcript_anchor_facts(path).anchors.items()) == [("a3", "a3"), ("b1", "b1")]
    assert transcript_anchor_facts(str(tmp_path / "missing.jsonl")).anchors == {}


def test_drop_incomplete_turn_drops_the_open_turns_anchor(tmp_path):
    rows = _transcript_rows()[:-1]          # end on the b1 turn
    path = _jsonl(tmp_path / "s.jsonl", rows)
    win = asyncio.run(replay_window(path, limit=None, drop_incomplete_turn=True))
    assert [a.uuid for a in _anchors(win.events)] == ["a3"]


def test_a_window_carries_only_its_own_turns_anchors(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", _transcript_rows())
    # Turn space: a1, a2, a3, b1 → the last page holds only b1.
    win = asyncio.run(replay_window(path, limit=1))
    assert [a.uuid for a in _anchors(win.events)] == ["b1"]
    older = asyncio.run(replay_window(path, before=3, limit=3))
    assert [a.uuid for a in _anchors(older.events)] == ["a3"]


# ── Session record ───────────────────────────────────────────────────────────

def _rec() -> SessionRecord:
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="e", cols=80, rows=24, cwd="/tmp")


def test_record_keeps_anchors_bounded_to_the_newest_turns():
    rec = _rec()
    for i in range(TURN_ANCHOR_CAP + 5):
        rec.record_turn_anchor(f"t{i}", f"u{i}")
    assert TURN_ANCHOR_CAP == 500
    assert len(rec.turn_anchors) == TURN_ANCHOR_CAP
    assert "t0" not in rec.turn_anchors and "t4" not in rec.turn_anchors
    assert rec.turn_anchors["t5"] == "u5"
    assert rec.turn_anchors[f"t{TURN_ANCHOR_CAP + 4}"] == f"u{TURN_ANCHOR_CAP + 4}"


def test_rerecording_a_turn_refreshes_its_position():
    rec = _rec()
    rec.record_turn_anchor("old", "u-old")
    for i in range(TURN_ANCHOR_CAP - 1):
        rec.record_turn_anchor(f"t{i}", f"u{i}")
    rec.record_turn_anchor("old", "u-new")      # now the newest
    rec.record_turn_anchor("one-more", "x")     # evicts t0, not "old"
    assert rec.turn_anchors["old"] == "u-new"
    assert "t0" not in rec.turn_anchors


def test_anchors_stay_daemon_local_and_never_cross_the_record_wire():
    rec = _rec()
    rec.record_turn_anchor("t1", "u1")
    d = rec.to_dict()
    assert not any("anchor" in k.lower() for k in d)
    assert SessionRecord.from_dict(d).turn_anchors == {}


# ── Driver → orchestrator callback ───────────────────────────────────────────

async def _settle(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _driver(clients, on_turn_anchor):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=make, registry=PermissionRegistry(), identity="me",
                             on_turn_anchor=on_turn_anchor)


def test_driver_reports_each_anchor_to_the_callback_and_still_emits_it():
    clients: list[FakeSDKClient] = []
    calls: list[tuple] = []
    d = _driver(clients, lambda sid, tid, uuid: calls.append((sid, tid, uuid)))

    async def run():
        await d.start()
        clients[0].inject([f.stream_start(uuid="se-1"),
                           f.assistant(TextBlock(text="hi"), uuid="a1"),
                           f.result(uuid="res")])
        await _settle(lambda: len(calls) == 1)
        assert calls == [("s1", "se-1", "a1")]
        await d.aclose()
    asyncio.new_event_loop().run_until_complete(run())


def test_a_failing_anchor_callback_never_breaks_the_pump():
    clients: list[FakeSDKClient] = []
    calls: list[str] = []

    def boom(sid, tid, uuid):
        calls.append(uuid)
        raise RuntimeError("record exploded")
    d = _driver(clients, boom)

    async def run():
        await d.start()
        clients[0].inject([f.stream_start(uuid="se-1"),
                           f.assistant(TextBlock(text="hi"), uuid="a1"),
                           f.result(uuid="res"),
                           f.stream_start(uuid="se-2"),
                           f.assistant(TextBlock(text="again"), uuid="b1"),
                           f.result(uuid="res2")])
        await _settle(lambda: calls == ["a1", "b1"])
        await d.aclose()
    asyncio.new_event_loop().run_until_complete(run())


# ── Orchestrator resolution (record first, transcript rebuild on a miss) ─────

def _orch(rec: SessionRecord, path: str | None):
    from server.sessions.orchestrator import SessionOrchestrator
    o = object.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o.transcript_path = lambda session_id: path   # type: ignore[method-assign]
    return o


def test_orchestrator_callback_records_the_anchor():
    rec = _rec()
    o = _orch(rec, None)
    o._record_turn_anchor("s1", "se-1", "a1")
    o._record_turn_anchor("unknown-session", "x", "y")      # no record: ignored
    assert rec.turn_anchors == {"se-1": "a1"}


def test_resolve_prefers_the_record_then_rebuilds_from_the_transcript(tmp_path):
    rec = _rec()
    path = _jsonl(tmp_path / "s.jsonl", _transcript_rows())
    o = _orch(rec, path)
    rec.record_turn_anchor("se-live", "a-live")

    async def run():
        assert await o.resolve_turn_anchor("s1", "se-live") == "a-live"
        # A replayed turn id the live record never saw (reload / daemon restart).
        assert await o.resolve_turn_anchor("s1", "b1") == "b1"
        assert rec.turn_anchors["a3"] == "a3"            # the rebuild was merged in
        assert rec.turn_anchors["se-live"] == "a-live"   # without dropping live ones
        assert await o.resolve_turn_anchor("s1", "a2") is None   # mid-turn entry
        assert await o.resolve_turn_anchor("nope", "b1") is None
    asyncio.run(run())


def test_resolve_finds_an_anchor_older_than_the_record_bound(tmp_path):
    rows: list[dict] = []
    for i in range(TURN_ANCHOR_CAP + 3):
        rows.append({"type": "user", "uuid": f"u{i}", "message": {"role": "user", "content": f"p{i}"}})
        rows.append({"type": "assistant", "uuid": f"a{i}", "message": {
            "model": "claude", "content": [{"type": "text", "text": f"r{i}"}]}})
    rec = _rec()
    o = _orch(rec, _jsonl(tmp_path / "s.jsonl", rows))
    assert asyncio.run(o.resolve_turn_anchor("s1", "a0")) == "a0"
    assert len(rec.turn_anchors) == TURN_ANCHOR_CAP


# ── Fix round 1: never anchor on a dangling tool_use; cheap repeated misses ──

def test_live_turn_ending_on_a_tool_use_entry_gets_no_anchor():
    # Interrupted mid-tool (or max_turns / error_during_execution): the last top-level
    # entry is a tool_use with no result. `--resume-session-at` cuts inclusively, so a
    # fork there would start from a dangling tool_use — no anchor.
    out = _feed([f.stream_start(uuid="se-1"),
                 f.assistant(TextBlock(text="Reading."), uuid="a1"),
                 f.tool_use("tc1", "Read", {"file_path": "a.py"}, uuid="a2"),
                 f.result(uuid="res", subtype="error_during_execution", is_error=True)])
    assert _anchors(out) == []


def test_live_signal_tool_call_as_last_entry_also_gets_no_anchor():
    out = _feed([f.stream_start(uuid="se-1"),
                 f.assistant(TextBlock(text="done"), uuid="a1"),
                 f.tool_use("tc1", "mcp__viberpit__mark_complete", {}, uuid="a2"),
                 f.result(uuid="res")])
    assert _anchors(out) == []


def test_live_tool_loop_ending_in_text_anchors_on_the_text_entry():
    out = _feed([f.stream_start(uuid="se-1"),
                 f.tool_use("tc1", "Read", {"file_path": "a.py"}, uuid="a1"),
                 f.user_tool_result("tc1", "x"),
                 f.assistant(TextBlock(text="Read it."), uuid="a2"),
                 f.result(uuid="res")])
    assert [a.uuid for a in _anchors(out)] == ["a2"]


def _tool_use_tail_rows() -> list[dict]:
    return [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "message": {"model": "claude", "content": [
            {"type": "text", "text": "ok"}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "next"}},
        {"type": "assistant", "uuid": "b1", "message": {"model": "claude", "content": [
            {"type": "text", "text": "Reading."}]}},
        {"type": "assistant", "uuid": "b2", "message": {"model": "claude", "content": [
            {"type": "tool_use", "id": "tc1", "name": "Read", "input": {}}]}},
        {"type": "user", "uuid": "u3", "message": {"role": "user", "content": "third"}},
        {"type": "assistant", "uuid": "c1", "message": {"model": "claude", "content": [
            {"type": "tool_use", "id": "tc2", "name": "mcp__viberpit__mark_complete",
             "input": {}}]}},
    ]


def test_replay_turn_ending_on_a_tool_use_entry_gets_no_anchor(tmp_path):
    path = _jsonl(tmp_path / "s.jsonl", _tool_use_tail_rows())
    # b-turn ends on a plain tool_use; c-turn on a signal tool (which emits no ToolCall).
    assert [a.uuid for a in _anchors(_parse(path, None))] == ["a1"]


def test_replay_tool_loop_ending_in_text_still_anchors_on_the_text_entry(tmp_path):
    # _transcript_rows: thinking, Task tool_use, tool_result, final text a3.
    path = _jsonl(tmp_path / "s.jsonl", _transcript_rows())
    assert [a.uuid for a in _anchors(_parse(path, None))] == ["a3", "b1"]


def test_repeated_misses_against_an_unchanged_transcript_parse_once(tmp_path, monkeypatch):
    import server.sessions.orchestrator as orch_mod
    calls: list[str] = []
    real = orch_mod.transcript_anchor_facts

    def counting(p):
        calls.append(p)
        return real(p)
    monkeypatch.setattr(orch_mod, "transcript_anchor_facts", counting)
    rec = _rec()
    tpath = tmp_path / "s.jsonl"
    path = _jsonl(tpath, _transcript_rows())
    o = _orch(rec, path)

    async def run():
        assert await o.resolve_turn_anchor("s1", "nope-1") is None
        assert await o.resolve_turn_anchor("s1", "nope-2") is None
        assert len(calls) == 1
        # The transcript grows → the next miss re-parses.
        rows = _transcript_rows() + [{"type": "assistant", "uuid": "c9", "message": {
            "model": "claude", "content": [{"type": "text", "text": "new"}]}}]
        _jsonl(tpath, rows)
        assert await o.resolve_turn_anchor("s1", "c9") == "c9"
        assert len(calls) == 2
    asyncio.run(run())


def test_merge_keeps_live_order_and_live_entries_newest(tmp_path):
    rec = _rec()
    rec.record_turn_anchor("a3", "a3")         # merged by an earlier rebuild
    rec.record_turn_anchor("se-2", "b1")       # live id for the b1 turn
    o = _orch(rec, _jsonl(tmp_path / "s.jsonl", _transcript_rows()))
    asyncio.run(o.resolve_turn_anchor("s1", "missing"))
    # Rebuilt ids in transcript order, then live-only ids — re-recording "a3" as a
    # "live" entry would have pushed it behind b1.
    assert list(rec.turn_anchors) == ["a3", "b1", "se-2"]
