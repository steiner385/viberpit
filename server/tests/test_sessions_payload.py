# server/tests/test_sessions_payload.py
from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(id="s1", name="n", account="a", claude_session_id="c",
                         cols=80, rows=24, cwd="/x")


def test_record_to_dict_includes_suggestions():
    d = _record_to_dict(_rec(), repo=None, suggestions=["Run the tests"])
    assert d["latestSuggestions"] == ["Run the tests"]


def test_record_to_dict_defaults_empty_suggestions():
    d = _record_to_dict(_rec(), repo=None)
    assert d["latestSuggestions"] == []
