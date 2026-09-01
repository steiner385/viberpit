"""Pure GraphQL builder/parser tests for pm-sync (spec §4)."""
from datetime import datetime, timezone

from server.pm.github_sync import (
    WorkItemView,
    build_issues_query,
    build_prs_query,
    parse_page,
)


def _issue_payload():
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": "CUR1", "hasNextPage": True},
        "nodes": [{
            "number": 7, "title": "Bug", "body": "It broke", "state": "OPEN",
            "url": "https://github.com/o/r/issues/7",
            "createdAt": "2026-01-02T03:04:05Z", "updatedAt": "2026-01-03T00:00:00Z",
            "closedAt": None, "author": {"login": "tony"},
            "labels": {"nodes": [{"name": "bug"}]},
        }],
    }}}}


def _pr_payload():
    return {"data": {"repository": {"pullRequests": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [{
            "number": 9, "title": "Fix bug", "body": "", "state": "MERGED",
            "url": "https://github.com/o/r/pull/9",
            "createdAt": "2026-01-04T00:00:00Z", "updatedAt": "2026-01-05T00:00:00Z",
            "closedAt": "2026-01-05T00:00:00Z", "author": {"login": "tony"},
            "labels": {"nodes": []},
            "closingIssuesReferences": {"nodes": [{"number": 7}]},
        }],
    }}}}


def test_issue_query_includes_since_and_cursor():
    q = build_issues_query("o/r", "CUR0", "2026-01-01T00:00:00Z")
    assert 'owner: "o"' in q and 'name: "r"' in q
    assert 'after: "CUR0"' in q
    assert "filterBy" in q and "2026-01-01T00:00:00Z" in q


def test_pr_query_orders_updated_desc():
    q = build_prs_query("o/r", None)
    assert "UPDATED_AT" in q and "DESC" in q
    assert "closingIssuesReferences" in q


def test_parse_issue_page():
    items, cursor, more = parse_page(_issue_payload(), "o/r", "issue")
    assert cursor == "CUR1" and more is True
    (item,) = items
    assert item == WorkItemView(
        repo="o/r", number=7, kind="issue", title="Bug", body="It broke",
        state="open", labels=("bug",), author="tony",
        url="https://github.com/o/r/issues/7",
        gh_created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        gh_updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        gh_closed_at=None, linked_issue_numbers=())


def test_parse_pr_page_normalizes_state_and_links():
    items, cursor, more = parse_page(_pr_payload(), "o/r", "pr")
    assert (cursor, more) == (None, False)
    assert items[0].state == "merged"
    assert items[0].linked_issue_numbers == (7,)


def test_parse_garbage_is_empty():
    assert parse_page({}, "o/r", "issue") == ([], None, False)
    assert parse_page({"data": None}, "o/r", "pr") == ([], None, False)


def test_parse_malformed_conn_non_dict_list():
    """conn resolves to list (truthy) instead of dict — must skip gracefully."""
    payload = {"data": {"repository": {"issues": [1, 2, 3]}}}
    assert parse_page(payload, "o/r", "issue") == ([], None, False)


def test_parse_malformed_conn_non_dict_string():
    """conn resolves to string (truthy) instead of dict — must skip gracefully."""
    payload = {"data": {"repository": {"issues": "oops"}}}
    assert parse_page(payload, "o/r", "issue") == ([], None, False)


def test_parse_malformed_node_is_int():
    """A node is an int (truthy) instead of dict — must skip gracefully."""
    payload = {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [42]
    }}}}
    assert parse_page(payload, "o/r", "issue") == ([], None, False)


def test_parse_malformed_node_is_string():
    """A node is a string (truthy) instead of dict — must skip gracefully."""
    payload = {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": ["oops"]
    }}}}
    assert parse_page(payload, "o/r", "issue") == ([], None, False)


def test_parse_malformed_node_number_non_int():
    """Node has a non-integer number field — must skip gracefully."""
    payload = {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [{
            "number": "seven",
            "title": "Bad",
            "body": "",
            "state": "OPEN",
            "url": "https://github.com/o/r/issues/7",
            "createdAt": "2026-01-02T03:04:05Z",
            "updatedAt": "2026-01-03T00:00:00Z",
            "closedAt": None,
            "author": {"login": "tony"},
            "labels": {"nodes": []},
        }]
    }}}}
    assert parse_page(payload, "o/r", "issue") == ([], None, False)


def test_parse_malformed_linked_issue_ref_is_int():
    """A linked-issue ref is an int (truthy) instead of dict — must skip gracefully."""
    payload = {"data": {"repository": {"pullRequests": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [{
            "number": 9,
            "title": "Fix",
            "body": "",
            "state": "MERGED",
            "url": "https://github.com/o/r/pull/9",
            "createdAt": "2026-01-04T00:00:00Z",
            "updatedAt": "2026-01-05T00:00:00Z",
            "closedAt": "2026-01-05T00:00:00Z",
            "author": {"login": "tony"},
            "labels": {"nodes": []},
            "closingIssuesReferences": {"nodes": [42]},
        }],
    }}}}
    items, _, _ = parse_page(payload, "o/r", "pr")
    assert len(items) == 1
    assert items[0].linked_issue_numbers == ()


def test_parse_malformed_label_is_string():
    """A label is a string (truthy) instead of dict — must skip gracefully."""
    payload = {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": None, "hasNextPage": False},
        "nodes": [{
            "number": 7,
            "title": "Bug",
            "body": "",
            "state": "OPEN",
            "url": "https://github.com/o/r/issues/7",
            "createdAt": "2026-01-02T03:04:05Z",
            "updatedAt": "2026-01-03T00:00:00Z",
            "closedAt": None,
            "author": {"login": "tony"},
            "labels": {"nodes": ["bug", {"name": "critical"}]},
        }]
    }}}}
    items, _, _ = parse_page(payload, "o/r", "issue")
    assert len(items) == 1
    assert items[0].labels == ("critical",)
