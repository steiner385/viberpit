"""Session-scoped PR follow-up proposals and best-effort review fetching."""
from __future__ import annotations

import json
import logging
from typing import Callable

from viberpit.gitrepos import _run_gh

log = logging.getLogger(__name__)

def broken_pr_proposal_text(number: int, title: str) -> str:
    """Return a chip-ready proposal string for a session's PR that is failing CI.

    An idle/done session whose own PR broke should address its failing checks
    (see _dispatch_proposal).
    """
    return f"Fix the failing CI on PR #{number}: {title}"


def review_proposal_text(number: int, title: str, count: int) -> str:
    """Return a chip-ready proposal string for unresolved review comments on a PR.

    Lower priority than a failing-CI proposal: an idle/done session whose own PR
    has unresolved review threads should address them (see _dispatch_proposal).
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
