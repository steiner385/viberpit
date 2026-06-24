"""Tests for healing an interrupted-mid-tool-call transcript.

When a turn is interrupted while a tool_use is in flight (the CLI's "interrupted
before running" window) the synthetic ``[Request interrupted by user for tool
use]`` tool_result is never written, leaving an ORPHANED tool_use as the
conversation tail. On resume the CLI rejects that terminal state with
``error_during_execution`` / ``[ede_diagnostic] ... stop_reason=tool_use`` and
every subsequent turn dead-ends. These tests pin the repair that re-writes the
missing tool_result so the resumed transcript is well-formed again.
"""
from __future__ import annotations

import json
import os

from server.sessions.transcript_repair import (
    plan_repair,
    repair_resumed_transcript,
    repair_transcript,
)

INTERRUPT_TEXT = "[Request interrupted by user for tool use]"


def _assistant_tool_use(uuid: str, tool_ids: list[str], *, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "cwd": "/repo",
        "sessionId": "sess-1",
        "version": "2.1.186",
        "gitBranch": "main",
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": tid, "name": "Agent", "input": {}}
                        for tid in tool_ids],
        },
    }


def _user_tool_result(uuid: str, tool_id: str, *, parent: str) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                 "content": "ok", "is_error": False}]},
    }


def _ids():
    seq = iter(f"synth-{i}" for i in range(100))
    return lambda: next(seq)


def test_plans_synthetic_result_for_trailing_orphan():
    entries = [_assistant_tool_use("a1", ["toolu_x"])]
    plan = plan_repair(entries, uuid_factory=_ids(), now_iso="2026-06-23T20:00:00.000Z")
    assert len(plan) == 1
    e = plan[0]
    assert e["type"] == "user"
    assert e["parentUuid"] == "a1"
    assert e["sourceToolAssistantUUID"] == "a1"
    block = e["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_x"
    assert block["is_error"] is True
    assert block["content"] == INTERRUPT_TEXT
    # carries the conversation's identity fields so the CLI accepts it on resume
    assert e["sessionId"] == "sess-1"
    assert e["cwd"] == "/repo"


def test_repairs_orphan_followed_only_by_marker_entries():
    # Real transcripts append non-conversation markers (last-prompt, mode, pr-link)
    # after the final turn. The binary's terminal-state check ignores them and looks
    # at the last assistant/user message — the repair must do the same.
    entries = [
        _assistant_tool_use("a1", ["toolu_x"]),
        {"type": "last-prompt"},
        {"type": "mode"},
        {"type": "pr-link"},
    ]
    plan = plan_repair(entries, uuid_factory=_ids(), now_iso="t")
    assert len(plan) == 1
    assert plan[0]["message"]["content"][0]["tool_use_id"] == "toolu_x"


def test_no_repair_when_result_already_present():
    entries = [
        _assistant_tool_use("a1", ["toolu_x"]),
        _user_tool_result("u1", "toolu_x", parent="a1"),
    ]
    assert plan_repair(entries, uuid_factory=_ids(), now_iso="t") == []


def test_no_repair_when_conversation_moved_past_the_orphan():
    # An orphan that is NOT the tail must not be "repaired" at the end — that would
    # put a tool_result after unrelated content and break API ordering.
    entries = [
        _assistant_tool_use("a1", ["toolu_x"]),
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "isSidechain": False,
         "message": {"role": "assistant", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "done"}]}},
    ]
    assert plan_repair(entries, uuid_factory=_ids(), now_iso="t") == []


def test_one_entry_with_all_results_for_a_multi_tool_assistant():
    entries = [_assistant_tool_use("a1", ["toolu_x", "toolu_y"])]
    plan = plan_repair(entries, uuid_factory=_ids(), now_iso="t")
    assert len(plan) == 1
    ids = [b["tool_use_id"] for b in plan[0]["message"]["content"]]
    assert ids == ["toolu_x", "toolu_y"]


def test_sidechain_tool_use_is_ignored():
    side = _assistant_tool_use("a1", ["toolu_x"])
    side["isSidechain"] = True
    assert plan_repair([side], uuid_factory=_ids(), now_iso="t") == []


def test_repair_transcript_appends_one_line_and_is_idempotent(tmp_path):
    path = os.path.join(tmp_path, "sess-1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(_assistant_tool_use("a1", ["toolu_x"])) + "\n")

    appended = repair_transcript(path)
    assert appended == 1

    lines = [json.loads(ln) for ln in open(path) if ln.strip()]
    assert len(lines) == 2
    assert lines[-1]["message"]["content"][0]["tool_use_id"] == "toolu_x"

    # Running again sees a well-formed tail and changes nothing.
    assert repair_transcript(path) == 0
    assert len([ln for ln in open(path) if ln.strip()]) == 2


def test_repair_transcript_noop_on_clean_transcript(tmp_path):
    path = os.path.join(tmp_path, "sess-1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(_assistant_tool_use("a1", ["toolu_x"])) + "\n")
        f.write(json.dumps(_user_tool_result("u1", "toolu_x", parent="a1")) + "\n")
    assert repair_transcript(path) == 0


def test_repair_transcript_missing_file_is_safe(tmp_path):
    assert repair_transcript(os.path.join(tmp_path, "nope.jsonl")) == 0


def test_repair_resumed_transcript_finds_file_under_projects(tmp_path):
    proj = os.path.join(tmp_path, "projects", "-repo")
    os.makedirs(proj)
    path = os.path.join(proj, "sess-1.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(_assistant_tool_use("a1", ["toolu_x"])) + "\n")

    assert repair_resumed_transcript(str(tmp_path), "sess-1") == 1
    assert len([ln for ln in open(path) if ln.strip()]) == 2


def test_repair_resumed_transcript_unknown_session_is_safe(tmp_path):
    os.makedirs(os.path.join(tmp_path, "projects"))
    assert repair_resumed_transcript(str(tmp_path), "ghost") == 0
