from server.sessions.events import Suggestions


def test_suggestions_to_wire():
    ev = Suggestions(session_id="s1", turn_id="t1", items=["Run the tests", "Commit it"])
    assert ev.to_wire() == {
        "type": "suggestions",
        "sessionId": "s1",
        "turnId": "t1",
        "items": ["Run the tests", "Commit it"],
    }
