"""The one fingerprint→issue decision (error-triage spec §6.1).

Three automations file GitHub issues for deduplicated findings: the CI/CD
Doctor (#1433 migrates it here), the session-feedback filer (#1434 builds on
it) and error triage. They differ in their tables, not in the rule for "we
already filed this" — so the rule lives here once:

1. no linked issue            → file (if the daily cap allows)
2. linked and open            → comment, at most once per ``comment_every``
3. closed as not_planned      → skip; the caller marks the row muted
4. closed (completed/other)   → regress: a NEW issue linking the old; never reopen

``decide`` is pure; ``apply`` runs the gh side effect and never raises. Row
writes stay with the caller, which knows its own table.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ccswitch import gitrepos
from server.github import ci_issues

KINDS = ("file", "comment", "regress", "skip")


@dataclass(frozen=True)
class Linked:
    """What the caller knows about the issue already linked to a fingerprint."""
    number: int | None
    closed: bool = False
    closed_reason: str | None = None          # "completed" | "not_planned" | None
    last_commented_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    kind: str     # file | comment | regress | skip
    reason: str   # no_issue | open | closed | comment_throttled | daily_cap | muted_by_close | retry


@dataclass(frozen=True)
class Applied:
    issue_number: int | None   # the new issue, or the existing one that was commented on
    commented: bool


def decide(linked: Linked | None, *, cap_remaining: int, comment_every: timedelta,
           now: datetime) -> Decision:
    if linked is None or linked.number is None:
        return Decision("file", "no_issue") if cap_remaining > 0 else Decision("skip", "daily_cap")
    if not linked.closed:
        if linked.last_commented_at is None or now - linked.last_commented_at >= comment_every:
            return Decision("comment", "open")
        return Decision("skip", "comment_throttled")
    if linked.closed_reason == "not_planned":
        return Decision("skip", "muted_by_close")
    return Decision("regress", "closed") if cap_remaining > 0 else Decision("skip", "daily_cap")


async def apply(decision: Decision, *, repo: str, title: str, body: str,
                labels: Sequence[str], linked: Linked | None,
                run=gitrepos._run_gh) -> Applied:
    """Run the gh side effect for *decision*. Never raises; a failed create is
    ``Applied(None, False)`` so the caller can leave its row retryable."""
    existing = linked.number if linked is not None else None
    if decision.kind in ("file", "regress"):
        number = await asyncio.to_thread(
            ci_issues.create_issue, repo, title, body, labels=list(labels), run=run)
        return Applied(issue_number=number, commented=False)
    if decision.kind == "comment" and existing is not None:
        url = await asyncio.to_thread(ci_issues.comment_on_issue, repo, existing, body, run=run)
        return Applied(issue_number=existing, commented=url is not None)
    return Applied(issue_number=existing, commented=False)


def neutralize_markers(text: str) -> str:
    """Break HTML-comment delimiters so quoted log text cannot smuggle a
    ``<!-- viberpit:… -->`` marker (or hide content) into an issue body."""
    return text.replace("<!--", "<!​--").replace("-->", "--​>")
