import asyncio
import json
from pathlib import Path
from server.sessions.replay import replay_events, replay_window, _parse
from server.sessions.events import ToolCall, ToolResult, TextDelta, ThinkingDelta

FIX = Path(__file__).parent / "fixtures" / "transcript.jsonl"


def _write_layout_b(root: Path, sid: str, *, main_cwd: str, sub_cwd: str | None = None):
    """Materialize a Layout-B session on disk under a fake ``projects`` tree.

    Main transcript at ``projects/<main_cwd>/<sid>.jsonl`` spawns one Task
    (tool-use id ``toolu_task``); the sub-agent's own thread lives in
    ``projects/<sub_cwd>/<sid>/subagents/agent-1.jsonl`` (+ ``.meta.json``).
    ``sub_cwd`` defaults to ``main_cwd`` (sibling); pass a different value to
    exercise the cross-cwd join (main checkout vs. worktree). Returns the main
    transcript path.
    """
    projects = root / "projects"
    main_dir = projects / main_cwd
    main_dir.mkdir(parents=True, exist_ok=True)
    main_path = main_dir / f"{sid}.jsonl"
    main_path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "message": {"model": "claude", "content": [
            {"type": "tool_use", "id": "toolu_task", "name": "Task",
             "input": {"subagent_type": "Explore", "description": "look"}}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_task", "content": "sub done"}]}},
    ]))
    sub_dir = projects / (sub_cwd or main_cwd) / sid / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / "agent-1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "isSidechain": True, "uuid": "s0", "parentUuid": None,
         "message": {"role": "user", "content": "the sub-agent prompt"}},
        {"type": "assistant", "isSidechain": True, "uuid": "s1", "parentUuid": "s0",
         "message": {"model": "claude", "content": [
             {"type": "thinking", "thinking": "let me search"},
             {"type": "text", "text": "searching now"},
             {"type": "tool_use", "id": "g1", "name": "Grep", "input": {"pattern": "x"}}]}},
        {"type": "user", "isSidechain": True, "uuid": "s2", "parentUuid": "s1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "g1", "content": "match"}]}},
    ]))
    (sub_dir / "agent-1.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "look", "toolUseId": "toolu_task"}))
    return str(main_path)


def test_replay_window_reconstructs_subagent_thread(tmp_path):
    """On reattach, each sub-agent's internal turns (text/thinking/tool calls +
    results) are reconstructed from its ``subagents/agent-*.jsonl`` sidecar and
    tagged with the spawning Task's tool-use id, so the reducer routes them into
    the Task card — matching the live view."""
    main = _write_layout_b(tmp_path, "sess1", main_cwd="-cwd-a")
    evs = asyncio.run(replay_window(main)).events

    # Internal steps carry the Task tool-use id (meta.toolUseId → parentToolUseId).
    sub_tools = [e for e in evs if isinstance(e, ToolCall) and e.parent_tool_use_id == "toolu_task"]
    assert [e.name for e in sub_tools] == ["Grep"]
    sub_text = [e for e in evs if isinstance(e, TextDelta) and e.parent_tool_use_id == "toolu_task"]
    assert any("searching now" in e.text for e in sub_text)
    sub_think = [e for e in evs if isinstance(e, ThinkingDelta) and e.parent_tool_use_id == "toolu_task"]
    assert any("let me search" in e.text for e in sub_think)
    sub_res = [e for e in evs if isinstance(e, ToolResult) and e.parent_tool_use_id == "toolu_task"]
    assert any(e.tool_id == "g1" for e in sub_res)

    # The spawning Task call stays on the MAIN thread (untagged).
    main_task = [e for e in evs if isinstance(e, ToolCall) and e.name == "Task"]
    assert main_task and main_task[0].parent_tool_use_id is None

    # The sub-agent's own prompt is NOT rendered as a bubble (mirrors live, where
    # the prompt is already the Task card's input).
    assert not any(getattr(e, "text", None) == "the sub-agent prompt" for e in evs)


def test_replay_window_subagent_cross_cwd_join(tmp_path):
    """The main transcript and the ``subagents/`` dir can live under DIFFERENT
    encoded-cwd dirs (main checkout vs. worktree). The session-id is the join
    key, so reconstruction still finds the sidecar."""
    main = _write_layout_b(tmp_path, "sess2", main_cwd="-cwd-main", sub_cwd="-cwd-worktree")
    evs = asyncio.run(replay_window(main)).events
    sub_tools = [e for e in evs if isinstance(e, ToolCall) and e.parent_tool_use_id == "toolu_task"]
    assert [e.name for e in sub_tools] == ["Grep"]


def test_replay_events_excludes_subagents(tmp_path):
    """``replay_events`` feeds the sub-agent SUMMARY + suggestions, which must stay
    main-only — reconstructing nested threads there would corrupt the summary.
    Only ``replay_window`` (the reattach/UI path) reconstructs sub-agent threads."""
    main = _write_layout_b(tmp_path, "sess3", main_cwd="-cwd-a")
    evs = asyncio.run(replay_events(main))
    assert not any(getattr(e, "parent_tool_use_id", None) for e in evs)


def test_replay_window_scopes_subagents_to_task_in_window(tmp_path):
    """A sub-agent sidecar whose ``toolUseId`` has no matching Task tool-call in
    the replayed window is not reconstructed (nothing to attach it to)."""
    main = _write_layout_b(tmp_path, "sess4", main_cwd="-cwd-a")
    # Rewrite the meta to reference a Task id that never appears in the main window.
    meta = Path(main).parent / "sess4" / "subagents" / "agent-1.meta.json"
    meta.write_text(json.dumps(
        {"agentType": "Explore", "description": "look", "toolUseId": "toolu_absent"}))
    evs = asyncio.run(replay_window(main)).events
    assert not any(getattr(e, "parent_tool_use_id", None) for e in evs)

def test_replay_emits_summary_events():
    out = asyncio.run(replay_events(str(FIX)))
    wires = [e.to_wire()["type"] for e in out]
    assert wires[0] == "user"
    assert "turnStart" in wires and "toolCall" in wires and "toolResult" in wires and "turnEnd" in wires
    tc = next(e for e in out if isinstance(e, ToolCall))
    assert tc.name == "Read"
    res = next(e for e in out if isinstance(e, ToolResult))
    assert res.payload["content"] == "print(1)\n"


def test_replay_skips_malformed_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user","uuid":"u1","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n'
        'this is not json\n'
        '{"type":"assistant","uuid":"u2","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n'
    )
    out = asyncio.run(replay_events(str(p)))
    types = [e.to_wire()["type"] for e in out]
    assert "user" in types and "turnStart" in types and "turnEnd" in types


