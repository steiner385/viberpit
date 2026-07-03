from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="c1",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_record_to_dict_includes_active_goal():
    d = _record_to_dict(_rec(active_goal="ship the PR"), repo=None)
    assert d["activeGoal"] == "ship the PR"


def test_record_to_dict_active_goal_none_by_default():
    d = _record_to_dict(_rec(), repo=None)
    assert d["activeGoal"] is None


def test_record_to_dict_includes_effective_goal_and_source():
    d = _record_to_dict(_rec(active_goal="ship the PR"), repo=None)
    assert d["effectiveGoal"] == "ship the PR"
    assert d["goalSource"] == "explicit"


def test_record_to_dict_effective_goal_falls_back_to_name():
    d = _record_to_dict(_rec(name="Fix login bug"), repo=None)
    assert d["effectiveGoal"] == "Fix login bug"
    assert d["goalSource"] == "initial_prompt"
