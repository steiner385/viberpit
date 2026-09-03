"""Dependency-readiness gate — pure logic (no I/O).

An issue is *blocked* (not ready to work) while any of its dependencies is
still open, from three OR'd sources: GitHub native ``blockedBy`` relationships,
free-text ``Depends on #N`` / ``Blocked by #N`` markers in the body, and epic
ordering (sub-issue lists and task-list checklists, where list order is
execution order). Unresolvable dependency states fail CLOSED — respecting
declared order is the feature's whole point; a sweep retries every tick.
"""
from __future__ import annotations

from server.automations.backlog_deps import (
    blocked_refs,
    body_dep_refs,
    parse_body_dep_refs,
    parse_checklist_refs,
)


def _node(repo: str = "o/r", number: int = 5, body: str = "", **extra) -> dict:
    return {"number": number, "repository": {"nameWithOwner": repo},
            "body": body, **extra}


def _dep(repo: str, number: int, state: str) -> dict:
    return {"number": number, "state": state,
            "repository": {"nameWithOwner": repo}}


# --------------------------------------------------------------------------- #
# parse_body_dep_refs                                                          #
# --------------------------------------------------------------------------- #

def test_parse_body_depends_on_same_repo():
    assert parse_body_dep_refs("Depends on #12", "o/r") == ["o/r#12"]


def test_parse_body_blocked_by_cross_repo_and_list():
    refs = parse_body_dep_refs("Blocked by acme/lib#3, #4 and #6", "o/r")
    assert refs == ["acme/lib#3", "o/r#4", "o/r#6"]


def test_parse_body_case_insensitive_with_colon():
    assert parse_body_dep_refs("depends On: #5", "o/r") == ["o/r#5"]


def test_parse_body_plain_mention_is_not_a_dependency():
    assert parse_body_dep_refs("Related to #7, see #8", "o/r") == []


def test_parse_body_none_or_empty():
    assert parse_body_dep_refs(None, "o/r") == []
    assert parse_body_dep_refs("", "o/r") == []


# --------------------------------------------------------------------------- #
# parse_checklist_refs                                                         #
# --------------------------------------------------------------------------- #

def test_parse_checklist_ordered_refs_with_checked_state():
    body = "- [ ] #1\n- [x] #2 done already\nprose line\n* [ ] acme/lib#3"
    assert parse_checklist_refs(body, "o/r") == [
        ("o/r#1", False), ("o/r#2", True), ("acme/lib#3", False)]


def test_parse_checklist_items_without_refs_are_skipped():
    assert parse_checklist_refs("- [ ] write docs\n- [ ] #9", "o/r") == [
        ("o/r#9", False)]


# --------------------------------------------------------------------------- #
# blocked_refs — native blockedBy relationships                                #
# --------------------------------------------------------------------------- #

def test_native_open_blocker_blocks():
    node = _node(blockedBy={"nodes": [_dep("o/r", 3, "OPEN")]})
    assert blocked_refs(node) == ["o/r#3"]


def test_native_closed_blocker_does_not_block():
    node = _node(blockedBy={"nodes": [_dep("o/r", 3, "CLOSED")]})
    assert blocked_refs(node) == []


# --------------------------------------------------------------------------- #
# blocked_refs — body-text dependencies                                        #
# --------------------------------------------------------------------------- #

def test_body_dep_open_blocks_and_closed_does_not():
    node = _node(body="Depends on #12 and #13")
    states = {"o/r#12": "OPEN", "o/r#13": "CLOSED"}
    assert blocked_refs(node, states=states) == ["o/r#12"]


def test_body_dep_unresolved_state_fails_closed():
    node = _node(body="Blocked by #40")
    assert blocked_refs(node, states={}) == ["o/r#40"]


def test_body_dep_merged_pr_counts_as_done():
    node = _node(body="Depends on #12")
    assert blocked_refs(node, states={"o/r#12": "MERGED"}) == []


