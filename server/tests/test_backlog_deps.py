"""Dependency-marker parsing — pure logic (no I/O).

Two text signals GitHub has no first-class field for, which the reconciler
compiles into dependency edges: ``Depends on #N`` / ``Blocked by #N`` body
markers, and a tracking epic's task-list checklist (list order is execution
order). Gating on the compiled graph is the queue's job (server/pm/queue.py);
what is asserted here is only that the markers parse the way authors write them.
"""
from __future__ import annotations

from server.automations.backlog_deps import parse_body_dep_refs, parse_checklist_refs


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
