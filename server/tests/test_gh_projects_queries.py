from server.pm.gh_projects import (
    build_owner_projects_query, build_project_items_query,
    parse_created_field, parse_project_fields, parse_project_items,
    parse_viewer_projects, build_issue_relations_query, parse_issue_relations)


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


def _owner_projects_payload():
    """Same projectsV2 connection, rooted at ``data.user`` (owner query)."""
    return {"data": {"user": {"projectsV2": {
        "pageInfo": {"endCursor": "C1", "hasNextPage": False},
        "nodes": [
            {"id": "PVT_1", "number": 7, "title": "cc-account-switcher", "closed": False,
             "updatedAt": "2026-09-01T10:00:00Z"},
            {"id": "PVT_2", "number": 8, "title": "Old board", "closed": True,
             "updatedAt": "2026-01-01T10:00:00Z"},
        ]}}}}


def _repository_owner_projects_payload():
    """Same projectsV2 connection, rooted at ``data.repositoryOwner`` — the
    ``repositoryOwner(login:)`` root resolves both Users and Organizations,
    unlike ``user(login:)`` which 404s on an org owner."""
    return {"data": {"repositoryOwner": {"projectsV2": {
        "pageInfo": {"endCursor": "C1", "hasNextPage": False},
        "nodes": [
            {"id": "PVT_1", "number": 7, "title": "cc-account-switcher", "closed": False,
             "updatedAt": "2026-09-01T10:00:00Z"},
            {"id": "PVT_2", "number": 8, "title": "Old board", "closed": True,
             "updatedAt": "2026-01-01T10:00:00Z"},
        ]}}}}


def test_parse_viewer_projects_skips_closed_and_pages():
    views, cursor, more = parse_viewer_projects(_projects_payload())
    assert [(v.number, v.title) for v in views] == [(7, "cc-account-switcher")]
    assert cursor == "C1" and more is False


def test_parse_viewer_projects_accepts_user_root():
    """Under an App token discovery roots at ``user(login:)`` (viewer = the bot),
    so the parser must read the same connection under ``data.user`` too."""
    views, cursor, more = parse_viewer_projects(_owner_projects_payload())
    assert [(v.number, v.title) for v in views] == [(7, "cc-account-switcher")]
    assert cursor == "C1" and more is False


def test_parse_viewer_projects_accepts_repository_owner_root():
    """Discovery now roots the owner query at ``repositoryOwner(login:)`` (works
    for both Users and Organizations), so the parser must read the same
    connection under ``data.repositoryOwner`` too."""
    views, cursor, more = parse_viewer_projects(_repository_owner_projects_payload())
    assert [(v.number, v.title) for v in views] == [(7, "cc-account-switcher")]
    assert cursor == "C1" and more is False


def test_build_owner_projects_query_roots_at_repository_owner():
    q = build_owner_projects_query("steiner385", None)
    assert 'repositoryOwner(login: "steiner385")' in q
    assert "... on ProjectV2Owner" in q
    assert "projectsV2(first: 20)" in q
    assert "after:" not in q
    assert "user(login:" not in q


def test_build_owner_projects_query_embeds_cursor():
    q = build_owner_projects_query("steiner385", "C1")
    assert 'after: "C1"' in q
    assert 'repositoryOwner(login: "steiner385")' in q
    assert "user(login:" not in q


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
    assert 'after: "C1"' in build_owner_projects_query("steiner385", "C1")
    assert "after:" not in build_owner_projects_query("steiner385", None)
    assert 'node(id: "PVT_1")' in build_project_items_query("PVT_1", None)


def _relations_payload():
    return {"data": {"repository": {"issues": {
        "pageInfo": {"endCursor": "R1", "hasNextPage": False},
        "nodes": [
            {"number": 945,
             "blockedBy": {"nodes": []},
             "parent": None,
             "subIssues": {"nodes": [
                 {"number": 950, "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
                 {"number": 951, "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             ]}},
            {"number": 950,
             "blockedBy": {"nodes": [
                 {"number": 12, "repository": {"nameWithOwner": "steiner385/KinDash"}}]},
             "parent": {"number": 945,
                        "repository": {"nameWithOwner": "steiner385/cc-account-switcher"}},
             "subIssues": {"nodes": []}},
        ]}}}}


def test_parse_issue_relations_builds_canonical_refs():
    rels, cursor, more = parse_issue_relations(
        _relations_payload(), "steiner385/cc-account-switcher")
    assert cursor == "R1" and more is False
    epic, child = rels
    assert epic.sub_issue_refs == [
        "steiner385/cc-account-switcher#950", "steiner385/cc-account-switcher#951"]
    assert epic.parent_ref is None
    assert child.blocked_by == ["steiner385/KinDash#12"]
    assert child.parent_ref == "steiner385/cc-account-switcher#945"


def test_relations_query_targets_open_issues():
    q = build_issue_relations_query("steiner385/cc-account-switcher", None)
    assert 'owner: "steiner385"' in q and 'name: "cc-account-switcher"' in q
    assert "states: OPEN" in q


def test_relations_capture_node_id():
    payload = _relations_payload()
    payload["data"]["repository"]["issues"]["nodes"][0]["id"] = "I_945"
    rels, _, _ = parse_issue_relations(payload, "steiner385/cc-account-switcher")
    assert rels[0].node_id == "I_945"
    assert "nodes { id number" in build_issue_relations_query(
        "steiner385/cc-account-switcher", None)


def test_parse_created_field_reads_create_root():
    payload = {"data": {"createProjectV2Field": {"projectV2Field": {
        "id": "F_sc", "name": "Score"}}}}
    assert parse_created_field(payload) == ("F_sc", "Score", {})


def test_parse_created_field_reads_update_root_with_options():
    payload = {"data": {"updateProjectV2Field": {"projectV2Field": {
        "id": "F_s", "name": "Status",
        "options": [{"id": "O_r", "name": "Ready"}, {"id": "O_d", "name": "Done"}]}}}}
    assert parse_created_field(payload) == (
        "F_s", "Status", {"Ready": "O_r", "Done": "O_d"})


def test_parse_created_field_none_on_missing_id():
    assert parse_created_field({"data": {"createProjectV2Field": {"projectV2Field": None}}}) is None
    assert parse_created_field({"data": {}}) is None
    assert parse_created_field({}) is None


def test_project_node_missing_distinguishes_deleted_from_empty():
    from server.pm.gh_projects import project_node_missing
    # board deleted on GitHub: node(id:) resolves null (issue #973).
    assert project_node_missing({"data": {"node": None}}) is True
    assert project_node_missing(
        {"data": {"node": None}, "errors": [{"message": "Could not resolve"}]}) is True
    # board exists but is empty: node is a dict with an empty items connection.
    assert project_node_missing(
        {"data": {"node": {"items": {"nodes": []}}}}) is False
    # transport/other failure: no data or no node key -> NOT a deletion signal,
    # so the caller's error handling (not the prune-halt) takes over.
    assert project_node_missing({}) is False
    assert project_node_missing({"data": {}}) is False
    assert project_node_missing({"data": None}) is False