def test_body_dep_refs_helper_returns_canonical_set():
    node = _node(body="Depends on #12, acme/lib#3")
    assert body_dep_refs(node) == {"o/r#12", "acme/lib#3"}


# --------------------------------------------------------------------------- #
# blocked_refs — sub-issue parent, list order = execution order                #
# --------------------------------------------------------------------------- #

def _parent(repo: str, number: int, siblings: list[dict]) -> dict:
    return {"number": number, "repository": {"nameWithOwner": repo},
            "subIssues": {"nodes": siblings}}


def test_subissue_earlier_open_sibling_blocks():
    node = _node(number=2, parent=_parent("o/r", 9, [
        _dep("o/r", 1, "OPEN"), _dep("o/r", 2, "OPEN"), _dep("o/r", 3, "OPEN")]))
    assert blocked_refs(node) == ["o/r#1"]


def test_subissue_later_open_sibling_does_not_block():
    node = _node(number=2, parent=_parent("o/r", 9, [
        _dep("o/r", 1, "CLOSED"), _dep("o/r", 2, "OPEN"), _dep("o/r", 3, "OPEN")]))
    assert blocked_refs(node) == []


def test_subissue_candidate_missing_from_parent_list_is_not_blocked():
    node = _node(number=99, parent=_parent("o/r", 9, [_dep("o/r", 1, "OPEN")]))
    assert blocked_refs(node) == []


# --------------------------------------------------------------------------- #
# blocked_refs — task-list epics (trackedInIssues)                             #
# --------------------------------------------------------------------------- #

def _epic(repo: str, number: int, body: str, tracked: list[dict]) -> dict:
    return {"number": number, "repository": {"nameWithOwner": repo},
            "body": body, "trackedIssues": {"nodes": tracked}}


def test_epic_checklist_earlier_open_item_blocks():
    epic = _epic("o/r", 20, "- [x] #1\n- [ ] #2\n- [ ] #5",
                 [_dep("o/r", 1, "CLOSED"), _dep("o/r", 2, "OPEN")])
    node = _node(number=5, trackedInIssues={"nodes": [epic]})
    assert blocked_refs(node) == ["o/r#2"]


def test_epic_checklist_all_earlier_closed_is_ready():
    epic = _epic("o/r", 20, "- [x] #1\n- [ ] #5",
                 [_dep("o/r", 1, "CLOSED")])
    node = _node(number=5, trackedInIssues={"nodes": [epic]})
    assert blocked_refs(node) == []


def test_epic_checklist_unknown_state_unchecked_fails_closed():
    epic = _epic("o/r", 20, "- [ ] #1\n- [ ] #5", [])
    node = _node(number=5, trackedInIssues={"nodes": [epic]})
    assert blocked_refs(node) == ["o/r#1"]


def test_epic_checklist_unknown_state_checked_counts_as_done():
    epic = _epic("o/r", 20, "- [x] #1\n- [ ] #5", [])
    node = _node(number=5, trackedInIssues={"nodes": [epic]})
    assert blocked_refs(node) == []


def test_epic_without_candidate_in_checklist_imposes_no_order():
    epic = _epic("o/r", 20, "- [ ] #1\n- [ ] #2", [_dep("o/r", 1, "OPEN")])
    node = _node(number=5, trackedInIssues={"nodes": [epic]})
    assert blocked_refs(node) == []


# --------------------------------------------------------------------------- #
# blocked_refs — combination + compat                                          #
# --------------------------------------------------------------------------- #

def test_sources_combine_and_dedupe_preserving_order():
    node = _node(
        number=5,
        body="Depends on #3",
        blockedBy={"nodes": [_dep("o/r", 3, "OPEN"), _dep("o/r", 4, "OPEN")]},
    )
    assert blocked_refs(node, states={}) == ["o/r#3", "o/r#4"]


def test_node_without_dependency_fields_is_ready():
    # Old fixtures / API responses missing the new fields must behave as before.
    assert blocked_refs({"number": 1, "repository": {"nameWithOwner": "o/r"}}) == []
