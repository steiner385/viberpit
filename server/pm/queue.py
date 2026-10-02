"""Global priority queue over the 003 mirror (spec §7a). Pure core; the loader lives in
``queue_store.py`` (Task 3). The runner, the grouper, the status card and ``GET /api/pm/queue``
all rank through ``rank`` — one definition of "what agents will do next".

Decision C-2: a row whose board Status is unset or outside the spec vocabulary
(``STATUS_OPTIONS``) is treated as Backlog-equivalent rather than excluded — see
``skip_reason`` for the mechanics and rationale."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .desired_state import (HELD_KINDS, STATUS_BACKLOG, STATUS_BLOCKED,
                            STATUS_OPTIONS, STATUS_READY)
from .outcomes import bounced_out

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

# Labels that mark work the queue must serve BEFORE ordinary backlog, whatever
# its score or age (#16150, #16151). An automation that reports a broken
# environment files an issue that is, by construction, unscored (no human has
# groomed it) and brand new — which under a score-then-oldest ordering is the
# worst position in the queue, not the best. Two such issues reported both admin
# environments down and sat at 328th and 329th of 332 while the environments
# stayed broken, behind three-day-old test flakes.
#
# Lowercase; compared case-insensitively like SKIP_ISSUE_LABELS. The label is
# OWNED by the automation that files the issue
# (``server.automations.deploy_monitor.LABEL``), but importing an automation into
# this pure ranking core would invert the layering — so the coupling is pinned by
# ``test_urgent_labels_pin_the_deploy_monitor_label`` instead, exactly as
# MIN_STATUS_OPTIONS pins its coupling to STATUS_OPTIONS below.
URGENT_LABELS = frozenset({"deploy-monitor"})

SKIP_REASONS = ("status", "blocked", "live_session", "claimed", "assigned", "epic_open", "bot",
                 "held", "needs_spec")

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
    # Spec 2026-09-18 §3: the hold on the item. ``owner_action`` / ``decision`` park
    # the row (``skip_reason`` → "held"); ``evidence`` / ``stronger_model`` ride
    # through to the entry so the spawn can pick its prompt and rung.
    hold_kind: str | None = None
    # D8: how many FINALISED (non-``running``/``merged``) dispatches this exact
    # body (by ``body_fingerprint``) has already had — the loader
    # (``queue_store.load_queue_rows``) computes it, ``skip_reason`` compares it
    # against the product's ``give_up_after_bounces``. Defaulted 0 (never
    # bounced) so a caller with no outcome history keeps constructing rows
    # positionally.
    bounces: int = 0
    # Spec 2026-09-30 §3: the hold's provenance, threaded for the escalation read
    # model (``server/pm/escalation.py``) so it can say who parked the item, why
    # and for how long. Defaulted so every positional construction keeps working.
    hold_reason: str | None = None
    hold_by: str | None = None
    hold_set_at: datetime | None = None
    # The mirror row's id, so a held item the escalation read model lists can be
    # ACTED on (Retry / approve-escalation are per-work-item routes) without the
    # client resolving ``ref`` → id itself. Defaulted for positional constructors.
    work_item_id: str | None = None


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
    # Carried off the row purely so ``rank_key`` applies to an ENTRY too: a caller
    # that merges several ``rank`` results (the runner ranks one product at a time)
    # has to re-sort by the same key, and age is the tie-break. Defaulted so every
    # positional construction keeps working.
    gh_created_at: datetime | None = None
    # Same reason as ``gh_created_at``: urgency is decided from the ROW's labels,
    # which an entry does not carry, so the verdict rides along. Without it the
    # cross-product merge would re-sort by a key that can no longer see urgency
    # and quietly undo the preemption.
    urgent: bool = False
    # Carried off the row so a caller downstream of ranking (item 4's spawn) can
    # still pick the hold's prompt/rung without re-reading the row.
    hold_kind: str | None = None


def _is_bot(row: QueueRow, extra_skip_labels: frozenset[str] = frozenset()) -> bool:
    if row.author.endswith("[bot]"):
        return True
    if {lbl.strip().lower() for lbl in row.labels} & (SKIP_ISSUE_LABELS | extra_skip_labels):
        return True
    return row.title.strip().lower() in SKIP_ISSUE_TITLES


def is_urgent(row: "QueueRow | QueueEntry") -> bool:
    """Whether this item preempts ordinary backlog (see ``URGENT_LABELS``).

    Reads labels off a ``QueueRow`` and the carried verdict off a ``QueueEntry``,
    for the same reason ``rank_key`` accepts both: the runner ranks each product
    separately and re-sorts the merged ENTRIES, which have no labels.
    """
    labels = getattr(row, "labels", None)
    if labels is not None:
        return bool({str(lbl).strip().lower() for lbl in labels} & URGENT_LABELS)
    return bool(getattr(row, "urgent", False))


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
    now: datetime,
    give_up_after_bounces: int = 0,
    extra_skip_labels: frozenset[str] = frozenset(),
) -> str | None:
    """Decision C-2: a row whose board Status is ``None`` (never stamped by the
    reconciler) or outside the spec vocabulary (e.g. GitHub's default "Todo" column)
    is treated as Backlog-equivalent, not excluded — an unreconciled board would
    otherwise yield an empty queue. It is then gated by ``min_status`` exactly like a
    real Backlog row: eligible in Backlog mode, skipped as "status" in Ready mode.
    """
    min_rank = _min_status_rank(min_status)
    # Checked BEFORE the status gate: the reconciler stamps the held board Status
    # from exactly this hold, so a status-first check would report these rows as
    # plain "status" skips and the card would lose its held gauge.
    if row.hold_kind in HELD_KINDS:
        return "held"
    # D8: this body has already bounced enough — a spec edit (which changes the
    # fingerprint the count is keyed on) is what re-arms it, not time.
    if bounced_out(row.bounces, give_up_after_bounces):
        return "needs_spec"
    status = row.status if row.status in STATUS_OPTIONS else STATUS_BACKLOG
    if status not in (STATUS_BACKLOG, STATUS_READY, STATUS_BLOCKED):
        return "status"  # Done / In progress / Held
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
    if _is_bot(row, extra_skip_labels):
        return "bot"
    return None


def rank_key(row: "QueueRow | QueueEntry") -> tuple:
    """THE queue ordering, as a sort key: urgent work first, then highest score
    (an unscored row last), then oldest first, then ref.

    Urgency LEADS because the other legs actively work against it: an incident
    filed by an automation is unscored and newest, so score-then-age sorts it to
    the very back of the queue at exactly the moment it matters most (#16150).

    Exported because ``rank`` is not always the last word on ordering: the Backlog
    Runner ranks each product separately (each has its own ``queue_min_status`` /
    ``respect_dependencies``) and then merges, and that merge must sort by exactly
    this key rather than a hand-copied tuple that can drift from it. Takes anything
    carrying ``score`` / ``gh_created_at`` / ``ref``, so it applies to a ``QueueRow``
    before ranking and a ``QueueEntry`` after it.

    A missing ``gh_created_at`` sorts last via its own boolean leg rather than a
    sentinel datetime, so the key never compares a naive datetime against an aware
    one (which raises).
    """
    created = row.gh_created_at
    return (not is_urgent(row), row.score is None, -(row.score or 0.0), created is None,
            created or datetime.min, row.ref)


def rank(
    rows: Iterable[QueueRow],
    *,
    min_status: str = STATUS_READY,
    respect_dependencies: bool = True,
    now: datetime,
    give_up_after_bounces: int = 0,
    extra_skip_labels: frozenset[str] = frozenset(),
) -> tuple[list[QueueEntry], dict[str, int]]:
    kept: list[QueueRow] = []
    skipped: dict[str, int] = {}
    for row in rows:
        why = skip_reason(
            row,
            min_status=min_status,
            respect_dependencies=respect_dependencies,
            now=now,
            give_up_after_bounces=give_up_after_bounces,
            extra_skip_labels=extra_skip_labels,
        )
        if why:
            skipped[why] = skipped.get(why, 0) + 1
        else:
            kept.append(row)
    kept.sort(key=rank_key)
    return [
        QueueEntry(r.ref, r.repo, r.number, r.product, r.title, r.body, r.url, r.score,
                   r.gh_created_at, is_urgent(r), hold_kind=r.hold_kind)
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
        # The grouper re-orders this pool into the plan the runner pops, so the
        # urgency verdict has to cross that hand-off as data — a candidate dict is
        # all it receives.
        "urgent": e.urgent,
        # Carried so the runner's spawn (item 4) can pick the hold's prompt/rung
        # without re-reading the row — same reason ``urgent`` rides along.
        "hold_kind": e.hold_kind,
    }
