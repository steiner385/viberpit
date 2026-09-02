"""Translator parses the marker onto TurnEnd; the driver latches it."""
from server.sessions import events as ev


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
    from server.sessions.driver import SessionDriver
    d = SessionDriver.__new__(SessionDriver)          # no __init__: pure latch test
    d._last_model_request = None
    e = ev.TurnEnd("turn-7", model_request={"model": "fable", "reason": "refactor"})
    SessionDriver._latch_model_request(d, e)
    assert d.last_model_request == {
        "turn_id": "turn-7", "model": "fable", "reason": "refactor"}


def test_driver_latch_ignores_a_turn_without_a_request():
    from server.sessions.driver import SessionDriver
    d = SessionDriver.__new__(SessionDriver)
    d._last_model_request = None
    SessionDriver._latch_model_request(d, ev.TurnEnd("turn-8"))
    assert d.last_model_request is None
