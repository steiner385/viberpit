"""Needs-you hygiene: the mechanical auto-resolves (design
``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md`` §2).

Each rule is a pure function over the queue's own rows (``queue_store.queue_inputs``)
plus the facts it needs, and answers ``HygieneAction``s — what could be tidied without
a person. Deciding what HAPPENS to an action (dry run, apply, or an owner
recommendation) is the executor's (``server/automations/needs_you_hygiene.py``); the
rules never write.

1. ``done_but_open`` — board Done for over a day, issue still open → close.
2. ``board_blocked_no_blockers`` — board Blocked with no open dependency and no hold →
   the product's ``queue_min_status``.
3. ``stale_holds`` — a SYSTEM actor's ``owner_action`` hand-off the queue already skips
   (``policy_skip.stale_system_holds``, the rule the backlog tick shares) → release.
4. ``resolved_edges`` — prunable stale dependency edges (``stale_deps.classify_stale``)
   → prune through ``dep_prune.prune``.

Triage-accepted items missing from the board (§2 rule 5) need no rule here: the
reconciler adds every open issue to its product's board (``reconcile_diff.membership``)
and places a classified one at Ready (``desired_state.derive_status``).

Pure: no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from server.automations.product_policy import (NEEDS_YOU_AUTOMATION_MODES, effective_policy,
                                               skip_labels)
from server.pm.desired_state import HELD_KINDS, STATUS_BLOCKED, STATUS_DONE
from server.pm.policy_skip import stale_system_holds
from server.pm.queue import QueueRow, is_bot_noise, is_self_closing_tracker
from server.pm.stale_deps import DEP_PRUNE_MAX, StaleEdge

RULE_DONE_BUT_OPEN = "done_but_open"
RULE_BOARD_BLOCKED = "board_blocked_no_blockers"
RULE_STALE_HOLDS = "stale_holds"
RULE_RESOLVED_EDGES = "resolved_edges"
RULES: tuple[str, ...] = (RULE_DONE_BUT_OPEN, RULE_BOARD_BLOCKED, RULE_STALE_HOLDS,
                          RULE_RESOLVED_EDGES)

ACTION_CLOSE = "close"
ACTION_SET_STATUS = "set_status"
ACTION_RELEASE_HOLD = "release_hold"
ACTION_PRUNE_EDGE = "prune_edge"
ACTIONS: tuple[str, ...] = (ACTION_CLOSE, ACTION_SET_STATUS, ACTION_RELEASE_HOLD,
                            ACTION_PRUNE_EDGE)

#: Writes per product per tick, by action: each close and status move is a GitHub
#: write, and an unexpected flood (a board migration, a sync glitch) must trickle,
#: not land at once. Prune uses the route's own batch cap.
ACTION_CAPS: Mapping[str, int] = {ACTION_CLOSE: 10, ACTION_SET_STATUS: 20,
                                  ACTION_RELEASE_HOLD: 20, ACTION_PRUNE_EDGE: DEP_PRUNE_MAX}

#: How long a card must have sat untouched in Done before its open issue is closed.
#: ``GhProjectItem.gh_updated_at`` is the last change to ANY field on the card (there
#: is no status-changed-at column), so "Done and untouched for 24 h" implies Done for
#: at least 24 h — a conservative proxy.
DONE_GRACE = timedelta(hours=24)

#: The needs-you metric (``AttentionSample``) is written at most this often per product.
SAMPLE_INTERVAL = timedelta(hours=1)

#: ``risk_tolerance`` values under which an action is applied rather than recommended.
APPLYING_TOLERANCES = frozenset({"balanced", "bold"})


@dataclass(frozen=True)
class HygieneAction:
    rule: str
    product: str
    #: The issue ref; for ``prune_edge`` the edge, ``<src><-<blocker>``
    #: (``edge_ref``: one target per edge, so two edges of one issue never share a
    #: recommendation).
    ref: str
    work_item_id: str | None
    action: str
    detail: dict = field(default_factory=dict)
    evidence: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """The pending-recommendation dedupe key (with the product)."""
        return (self.rule, self.ref, self.action)

    def to_wire(self) -> dict:
        return {"rule": self.rule, "product": self.product, "ref": self.ref,
                "workItemId": self.work_item_id, "action": self.action,
                "detail": dict(self.detail), "evidence": self.evidence}


@dataclass(frozen=True)
class ProductInputs:
    """One product's facts for one tick (the loop's loader builds it)."""
    slug: str
    project_id: str
    policy: Mapping
    rows: list[QueueRow]
    #: ``work_item_id`` → the board card's ``gh_updated_at`` (aware UTC).
    board_updated_at: Mapping[str, datetime]
    stale_edges: list[StaleEdge]


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _busy(row: QueueRow) -> bool:
    """Someone is on it: a live session, a runner claim, or a hold of any kind."""
    return row.live_session or row.claimed or row.hold_kind is not None


def done_but_open(rows: Iterable[QueueRow], board_updated_at: Mapping[str, datetime],
                  policies: Mapping[str, Mapping], *, now: datetime) -> list[HygieneAction]:
    """Rule 1: the board says Done, the issue is open (queue rows are open by
    construction), and the card has not changed for ``DONE_GRACE`` → close as
    completed. Never bot noise (``queue.is_bot_noise`` — the Renovate Dependency
    Dashboard by title — and the product's ``skip_labels``), a self-closing tracker or
    a generated / daily auto-upserted report (``queue.is_self_closing_tracker``,
    whatever the product's tracker opt-in: those close themselves), an open epic, or
    an item somebody is on."""
    out: list[HygieneAction] = []
    for row in rows:
        if row.status != STATUS_DONE or _busy(row) or row.open_children:
            continue
        at = board_updated_at.get(row.work_item_id or "")
        if at is None or now - _aware(at) < DONE_GRACE:
            continue
        if is_bot_noise(author=row.author, labels=row.labels, title=row.title,
                        extra_skip_labels=skip_labels(policies.get(row.product))):
            continue
        if is_self_closing_tracker(row.labels, row.title, author=row.author):
            continue
        out.append(HygieneAction(
            RULE_DONE_BUT_OPEN, row.product, row.ref, row.work_item_id, ACTION_CLOSE,
            {"title": row.title, "boardUpdatedAt": _aware(at).isoformat()},
            f"the board has shown {row.ref} as Done since at least "
            f"{_aware(at):%Y-%m-%d %H:%M} UTC and the issue is still open"))
    return out


def board_blocked_no_blockers(rows: Iterable[QueueRow],
                              policies: Mapping[str, Mapping]) -> list[HygieneAction]:
    """Rule 2: the board says Blocked, but no open dependency blocks the issue and no
    ``HELD_KINDS`` hold parks it → move the card to the product's
    ``queue_min_status`` (through ``board_status.apply``). A tracker's column is its
    workflow's (``board_status.refusal`` would refuse it), and an item somebody is
    on is left for the reconciler."""
    out: list[HygieneAction] = []
    for row in rows:
        if row.status != STATUS_BLOCKED or row.open_blockers or row.hold_kind in HELD_KINDS:
            continue
        if row.live_session or row.claimed:
            continue
        if is_self_closing_tracker(row.labels, row.title, author=row.author):
            continue
        policy = policies.get(row.product)
        target = str(effective_policy(dict(policy) if policy is not None else None)
                     ["queue_min_status"])
        out.append(HygieneAction(
            RULE_BOARD_BLOCKED, row.product, row.ref, row.work_item_id, ACTION_SET_STATUS,
            {"status": target, "from": STATUS_BLOCKED, "title": row.title},
            f"{row.ref} sits in Blocked but nothing open blocks it and nothing holds it"))
    return out


def stale_holds(rows: Iterable[QueueRow], policies: Mapping[str, Mapping], *,
                now: datetime) -> list[HygieneAction]:
    """Rule 3: every system-written ``owner_action`` hold the queue already skips
    (``policy_skip.stale_system_holds`` — the same rule the backlog tick runs) →
    release, signal ``policy_skip``."""
    return [HygieneAction(
        RULE_STALE_HOLDS, row.product, row.ref, row.work_item_id, ACTION_RELEASE_HOLD,
        {"holdBy": row.hold_by, "holdReason": row.hold_reason, "title": row.title},
        f"{row.ref} is held for you by {row.hold_by}, but the queue skips it anyway "
        "(not work under the product's policy, or a standing not-needed verdict)")
        for row in stale_system_holds(rows, policies, now=now)]


def edge_ref(src: str, blocker: str) -> str:
    return f"{src}<-{blocker}"


def resolved_edges(product: str, edges: Iterable[StaleEdge],
                   work_item_ids: Mapping[str, str]) -> list[HygieneAction]:
    """Rule 4: every PRUNABLE stale edge (``stale_deps.PRUNABLE_KINDS``) → prune."""
    return [HygieneAction(
        RULE_RESOLVED_EDGES, product, edge_ref(e.src, e.blocker), work_item_ids.get(e.src),
        ACTION_PRUNE_EDGE,
        {"src": e.src, "blocker": e.blocker, "kind": e.kind, "blockerState": e.blocker_state},
        f"{e.src} is still linked as blocked by {e.blocker}, which is "
        f"{e.blocker_state or 'resolved'}" if e.kind == "native_resolved" else
        f"{e.src} no longer names {e.blocker} in its body")
        for e in edges if e.prunable]


def plan(inputs: ProductInputs, *, now: datetime) -> list[HygieneAction]:
    """Every rule over one product, uncapped (``capped`` limits what is WRITTEN; a
    recommendation for every finding is cheap and must not be superseded just
    because a cap cut it this tick)."""
    policies = {inputs.slug: inputs.policy}
    rows = [r for r in inputs.rows if r.product == inputs.slug]
    ids = {r.ref: r.work_item_id for r in rows if r.work_item_id}
    return [*done_but_open(rows, inputs.board_updated_at, policies, now=now),
            *board_blocked_no_blockers(rows, policies),
            *stale_holds(rows, policies, now=now),
            *resolved_edges(inputs.slug, inputs.stale_edges, ids)]


def capped(actions: Iterable[HygieneAction]) -> list[HygieneAction]:
    """*actions* in order, at most ``ACTION_CAPS[action]`` of each action."""
    left = dict(ACTION_CAPS)
    out: list[HygieneAction] = []
    for a in actions:
        if left.get(a.action, 0) > 0:
            left[a.action] -= 1
            out.append(a)
    return out


def effective_mode(automation_mode: str, product_mode: str) -> str:
    """The lower of the automation's mode and the product's ``needs_you_automation``
    (``off < dry_run < on``); an unknown value on either side reads as ``off``."""
    order = NEEDS_YOU_AUTOMATION_MODES
    if automation_mode not in order or product_mode not in order:
        return "off"
    return order[min(order.index(automation_mode), order.index(product_mode))]


def applies(policy: Mapping) -> bool:
    """Whether the product's ``risk_tolerance`` lets hygiene APPLY an action, rather
    than leave it as an owner recommendation (``cautious``, the default)."""
    return policy.get("risk_tolerance") in APPLYING_TOLERANCES


def sample_due(last_at: datetime | None, now: datetime) -> bool:
    """Whether a product's needs-you sample is due (``SAMPLE_INTERVAL``)."""
    return last_at is None or now - _aware(last_at) >= SAMPLE_INTERVAL
