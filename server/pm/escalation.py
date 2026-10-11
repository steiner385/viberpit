"""The escalation read model (spec 2026-09-30 §3): what is held, per product, what
each hold blocks, and whether the product's queue is starving.

Pure. Rows come from ``queue_store.load_queue_rows`` (the runner's own pool), the
eligibility rule is ``queue.skip_reason`` (never re-derived here), "parked" is
``desired_state.HELD_KINDS`` and the reverse dependency index is
``drift.reverse_blockers`` — one definition each (constitution VI). Two routes read
this module (``GET /api/pm/holds``, ``GET /api/pm/decisions?impact=true``) and the
decision-escalation automation ticks on it.

Scope: ``load_queue_rows`` scopes dependency edges to each product's own source
repos, so ``downstream`` counts only same-board dependents; a dependent on another
product's board is that product's row and is not counted.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from server.automations.product_policy import (is_on, skip_labels, skips_self_closing_trackers,
                                               trusted_authors)
from server.pm.desired_state import (HELD_KINDS, STATUS_BACKLOG, STATUS_BLOCKED,
                                     STATUS_HELD, STATUS_READY)
from server.pm.drift import DepEdgeView, reverse_blockers
from server.pm.outcomes import Escalation, escalation_for
from server.pm.queue import QueueRow, rank, skip_reason

#: How a parked item comes back (spec 2026-09-18 §6/§7), as the one line a card or a
#: digest shows. ``decision`` ends only by accepting its proposal; ``owner_action``
#: ends on any of the three human signals in ``holds.RELEASE_SIGNALS``.
RELEASED_BY: dict[str, str] = {
    "decision": "Accept an option in the Inbox",
    "owner_action": "Retry, a board move to Ready/Backlog, or an issue-body edit",
}
#: The owner_action line when the hold is an approval-gated hand-off
#: (``outcomes.escalation_for``): the fourth way out is approving the climb.
RELEASED_BY_ESCALATION = ("Retry, a board move to Ready/Backlog, an issue-body edit, "
                          "or approving the escalation to {tier}")


def released_by(hold_kind: str, escalation: Escalation | None) -> str:
    """The one "how it comes back" sentence for a hold — every reader of the
    escalation read model (card, digest, assistant) spells it through here."""
    if escalation is not None:
        return RELEASED_BY_ESCALATION.format(tier=escalation.needs_approval)
    return RELEASED_BY.get(hold_kind, "")

#: Spec §5 default: a product with this many or fewer dispatchable items while
#: anything is held is starving. The automation's config overrides it.
DEFAULT_STARVING_MAX = 1

#: Cap on the refs a HeldItem names; the counts are exact, the list is a sample.
DOWNSTREAM_REFS_CAP = 10


@dataclass(frozen=True)
class HeldItem:
    ref: str
    repo: str
    number: int
    product: str
    title: str
    url: str
    hold_kind: str
    hold_reason: str
    hold_by: str
    hold_set_at: datetime | None
    age_hours: float | None
    proposal_id: str | None
    options: tuple[dict, ...]
    recommended_key: str | None
    downstream: int
    downstream_dispatchable: int
    downstream_refs: tuple[str, ...]
    dispatchable_on_release: bool
    released_by: str
    # The mirror row to act on, and — for an owner_action hold the runner wrote at
    # an approval-clamped ladder top — the climb a person can approve. Defaulted so
    # positional constructions in older callers keep working.
    work_item_id: str | None = None
    escalation: Escalation | None = None


@dataclass(frozen=True)
class ProductEscalation:
    slug: str
    dispatchable: int
    skipped: dict[str, int]
    held: tuple[HeldItem, ...]
    starving: bool
    oldest_hold_hours: float | None


def reverse_index(rows: Iterable[QueueRow]) -> dict[str, set[str]]:
    """blocker ref → the refs on this board it blocks, via ``drift.reverse_blockers``
    over the rows' already-open ``open_blockers`` (the loader keeps only open,
    repo-scoped destinations, so every edge here is active and open)."""
    edges = [DepEdgeView(src_ref=r.ref, dst_ref=b, source="mirror", active=True,
                         dst_state="open") for r in rows for b in r.open_blockers]
    return reverse_blockers(edges)


def downstream(ref: str, reverse: Mapping[str, set[str]]) -> tuple[str, ...]:
    """Every ref transitively behind ``ref``, sorted; cycle-safe, ``ref`` excluded.
    Only same-board dependents are reachable: the loader scopes edges to the
    product's own source repos, so a dependent on another product's board is that
    product's row and is not counted."""
    seen: set[str] = set()
    todo = deque(reverse.get(ref, ()))
    while todo:
        cur = todo.popleft()
        if cur == ref or cur in seen:
            continue
        seen.add(cur)
        todo.extend(reverse.get(cur, ()))
    return tuple(sorted(seen))


def _hours(since: datetime | None, now: datetime) -> float | None:
    if since is None:
        return None
    return round((now - since).total_seconds() / 3600, 1)


def _simulated_status(row: QueueRow) -> str | None:
    """The board Status the reconciler WOULD stamp once the hold/blockers we are
    simulating away are gone (``desired_state.derive_status``): Ready when the
    item is classified — ``score`` is set exactly then — else Backlog. Any other
    status (Done, In progress, …) is left alone: we only undo the two stamps that
    come from the facts we strike."""
    if row.status in (STATUS_HELD, STATUS_BLOCKED):
        return STATUS_READY if row.score is not None else STATUS_BACKLOG
    return row.status


