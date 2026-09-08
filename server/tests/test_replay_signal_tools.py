"""Replay must suppress a top-level signalling tool call, exactly as the live
translator does.

`replay.py` rebuilds the transcript from the CLI's JSONL independently of the
Translator, so a rule applied only in `translator.py` holds live and then breaks
on the first reload / detach-reattach — and leaks the raw call into the autopilot
transcript tail. Same divergence the AskUserQuestion special case exists to
prevent (`test_replay_questions.py`).
"""
import json

from server.sessions import events as ev
from server.sessions.replay import _assistant_block_events, _parse

MARK_COMPLETE = "mcp__viberpit__mark_complete"


def _assistant_line(uuid: str, name: str, inp: dict) -> str:
    return json.dumps({"type": "assistant", "uuid": uuid, "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_x", "name": name, "input": inp}]}})


def test_a_top_level_signalling_call_replays_as_nothing(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_assistant_line("a1", MARK_COMPLETE, {}) + "\n")
    out = _parse(str(p), max_turns=20)
    assert not any(isinstance(e, ev.ToolCall) for e in out)


def test_the_pre_rebrand_signalling_name_is_suppressed_too(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_assistant_line("a1", "mcp__ccswitch__mark_no_pr",
                                 {"resolution": "cancelled"}) + "\n")
    out = _parse(str(p), max_turns=20)
    assert not any(isinstance(e, ev.ToolCall) for e in out)


def test_a_non_signalling_session_tool_still_replays_as_a_card(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_assistant_line("a1", "mcp__viberpit__session_context", {}) + "\n")
    out = _parse(str(p), max_turns=20)
    assert [e.name for e in out if isinstance(e, ev.ToolCall)] == [
        "mcp__viberpit__session_context"]


def test_a_subagent_signalling_call_DOES_replay_as_a_card():
    """Matches the live translator and the AskUserQuestion precedent one branch
    below: a sub-agent's call took no effect, so the card is the only sign a human
    reading the transcript gets that the tool was misused."""
    blocks = [{"type": "tool_use", "id": "toolu_x", "name": MARK_COMPLETE, "input": {}}]
    out = _assistant_block_events(blocks, "a1", 1.0, "toolu_parent")
    cards = [e for e in out if isinstance(e, ev.ToolCall)]
    assert len(cards) == 1
    assert cards[0].name == MARK_COMPLETE
    assert cards[0].parent_tool_use_id == "toolu_parent"


def test_replay_and_the_live_translator_share_one_signal_name_set():
    """The bug this file guards is a SECOND copy of the name list drifting from the
    first, so pin that both readers resolve the same object."""
    from server.sessions import replay, translator
    from server.sessions.tools.registry import SIGNAL_TOOL_NAMES
    assert replay.SIGNAL_TOOL_NAMES is SIGNAL_TOOL_NAMES
    assert translator.SIGNAL_TOOL_NAMES is SIGNAL_TOOL_NAMES
