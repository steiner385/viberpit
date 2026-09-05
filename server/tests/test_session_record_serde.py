from datetime import datetime, timezone
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="S1", account="acct", engine=AgentEngine.CLAUDE_CODE, engine_session_id="cs",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_round_trip_with_created_at():
    r = _rec(created_at=datetime(2026, 6, 22, 10, 30, tzinfo=timezone.utc),
             repo="o/r", status="in_progress", name_is_auto=False)
    d = r.to_dict()
    assert d["createdAt"] == "2026-06-22T10:30:00+00:00"
    back = SessionRecord.from_dict(d)
    assert back == r


def test_round_trip_with_none_created_at():
    r = _rec(created_at=None)
    d = r.to_dict()
    assert d["createdAt"] is None
    assert SessionRecord.from_dict(d) == r


def test_to_dict_is_json_safe():
    import json
    json.dumps(_rec(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)).to_dict())


# ---------------------------------------------------------------------------
# last_user_interaction_at round-trip
# ---------------------------------------------------------------------------

def test_round_trip_with_last_user_interaction_at():
    r = _rec(last_user_interaction_at=datetime(2026, 6, 22, 10, 30, tzinfo=timezone.utc))
    d = r.to_dict()
    assert d["lastUserInteractionAt"] == "2026-06-22T10:30:00+00:00"
    back = SessionRecord.from_dict(d)
    assert back == r


def test_round_trip_with_none_last_user_interaction_at():
    r = _rec(last_user_interaction_at=None)
    d = r.to_dict()
    assert d["lastUserInteractionAt"] is None
    assert SessionRecord.from_dict(d) == r
