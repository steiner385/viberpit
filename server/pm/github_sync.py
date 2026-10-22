"""GraphQL query builders + payload parsers for the pm-sync mirror (spec §4).

Pure functions: the loop owns pagination/watermark policy and the gh_run seam
owns transport. Issues support server-side ``filterBy: {since:}``; pull
requests do NOT, so PR queries are newest-first by UPDATED_AT and the caller
stops paging once nodes fall behind the watermark.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from viberpit import brand

_PAGE_ISSUES = 100
_PAGE_PRS = 50

_ITEM_FIELDS = """
      number title body state url createdAt updatedAt closedAt
      author { __typename login ... on User { databaseId } }
      labels(first: 20) { nodes { name } }
      assignees(first: 10) { nodes { login } }"""

# Issues only (#2683): the newest comments and the newest reopen, so the mirror
# knows when a PERSON last commented and when the issue was last reopened — the
# "material changes" that re-arm an issue a session found needed no PR. A comment by
# a Bot/App, or one carrying ``AUTOMATION_COMMENT_MARKER`` (ViberPit's own posts,
# which may go out under the operator's USER token), never counts. The bodies are
# fetched only to look for the marker; ten comments is enough, since a re-arm only
# cares whether a person commented after the verdict's dispatch started.
_ISSUE_COMMENTS = """
      comments(last: 10) { nodes { createdAt body author { __typename login } } }
      timelineItems(itemTypes: [REOPENED_EVENT], last: 1) {
        nodes { ... on ReopenedEvent { createdAt } } }"""

#: Appended (as an invisible HTML comment) to every issue comment ViberPit posts —
#: ``backlog_github._issue_comment`` and ``ci_issues.comment_on_issue``, the two
#: choke points — so the mirror never mistakes ViberPit's own words for a person's
#: (#2683 review). Quoted log text cannot forge it: ``issue_filing.neutralize_markers``.
AUTOMATION_COMMENT_MARKER = f"<!-- {brand.SLUG}:automation -->"


def with_automation_marker(body: str) -> str:
    """*body* with ``AUTOMATION_COMMENT_MARKER`` appended, exactly once."""
    if AUTOMATION_COMMENT_MARKER in body:
        return body
    return f"{body.rstrip()}\n\n{AUTOMATION_COMMENT_MARKER}"


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
    # GitHub assignee logins; an assigned item is never queue-eligible (spec 003 §7a, C-5).
    assignees: tuple[str, ...] = ()
    # Newest comment by a User among the issue's recent comments (#2683); None for a
    # PR, or an issue with none.
    last_human_comment_at: datetime | None = None
    # The issue's newest reopen (#2683 review); None for a PR or a never-reopened issue.
    last_reopened_at: datetime | None = None
    # Immutable GitHub databaseId of a User author (#3147); None for Bots/ghosts. A login
    # is mutable, so the trusted-author gate keys on this.
    author_id: int | None = None


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
      nodes {{ id {_ITEM_FIELDS}{_ISSUE_COMMENTS}
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


def _author_id(author) -> int | None:
    """The immutable GitHub user id, only for a ``User`` author with an int
    ``databaseId`` (``bool`` is not one); Bots, ghosts and anything malformed -> None."""
    if not isinstance(author, dict) or author.get("__typename") != "User":
        return None
    db_id = author.get("databaseId")
    return db_id if isinstance(db_id, int) and not isinstance(db_id, bool) else None


def _author(author) -> str:
    """The stored author string. GitHub's Bot and User logins share a namespace (the
    Bot ``viberpit`` and a user who registers ``viberpit`` both read ``viberpit``), so
    Bot-ness is recorded in the string as ``app/<slug>`` — the trusted-author gate
    (``queue.is_trusted_author``) must not be squattable by a same-named user."""
    if not isinstance(author, dict):
        return ""
    login = str(author.get("login") or "")
    if login and author.get("__typename") == "Bot":
        return f"app/{login}"
    return login


def _last_human_comment_at(node: dict) -> datetime | None:
    """The newest ``createdAt`` among *node*'s comments whose author is a User and
    whose body does not carry ``AUTOMATION_COMMENT_MARKER`` — a Bot author
    (``__typename == "Bot"``), a deleted one (no author) or ViberPit's own marked
    post never counts."""
    newest: datetime | None = None
    for c in ((node.get("comments") or {}).get("nodes") or []):
        if not isinstance(c, dict):
            continue
        author = c.get("author")
        if not isinstance(author, dict) or author.get("__typename") != "User":
            continue
        if AUTOMATION_COMMENT_MARKER in str(c.get("body") or ""):
            continue
        at = _parse_dt(c.get("createdAt"))
        if at is not None and (newest is None or at > newest):
            newest = at
    return newest


def _last_reopened_at(node: dict) -> datetime | None:
    """The newest ReopenedEvent's ``createdAt`` (the query asks for the last one)."""
    newest: datetime | None = None
    for ev in ((node.get("timelineItems") or {}).get("nodes") or []):
        at = _parse_dt(ev.get("createdAt")) if isinstance(ev, dict) else None
        if at is not None and (newest is None or at > newest):
            newest = at
    return newest


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
            author=_author(node.get("author")),
            author_id=_author_id(node.get("author")),
            url=str(node.get("url") or ""),
            gh_created_at=_parse_dt(node.get("createdAt")),
            gh_updated_at=_parse_dt(node.get("updatedAt")),
            gh_closed_at=_parse_dt(node.get("closedAt")),
            linked_issue_numbers=refs,
            node_id=node.get("id") if isinstance(node.get("id"), str) else None,
            assignees=tuple(
                a["login"] for a in ((node.get("assignees") or {}).get("nodes") or [])
                if isinstance(a, dict) and a.get("login")),
            last_human_comment_at=_last_human_comment_at(node) if kind == "issue" else None,
            last_reopened_at=_last_reopened_at(node) if kind == "issue" else None,
        ))
    return items, info.get("endCursor"), bool(info.get("hasNextPage"))
