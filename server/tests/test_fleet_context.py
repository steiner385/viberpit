"""Pure fleet-view shaping (server/automations/fleet_context.py): the decider's
cross-session awareness must be bounded, self-excluding, and correctly split into
same-repo detail vs one-line summaries."""
from server.automations.completion_bar import PrFact, PrFacts
from server.automations.fleet_context import (
    MAX_ARCHIVED, MAX_OTHER_ACTIVE, MAX_SAME_REPO,
    ArchivedFleetSession, FleetSession, build_fleet_view, pr_summary,
)


def _fs(sid, *, name=None, repo=None, branch=None, status="in_progress",
        goal="do the thing", pr=None):
    return FleetSession(session_id=sid, name=name or sid, repo=repo, branch=branch,
                        status=status, goal=goal, pr=pr)


# --- pr_summary -----------------------------------------------------------------

def test_pr_summary_precedence_merged_beats_armed_beats_unarmed():
    merged = PrFact(1, "merged", False, False, "u")
    armed = PrFact(2, "open", False, True, "u")
    unarmed = PrFact(3, "open", False, False, "u")
    assert pr_summary(PrFacts(True, (unarmed, armed, merged))) == "PR #1 merged"
    assert pr_summary(PrFacts(True, (unarmed, armed))) == "PR #2 open (auto-merge armed)"
    assert pr_summary(PrFacts(True, (unarmed,))) == "PR #3 open (not armed)"


def test_pr_summary_draft_and_empty():
    draft = PrFact(4, "draft", True, False, "u")
    assert pr_summary(PrFacts(True, (draft,))) == "PR #4 draft (not armed)"
    assert pr_summary(PrFacts(True, ())) is None
    assert pr_summary(None) is None


# --- build_fleet_view -----------------------------------------------------------

def test_alone_in_fleet_returns_none():
    assert build_fleet_view("s1", [_fs("s1", repo="o/r")], []) is None
    assert build_fleet_view("s1", [], []) is None


def test_excludes_self_and_splits_same_repo_from_others():
    active = [
        _fs("me", repo="o/r", branch="feat/me"),
        _fs("sib", repo="o/r", branch="feat/sib", status="done",
            goal="fix the parser", pr="PR #591 merged"),
        _fs("elsewhere", repo="o/other", goal="unrelated work"),
        _fs("scratch", repo=None, goal="scratch pad"),
    ]
    view = build_fleet_view("me", active, [])
    assert view is not None
    sibs = view["same_repo_sessions"]
    assert [s["session"] for s in sibs] == ["sib"]
    assert sibs[0]["branch"] == "feat/sib" and sibs[0]["pr"] == "PR #591 merged"
    # Everything not in my repo (including scratch/None-repo) is a one-liner.
    others = view["other_active_sessions"]
    assert len(others) == 2
    assert any("elsewhere" in line and "o/other" in line for line in others)
    # And I never appear anywhere.
    blob = str(view)
    assert "feat/me" not in blob


def test_no_repo_session_sees_all_actives_as_one_liners():
    # A scratch session (repo=None) must not lump None-repo siblings into
    # "same_repo_sessions" — None == None is not a shared repo.
    active = [_fs("me"), _fs("other-scratch"), _fs("repoed", repo="o/r")]
    view = build_fleet_view("me", active, [])
    assert view is not None
    assert "same_repo_sessions" not in view
    assert len(view["other_active_sessions"]) == 2


def test_caps_bound_the_prompt():
    active = [_fs("me", repo="o/r")]
    active += [_fs(f"sib{i}", repo="o/r") for i in range(MAX_SAME_REPO + 5)]
    active += [_fs(f"other{i}", repo="x/y") for i in range(MAX_OTHER_ACTIVE + 5)]
    archived = [ArchivedFleetSession(name=f"a{i}", repo="o/r", goal="g",
                                     outcome="archived", hours_ago=1.0)
                for i in range(MAX_ARCHIVED + 5)]
    view = build_fleet_view("me", active, archived)
    assert len(view["same_repo_sessions"]) == MAX_SAME_REPO
    assert len(view["other_active_sessions"]) == MAX_OTHER_ACTIVE
    assert len(view["recently_archived"]) == MAX_ARCHIVED


def test_goals_are_clipped():
    long_goal = "x" * 500
    view = build_fleet_view("me", [_fs("me", repo="o/r"),
                                   _fs("sib", repo="o/r", goal=long_goal)], [])
    assert len(view["same_repo_sessions"][0]["goal"]) <= 121


def test_archived_entries_carry_outcome_and_age():
    archived = [ArchivedFleetSession(name="shipped", repo="o/r",
                                     goal="fix parser", outcome="PR #591 merged",
                                     hours_ago=3.2)]
    view = build_fleet_view("me", [_fs("me", repo="o/r")], archived)
    entry = view["recently_archived"][0]
    assert entry == {"session": "shipped", "goal": "fix parser",
                     "outcome": "PR #591 merged", "hours_ago": 3.2, "repo": "o/r"}
