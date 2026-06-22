from server.sessions.events import Suggestions
from server.sessions.suggestions import build_prompt, parse_suggestions


def test_suggestions_to_wire():
    ev = Suggestions(session_id="s1", turn_id="t1", items=["Run the tests", "Commit it"])
    assert ev.to_wire() == {
        "type": "suggestions",
        "sessionId": "s1",
        "turnId": "t1",
        "items": ["Run the tests", "Commit it"],
    }


def test_parse_plain_json_array():
    assert parse_suggestions('["Run tests", "Commit it"]') == ["Run tests", "Commit it"]


def test_parse_tolerates_surrounding_prose_and_fences():
    raw = 'Here you go:\n```json\n["A", "B"]\n```\nHope that helps!'
    assert parse_suggestions(raw) == ["A", "B"]


def test_parse_filters_nonstrings_and_blanks_and_truncates():
    raw = '["a", "", "  ", 5, null, "b", "c", "d", "e"]'
    assert parse_suggestions(raw, limit=4) == ["a", "b", "c", "d"]


def test_parse_garbage_returns_empty():
    assert parse_suggestions("not json at all") == []
    assert parse_suggestions("") == []
    assert parse_suggestions('{"not": "a list"}') == []


def test_build_prompt_includes_context():
    p = build_prompt("User: hi\n\nAssistant: hello")
    assert "User: hi" in p and "Assistant: hello" in p