def test_replay_emits_session_meta_and_usage(tmp_path):
    """Re-attach must reconstruct the model (from the assistant message) and the
    per-result usage so the status strip can render context/quota after replay."""
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","model":"claude-opus-4-1",'
        '"content":[{"type":"text","text":"ok"}],'
        '"usage":{"input_tokens":10,"output_tokens":2,"cache_read_input_tokens":3}}}\n'
    )
    out = asyncio.run(replay_events(str(p)))
    metas = [e for e in out if e.to_wire()["type"] == "sessionMeta"]
    assert metas and metas[0].to_wire()["model"] == "claude-opus-4-1"
    te = next(e for e in out if e.to_wire()["type"] == "turnEnd")
    assert te.to_wire()["usage"] == {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 3}


def test_replay_keeps_session_meta_after_trim(tmp_path):
    """The model id is emitted once (on the first assistant turn). In a session with
    more than max_turns turns, the trim must NOT drop that SessionMeta — otherwise the
    status strip loses the model after re-attach to a long session."""
    p = tmp_path / "long.jsonl"
    lines = []
    for i in range(25):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
        if i == 0:
            msg["model"] = "claude-opus-4-1"  # model only on the first turn
        lines.append('{"type":"assistant","uuid":"a%d","message":%s}\n' % (i, __import__("json").dumps(msg)))
    p.write_text("".join(lines))
    out = asyncio.run(replay_events(str(p), max_turns=20))
    metas = [e for e in out if e.to_wire()["type"] == "sessionMeta"]
    assert metas, "SessionMeta must survive trimming of a long session"
    assert metas[0].to_wire()["model"] == "claude-opus-4-1"
    # Trim still applies to turns themselves.
    starts = [e for e in out if e.to_wire()["type"] == "turnStart"]
    assert len(starts) == 20


def test_replay_handles_string_content(tmp_path):
    """Regression: a user message whose `content` is a plain STRING (not a list of
    blocks) must parse — otherwise iterating it yields chars and b.get() raises
    AttributeError, crashing replay → blank history."""
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"type":"user","uuid":"u1","message":{"role":"user","content":"tell me a lullaby"}}\n'
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n'
    )
    out = asyncio.run(replay_events(str(p)))
    user_evs = [e for e in out if e.to_wire()["type"] == "user"]
    assert user_evs, "string-content user message should produce a user event"
    assert user_evs[0].to_wire()["text"] == "tell me a lullaby"


