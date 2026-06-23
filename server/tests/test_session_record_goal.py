from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="c1",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_active_goal_defaults_none():
    assert _rec().active_goal is None


def test_active_goal_round_trips_through_dict():
    rec = _rec(active_goal="ship the PR")
    d = rec.to_dict()
    assert d["activeGoal"] == "ship the PR"
    assert SessionRecord.from_dict(d).active_goal == "ship the PR"


def test_from_dict_missing_active_goal_is_none():
    d = _rec().to_dict()
    del d["activeGoal"]
    assert SessionRecord.from_dict(d).active_goal is None
