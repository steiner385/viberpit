"""Replay follows the transcript's ACTIVE chain (D-9 rewind).

``rewind_conversation`` never deletes rows: it appends a ``last-prompt`` row whose
``leafUuid`` is the new leaf, and the dropped turns stay in the file
(docs/superpowers/specs/2026-11-03-d9-rewind-verification.md §3). The CLI loads the
``parentUuid`` chain ending at the newest leaf, so replay — and D-8's fork anchor
facts — must do the same, or a rewound turn reappears on reload and stays a fork point.

Fixture rows copy the field shapes of the real spike transcripts (CLI 2.1.317):
``last-prompt`` rows are written routinely and LAG the chain (one lands mid-turn
pointing at the prompt's last attachment, before the reply's entries exist, and a
finished turn often gets no row after it for hours), a rewind appends an
``explicit``/``rewound`` row and then a plain one with the same leaf,
uuid-less bookkeeping rows (queue-operation, file-history-snapshot, cost-state) sit
between entries, and a ``compact_boundary`` has ``parentUuid: null`` with the chain
carried by ``logicalParentUuid``."""
from __future__ import annotations

import json

import pytest

from server.sessions import events as ev
from server.sessions.replay import (
    _parse_transcript,
    _window,
    replay_window,
    transcript_anchor_facts,
)

SID = "785a0add-7e20-4165-ac2b-02f32f8a5c5a"


