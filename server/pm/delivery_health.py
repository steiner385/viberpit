"""Production delivery health (#3404): is the product's production running a
verified, current SHA, and if not, is anyone on it?

KinDash prod sat on a stale SHA with automatic promotion blocked for nine days
(2026-11-08 → 11-17) while every signal was an auto-managed, self-closing GitHub
issue the Backlog Runner rightly skips. This module turns those signals into ONE
owner-visible state, computed each milestone refresh
(``server/pm/delivery_health_refresh.py`` gathers the facts):

* the SHA prod's health endpoint reports, and how far it is behind the default
  branch (commits, and hours since the oldest commit prod does not have);
* whether a green run of one of the product's gate workflows
  (``delivery_verified_workflows`` in its policy — e.g. the scheduled Nightly or
  ``deploy-reattempt``) ran on that SHA;
* the first gate workflow's scheduled green streak;
* the open ``ci:regressed`` issues, each with whether a live session or an open PR
  is on it.

It **escalates** (an owner ask on the ladder, which ``roadmap_needs_you``, the map
badge, the Milestones page and the owner digest all read, plus one push) when
either holds:

* promotion has been blocked longer than ``BLOCKED_AFTER`` (prod is missing a main
  commit that old) and no regressed issue has a live session or PR;
* prod runs a SHA no gate workflow verified.

When neither holds — the next promotion landed on a verified main, or someone took
the blocker — the ask clears itself. Pure: no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.pm.queue import REGRESSED_LABELS
from server.time_iso import aware_utc, iso_utc

#: The ``MilestoneMeasurement`` row the readout is stored on. ``@`` keeps it clear of
#: every milestone key (a slug) and of ``numbers`` / ``leaves:<key>``.
SCOPE = "@delivery"
KEY = "health"
#: How long prod may miss a main commit before promotion counts as blocked.
BLOCKED_AFTER = timedelta(hours=48)
#: The ``ci:regressed`` label (``queue.REGRESSED_LABELS``, #3402): a deterministic
#: CI break that keeps the Nightly red and so blocks promotion.
(REGRESSED_LABEL,) = tuple(REGRESSED_LABELS)
#: ``MilestoneNote.created_by`` / ``resolved_by`` of the asks this signal writes.
ASK_AUTHOR = "system:delivery-health"
#: ``MilestoneNote.ref`` prefix: one ask per prod SHA (an episode), so a person who
#: dropped it is not asked again until the next promotion.
ASK_REF_PREFIX = "delivery:"

STATE_HEALTHY = "healthy"
STATE_BEHIND = "behind"
STATE_BLOCKED = "blocked"
STATE_UNVERIFIED = "unverified"
STATE_UNKNOWN = "unknown"
STATES: tuple[str, ...] = (STATE_HEALTHY, STATE_BEHIND, STATE_BLOCKED, STATE_UNVERIFIED,
                           STATE_UNKNOWN)

_SHORT = 7


def short(sha: str | None) -> str:
    return (sha or "")[:_SHORT]


@dataclass(frozen=True)
class RegressedIssue:
    ref: str
    title: str
    url: str
    #: A live ViberPit session was dispatched on it.
    session: bool = False
    #: Open PRs that would close it (``owner/repo#n``).
    prs: tuple[str, ...] = ()

    @property
    def owned(self) -> bool:
        return self.session or bool(self.prs)

    def to_wire(self) -> dict[str, Any]:
        return {"ref": self.ref, "title": self.title, "url": self.url,
                "session": self.session, "prs": list(self.prs), "owned": self.owned}


@dataclass(frozen=True)
class DeliveryFacts:
    repo: str
    branch: str
    #: The SHA prod's health endpoint reports; ``None`` when unreachable.
    prod_sha: str | None
    #: Commits on *branch* that prod does not have; ``None`` when the compare failed.
    behind_commits: int | None = None
    #: Committer time of the oldest of those commits.
    oldest_unshipped_at: datetime | None = None
    #: SHA -> run URL of a green gate-workflow run on it.
    verified: Mapping[str, str] = field(default_factory=dict)
    gate_workflows: tuple[str, ...] = ()
    #: The first gate workflow's scheduled green streak (``None``: not read).
    streak: int | None = None
    streak_url: str | None = None
    #: Open regressed issues; ``None`` when the read failed.
    regressed: tuple[RegressedIssue, ...] | None = ()
    errors: tuple[str, ...] = ()

    @property
    def compare_url(self) -> str | None:
        if not self.prod_sha:
            return None
        return f"https://github.com/{self.repo}/compare/{self.prod_sha}...{self.branch}"


@dataclass(frozen=True)
class Verdict:
    state: str
    hours_behind: float | None
    #: Whether a gate workflow verified prod's SHA (``None``: no gate workflows set,
    #: or prod unreachable).
    verified: bool | None
    escalate: bool
    #: ``blocked`` and/or ``unverified``: which escalation rules hold.
    reasons: tuple[str, ...]
    #: The unowned regressed issues a ``blocked`` escalation names.
    blockers: tuple[RegressedIssue, ...] = ()


def evaluate(facts: DeliveryFacts, now: datetime) -> Verdict:
    """THE delivery-health rule (module docstring). A fact that could not be read
    never escalates on its own: an unreachable prod is ``unknown``, a failed compare
    leaves the blocked rule unjudged, and a failed regressed-issue read cannot prove
    nobody is on it."""
    if not facts.prod_sha:
        return Verdict(STATE_UNKNOWN, None, None, False, ())
    at = aware_utc(now)
    assert at is not None
    hours: float | None = None
    if facts.behind_commits == 0:
        hours = 0.0
    elif facts.behind_commits and facts.oldest_unshipped_at is not None:
        oldest = aware_utc(facts.oldest_unshipped_at)
        assert oldest is not None
        hours = max(0.0, (at - oldest).total_seconds() / 3600)
    verified = (facts.prod_sha in facts.verified) if facts.gate_workflows else None
    blocked = hours is not None and hours > BLOCKED_AFTER.total_seconds() / 3600
    reasons: list[str] = []
    blockers: tuple[RegressedIssue, ...] = ()
    if blocked and facts.regressed is not None and not any(r.owned for r in facts.regressed):
        reasons.append(STATE_BLOCKED)
        blockers = facts.regressed
    if verified is False:
        reasons.append(STATE_UNVERIFIED)
    if verified is False:
        state = STATE_UNVERIFIED
    elif blocked:
        state = STATE_BLOCKED
    elif facts.behind_commits:
        state = STATE_BEHIND
    else:
        state = STATE_HEALTHY
    return Verdict(state, hours, verified, bool(reasons), tuple(reasons), blockers)


def episode_ref(facts: DeliveryFacts) -> str:
    """The ask's ``ref``: one episode per prod SHA."""
    return f"{ASK_REF_PREFIX}{short(facts.prod_sha)}"


