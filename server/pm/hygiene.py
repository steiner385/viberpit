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
5. ``closeout_assess`` (§3) — a system-written close-out hold
   (``closeout_assess.is_closeout_hold``) whose assessment for the issue's current
   body has decided → close, release the hold, or rewrite its reason to the checklist
   of what only a person can do. The assessment itself (a helper model call) is the
   loop's, before ``plan``; this rule only reads its stored outcome.
6. ``parked_review`` (§4) — a ``parked`` issue whose stored review (``parked_review``,
   the loop's helper call) says it is fixed or obsolete → close; still relevant and
   actionable → unpark. ALWAYS a recommendation (``RECOMMEND_ONLY_RULES``), whatever
   the product's ``risk_tolerance``: a person's park ends only with the owner's yes.
7. ``evidence_posted`` (#3113) — an ``owner_evidence`` hold (evidence only the owner
   can supply) with a human comment on the issue AFTER the hold was set → close as
   completed, quoting what was asked. ALWAYS a recommendation: the mirror knows a
   human commented, not WHO — a session's own follow-up comment lands under the
   owner's GitHub login too — so the owner's one tap in the digest is the proof.

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
from server.pm import closeout_assess as ca
from server.pm import parked_review as pr
from server.pm.desired_state import HELD_KINDS, STATUS_BLOCKED, STATUS_DONE
from server.pm.holds import OWNER_EVIDENCE, actor_class
from server.pm.outcomes import body_fingerprint
from server.pm.policy_skip import stale_system_holds
from server.pm.queue import (PARKED, PARKED_LABELS, QueueRow, is_bot_noise,
                             is_self_closing_tracker)
from server.pm.stale_deps import DEP_PRUNE_MAX, StaleEdge

RULE_DONE_BUT_OPEN = "done_but_open"
RULE_BOARD_BLOCKED = "board_blocked_no_blockers"
RULE_STALE_HOLDS = "stale_holds"
RULE_RESOLVED_EDGES = "resolved_edges"
RULE_CLOSEOUT_ASSESS = "closeout_assess"
RULE_PARKED_REVIEW = "parked_review"
RULE_EVIDENCE_POSTED = "evidence_posted"
RULES: tuple[str, ...] = (RULE_DONE_BUT_OPEN, RULE_BOARD_BLOCKED, RULE_STALE_HOLDS,
                          RULE_RESOLVED_EDGES, RULE_CLOSEOUT_ASSESS, RULE_PARKED_REVIEW,
                          RULE_EVIDENCE_POSTED)
#: Rules whose findings are only ever owner recommendations — never applied by the
#: loop, whatever ``risk_tolerance`` says (design §4: no automatic unpark, ever;
#: #3113: a comment's author is not in the mirror, so the owner confirms the reply).
RECOMMEND_ONLY_RULES = frozenset({RULE_PARKED_REVIEW, RULE_EVIDENCE_POSTED})

ACTION_CLOSE = "close"
ACTION_SET_STATUS = "set_status"
ACTION_RELEASE_HOLD = "release_hold"
ACTION_PRUNE_EDGE = "prune_edge"
ACTION_REWRITE_HOLD = "rewrite_hold"
ACTION_UNPARK = "unpark"
ACTIONS: tuple[str, ...] = (ACTION_CLOSE, ACTION_SET_STATUS, ACTION_RELEASE_HOLD,
                            ACTION_PRUNE_EDGE, ACTION_REWRITE_HOLD, ACTION_UNPARK)

#: Writes per product per tick, by action: each close and status move is a GitHub
#: write, and an unexpected flood (a board migration, a sync glitch) must trickle,
#: not land at once. Prune uses the route's own batch cap.
ACTION_CAPS: Mapping[str, int] = {ACTION_CLOSE: 10, ACTION_SET_STATUS: 20,
                                  ACTION_RELEASE_HOLD: 20, ACTION_PRUNE_EDGE: DEP_PRUNE_MAX,
                                  ACTION_REWRITE_HOLD: 20, ACTION_UNPARK: 20}

#: Close-out assessments (one helper model call each) per tick, fleet-wide.
ASSESSMENTS_PER_TICK = 3

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
    #: Issue ref → its stored close-out assessment (``closeout_assess.assessment_state``).
    closeout_states: Mapping[str, Mapping] = field(default_factory=dict)
    #: Parked issue ref → its last review (``parked_review.review_state``).
    parked_states: Mapping[str, Mapping] = field(default_factory=dict)
    #: The product's review run (``parked_review.run_target``): ``startedAt`` and
    #: the refs still ``pending`` in it.
    parked_run: Mapping = field(default_factory=dict)


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


def _closeout_held(rows: Iterable[QueueRow]) -> list[QueueRow]:
    """Close-out hand-offs a SYSTEM actor wrote, with nobody on the item."""
    return [r for r in rows if ca.is_closeout_hold(r.hold_kind, r.hold_reason)
            and r.hold_by and actor_class(str(r.hold_by)) == "system"
            and not r.live_session and not r.claimed]


def _assessed(row: QueueRow, states: Mapping[str, Mapping]) -> Mapping | None:
    """*row*'s stored assessment when it judged the issue's CURRENT body."""
    state = states.get(row.ref)
    if state and state.get("fingerprint") == body_fingerprint(row.title, row.body):
        return state
    return None


def _closeout_rows(inputs: ProductInputs, *, now: datetime) -> list[QueueRow]:
    """*inputs*' close-out holds, less any rule 3 already releases (the queue skips
    the issue anyway, so there is nothing to assess)."""
    rows = [r for r in inputs.rows if r.product == inputs.slug]
    stale = {r.ref for r in stale_system_holds(rows, {inputs.slug: inputs.policy}, now=now)}
    return [r for r in _closeout_held(rows) if r.ref not in stale]


def closeout_pending(inputs: ProductInputs, *, now: datetime) -> list[QueueRow]:
    """The close-out holds still waiting for an assessment of their current body."""
    return [r for r in _closeout_rows(inputs, now=now)
            if _assessed(r, inputs.closeout_states) is None]


def closeout_assess(rows: Iterable[QueueRow],
                    states: Mapping[str, Mapping]) -> list[HygieneAction]:
    """Rule 5 (§3): each of *rows* (close-out holds, ``_closeout_rows``) whose
    current-body assessment decided and has not been applied yet → its outcome.
    ``holdReason`` is the reason judged: every write re-checks it
    (``holds.assessed_system_hold``)."""
    out: list[HygieneAction] = []
    verbs = {ca.DECISION_CLOSE: ACTION_CLOSE, ca.DECISION_RELEASE: ACTION_RELEASE_HOLD,
             ca.DECISION_CHECKLIST: ACTION_REWRITE_HOLD}
    for row in rows:
        state = _assessed(row, states)
        if state is None or state.get("appliedAt") or state.get("decision") not in verbs:
            continue
        decision, text = str(state["decision"]), str(state.get("text") or "")
        detail = {"title": row.title, "holdReason": row.hold_reason or "",
                  "decision": decision, "summary": state.get("summary") or ""}
        if decision == ca.DECISION_CLOSE:
            detail["comment"] = text
            evidence = f"the merged PR(s) already cover every item {row.ref} still listed"
        elif decision == ca.DECISION_RELEASE:
            detail["note"] = text
            evidence = (f"nothing left on {row.ref} needs a person; the runner can "
                        f"finish it ({text})")
        else:
            detail["reason"] = text
            evidence = f"only some of {row.ref} needs a person; the hold now lists just that"
        out.append(HygieneAction(RULE_CLOSEOUT_ASSESS, row.product, row.ref,
                                 row.work_item_id, verbs[decision], detail, evidence))
    return out


def _parked_comment(row: QueueRow, state: Mapping) -> str:
    evidence = str(state.get("evidence") or "").strip()
    if state.get("verdict") == pr.VERDICT_CLOSE_FIXED:
        number = state.get("pr")
        head = (f"Merged PR #{number} ({pr.pr_url(row.repo, int(number))}) already does what "
                "this parked issue asked." if isinstance(number, int)
                else "A merged PR already does what this parked issue asked.")
        return f"{head}\n\n{evidence}".strip()
    return f"This parked issue no longer applies.\n\n{evidence}".strip()


def parked_review(rows: Iterable[QueueRow],
                  states: Mapping[str, Mapping]) -> list[HygieneAction]:
    """Rule 6 (§4): each parked row whose stored review judged its CURRENT body, said
    something other than ``keep`` and has not been applied → close (``completed``
    for fixed, ``not_planned`` for obsolete) or unpark. Recommend-only
    (``RECOMMEND_ONLY_RULES``); the merged PRs it read ride along as evidence links."""
    out: list[HygieneAction] = []
    for row in pr.parked_rows(rows):
        state = states.get(row.ref)
        if (not state or state.get("appliedAt")
                or state.get("fingerprint") != body_fingerprint(row.title, row.body)):
            continue
        verdict = state.get("verdict")
        detail: dict = {"title": row.title, "verdict": verdict,
                        "summary": state.get("summary") or "",
                        "prs": list(state.get("prs") or [])}
        if verdict in (pr.VERDICT_CLOSE_FIXED, pr.VERDICT_CLOSE_OBSOLETE):
            fixed = verdict == pr.VERDICT_CLOSE_FIXED
            detail["closeReason"] = "completed" if fixed else "not_planned"
            detail["comment"] = _parked_comment(row, state)
            action = ACTION_CLOSE
            evidence = (f"{row.ref} is parked, but a merged PR already does it"
                        if fixed else f"{row.ref} is parked, but it no longer applies")
        elif verdict == pr.VERDICT_UNPARK:
            detail["label"] = next((str(lbl) for lbl in row.labels
                                    if str(lbl).strip().lower() in PARKED_LABELS), PARKED)
            action = ACTION_UNPARK
            evidence = f"{row.ref} is parked, but it is still relevant and an agent could do it now"
        else:
            continue
        reason = str(state.get("evidence") or "").strip()
        out.append(HygieneAction(RULE_PARKED_REVIEW, row.product, row.ref, row.work_item_id,
                                 action, detail, f"{evidence}: {reason}" if reason else evidence))
    return out


def evidence_replied(hold_set_at: datetime | None,
                     last_human_comment_at: datetime | None) -> bool:
    """THE "the owner replied" test (#3113): a human comment strictly after the
    hold was set. Unknown on either side is no reply."""
    if hold_set_at is None or last_human_comment_at is None:
        return False
    return _aware(last_human_comment_at) > _aware(hold_set_at)


def evidence_posted(rows: Iterable[QueueRow]) -> list[HygieneAction]:
    """Rule 7 (#3113): every ``owner_evidence`` hold whose issue has a human comment
    after the hold → close as completed, with a comment quoting the ask. The hold
    reason is carried in ``detail["holdReason"]`` so the write can re-check the
    very hold it judged. Recommend-only (``RECOMMEND_ONLY_RULES``). An item somebody
    is on is left alone."""
    out: list[HygieneAction] = []
    for row in rows:
        if row.hold_kind != OWNER_EVIDENCE or row.live_session or row.claimed:
            continue
        if not evidence_replied(row.hold_set_at, row.last_human_comment_at):
            continue
        assert row.hold_set_at is not None and row.last_human_comment_at is not None
        asked, replied = _aware(row.hold_set_at), _aware(row.last_human_comment_at)
        ask = (row.hold_reason or "").strip()
        out.append(HygieneAction(
            RULE_EVIDENCE_POSTED, row.product, row.ref, row.work_item_id, ACTION_CLOSE,
            {"title": row.title, "holdReason": row.hold_reason or "",
             "closeReason": "completed",
             "askedAt": asked.isoformat(), "repliedAt": replied.isoformat(),
             "comment": (f"The evidence asked for on {asked:%Y-%m-%d} was posted here on "
                         f"{replied:%Y-%m-%d}." + (f"\n\nAsked: {ask}" if ask else ""))},
            f"{row.ref} waited for evidence only you can supply, and a comment was "
            f"posted after the ask ({replied:%Y-%m-%d %H:%M} UTC)"))
    return out


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
            *resolved_edges(inputs.slug, inputs.stale_edges, ids),
            *closeout_assess(_closeout_rows(inputs, now=now), inputs.closeout_states),
            *parked_review(rows, inputs.parked_states),
            *evidence_posted(rows)]


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
