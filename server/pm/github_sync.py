"""GraphQL query builders + payload parsers for the pm-sync mirror (spec §4).

Pure functions: the loop owns pagination/watermark policy and the gh_run seam
owns transport. Issues support server-side ``filterBy: {since:}``; pull
requests do NOT, so PR queries are newest-first by UPDATED_AT and the caller
stops paging once nodes fall behind the watermark.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_PAGE_ISSUES = 100
_PAGE_PRS = 50

_ITEM_FIELDS = """
      number title body state url createdAt updatedAt closedAt
      author { login }
      labels(first: 20) { nodes { name } }"""


@dataclass(frozen=True)
class WorkItemView:
    repo: str
    number: int
    kind: str  # "issue" | "pr"
    title: str
    body: str
    state: str  # open | closed | merged
    labels: tuple[str, ...]
    author: str
    url: str
    gh_created_at: datetime | None
    gh_updated_at: datetime | None
    gh_closed_at: datetime | None
    linked_issue_numbers: tuple[int, ...]
    # GitHub GraphQL node id, captured for the reconciler (addProjectV2ItemById
    # content ids, addBlockedBy targets — spec 003 Phase B). Defaulted so every
    # existing positional/keyword construction keeps working.
    node_id: str | None = None


def _owner_name(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def canonical_repo_name(payload: dict) -> str | None:
    """GitHub's own ``nameWithOwner`` for the repository a repo-scoped query was
    rooted at, or ``None`` when the payload doesn't carry it.

    GitHub resolves ``repository(owner:, name:)`` through renames and
    case-insensitively, so a query for a stale or differently-cased alias
    still succeeds — and every mirrored row would be keyed on the alias while
    GitHub-sourced refs (``parent``, ``subIssues``, ``blockedBy``, PR closing
    refs) carry the canonical name, splitting the graph (issue #999). The loop
    compares this against ``ProjectRepo.repo`` and renames the mirror when they
    differ, so the canonical name is the single key everywhere downstream."""
    try:
        repo = (payload.get("data") or {}).get("repository") or {}
    except AttributeError:
        return None
    name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
    return name if isinstance(name, str) and "/" in name else None


def build_issues_query(repo: str, cursor: str | None, since_iso: str | None) -> str:
    owner, name = _owner_name(repo)
    after = f', after: "{cursor}"' if cursor else ""
    filter_by = f", filterBy: {{since: \"{since_iso}\"}}" if since_iso else ""
    return f"""query {{
  repository(owner: "{owner}", name: "{name}") {{
    nameWithOwner
    issues(first: {_PAGE_ISSUES}{after}, states: [OPEN, CLOSED],
           orderBy: {{field: UPDATED_AT, direction: ASC}}{filter_by}) {{
      pageInfo {{ endCursor hasNextPage }}
      nodes {{ id {_ITEM_FIELDS}
      }}
    }}
  }}
}}"""


def build_prs_query(repo: str, cursor: str | None) -> str:
    owner, name = _owner_name(repo)
    after = f', after: "{cursor}"' if cursor else ""
    return f"""query {{
  repository(owner: "{owner}", name: "{name}") {{
    nameWithOwner
    pullRequests(first: {_PAGE_PRS}{after}, states: [OPEN, CLOSED, MERGED],
                 orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ endCursor hasNextPage }}
      nodes {{{_ITEM_FIELDS}
        closingIssuesReferences(first: 10) {{ nodes {{ number }} }}
      }}
    }}
  }}
}}"""


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_page(payload: dict, repo: str, kind: str) -> tuple[list[WorkItemView], str | None, bool]:
    field = "issues" if kind == "issue" else "pullRequests"
    try:
        conn = (payload.get("data") or {}).get("repository", {}).get(field) or {}
    except AttributeError:
        return [], None, False
    # conn must be a dict; if it's any other truthy type, bail
    if not isinstance(conn, dict):
        return [], None, False
    info = conn.get("pageInfo") or {}
    items: list[WorkItemView] = []
    for node in conn.get("nodes") or []:
        # node must be a dict; skip non-dict nodes
        if not isinstance(node, dict):
            continue
        if node.get("number") is None:
            continue
        # Try to convert number to int; skip if it fails
        try:
            number = int(node["number"])
        except (ValueError, TypeError):
            continue
        refs = tuple(
            n["number"]
            for n in ((node.get("closingIssuesReferences") or {}).get("nodes") or [])
            if isinstance(n, dict) and n.get("number") is not None
        ) if kind == "pr" else ()
        items.append(WorkItemView(
            repo=repo,
            number=number,
            kind=kind,
            title=str(node.get("title") or ""),
            body=str(node.get("body") or ""),
            state=str(node.get("state") or "").lower(),
            labels=tuple(
                lbl["name"] for lbl in ((node.get("labels") or {}).get("nodes") or [])
                if isinstance(lbl, dict) and lbl.get("name")),
            author=str(((node.get("author") or {}).get("login")) or ""),
            url=str(node.get("url") or ""),
            gh_created_at=_parse_dt(node.get("createdAt")),
            gh_updated_at=_parse_dt(node.get("updatedAt")),
            gh_closed_at=_parse_dt(node.get("closedAt")),
            linked_issue_numbers=refs,
            node_id=node.get("id") if isinstance(node.get("id"), str) else None,
        ))
    return items, info.get("endCursor"), bool(info.get("hasNextPage"))