def _hours(h: float | None) -> str:
    if h is None:
        return "?h"
    return f"{h / 24:.1f}d" if h >= 72 else f"{h:.0f}h"


def _workflows(names: tuple[str, ...]) -> str:
    if not names:
        return "gate workflow"
    return " or ".join(names)


def ask_text(facts: DeliveryFacts, verdict: Verdict) -> str:
    """The owner ask: the blocker first (the summary line clips at 60 characters),
    then what prod is running and why it matters."""
    parts: list[str] = []
    prod = short(facts.prod_sha)
    if STATE_BLOCKED in verdict.reasons:
        behind = (f"prod {prod} is {facts.behind_commits} commit(s) / "
                  f"{_hours(verdict.hours_behind)} behind {facts.branch}")
        if verdict.blockers:
            refs = ", ".join(b.ref for b in verdict.blockers)
            parts.append(f"Prod delivery blocked by {refs} ({REGRESSED_LABEL}, no session or "
                         f"PR): {behind}. Dispatch or fix it so promotion can resume.")
        else:
            parts.append(f"Prod delivery blocked: {behind} and no {REGRESSED_LABEL} issue "
                         f"explains it — check the {_workflows(facts.gate_workflows)} runs.")
    if STATE_UNVERIFIED in verdict.reasons:
        parts.append(f"Prod runs {prod}, which no green {_workflows(facts.gate_workflows)} "
                     "run verified.")
    return " ".join(parts)


def ask_url(facts: DeliveryFacts, verdict: Verdict) -> str | None:
    """Where the ask links: the first blocker, else the prod...main compare."""
    if verdict.blockers:
        return verdict.blockers[0].url
    return facts.compare_url


def to_wire(facts: DeliveryFacts, verdict: Verdict, *, notified: str | None = None) -> dict:
    """The stored readout (``MilestoneMeasurement.value``) the Milestones view serves.
    *notified* is the episode a push already went out for (the push dedupe)."""
    return {
        "state": verdict.state,
        "repo": facts.repo,
        "branch": facts.branch,
        "prodSha": facts.prod_sha,
        "behindCommits": facts.behind_commits,
        "hoursBehind": (round(verdict.hours_behind, 1)
                        if verdict.hours_behind is not None else None),
        "oldestUnshippedAt": iso_utc(facts.oldest_unshipped_at),
        "verified": verdict.verified,
        "verifiedBy": facts.verified.get(facts.prod_sha or "") if facts.prod_sha else None,
        "gateWorkflows": list(facts.gate_workflows),
        "streak": facts.streak,
        "streakUrl": facts.streak_url,
        "regressed": ([r.to_wire() for r in facts.regressed]
                      if facts.regressed is not None else None),
        "escalated": verdict.escalate,
        "reasons": list(verdict.reasons),
        "blockers": [b.ref for b in verdict.blockers],
        "compareUrl": facts.compare_url,
        "errors": list(facts.errors),
        "notified": notified,
    }


def summary_line(value: Mapping[str, Any] | None) -> str | None:
    """One line for the session tools' milestones summary."""
    if not value:
        return None
    state = value.get("state") or STATE_UNKNOWN
    if state == STATE_UNKNOWN:
        return "Prod delivery: unknown (health endpoint unreachable)"
    line = f"Prod delivery: {state} — prod {short(value.get('prodSha'))}"
    behind = value.get("behindCommits")
    if behind:
        line += f", {behind} commit(s) / {_hours(value.get('hoursBehind'))} behind"
    if value.get("verified") is not None:
        line += ", verified" if value.get("verified") else ", unverified"
    if value.get("streak") is not None:
        line += f", nightly streak {value['streak']}"
    if value.get("blockers"):
        line += f"; blocked by {', '.join(value['blockers'])}"
    return line
