"""Wire-format tests for recovery-related session events."""
from server.sessions import events as ev


def test_turn_failure_wire():
    e = ev.TurnFailure(kind="rate_limit", account="kindash", retry_after=8.0,
                       limit_type="five_hour")
    assert e.to_wire() == {
        "type": "turnFailure",
        "kind": "rate_limit",
        "account": "kindash",
        "retryAfter": 8.0,
        "limitType": "five_hour",
    }


def test_turn_failure_wire_no_retry_after():
    e = ev.TurnFailure(kind="auth_401", account="kindash")
    assert e.to_wire() == {
        "type": "turnFailure",
        "kind": "auth_401",
        "account": "kindash",
        "retryAfter": None,
        "limitType": None,
    }


