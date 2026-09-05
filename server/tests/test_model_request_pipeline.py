"""Translator parses the marker onto TurnEnd; the driver latches it."""
from claude_agent_sdk import TextBlock
from server.sessions import events as ev
from server.tests.fakes import fake_sdk as f
# Reused from test_translator.py rather than reinventing a second fake-message
# driver — module-private (no leading underscore export guard, just convention)
# but the same helper every other translator integration test in the suite uses.
from server.tests.test_translator import _drive, _turn_end


def test_turn_end_carries_model_request_on_the_wire():
    e = ev.TurnEnd("t1", model_request={"model": "opus", "reason": "hard"})
    assert e.to_wire()["modelRequest"] == {"model": "opus", "reason": "hard"}


def test_turn_end_model_request_defaults_to_none():
    assert ev.TurnEnd("t1").to_wire()["modelRequest"] is None


def test_translator_extracts_model_request_from_turn_text():
    from server.sessions.translator import Translator
    tr = Translator()                      # __init__ takes no args
    tr._turn_text = 'work done\n<!-- ccswitch:model=haiku reason="trivial" -->'
    out = tr._turn_end_model_request()
    assert out == {"model": "haiku", "reason": "trivial"}


def test_translator_returns_none_without_a_marker():
    from server.sessions.translator import Translator
    tr = Translator()                      # __init__ takes no args
    tr._turn_text = "just prose"
    assert tr._turn_end_model_request() is None


def test_driver_latches_the_request_with_its_turn_id():
    from server.sessions.claude_driver import ClaudeAgentDriver
    d = ClaudeAgentDriver.__new__(ClaudeAgentDriver)          # no __init__: pure latch test
    d._last_model_request = None
    e = ev.TurnEnd("turn-7", model_request={"model": "fable", "reason": "refactor"})
    ClaudeAgentDriver._latch_model_request(d, e)
    assert d.last_model_request == {
        "turn_id": "turn-7", "model": "fable", "reason": "refactor"}


def test_driver_latch_ignores_a_turn_without_a_request():
    from server.sessions.claude_driver import ClaudeAgentDriver
    d = ClaudeAgentDriver.__new__(ClaudeAgentDriver)
    d._last_model_request = None
    ClaudeAgentDriver._latch_model_request(d, ev.TurnEnd("turn-8"))
    assert d.last_model_request is None


def test_result_message_end_to_end_carries_model_request():
    """Drives the real feed()/ResultMessage path (not the isolated unit above) so a
    future edit that moves the parse-before-_reset() ordering breaks a test, not just
    production behavior."""
    marker = '<!-- ccswitch:model=haiku reason="trivial" -->'
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.stream_text(f"work done\n{marker}", uuid="t1"),
            f.assistant(TextBlock(text=f"work done\n{marker}"), uuid="t1", model="claude-opus-4-8"),
            f.result(session_id="s", uuid="t1")]
    end = _turn_end(_drive(msgs))
    assert end.model_request == {"model": "haiku", "reason": "trivial"}
