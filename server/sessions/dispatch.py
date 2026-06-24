"""Work-dispatcher: pick the top open GitHub issue for an idle/done session.

Pure functions (pick_top_issue, issue_proposal_text, fetch_open_issues) with an
injectable gh runner so tests never touch the real binary.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from ccswitch.gitrepos import _run_gh

log = logging.getLogger(__name__)

# Labels that indicate a high-priority issue (lowercased match).
PRIORITY_LABELS: frozenset[str] = frozenset(
    {"priority", "p0", "p1", "high-priority", "priority:high"}
)


def pick_top_issue(issues: list[dict]) -> dict | None:
    """Pick the best open issue from a list.

    Sort key: issues whose labels intersect PRIORITY_LABELS first, then most
    recent updatedAt descending, then lowest number as a final tie-breaker.
    Returns None for an empty list.
    """
    if not issues:
        return None

    best = issues[0]
    for issue in issues[1:]:
        best = _better_issue(best, issue)
    return best


def _has_priority(issue: dict) -> bool:
    return bool(set(issue.get("labels") or []) & PRIORITY_LABELS)


def _better_issue(a: dict, b: dict) -> dict:
    """Return whichever of a/b ranks higher (priority > recency > lower number)."""
    a_pri = _has_priority(a)
    b_pri = _has_priority(b)
    if a_pri != b_pri:
        return a if a_pri else b
    # Same priority tier: more recent updatedAt wins.
    a_updated = a.get("updatedAt") or ""
    b_updated = b.get("updatedAt") or ""
    if a_updated != b_updated:
        return a if a_updated > b_updated else b
    # Tie-break on lowest number.
    a_num = a.get("number") or 0
    b_num = b.get("number") or 0
    return a if a_num <= b_num else b


def issue_proposal_text(issue: dict) -> str:
    """Return a chip-ready proposal string for a single issue."""
    return f"Work on issue #{issue['number']}: {issue['title']}"


def broken_pr_proposal_text(number: int, title: str) -> str:
    """Return a chip-ready proposal string for a session's PR that is failing CI.

    Higher priority than an issue proposal: an idle/done session whose own PR
    broke should fix it before starting new backlog work (see _dispatch_proposal).
    """
    return f"Fix the failing CI on PR #{number}: {title}"


def review_proposal_text(number: int, title: str, count: int) -> str:
    """Return a chip-ready proposal string for unresolved review comments on a PR.

    Higher priority than an issue proposal but lower than a failing-CI proposal:
    an idle/done session whose PR has unresolved review threads should address them
    before picking up new backlog work (see _dispatch_proposal).
    """
    plural = "s" if count != 1 else ""
    return f"Address {count} review comment{plural} on PR #{number}: {title}"


def fetch_unresolved_review_count(
    repo: str,
    number: int,
    *,
    run_gh: Callable = _run_gh,
) -> int:
    """Fetch the count of unresolved review threads for a PR via GraphQL.

    Returns 0 on any error (non-zero exit code, bad JSON, exception). Never raises.
    Splits *repo* ("owner/name") into owner and name for the GraphQL variables.
    """
    parts = repo.split("/", 1)
    if len(parts) != 2:
        log.warning("fetch_unresolved_review_count: bad repo format: %s", repo)
        return 0
    owner, name = parts
    query = (
        "query($owner:String!,$name:String!,$num:Int!)"
        "{repository(owner:$owner,name:$name)"
        "{pullRequest(number:$num){reviewThreads(first:100){nodes{isResolved}}}}}"
    )
    args = [
        "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "-F", f"num={number}",
    ]
    try:
        result = run_gh(args)
    except Exception:
        log.warning("fetch_unresolved_review_count: gh subprocess failed for %s#%d", repo, number, exc_info=True)
        return 0

    if result.returncode != 0:
        log.warning("fetch_unresolved_review_count: gh exited %d for %s#%d", result.returncode, repo, number)
        return 0

    try:
        data = json.loads(result.stdout)
        nodes = (
            data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        )
        return sum(1 for node in nodes if not node["isResolved"])
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        log.warning("fetch_unresolved_review_count: bad JSON/structure for %s#%d", repo, number, exc_info=True)
        return 0


def fetch_open_issues(
    repo: str,
    *,
    run_gh: Callable = _run_gh,
) -> list[dict]:
    """Fetch the top 30 open issues from GitHub for *repo* (``owner/name``).

    Normalizes each issue's ``labels`` field from ``[{"name": ...}]`` objects
    (as returned by ``gh``) to a list of lowercased name strings.

    Returns ``[]`` on any error (non-zero exit code, missing ``gh``, bad JSON).
    Never raises.
    """
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,labels,updatedAt",
        "--limit", "30",
    ]
    try:
        result = run_gh(args)
    except Exception:
        log.warning("fetch_open_issues: gh subprocess failed for repo=%s", repo, exc_info=True)
        return []

    if result.returncode != 0:
        log.warning("fetch_open_issues: gh exited %d for repo=%s: %s", result.returncode, repo, result.stderr)
        return []

    try:
        raw: list[dict] = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        log.warning("fetch_open_issues: bad JSON from gh for repo=%s", repo, exc_info=True)
        return []

    # Normalize labels: gh returns [{"name": "Bug"}, ...] → ["bug", ...]
    normalized: list[dict] = []
    for issue in raw:
        labels_raw = issue.get("labels") or []
        if labels_raw and isinstance(labels_raw[0], dict):
            labels = [lbl["name"].lower() for lbl in labels_raw if isinstance(lbl.get("name"), str)]
        else:
            # Already strings (from tests or alternate gh output)
            labels = [str(lbl).lower() for lbl in labels_raw]
        normalized.append({**issue, "labels": labels})

    return normalized
