from types import SimpleNamespace
from server.sessions.goals import effective_goal


def test_explicit_goal_wins():
    rec = SimpleNamespace(active_goal="ship the PR", name="Fix login bug")
    assert effective_goal(rec) == ("ship the PR", "explicit")


def test_falls_back_to_name_when_no_goal():
    rec = SimpleNamespace(active_goal=None, name="Fix login bug")
    assert effective_goal(rec) == ("Fix login bug", "initial_prompt")


def test_whitespace_goal_is_ignored():
    rec = SimpleNamespace(active_goal="   ", name="Fix login bug")
    assert effective_goal(rec) == ("Fix login bug", "initial_prompt")
