import asyncio
import json
from pathlib import Path
from server.sessions.replay import replay_events, _parse
from server.sessions.events import ToolCall, ToolResult

FIX = Path(__file__).parent / "fixtures" / "transcript.jsonl"

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
