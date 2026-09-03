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


def build_viewer_projects_query(cursor: str | None) -> str:
    return (
        "query { viewer { projectsV2(first: 20%s) {"
        " pageInfo { endCursor hasNextPage }"
        " nodes { id number title closed updatedAt } } } }" % _after(cursor))


def parse_viewer_projects(payload: dict) -> tuple[list[GhProjectView], str | None, bool]:
    conn = (payload.get("data") or {}).get("viewer", {}).get("projectsV2", {}) or {}
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
