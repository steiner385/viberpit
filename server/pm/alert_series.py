"""Recurring alert issues as ONE rolling issue per type (#2680).

A CI workflow that files an issue per occurrence ("⚠️ Unverified prod deploy
<sha> — <date>", "Weekly failed — <date>") leaves a pile of open issues where only
the newest matters: older deploys are superseded, an older weekly run is history.
Each one became its own runner claim and, eventually, its own hold.

This module is the pure half: which open issues are alerts of the same type, and
which of them the newest one supersedes. The orchestrator's backlog tick closes
the superseded ones on GitHub with a comment naming the survivor
(``SessionOrchestrator._backlog_supersede_alerts``), so the queue and the hold
list are left with at most one open issue per alert type per repo.

An issue is an ALERT only when an automation filed it AND its title ends in an
ISO date — the per-occurrence suffix those workflows stamp. Both, because a
person's "Sprint review — 2026-10-01" also ends in a date and is not a series.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Logins GitHub's GraphQL API returns WITHOUT the ``[bot]`` suffix REST adds
#: (``author { login }`` on an Actions-filed issue is ``github-actions``).
AUTOMATION_LOGINS: frozenset[str] = frozenset({"github-actions"})

#: Most superseded issues one pass closes — the one-time sweep of an existing pile
#: spreads over a few ticks instead of a burst of GitHub writes.
SUPERSEDE_PER_PASS = 25

_DATE_SUFFIX_RE = re.compile(r"[\s\-–—:|,]*\b\d{4}-\d{2}-\d{2}\s*$")
# A commit sha: 7–40 hex chars with at least one digit, so an all-letter word
# ("defaced") is never mistaken for one.
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_NUMBER_RE = re.compile(r"\b\d+\b")
_SPACE_RE = re.compile(r"\s+")


def is_automation_login(login: str | None) -> bool:
    # The mirror stores a Bot as ``app/<slug>`` (``github_sync._author``).
    login = (login or "").strip().lower().removeprefix("app/")
    return login.endswith("[bot]") or login in AUTOMATION_LOGINS


def alert_key(title: str | None, author: str | None) -> str | None:
    """The alert TYPE of an issue — its title with the per-occurrence parts (the
    trailing date, a commit sha, counts) normalised away — or ``None`` when the
    issue is not a recurring alert at all."""
    if not is_automation_login(author):
        return None
    text = (title or "").strip().lower()
    stem = _DATE_SUFFIX_RE.sub("", text)
    if stem == text or not stem:
        return None
    stem = _SHA_RE.sub("<sha>", stem)
    stem = _NUMBER_RE.sub("<n>", stem)
    return _SPACE_RE.sub(" ", stem).strip()


@dataclass(frozen=True)
class AlertIssue:
    repo: str
    number: int
    title: str
    author: str

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


def superseded(issues: Iterable[AlertIssue]) -> list[tuple[str, str]]:
    """``(older_ref, newest_ref)`` for every open alert a newer one of the same
    type in the same repo supersedes, oldest first. "Newest" is the highest issue
    number — GitHub numbers are monotonic per repo, and the date in the title is
    only day-resolution."""
    series: dict[tuple[str, str], list[AlertIssue]] = {}
    for issue in issues:
        key = alert_key(issue.title, issue.author)
        if key is not None:
            series.setdefault((issue.repo, key), []).append(issue)
    out: list[tuple[str, str]] = []
    for members in series.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: i.number)
        newest = members[-1].ref
        out.extend((m.ref, newest) for m in members[:-1])
    return sorted(out, key=lambda pair: (pair[0].split("#")[0], int(pair[0].split("#")[1])))
