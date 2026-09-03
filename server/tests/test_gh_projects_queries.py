from server.pm.gh_projects import (
    build_project_items_query, build_viewer_projects_query, parse_project_fields,
    parse_project_items, parse_viewer_projects)


def _projects_payload():
    return {"data": {"viewer": {"projectsV2": {
        "pageInfo": {"endCursor": "C1", "hasNextPage": False},
        "nodes": [
            {"id": "PVT_1", "number": 7, "title": "cc-account-switcher", "closed": False,
             "updatedAt": "2026-09-01T10:00:00Z"},
            {"id": "PVT_2", "number": 8, "title": "Old board", "closed": True,
             "updatedAt": "2026-01-01T10:00:00Z"},
        ]}}}}


def _fields_payload():
    return {"data": {"node": {"fields": {"nodes": [
        {"id": "F_status", "name": "Status",
         "options": [{"id": "O_ready", "name": "Ready"}, {"id": "O_done", "name": "Done"}]},
        {"id": "F_score", "name": "Score"},
        {},  # unrecognized field type -> empty object from inline fragments
    ]}}}}


def _items_payload():
    return {"data": {"node": {"items": {
        "pageInfo": {"endCursor": "I1", "hasNextPage": True},
        "nodes": [
            {"id": "PVTI_1", "updatedAt": "2026-09-02T09:00:00Z",
             "content": {"__typename": "Issue", "number": 950,
                         "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             "fieldValues": {"nodes": [
                 {"name": "Ready", "field": {"name": "Status"}},
                 {"number": 12.0, "field": {"name": "Score"}},
             ]}},
            {"id": "PVTI_2", "updatedAt": None,
             "content": {"__typename": "DraftIssue"}, "fieldValues": {"nodes": []}},
        ]}}}}


def test_parse_viewer_projects_skips_closed_and_pages():
    views, cursor, more = parse_viewer_projects(_projects_payload())
    assert [(v.number, v.title) for v in views] == [(7, "cc-account-switcher")]
    assert cursor == "C1" and more is False


def test_parse_project_fields_caches_ids_and_options():
    cache = parse_project_fields(_fields_payload())
    assert cache["Status"] == {"field_id": "F_status",
                               "options": {"Ready": "O_ready", "Done": "O_done"}}
    assert cache["Score"] == {"field_id": "F_score", "options": {}}


def test_parse_project_items_extracts_fields_and_marks_drafts():
    items, cursor, more = parse_project_items(_items_payload())
    assert cursor == "I1" and more is True
    issue, draft = items
    assert (issue.repo, issue.number, issue.kind) == ("steiner385/cc-account-switcher", 950, "issue")
    assert (issue.status, issue.score, issue.stage) == ("Ready", 12.0, None)
    assert draft.kind is None and draft.repo is None


def test_queries_embed_pagination_cursor():
    assert 'after: "C1"' in build_viewer_projects_query("C1")
    assert "after:" not in build_viewer_projects_query(None)
    assert 'node(id: "PVT_1")' in build_project_items_query("PVT_1", None)