class _T:
    """Builds a transcript in the CLI's append order, chaining ``parentUuid``."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.tip: str | None = None

    def _entry(self, typ: str, uuid: str, parent: str | None, **extra) -> str:
        row = {"parentUuid": parent, "isSidechain": False, "type": typ, "uuid": uuid,
               "timestamp": "2026-11-03T11:21:00.000Z", "sessionId": SID, **extra}
        self.rows.append(row)
        self.tip = uuid
        return uuid

    def noise(self) -> None:
        self.rows.append({"type": "queue-operation", "operation": "enqueue", "sessionId": SID})
        self.rows.append({"type": "file-history-snapshot", "messageId": "m",
                          "snapshot": {"trackedFileBackups": {}}})

    def last_prompt(self, leaf: str | None, **extra) -> None:
        self.rows.append({"type": "last-prompt", "leafUuid": leaf, "sessionId": SID, **extra})

    def rewind(self, leaf: str | None, cut: str) -> None:
        """The two rows ``rewind_conversation`` appends (then a cost-state row)."""
        self.last_prompt(leaf, explicit=True, rewound=True, cutUuid=cut)
        self.last_prompt(leaf, lastPrompt="(the cut prompt)")
        self.rows.append({"type": "cost-state", "sessionId": SID})
        self.tip = leaf

    def turn(self, n: str, *, parent: str | None = "tip", tool: bool = True,
             leaf_after: bool = False) -> str:
        """One real-shaped turn: prompt, attachment, a lagging last-prompt row, an
        assistant tool_use + its tool_result (optional), then the final text reply —
        with a last-prompt row after it only if *leaf_after* (the CLI often writes
        none). Returns the reply's uuid (the turn's anchor)."""
        p = self.tip if parent == "tip" else parent
        self.noise()
        u = self._entry("user", f"u{n}", p,
                        message={"role": "user", "content": f"prompt {n}"})
        att = self._entry("attachment", f"att{n}", u, attachment={"type": "skill_listing"})
        self.last_prompt(att)                     # lags: written before the reply exists
        if tool:
            t = self._entry("assistant", f"t{n}", att, message={
                "role": "assistant", "model": "claude-x",
                "content": [{"type": "tool_use", "id": f"toolu_{n}", "name": "Read",
                             "input": {"file_path": "a.txt"}}]})
            r = self._entry("user", f"r{n}", t, message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": "v0"}]})
            att = r
        a = self._entry("assistant", f"a{n}", att, message={
            "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": f"reply {n}"}], "usage": {"input_tokens": 1}})
        if leaf_after:
            self.last_prompt(a)
        return a

    def compact(self) -> str:
        assert self.tip is not None
        b = self._entry("system", "cb", None, subtype="compact_boundary",
                        logicalParentUuid=self.tip, content="Conversation compacted")
        return self._entry("user", "summary", b, isCompactSummary=True, message={
            "role": "user", "content": "This session is being continued from a previous one."})

    def write(self, tmp_path, name: str = "t.jsonl") -> str:
        p = tmp_path / name
        p.write_text("\n".join(json.dumps(o) for o in self.rows) + "\n")
        return str(p)


def _prompts(events) -> list[str]:
    return [e.text for e in events if isinstance(e, ev.UserMessage)]


def _replies(events) -> list[str]:
    return [e.turn_id for e in events if isinstance(e, ev.TurnStart)]


def _three_turns() -> _T:
    t = _T()
    t.turn("1", leaf_after=True)
    t.turn("2")
    t.turn("3", leaf_after=True)
    return t


def test_no_rewind_replays_every_turn_through_lagging_leaf_rows(tmp_path):
    path = _three_turns().write(tmp_path)
    events, _meta, facts = _parse_transcript(path)
    assert _prompts(events) == ["prompt 1", "prompt 2", "prompt 3"]
    assert facts.anchors == {"a1": "a1", "a2": "a2", "a3": "a3"}


def test_in_progress_turn_past_the_last_leaf_row_still_replays(tmp_path):
    """The newest last-prompt row points at the prompt's attachment while the reply is
    still being written: the entries after it descend from it and are on the chain."""
    t = _three_turns()
    t.noise()
    u = t._entry("user", "u4", t.tip, message={"role": "user", "content": "prompt 4"})
    att = t._entry("attachment", "att4", u)
    t.last_prompt(att)
    t._entry("assistant", "t4", att, message={"role": "assistant", "model": "claude-x",
                                              "content": [{"type": "text", "text": "partial"}]})
    events, _meta, _facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events)[-1] == "prompt 4"
    assert _replies(events)[-1] == "t4"


def test_rewind_in_the_middle_replays_only_the_kept_turn(tmp_path):
    t = _T()
    a1 = t.turn("1")
    t.turn("2")
    t.turn("3")
    t.rewind(a1, cut="u2")
    path = t.write(tmp_path)
    events, _meta, facts = _parse_transcript(path)
    assert _prompts(events) == ["prompt 1"]
    assert _replies(events) == ["t1", "a1"]
    assert facts.anchors == {"a1": "a1"}
    # Dropped entries are no fork points: not anchors, not even resumable entries.
    assert not {"u2", "a2", "u3", "a3", "t2", "t3"} & facts.entries
    assert not {"t2", "t3"} & facts.dangling
    assert transcript_anchor_facts(path) == facts


@pytest.mark.asyncio
async def test_rewind_then_a_new_prompt_replays_kept_and_new_turns(tmp_path):
    t = _T()
    a1 = t.turn("1")
    t.turn("2")
    t.turn("3")
    t.rewind(a1, cut="u2")
    t.turn("4")                                 # parentUuid = a1, the rewound leaf
    path = t.write(tmp_path)
    events, _meta, facts = _parse_transcript(path)
    assert _prompts(events) == ["prompt 1", "prompt 4"]
    assert facts.anchors == {"a1": "a1", "a4": "a4"}
    w = await replay_window(path, limit=None)
    assert w.total_turns == len([e for e in w.events if isinstance(e, ev.TurnStart)])
    assert _prompts(w.events) == ["prompt 1", "prompt 4"]


def _parallel_tools_turn(t: _T, n: str) -> str:
    """A turn with two parallel tool calls, shaped like a real CLI 2.1.317 transcript:
    one API message split into two assistant entries sharing ``message.id``, and each
    tool_result parented on its ``sourceToolAssistantUUID`` — so the first result hangs
    OFF the parentUuid chain the second one continues. Plus a structured_output
    attachment under the first call, also off the chain."""
    u = t._entry("user", f"u{n}", t.tip, message={"role": "user", "content": f"prompt {n}"})
    mid = f"msg_{n}"
    x = t._entry("assistant", f"x{n}", u, message={
        "id": mid, "role": "assistant", "model": "claude-x",
        "content": [{"type": "tool_use", "id": f"tx{n}", "name": "Read", "input": {}}]})
    y = t._entry("assistant", f"y{n}", x, message={
        "id": mid, "role": "assistant", "model": "claude-x",
        "content": [{"type": "tool_use", "id": f"ty{n}", "name": "Read", "input": {}}]})
    t._entry("attachment", f"so{n}", x, attachment={"type": "structured_output"})
    t._entry("user", f"rx{n}", x, sourceToolAssistantUUID=x, message={"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": f"tx{n}", "content": "1"}]})
    ry = t._entry("user", f"ry{n}", y, sourceToolAssistantUUID=y, message={"role": "user",
                  "content": [{"type": "tool_result", "tool_use_id": f"ty{n}", "content": "2"}]})
    a = t._entry("assistant", f"a{n}", ry, message={
        "id": f"msg_{n}b", "role": "assistant", "model": "claude-x",
        "content": [{"type": "text", "text": f"reply {n}"}]})
    t.last_prompt(a)
    return a


def _results(events) -> list[str]:
    return [e.tool_id for e in events if isinstance(e, ev.ToolResult)]


def test_parallel_tool_results_off_the_chain_still_replay(tmp_path):
    t = _T()
    t.turn("1", tool=False)
    _parallel_tools_turn(t, "2")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _results(events) == ["tx2", "ty2"]
    assert {"rx2", "so2", "x2", "y2"} <= facts.entries
    assert facts.anchors == {"a1": "a1", "a2": "a2"}


def test_parallel_turn_dropped_by_a_rewind_takes_its_off_chain_results(tmp_path):
    t = _T()
    a1 = t.turn("1", tool=False)
    _parallel_tools_turn(t, "2")
    t.rewind(a1, cut="u2")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _results(events) == [] and _prompts(events) == ["prompt 1"]
    assert not {"x2", "y2", "rx2", "ry2", "so2", "a2"} & facts.entries


def test_parallel_turn_kept_by_a_rewind_keeps_its_off_chain_results(tmp_path):
    t = _T()
    t.turn("1", tool=False)
    a2 = _parallel_tools_turn(t, "2")
    t.turn("3")
    t.rewind(a2, cut="u3")
    events, _meta, _facts = _parse_transcript(t.write(tmp_path))
    assert _results(events) == ["tx2", "ty2"]
    assert _prompts(events) == ["prompt 1", "prompt 2"]


def test_rewind_to_the_first_prompt_replays_nothing(tmp_path):
    t = _three_turns()
    t.rewind(None, cut="u1")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == [] and _replies(events) == []
    assert facts.anchors == {} and facts.entries == frozenset()


def test_new_first_prompt_after_a_rewind_to_the_start(tmp_path):
    t = _three_turns()
    t.rewind(None, cut="u1")
    t.turn("4", parent=None)
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 4"]
    assert facts.anchors == {"a4": "a4"}


def test_undo_by_truncation_brings_the_dropped_turns_back(tmp_path):
    t = _three_turns()
    before = t.write(tmp_path, "before.jsonl")
    keep = len(open(before, "rb").read())
    t.rewind("a1", cut="u2")
    path = t.write(tmp_path)
    assert _prompts(_parse_transcript(path)[0]) == ["prompt 1"]
    with open(path, "r+b") as f:
        f.truncate(keep)
    assert _prompts(_parse_transcript(path)[0]) == ["prompt 1", "prompt 2", "prompt 3"]


def test_compaction_boundary_stays_on_the_chain_via_logical_parent(tmp_path):
    """Pins today's compaction behaviour: the null-parent boundary does not cut the
    history off, and turns at or before it stay unanchorable."""
    t = _T()
    t.turn("1")
    t.turn("2")
    t.compact()
    t.turn("3")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 2", "prompt 3"]
    assert facts.anchors == {"a3": "a3"}
    assert {"u1", "a1", "u2", "a2"} <= facts.compacted
    assert not {"u3", "a3"} & facts.compacted


def test_rewind_after_compaction_keeps_pre_boundary_history(tmp_path):
    t = _T()
    t.turn("1")
    t.compact()
    a2 = t.turn("2")
    t.turn("3")
    t.rewind(a2, cut="u3")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 2"]
    assert facts.anchors == {"a2": "a2"}
    assert {"u1", "a1"} <= facts.compacted


def test_rewind_across_a_compaction_restores_the_uncompacted_view(tmp_path):
    """A boundary on the dropped branch no longer marks the kept turns compacted."""
    t = _T()
    t.turn("1")
    a2 = t.turn("2")
    t.compact()
    t.turn("3")
    t.rewind(a2, cut="summary")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 2"]
    assert facts.compacted == frozenset()
    assert facts.anchors == {"a1": "a1", "a2": "a2"}


def test_sidechain_entries_stay_skipped_and_do_not_break_the_chain(tmp_path):
    t = _T()
    a1 = t.turn("1")
    t.rows.append({"parentUuid": None, "isSidechain": True, "type": "user", "uuid": "s0",
                   "message": {"role": "user", "content": "sub-agent task"}})
    t.rows.append({"parentUuid": "s0", "isSidechain": True, "type": "assistant", "uuid": "s1",
                   "message": {"role": "assistant",
                               "content": [{"type": "text", "text": "sub reply"}]}})
    t.tip = a1
    t.turn("2")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 2"]
    assert not {"s0", "s1"} & facts.entries


def test_task_notification_still_merges_into_the_turn_before_it(tmp_path):
    t = _T()
    t.turn("1")
    n = t._entry("user", "note", t.tip, message={"role": "user", "content":
                 "<task-notification><task-id>x</task-id></task-notification>"})
    t._entry("assistant", "a1b", n, message={"role": "assistant", "model": "claude-x",
             "content": [{"type": "text", "text": "noted"}]})
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1"]
    assert facts.anchors == {"a1b": "a1b"}


def test_until_uuid_cut_follows_the_chain_to_the_cut(tmp_path):
    """A fork's source read up to the anchor shows that anchor's chain, even when the
    file holds a rewound branch before it."""
    t = _T()
    a1 = t.turn("1")
    t.turn("2")
    t.rewind(a1, cut="u2")
    a3 = t.turn("3")
    t.turn("4")
    path = t.write(tmp_path)
    events, _meta, _facts = _parse_transcript(path, until_uuid=a3)
    assert _prompts(events) == ["prompt 1", "prompt 3"]
    w = _window(path, None, None, until_uuid=a1)
    assert _prompts(w.events) == ["prompt 1"]


def test_unknown_leaf_falls_back_to_the_files_last_chain(tmp_path):
    t = _three_turns()
    t.last_prompt("not-in-this-file")
    t.rewind("also-not-in-this-file", cut="u2")
    events, _meta, _facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 2", "prompt 3"]


def test_lagging_leaf_on_a_parallel_result_hides_no_later_turn(tmp_path):
    """Shaped like a real never-rewound transcript (322869a1…): a routine last-prompt
    row lands on the parallel tool_result that ends up OFF the parentUuid walk, then a
    whole further turn completes with no new leaf row for hours. A routine row is not
    a cut, so every turn replays."""
    t = _T()
    t.turn("1", tool=False)
    u = t._entry("user", "u2", t.tip, message={"role": "user", "content": "prompt 2"})
    x = t._entry("assistant", "x2", u, message={"id": "m2", "role": "assistant",
                 "model": "claude-x", "content": [
                     {"type": "tool_use", "id": "tx", "name": "Read", "input": {}}]})
    y = t._entry("assistant", "y2", x, message={"id": "m2", "role": "assistant",
                 "model": "claude-x", "content": [
                     {"type": "tool_use", "id": "ty", "name": "Read", "input": {}}]})
    rx = t._entry("user", "rx2", x, sourceToolAssistantUUID=x, message={"role": "user",
                  "content": [{"type": "tool_result", "tool_use_id": "tx", "content": "1"}]})
    t.last_prompt(rx)                           # lags onto the off-walk result
    ry = t._entry("user", "ry2", y, sourceToolAssistantUUID=y, message={"role": "user",
                  "content": [{"type": "tool_result", "tool_use_id": "ty", "content": "2"}]})
    t._entry("assistant", "a2", ry, message={"role": "assistant", "model": "claude-x",
             "content": [{"type": "text", "text": "reply 2"}]})
    t.turn("3")                                 # completed, no leaf row after it
    path = t.write(tmp_path)
    events, _meta, facts = _parse_transcript(path)
    assert _prompts(events) == ["prompt 1", "prompt 2", "prompt 3"]
    assert facts.anchors == {"a1": "a1", "a2": "a2", "a3": "a3"}
    # Every prefix the live file passes through replays every prompt it holds.
    rows = list(t.rows)
    for cut in range(1, len(rows) + 1):
        t.rows = rows[:cut]
        held = [r["message"]["content"] for r in t.rows
                if r.get("type") == "user" and isinstance(r["message"]["content"], str)]
        assert _prompts(_parse_transcript(t.write(tmp_path, "p.jsonl"))[0]) == held


def test_rewind_with_a_parent_written_after_its_child(tmp_path):
    """The CLI can write a row before the parent it names. After a rewind the tip is
    chosen once the whole file is read, so the new turn is still found."""
    t = _T()
    a1 = t.turn("1")
    t.turn("2")
    t.rewind(a1, cut="u2")
    u4 = t._entry("user", "u4", a1, message={"role": "user", "content": "prompt 4"})
    t._entry("assistant", "a4", "att4", message={"role": "assistant", "model": "claude-x",
             "content": [{"type": "text", "text": "reply 4"}]})
    t._entry("attachment", "att4", u4)          # a4's parent, written after it
    t._entry("system", "hook4", "a4", subtype="stop_hook_summary")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 4"]
    assert _replies(events) == ["t1", "a1", "a4"]
    assert facts.anchors == {"a1": "a1", "a4": "a4"}


def test_off_chain_harness_rows_are_not_dropped_roots(tmp_path):
    """An interrupt marker or task-notification hanging off the kept chain is a
    harness row, not a human prompt: it does not start a dropped branch."""
    t = _T()
    a1 = t.turn("1")
    intr = t._entry("user", "intr", a1, message={"role": "user", "content": [
        {"type": "text", "text": "[Request interrupted by user]"}]})
    t._entry("assistant", "after_intr", intr, message={"role": "assistant",
             "model": "claude-x", "content": [{"type": "text", "text": "stopped"}]})
    t.tip = a1
    t.turn("2")
    t.turn("3")
    t.rewind("a2", cut="u3")
    events, _meta, facts = _parse_transcript(t.write(tmp_path))
    assert _prompts(events) == ["prompt 1", "prompt 2"]
    assert {"intr", "after_intr"} <= facts.entries


def test_until_uuid_naming_a_sidechain_row_falls_back_to_the_rewind_tip(tmp_path):
    """Unreachable from the UI (a sidechain row is never an anchor), but defined: the
    cut still reads only up to that row, and the chain is the rewind's."""
    t = _T()
    a1 = t.turn("1")
    t.turn("2")
    t.rewind(a1, cut="u2")
    a3 = t.turn("3")
    t.rows.append({"parentUuid": None, "isSidechain": True, "type": "user", "uuid": "s0",
                   "message": {"role": "user", "content": "sub-agent task"}})
    t.tip = a3
    t.turn("4")
    events, _meta, _facts = _parse_transcript(t.write(tmp_path), until_uuid="s0")
    assert _prompts(events) == ["prompt 1", "prompt 3"]


def test_legacy_rows_without_parent_uuid_replay_linearly(tmp_path):
    rows = [{"type": "user", "uuid": "u0", "message": {"content": "q0"}},
            {"type": "assistant", "uuid": "a0", "message": {
                "model": "m", "content": [{"type": "text", "text": "r0"}]}},
            {"type": "user", "uuid": "u1", "message": {"content": "q1"}}]
    p = tmp_path / "legacy.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in rows))
    assert _prompts(_parse_transcript(str(p))[0]) == ["q0", "q1"]
