"""#3076: a live session's plan leaves are not backlog-runner candidates."""
import json
from types import SimpleNamespace

import pytest

from server.db.models import Session as DbSession
from server.pm.queue import skip_reason
from server.pm.queue_store import load_queue_rows
from server.pm.session_ownership import (MAX_ANCESTOR_DEPTH, marker_owners, owned_by_live_session,
                                         owner_marker)
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.tools import build_session_tools
from server.tests.test_pm_queue_store import NOW, _db, _item, _seed_product

REPO = "cairnea/KinDash"
SID = "5e1d8542-232c-413c-98f7-0854461dddea"


def test_marker_round_trips_and_ignores_other_comments():
    body = f"Leaf T1030\n\n{owner_marker(SID)}\n<!-- viberpit:model=opus -->"
    assert marker_owners(body) == {SID}
    assert marker_owners(None) == frozenset()
    assert marker_owners("<!-- viberpit:owner-session= -->") == frozenset()


def test_ownership_walk_inherits_from_ancestors_and_stops_on_a_cycle():
    nodes = {"leaf": ("r#3", "", "epic"), "epic": ("r#2", owner_marker(SID), "init"),
             "init": ("r#1", "", None)}
    assert owned_by_live_session("leaf", nodes=nodes, live_refs=set(), live_ids={SID})
    assert not owned_by_live_session("leaf", nodes=nodes, live_refs=set(), live_ids={"other"})
    assert owned_by_live_session("leaf", nodes=nodes, live_refs={"r#1"}, live_ids=set())
    cyclic = {"a": ("r#1", "", "b"), "b": ("r#2", "", "a")}
    assert not owned_by_live_session("a", nodes=cyclic, live_refs=set(), live_ids=set())


def test_ownership_walk_is_depth_capped():
    depth = MAX_ANCESTOR_DEPTH + 1
    nodes = {f"n{i}": (f"r#{i}", "", f"n{i + 1}") for i in range(depth)}
    nodes[f"n{depth}"] = (f"r#{depth}", owner_marker(SID), None)
    assert not owned_by_live_session("n0", nodes=nodes, live_refs=set(), live_ids={SID})
    assert owned_by_live_session("n1", nodes=nodes, live_refs=set(), live_ids={SID})


def _plan(db, *, epic_body="", leaf_body="body"):
    _seed_product(db, "kindash", REPO)
    epic = _item(db, REPO, 19162, status="Backlog", is_epic=True)
    epic.body = epic_body
    leaf = _item(db, REPO, 19169, status="Ready")
    leaf.body = leaf_body
    leaf.parent_work_item_id = epic.id
    return epic, leaf


def _session(db, sid, *, status="in_progress", origin_ref=None):
    db.add(DbSession(id=sid, name="s", account_name="a", cwd="/tmp", origin_ref=origin_ref,
                     status=status))


def _leaf_row(db):
    db.commit()
    return {r.ref: r for r in load_queue_rows(db, now=NOW)}[f"{REPO}#19169"]


def test_marker_on_the_epic_claims_the_leaf_for_a_live_human_session():
    db = _db()
    _plan(db, epic_body=f"Plan\n{owner_marker(SID)}")
    _session(db, SID)  # human-created: no origin_ref — the #3076 case
    row = _leaf_row(db)
    assert row.live_session is True
    assert skip_reason(row, min_status="Backlog", respect_dependencies=True, now=NOW,
                       trusted_authors=frozenset({"steiner385"})) == "live_session"


def test_marker_on_the_leaf_itself_claims_it():
    db = _db()
    _plan(db, leaf_body=f"T1030\n{owner_marker(SID)}")
    _session(db, SID)
    assert _leaf_row(db).live_session is True


def test_claim_lapses_when_the_owning_session_is_no_longer_live():
    db = _db()
    _plan(db, epic_body=owner_marker(SID))
    _session(db, SID, status="archived")
    assert _leaf_row(db).live_session is False


def test_a_marker_naming_an_unknown_session_claims_nothing():
    db = _db()
    _plan(db, epic_body=owner_marker("not-a-session"))
    _session(db, SID)
    assert _leaf_row(db).live_session is False


def test_a_session_dispatched_on_the_epic_owns_its_leaves():
    db = _db()
    _plan(db)
    _session(db, SID, origin_ref=f"{REPO}#19162")
    assert _leaf_row(db).live_session is True


def test_ancestors_off_the_board_are_still_walked():
    """The epic need not be on the board (or even open) to pass its claim down."""
    db = _db()
    _seed_product(db, "kindash", REPO)
    from server.db.models import WorkItem
    epic = WorkItem(repo=REPO, number=19162, kind="issue", state="closed", title="epic",
                    body=owner_marker(SID), url="u", author="steiner385", labels=[], assignees=[])
    db.add(epic)
    db.flush()
    leaf = _item(db, REPO, 19169, status="Ready")
    leaf.parent_work_item_id = epic.id
    _session(db, SID)
    assert _leaf_row(db).live_session is True


@pytest.mark.asyncio
async def test_session_context_hands_out_this_sessions_marker():
    rec = SimpleNamespace(id=SID, name="S", cwd=None, parent_session_id=None)
    ctx = SessionToolContext(session_id=SID, identity="u@x", kind=None, engine="claude_code",
                             record=lambda: rec, host="rocky")
    tool = next(t for t in build_session_tools(ctx) if t.name == "session_context")
    data = json.loads((await tool.handler({}))["content"][0]["text"])["data"]
    assert data["issueOwnerMarker"] == owner_marker(SID)
    assert marker_owners(data["issueOwnerMarker"]) == {SID}


def test_comment_borne_marker_claims_the_issue_for_a_live_session():
    """#3152: a session that claims by comment, not body, still suppresses the runner."""
    db = _db()
    _, leaf = _plan(db)
    leaf.comment_owner_ids = [SID]
    _session(db, SID)
    assert _leaf_row(db).live_session is True


def test_comment_borne_marker_lapses_with_its_session():
    db = _db()
    _, leaf = _plan(db)
    leaf.comment_owner_ids = [SID]
    _session(db, SID, status="archived")
    assert _leaf_row(db).live_session is False


def test_sync_extracts_owner_markers_from_comments():
    from server.pm.github_sync import _comment_owner_ids
    node = {"comments": {"nodes": [
        {"body": f"claim {owner_marker(SID)}"}, {"body": "no marker"}, None,
        {"body": f"{owner_marker(SID)} {owner_marker('other-1')}"}]}}
    assert _comment_owner_ids(node) == (SID, "other-1")
    assert _comment_owner_ids({}) == ()