def test_replay_skips_meta_skill_body(tmp_path):
    """A Skill invocation injects the skill body as a `type:"user"` entry flagged
    `isMeta: true` (with a `sourceToolUseID`). That is harness-injected context, not
    a real user turn — replay must NOT render it as a conversation message, while the
    sibling `"Launching skill: …"` tool_result (a non-meta entry) still shows."""
    p = tmp_path / "skill.jsonl"
    rows = [
        {"type": "assistant", "uuid": "a1",
         "message": {"model": "claude", "content": [
             {"type": "tool_use", "id": "tu1", "name": "Skill", "input": {"command": "x"}}]}},
        {"type": "user", "uuid": "u1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1", "content": "Launching skill: x"}]}},
        {"type": "user", "uuid": "u2", "isMeta": True, "sourceToolUseID": "tu1",
         "message": {"role": "user", "content": "Base directory for this skill: /x\n# Skill body"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    out = _parse(str(p), 20)
    texts = [getattr(e, "text", None) for e in out]
    assert "Base directory for this skill: /x\n# Skill body" not in texts
    user_evs = [e for e in out if e.to_wire()["type"] == "user"]
    assert not user_evs, "isMeta skill body must not produce a user message"
    # The real tool_result still renders.
    results = [e for e in out if e.to_wire()["type"] == "toolResult"]
    assert results and results[0].payload["content"] == "Launching skill: x"


def test_replay_skips_task_notification_delivery(tmp_path):
    """When a background task (a `run_in_background` Bash / Task / Monitor) ends, the
    harness delivers a `<task-notification>` block as a plain-string `type:"user"`
    entry (NOT flagged `isMeta`). That is harness plumbing — the live view drops it
    (the translator forwards only tool_result blocks from user messages), so replay
    must not render it as a raw user bubble either. A sibling real user turn still shows."""
    p = tmp_path / "notif.jsonl"
    notif = ("<task-notification>\n<task-id>bafenk8xx</task-id>\n"
             "<tool-use-id>toolu_019</tool-use-id>\n<status>killed</status>\n"
             "<summary>Background command \"sleep 120\" was stopped</summary>\n"
             "</task-notification>")
    rows = [
        {"type": "user", "uuid": "u1",
         "message": {"role": "user", "content": "keep an eye on PR #10863"}},
        {"type": "user", "uuid": "u2",
         "message": {"role": "user", "content": notif}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    out = _parse(str(p), 20)
    texts = [getattr(e, "text", None) for e in out]
    assert not any(t and "<task-notification>" in t for t in texts), \
        "task-notification delivery must not render as a user bubble"
    user_evs = [e for e in out if e.to_wire()["type"] == "user"]
    assert [e.text for e in user_evs] == ["keep an eye on PR #10863"]


def test_replay_skips_sidechain_entries(tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "uuid": "m1",
         "message": {"model": "claude", "content": [{"type": "text", "text": "main-line"}]}},
        {"type": "assistant", "uuid": "s1", "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "subagent-secret"}]}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    out = _parse(str(p), 20)
    texts = [getattr(e, "text", None) for e in out]
    assert "main-line" in texts
    assert "subagent-secret" not in texts


def test_replay_emits_turn_model_per_assistant_turn(tmp_path):
    """Replay reconstructs the per-turn model (mirroring the live translator) so the
    model-switch divider shows on historical/replayed sessions, not just live ones."""
    from server.sessions.events import TurnModel
    p = tmp_path / "tm.jsonl"
    p.write_text(
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","model":"claude-opus-4-8",'
        '"content":[{"type":"text","text":"one"}]}}\n'
        '{"type":"assistant","uuid":"a2","message":{"role":"assistant","model":"claude-haiku-4-5",'
        '"content":[{"type":"text","text":"two"}]}}\n'
    )
    out = _parse(str(p), 20)
    tms = [e for e in out if isinstance(e, TurnModel)]
    assert [(e.turn_id, e.model) for e in tms] == [
        ("a1", "claude-opus-4-8"), ("a2", "claude-haiku-4-5")]


def test_replay_assistant_without_model_emits_no_turn_model(tmp_path):
    from server.sessions.events import TurnModel
    p = tmp_path / "nm.jsonl"
    p.write_text(
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"hi"}]}}\n'
    )
    out = _parse(str(p), 20)
    assert not any(isinstance(e, TurnModel) for e in out)
