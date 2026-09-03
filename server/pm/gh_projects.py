"""Pure GraphQL builders/parsers for the Projects v2 mirror (spec 003 Phase A).

Field selections were verified against the live schema by the Phase 0 spike
(specs/003-gh-projects-integration/phase0-findings.md).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GhProjectView:
    node_id: str
    number: int
    title: str
    updated_at: str | None


@dataclass(frozen=True)
class GhProjectItemView:
    item_node_id: str
    repo: str | None
    number: int | None
    kind: str | None
    status: str | None
    score: float | None
    stage: str | None
    updated_at: str | None


def _after(cursor: str | None) -> str:
    return f', after: "{cursor}"' if cursor else ""


_PROJECTS_SELECTION = (
    " projectsV2(first: 20%s) {"
    " pageInfo { endCursor hasNextPage }"
    " nodes { id number title closed updatedAt } }")


def build_owner_projects_query(owner: str, cursor: str | None) -> str:
    """Projects v2 rooted at ``repositoryOwner(login: owner)`` — usable under a
    GitHub App installation token, where ``viewer`` resolves to the App bot
    rather than the owner (Phase 0 finding), AND for both User and
    Organization owners (verified live against the GitHub API): ``user(login:)``
    404s on an org login (e.g. ``cairnea`` on ``cairnea/kindash``), while
    ``repositoryOwner`` resolves either via the ``ProjectV2Owner`` interface."""
    return 'query { repositoryOwner(login: "%s") { ... on ProjectV2Owner {%s } } }' % (
        owner, _PROJECTS_SELECTION % _after(cursor))


def _projects_root(data: dict) -> dict:
    """Return the first owner root present in a projectsV2 discovery payload.
    ``viewer`` is a leftover viewer-rooted shape; ``user``/``repositoryOwner``
    are the owner-rooted query's roots — ``repositoryOwner`` (current) resolves
    both Users and Organizations, ``user`` (legacy) only Users."""
    for key in ("repositoryOwner", "user", "viewer"):
        root = data.get(key)
        if root:
            return root
    return {}


def parse_viewer_projects(payload: dict) -> tuple[list[GhProjectView], str | None, bool]:
    """Parse a projectsV2 connection under ``data.repositoryOwner`` (current
    owner query), or the legacy ``data.user`` / ``data.viewer`` roots — all
    carry the identical selection, so one parser serves every root
    ``build_owner_projects_query`` has ever used."""
    data = payload.get("data") or {}
    root = _projects_root(data)
    conn = (root or {}).get("projectsV2", {}) or {}
    views = [
        GhProjectView(node_id=n["id"], number=n["number"], title=n.get("title") or "",
                      updated_at=n.get("updatedAt"))
        for n in conn.get("nodes") or [] if n and not n.get("closed")]
    page = conn.get("pageInfo") or {}
    return views, page.get("endCursor"), bool(page.get("hasNextPage"))


def build_project_fields_query(project_node_id: str) -> str:
    return (
        'query { node(id: "%s") { ... on ProjectV2 { fields(first: 50) { nodes {'
        " ... on ProjectV2FieldCommon { id name }"
        " ... on ProjectV2SingleSelectField { id name options { id name } }"
        " } } } } }" % project_node_id)


def project_node_missing(payload: dict) -> bool:
    """True when a ``node(id:)`` query resolved the board to ``null`` — i.e. the
    Projects v2 board was DELETED on GitHub (spec 003 §10, issue #973).

    This is deliberately narrow: only ``data.node is None`` with the ``node`` key
    present counts as deletion. A missing ``data``/``node`` key (transport error,
    scope error, empty ``{}``) is NOT a deletion signal — the caller's normal
    error handling owns those. This lets the mirror distinguish "board deleted"
    (loud needs_human halt, no pruning) from "board empty" (``node`` is a dict
    with zero items — a legitimate reconcile that prunes stale rows)."""
    data = payload.get("data")
    return isinstance(data, dict) and "node" in data and data["node"] is None


def parse_project_fields(payload: dict) -> dict:
    nodes = (((payload.get("data") or {}).get("node") or {}).get("fields") or {}).get("nodes") or []
    cache: dict = {}
    for node in nodes:
        if not node or "name" not in node:
            continue
        cache[node["name"]] = {
            "field_id": node.get("id"),
            "options": {o["name"]: o["id"] for o in node.get("options") or []},
        }
    return cache


def build_project_items_query(project_node_id: str, cursor: str | None) -> str:
    return (
        'query { node(id: "%s") { ... on ProjectV2 { items(first: 100%s) {'
        " pageInfo { endCursor hasNextPage }"
        " nodes { id updatedAt"
        " content { __typename"
        " ... on Issue { number repository { nameWithOwner } }"
        " ... on PullRequest { number repository { nameWithOwner } } }"
        " fieldValues(first: 20) { nodes {"
        " ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }"
        " ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }"
        " } } } } } } }" % (project_node_id, _after(cursor)))


_KINDS = {"Issue": "issue", "PullRequest": "pr"}


def parse_project_items(payload: dict) -> tuple[list[GhProjectItemView], str | None, bool]:
    conn = (((payload.get("data") or {}).get("node") or {}).get("items") or {})
    items: list[GhProjectItemView] = []
    for node in conn.get("nodes") or []:
        if not node:
            continue
        content = node.get("content") or {}
        typename = content.get("__typename")
        kind = _KINDS.get(typename) if isinstance(typename, str) else None
        status = score = stage = None
        for fv in (node.get("fieldValues") or {}).get("nodes") or []:
            fname = ((fv or {}).get("field") or {}).get("name")
            if fname == "Status":
                status = fv.get("name")
            elif fname == "Score":
                score = fv.get("number")
            elif fname == "Stage":
                stage = fv.get("name")
        items.append(GhProjectItemView(
            item_node_id=node["id"],
            repo=(content.get("repository") or {}).get("nameWithOwner") if kind else None,
            number=content.get("number") if kind else None,
            kind=kind, status=status, score=score, stage=stage,
            updated_at=node.get("updatedAt")))
    page = conn.get("pageInfo") or {}
    return items, page.get("endCursor"), bool(page.get("hasNextPage"))


@dataclass(frozen=True)
class IssueRelView:
    repo: str
    number: int
    blocked_by: list[str]
    parent_ref: str | None
    sub_issue_refs: list[str]


def _ref(node: dict | None) -> str | None:
    if not node:
        return None
    repo = (node.get("repository") or {}).get("nameWithOwner")
    number = node.get("number")
    if not repo or number is None:
        return None
    return f"{repo}#{number}"


def build_issue_relations_query(repo: str, cursor: str | None) -> str:
    owner, name = repo.split("/", 1)
    return (
        'query { repository(owner: "%s", name: "%s") {'
        " issues(first: 100, states: OPEN%s) {"
        " pageInfo { endCursor hasNextPage }"
        " nodes { number"
        " blockedBy(first: 50) { nodes { number repository { nameWithOwner } } }"
        " parent { number repository { nameWithOwner } }"
        " subIssues(first: 100) { nodes { number repository { nameWithOwner } } }"
        " } } } }" % (owner, name, _after(cursor)))


def parse_issue_relations(payload: dict, repo: str) -> tuple[list[IssueRelView], str | None, bool]:
    conn = (((payload.get("data") or {}).get("repository") or {}).get("issues") or {})
    rels = []
    for node in conn.get("nodes") or []:
        if not node:
            continue
        rels.append(IssueRelView(
            repo=repo, number=node["number"],
            blocked_by=[r for r in (
                _ref(b) for b in (node.get("blockedBy") or {}).get("nodes") or []) if r],
            parent_ref=_ref(node.get("parent")),
            sub_issue_refs=[r for r in (
                _ref(s) for s in (node.get("subIssues") or {}).get("nodes") or []) if r]))
    page = conn.get("pageInfo") or {}
    return rels, page.get("endCursor"), bool(page.get("hasNextPage"))
