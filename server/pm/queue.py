"""Global priority queue over the 003 mirror (spec §7a). Pure core; the loader lives in
``queue_store.py`` (Task 3). The runner, the grouper, the status card and ``GET /api/pm/queue``
all rank through ``rank`` — one definition of "what agents will do next".

Decision C-2: a row whose board Status is unset or outside the spec vocabulary
(``STATUS_OPTIONS``) is treated as Backlog-equivalent rather than excluded — see
``skip_reason`` for the mechanics and rationale."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .desired_state import STATUS_BACKLOG, STATUS_BLOCKED, STATUS_OPTIONS, STATUS_READY

IN_PROGRESS_LABEL = "in-progress"
NEEDS_HUMAN_LABEL = "needs-human"

# How long an escalated (``needs-human``) issue must sit untouched before the
# runner reconsiders it. Escalation itself bumps the issue's ``updatedAt``, so
# every give-up re-arms the cooldown — a permanently-failing issue is retried at
# most once per window, never in a tight loop. 0 disables retry entirely
# (needs-human becomes a permanent handoff again).
NEEDS_HUMAN_RETRY_DAYS_DEFAULT = 7

# Issues the runner must never treat as implementable work (#637). A backlog
# session writes code and opens a PR — but bot/app accounts open automated
# meta-tickets (Renovate's "Dependency Dashboard", Dependabot bumps) that have
# nothing to implement, so a spawned session just burns a slot. GitHub suffixes
# EVERY bot/app account login with ``[bot]`` (``renovate[bot]``,
# ``dependabot[bot]``, ``github-actions[bot]``, …), so the suffix is the
# canonical, future-proof author signal. Labels and title are defense-in-depth
# for the rare human-authored (or login-less) mirror of the same meta-ticket.
SKIP_ISSUE_LABELS = frozenset({"dependencies", "renovate"})
SKIP_ISSUE_TITLES = frozenset({"dependency dashboard"})

SKIP_REASONS = ("status", "blocked", "live_session", "claimed", "assigned", "epic_open", "bot",
                 "needs_human_cooldown")

# The only two ``min_status`` values the queue accepts today (C1 will feed this from
# stored runner config). Explicit rather than derived from a slice of STATUS_OPTIONS
# at call time — see MIN_STATUS_OPTIONS below for the pinned coupling.
_MIN_STATUS_RANK = {STATUS_BACKLOG: 0, STATUS_READY: 1}

# The board-display order (STATUS_OPTIONS, which gh_reconcile._ensure_fields also uses
# to seed the Status field's options) happens to put Backlog and Ready first. This
# constant pins that coupling explicitly rather than depending on it silently — see
# test_min_status_options_are_the_first_two_board_statuses.
MIN_STATUS_OPTIONS = (STATUS_BACKLOG, STATUS_READY)


@dataclass(frozen=True)
class QueueRow:
    """One mirrored board item + the facts the ranking rules need."""

    ref: str
    repo: str
    number: int
    product: str
    title: str
    body: str
    url: str
    status: str | None
    score: float | None
    gh_created_at: datetime | None
    gh_updated_at: datetime | None
    assignees: tuple[str, ...]
    author: str
    labels: tuple[str, ...]
    open_children: int
    open_blockers: tuple[str, ...]
    live_session: bool
    claimed: bool


@dataclass(frozen=True)
class QueueEntry:
    ref: str
    repo: str
    number: int
    product: str
    title: str
    body: str
    url: str
    score: float | None


def _is_bot(row: QueueRow) -> bool:
    if row.author.endswith("[bot]"):
        return True
    if {lbl.strip().lower() for lbl in row.labels} & SKIP_ISSUE_LABELS:
        return True
    return row.title.strip().lower() in SKIP_ISSUE_TITLES


def _min_status_rank(min_status: str) -> int:
    try:
        return _MIN_STATUS_RANK[min_status]
    except KeyError:
        raise ValueError(
            f"min_status must be one of {MIN_STATUS_OPTIONS}, got {min_status!r}") from None


def skip_reason(
    row: QueueRow,
    *,
    min_status: str,
    respect_dependencies: bool,
    needs_human_retry_days: int,
    now: datetime,
) -> str | None:
    """Decision C-2: a row whose board Status is ``None`` (never stamped by the
    reconciler) or outside the spec vocabulary (e.g. GitHub's default "Todo" column)
    is treated as Backlog-equivalent, not excluded — an unreconciled board would
    otherwise yield an empty queue. It is then gated by ``min_status`` exactly like a
    real Backlog row: eligible in Backlog mode, skipped as "status" in Ready mode.
    """
    min_rank = _min_status_rank(min_status)
    status = row.status if row.status in STATUS_OPTIONS else STATUS_BACKLOG
    if status not in (STATUS_BACKLOG, STATUS_READY, STATUS_BLOCKED):
        return "status"  # Done / In progress / Needs human
    if status == STATUS_BLOCKED or (respect_dependencies and row.open_blockers):
        return "blocked"
    # status is now guaranteed Backlog or Ready (Blocked returned above).
    if _MIN_STATUS_RANK[status] < min_rank:
        return "status"
    if row.live_session:
        return "live_session"
    if row.claimed:
        return "claimed"
    if row.assignees:
        return "assigned"
    if row.open_children:
        return "epic_open"
    if _is_bot(row):
        return "bot"
    if NEEDS_HUMAN_LABEL in row.labels:
        if (
            needs_human_retry_days <= 0
            or row.gh_updated_at is None
            or now - row.gh_updated_at < timedelta(days=needs_human_retry_days)
        ):
            return "needs_human_cooldown"
    return None


def rank(
    rows: Iterable[QueueRow],
    *,
    min_status: str = STATUS_READY,
    respect_dependencies: bool = True,
    needs_human_retry_days: int = NEEDS_HUMAN_RETRY_DAYS_DEFAULT,
    now: datetime,
) -> tuple[list[QueueEntry], dict[str, int]]:
    kept: list[QueueRow] = []
    skipped: dict[str, int] = {}
    for row in rows:
        why = skip_reason(
            row,
            min_status=min_status,
            respect_dependencies=respect_dependencies,
            needs_human_retry_days=needs_human_retry_days,
            now=now,
        )
        if why:
            skipped[why] = skipped.get(why, 0) + 1
        else:
            kept.append(row)
    far_future = datetime.max.replace(tzinfo=now.tzinfo)
    kept.sort(key=lambda r: (r.score is None, -(r.score or 0.0), r.gh_created_at or far_future, r.ref))
    return [
        QueueEntry(r.ref, r.repo, r.number, r.product, r.title, r.body, r.url, r.score)
        for r in kept
    ], skipped


def to_candidate(e: QueueEntry) -> dict:
    """The runner/grouper candidate shape."""
    return {
        "ref": e.ref,
        "number": e.number,
        "repo": e.repo,
        "title": e.title,
        "body": e.body,
        "url": e.url,
        "score": e.score,
        "product": e.product,
    }