def held_items(rows: Iterable[QueueRow], proposals_by_ref: Mapping[str, Any], *,
               now: datetime, min_status: str, respect_dependencies: bool,
               give_up_after_bounces: int = 0,
               extra_skip_labels: frozenset[str] = frozenset(),
               trusted_authors: frozenset[str] = frozenset(),
               skip_trackers: bool = True) -> list[HeldItem]:
    """One ``HeldItem`` per parked row (``HELD_KINDS``), unranked.

    ``downstream_dispatchable`` re-runs ``skip_reason`` on each dependent with the
    whole chain (this ref plus everything behind it) struck from its blockers: it
    answers "if this chain shipped, what would the runner pick up?" — honest about
    the fact that releasing a hold frees the HELD item now and its dependents only
    once it ships. A dependent that is also assigned, claimed or held in its own
    right stays skipped and is not counted. With respect_dependencies=False the blockers were never gating, so a
    dependent's Blocked STAMP still skips it via ``skip_reason`` — the count then
    reflects the stamp, not the chain.
    """
    rows = list(rows)
    by_ref = {r.ref: r for r in rows}
    reverse = reverse_index(rows)

    def skipped(r: QueueRow) -> str | None:
        return skip_reason(r, min_status=min_status, respect_dependencies=respect_dependencies,
                           now=now, give_up_after_bounces=give_up_after_bounces,
                           extra_skip_labels=extra_skip_labels,
                           trusted_authors=trusted_authors, skip_trackers=skip_trackers)

    out: list[HeldItem] = []
    for row in rows:
        if row.hold_kind not in HELD_KINDS:
            continue
        chain_refs = downstream(row.ref, reverse)
        chain = set(chain_refs) | {row.ref}
        freed = 0
        for dep in chain_refs:
            dep_row = by_ref.get(dep)
            if dep_row is None:
                continue
            remaining = tuple(b for b in dep_row.open_blockers if b not in chain)
            # A blocker outside the chain keeps the stamp (and skip_reason says blocked).
            unblocked = replace(dep_row, open_blockers=remaining,
                                status=dep_row.status if remaining
                                else _simulated_status(dep_row))
            if skipped(unblocked) is None:
                freed += 1
        on_release = skipped(replace(row, hold_kind=None, status=_simulated_status(row))) is None
        proposal = proposals_by_ref.get(row.ref)
        escalation = escalation_for(row.hold_kind, row.hold_reason)
        out.append(HeldItem(
            ref=row.ref, repo=row.repo, number=row.number, product=row.product,
            title=row.title, url=row.url, hold_kind=str(row.hold_kind),
            hold_reason=row.hold_reason or "", hold_by=row.hold_by or "",
            hold_set_at=row.hold_set_at, age_hours=_hours(row.hold_set_at, now),
            proposal_id=getattr(proposal, "id", None),
            options=tuple(getattr(proposal, "options", None) or ()),
            recommended_key=getattr(proposal, "recommended_key", None),
            downstream=len(chain_refs), downstream_dispatchable=freed,
            downstream_refs=chain_refs[:DOWNSTREAM_REFS_CAP],
            dispatchable_on_release=on_release,
            released_by=released_by(str(row.hold_kind), escalation),
            work_item_id=row.work_item_id, escalation=escalation,
        ))
    return out


def rank_held(items: Iterable[HeldItem]) -> list[HeldItem]:
    """Loudest first: most downstream, then oldest hold (unknown age last), then ref."""
    return sorted(items, key=lambda i: (-i.downstream, i.age_hours is None,
                                        -(i.age_hours or 0.0), i.ref))


def product_escalation(slug: str, rows: Iterable[QueueRow], proposals_by_ref: Mapping[str, Any],
                       policy: dict, *, now: datetime, starving_max: int = DEFAULT_STARVING_MAX,
                       min_status: str | None = None,
                       respect_dependencies: bool | None = None) -> ProductEscalation:
    """The per-product summary. Reach (``min_status`` / ``respect_dependencies``)
    defaults to the product's own policy, as the runner reads it; a caller with a
    view of its own (``GET /api/pm/holds``'s query params) overrides both.
    ``starving`` is false for a product whose dispatch is off — it is not waiting
    on anyone."""
    rows = list(rows)
    ms = min_status if min_status is not None else str(policy["queue_min_status"])
    rd = (respect_dependencies if respect_dependencies is not None
          else bool(policy["respect_dependencies"]))
    bounces = int(policy.get("give_up_after_bounces", 0))
    labels = skip_labels(policy)
    trusted = trusted_authors(policy)
    trackers = skips_self_closing_trackers(policy)
    entries, skipped = rank(rows, min_status=ms, respect_dependencies=rd, now=now,
                            give_up_after_bounces=bounces, extra_skip_labels=labels,
                            trusted_authors=trusted, skip_trackers=trackers)
    held = tuple(rank_held(held_items(
        rows, proposals_by_ref, now=now, min_status=ms, respect_dependencies=rd,
        give_up_after_bounces=bounces, extra_skip_labels=labels,
        trusted_authors=trusted, skip_trackers=trackers)))
    ages = [i.age_hours for i in held if i.age_hours is not None]
    return ProductEscalation(
        slug=slug, dispatchable=len(entries), skipped=skipped, held=held,
        starving=bool(held) and len(entries) <= starving_max and is_on(policy),
        oldest_hold_hours=max(ages) if ages else None)
