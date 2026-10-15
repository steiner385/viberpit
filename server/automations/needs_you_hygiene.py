"""Needs-you Hygiene (design ``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md``
§2–§5): the mechanical auto-resolves — so "needs you" stops counting things a machine can
tidy — the close-out assessment (§3), the parked review (§4), the daily owner digest
push (§5) and the hourly needs-you sample.

The rules are pure (``server/pm/hygiene.py``). This module is the executor and the
loop, in the ``DecisionEscalation`` mould:

* **Mode:** the automation's own (``off | dry_run | on``) and the product's
  ``needs_you_automation`` (same vocabulary); the lower wins (``hygiene.effective_mode``).
* **Risk:** ``risk_tolerance`` ``balanced``/``bold`` apply; ``cautious`` records an
  ``OwnerRecommendation`` the owner applies (``POST /api/pm/recommendations/apply``,
  through ``apply_action`` — the SAME executor) or dismisses.
* **Audit:** every applied action writes ``pm.hygiene.<rule>``; dry run only emits.

A refused or failed GitHub write is expected (a conflict, a stale mirror): it emits at
``info`` and logs at INFO — a WARNING would auto-file an issue — and is not retried
for ``FAILED_RETRY``. Only an unexpected exception logs a WARNING.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import func, select

from server.automations.base import AutomationStatus
from server.automations.context import observation_failed, publish_observation
from server.automations.engine_policy import EnginePolicy
from server.automations.product_policy import NEEDS_YOU_AUTOMATION_MODES
from server.pm import digest as dg
from server.pm import hygiene as h
from server.pm import parked_review as pr
from server.pm.holds import OWNER_EVIDENCE
from server.pm.hygiene import HygieneAction
from server.pm.queue import QueueRow

log = logging.getLogger(__name__)

AUTOMATION_ID = "needs-you-hygiene"
#: The actor a hold release is recorded as when the loop applies it: system class
#: (``holds.SYSTEM_PREFIXES``), so it may end another system actor's hand-off.
HYGIENE_ACTOR = f"automation:{AUTOMATION_ID}"
TICK_S = 15 * 60.0
#: A failed action (a refused write) is not re-attempted for this long: a close
#: comments on the issue first, so retrying every tick would stack comments.
FAILED_RETRY = timedelta(hours=24)

_VERB = {h.ACTION_CLOSE: "close", h.ACTION_SET_STATUS: "move", h.ACTION_RELEASE_HOLD:
         "release the hold on", h.ACTION_PRUNE_EDGE: "prune the edge",
         h.ACTION_REWRITE_HOLD: "narrow the hold on", h.ACTION_UNPARK: "unpark"}


class IssueSeams(Protocol):
    """The orchestrator's issue-write seams, by the names ``app.state.sessions``
    (``InProcessOrchestratorAdapter`` / ``RemoteOrchestrator``) exposes (``SessionOrchestrator._hygiene_seams`` adapts the
    orchestrator's own for the loop)."""
    @property
    def pm_issue_close(self) -> Callable[[str, dict], Awaitable[dict]]: ...
    @property
    def pm_board_status(self) -> Callable[[str, dict], Awaitable[dict]]: ...
    @property
    def pm_remove_dependency(self) -> Callable[[str, str], Awaitable[dict]]: ...


class DigestSeams(Protocol):
    """What the daily owner digest push (§5) needs from the orchestrator."""
    async def in_quiet_hours(self, now: datetime) -> bool: ...
    async def load(self) -> dict: ...
    async def last_sent(self) -> dict[str, datetime]: ...
    async def notify(self, title: str, body: str, *, slug: str, url: str) -> None: ...
    async def record_sent(self, slug: str, detail: dict) -> None: ...


def _describe(action: HygieneAction) -> str:
    if action.action == h.ACTION_SET_STATUS:
        return f"move {action.ref} to {action.detail.get('status')}"
    return f"{_VERB.get(action.action, action.action)} {action.ref}"


def _close_reason(action: HygieneAction) -> str:
    """``completed`` unless the finding says otherwise (a parked review's obsolete
    verdict closes ``not_planned``)."""
    return "not_planned" if action.detail.get("closeReason") == "not_planned" else "completed"


def close_comment(action: HygieneAction) -> str:
    """The note a hygiene close posts before closing: the close-out assessment's or
    the parked review's evidence (design §3/§4), else the one line of §2 rule 1."""
    comment = str(action.detail.get("comment") or "").strip()
    closing = ("Closing as not planned." if _close_reason(action) == "not_planned"
               else "Closing as completed.")
    if comment:
        return f"{comment}\n\n{closing} Reopen it if there is still work to do here."
    return (f"{closing[:-1]}: {action.evidence}. "
            "Reopen it if there is still work to do here.")


def unpark_comment(action: HygieneAction) -> str:
    """The note an approved unpark posts before removing the label (design §4)."""
    reason = str(action.detail.get("summary") or "").strip() or action.evidence
    return (f"Unparking: {reason}\n\nThe owner approved this from the parked review; the "
            f"`{action.detail.get('label') or 'parked'}` label is removed so the queue can "
            "pick it up. Re-add it to park it again.")


async def _close(action: HygieneAction, seams: IssueSeams) -> dict:
    from server.github import ci_issues
    from server.pm.refs import split_ref
    parts = split_ref(action.ref)
    if parts is None or not action.work_item_id:
        return {"ok": False, "step": "load", "error": f"{action.ref} is not a mirrored issue"}
    if action.rule == h.RULE_CLOSEOUT_ASSESS and not await _assessed_hold_current(action):
        return _HOLD_MOVED
    if action.rule == h.RULE_PARKED_REVIEW and not await _still_parked(action):
        return _NOT_PARKED
    if action.rule == h.RULE_EVIDENCE_POSTED and not await _evidence_still_posted(action):
        return _EVIDENCE_GONE
    # Comment first, then close — ``POST /api/pm/issues/close``'s order: an issue is
    # never closed without its explanation.
    url = await asyncio.to_thread(ci_issues.comment_on_issue, parts[0], parts[1],
                                  close_comment(action), automation=True)
    if url is None:
        return {"ok": False, "step": "comment",
                "error": "GitHub rejected the comment; the issue was not closed"}
    result = await seams.pm_issue_close(action.work_item_id, {"reason": _close_reason(action)})
    return {**result, "commentUrl": url}


#: A parked review's write found the issue no longer parked (or gone).
_NOT_PARKED = {"ok": False, "step": "not_parked",
               "error": "the issue is no longer parked (or no longer mirrored)"}

#: An evidence-posted close found the ask, or the reply, gone (#3113).
_EVIDENCE_GONE = {"ok": False, "step": "evidence_hold_moved",
                  "error": "the evidence ask changed, was released, or the reply is gone"}


async def _evidence_still_posted(action: HygieneAction) -> bool:
    """Whether the mirror still shows *action*'s issue open, under the SAME
    ``owner_evidence`` ask it judged (reason word for word), with a human comment
    after it (``hygiene.evidence_replied``) — a re-ask or a release in between is
    never closed over (#3113)."""
    from server.db import db_session
    from server.db.models import WorkItem
    if not action.work_item_id:
        return False
    expected = str(action.detail.get("holdReason") or "").strip()
    async with db_session() as db:
        wi = await db.get(WorkItem, action.work_item_id)
        return (wi is not None and wi.state == "open" and wi.hold_kind == OWNER_EVIDENCE
                and (wi.hold_reason or "").strip() == expected
                and h.evidence_replied(wi.hold_set_at, wi.last_human_comment_at))


async def _still_parked(action: HygieneAction) -> bool:
    """Whether the mirror still shows *action*'s issue open and parked — a person
    who unparked or closed it in between is never overridden."""
    from server.db import db_session
    from server.db.models import WorkItem
    from server.pm.queue import is_parked
    if not action.work_item_id:
        return False
    async with db_session() as db:
        wi = await db.get(WorkItem, action.work_item_id)
        return wi is not None and wi.state == "open" and is_parked(wi.labels or ())


async def _unpark(action: HygieneAction) -> dict:
    """Remove the ``parked`` label an approved parked review named (design §4):
    re-check it is still parked, explain on the issue (marked as automation's, so
    it does not re-arm a not-needed verdict), then remove the label as spelled."""
    from server.github import ci_issues
    from server.pm.refs import split_ref
    parts = split_ref(action.ref)
    if parts is None or not action.work_item_id:
        return {"ok": False, "step": "load", "error": f"{action.ref} is not a mirrored issue"}
    if not await _still_parked(action):
        return _NOT_PARKED
    url = await asyncio.to_thread(ci_issues.comment_on_issue, parts[0], parts[1],
                                  unpark_comment(action), automation=True)
    if url is None:
        return {"ok": False, "step": "comment",
                "error": "GitHub rejected the comment; the label was not removed"}
    label = str(action.detail.get("label") or "parked")
    if not await asyncio.to_thread(ci_issues.remove_label, parts[0], parts[1], label):
        return {"ok": False, "step": "label", "commentUrl": url,
                "error": f"GitHub refused to remove the {label!r} label"}
    return {"ok": True, "commentUrl": url}


#: A close-out assessment's write found the hold it judged gone or changed.
_HOLD_MOVED = {"ok": False, "step": "hold_changed",
               "error": "the close-out hold that was assessed is gone or has changed"}


async def _assessed_hold_current(action: HygieneAction) -> bool:
    from server.db import db_session
    from server.pm import holds
    expected = str(action.detail.get("holdReason") or "")
    async with db_session() as db:
        return await db.run_sync(lambda s: holds.assessed_system_hold(
            s, action.ref, expected_reason=expected) is not None)


async def _release(action: HygieneAction, *, actor: str, via: str) -> dict:
    from server.db import db_session
    from server.pm import holds
    if action.rule == h.RULE_CLOSEOUT_ASSESS:
        expected = str(action.detail.get("holdReason") or "")
        async with db_session() as db:
            done = await db.run_sync(lambda s: holds.release_assessed_closeout_hold(
                s, action.ref, expected_reason=expected, actor=actor, via=via))
        return {"ok": True} if done else _HOLD_MOVED
    try:
        async with db_session() as db:
            released = await db.run_sync(lambda s: holds.release_policy_skipped_holds(
                s, [action.ref], actor=actor, via=via))
    except holds.HoldPermissionError as exc:
        return {"ok": False, "step": "forbidden", "error": str(exc)}
    if not released:
        return {"ok": False, "step": "not_held",
                "error": f"{action.ref} carries no system hold to release any more"}
    return {"ok": True}


async def _rewrite(action: HygieneAction) -> dict:
    """Narrow a close-out hold to the checklist only a person can do (design §3).
    The words are the automation's, so the hold is attributed to ``HYGIENE_ACTOR``
    whoever applies it (the audit row records who did)."""
    from server.db import db_session
    from server.pm import holds
    expected = str(action.detail.get("holdReason") or "")
    reason = str(action.detail.get("reason") or "")
    if not reason.strip():
        return {"ok": False, "step": "load", "error": "no checklist to write"}
    async with db_session() as db:
        done = await db.run_sync(lambda s: holds.rewrite_assessed_hold_reason(
            s, action.ref, expected_reason=expected, reason=reason, actor=HYGIENE_ACTOR))
    return {"ok": True} if done else _HOLD_MOVED


async def _mark_applied(target: str) -> None:
    """Stamp the stored assessment or review at *target* applied, so its rule stops
    proposing it — a later re-hold (or a stale mirror still showing the label) of
    the same body is left for a person rather than looped through the same outcome
    again."""
    from server.automations.context import AutomationContext
    from server.time_iso import iso_utc
    ctx = AutomationContext(AUTOMATION_ID)
    state = await ctx.get_target_state(target)
    if state:
        await ctx.set_target_state(target, {**state,
                                            "appliedAt": iso_utc(datetime.now(timezone.utc))})


#: Where each rule with a stored outcome keeps it (``AutomationTargetState`` target).
_STORED_OUTCOME = {h.RULE_CLOSEOUT_ASSESS: lambda ref: ref,
                   h.RULE_PARKED_REVIEW: pr.item_target}


async def _prune(action: HygieneAction, seams: IssueSeams, *, identity: str, via: str) -> dict:
    from sqlalchemy import or_

    from server.db import db_session
    from server.db.models import Project
    from server.pm import dep_prune
    async with db_session() as db:
        project = (await db.execute(select(Project).where(or_(
            Project.slug == action.product, Project.id == action.product)))).scalars().first()
        if project is None:
            return {"ok": False, "step": "load", "error": f"no product {action.product}"}
        results = await dep_prune.prune(
            db, slug=project.slug or project.id, scope=await dep_prune.dep_scope(db, project),
            pairs=[(str(action.detail.get("src")), str(action.detail.get("blocker")))],
            remove_dependency=seams.pm_remove_dependency, identity=identity, via=via)
    r = results[0]
    return {"ok": r["pruned"] is not None, "step": r["refused"], "error": r["message"]}


async def apply_action(action: HygieneAction, *, seams: IssueSeams, identity: str,
                       actor: str, via: str) -> dict:
    """THE executor for one hygiene action — the loop (as ``SYSTEM_IDENTITY`` /
    ``HYGIENE_ACTOR``, via ``automation``) and a person applying a recommendation
    (as themselves, via ``api``) both come through here. Each write goes through its
    existing seam: the journaled close (after a comment), ``board_status.apply``,
    the policy-skip hold release (system holds only), and ``dep_prune.prune``; a
    close-out assessment's (§3) close, release and hold rewrite first re-check the
    hold it judged (``holds.assessed_system_hold``), and once one lands its stored
    assessment is stamped applied. A parked review's (§4) close and unpark first
    re-check the issue is still parked, and are refused outright via ``automation``
    (``RECOMMEND_ONLY_RULES``: only a person's approval applies them).
    Answers ``{"ok", "step", "error", …}``; a refusal is data, not an exception.
    Every attempt is audited ``pm.hygiene.<rule>`` with its outcome."""
    from server.audit import record_audit
    from server.db import db_session
    if action.rule in h.RECOMMEND_ONLY_RULES and via == "automation":
        result = {"ok": False, "step": "recommend_only",
                  "error": f"{action.rule} findings are applied only on the owner's approval"}
    elif action.action == h.ACTION_CLOSE:
        result = await _close(action, seams)
    elif action.action == h.ACTION_SET_STATUS:
        result = await seams.pm_board_status(str(action.work_item_id), {
            "status": str(action.detail.get("status")), "add_if_missing": False})
    elif action.action == h.ACTION_RELEASE_HOLD:
        result = await _release(action, actor=actor, via=via)
    elif action.action == h.ACTION_PRUNE_EDGE:
        result = await _prune(action, seams, identity=identity, via=via)
    elif action.action == h.ACTION_REWRITE_HOLD:
        result = await _rewrite(action)
    elif action.action == h.ACTION_UNPARK:
        result = await _unpark(action)
    else:
        result = {"ok": False, "step": "unknown_action", "error": f"unknown {action.action!r}"}
    async with db_session() as db:
        await record_audit(db, f"pm.hygiene.{action.rule}", identity, {
            **action.to_wire(), "ok": bool(result.get("ok")), "step": result.get("step"),
            "error": result.get("error"), "commentUrl": result.get("commentUrl")}, via=via)
    stored = _STORED_OUTCOME.get(action.rule)
    if result.get("ok") and stored is not None:
        await _mark_applied(stored(action.ref))
    return result


# --------------------------------------------------------------------------------------
# Owner recommendations + samples (single writers; the loop runs them under its lock)
# --------------------------------------------------------------------------------------

async def upsert_recommendation(db, project_id: str, action: HygieneAction) -> bool:
    """Record *action* as a pending ``OwnerRecommendation`` unless one is already
    pending for the same ``(project, rule, target, action)`` (its evidence is
    refreshed) or the owner DISMISSED that finding (their no stands). The single
    writer, so the at-most-one-pending invariant needs no partial unique index.
    Answers whether a row was created."""
    from server.db.models import OwnerRecommendation
    rows = (await db.execute(select(OwnerRecommendation).where(
        OwnerRecommendation.project_id == project_id,
        OwnerRecommendation.status.in_(("pending", "dismissed")),
        OwnerRecommendation.rule == action.rule,
        OwnerRecommendation.target_ref == action.ref,
        OwnerRecommendation.action == action.action))).scalars().all()
    if any(r.status == "dismissed" for r in rows):
        return False
    pending = next((r for r in rows if r.status == "pending"), None)
    if pending is not None:
        pending.detail, pending.evidence = dict(action.detail), action.evidence
        pending.work_item_id = action.work_item_id
        return False
    db.add(OwnerRecommendation(project_id=project_id, rule=action.rule, target_ref=action.ref,
                               work_item_id=action.work_item_id, action=action.action,
                               detail=dict(action.detail), evidence=action.evidence))
    await db.flush()
    return True


async def supersede_recommendations(db, project_id: str,
                                    keep: Iterable[tuple[str, str, str]], *,
                                    now: datetime) -> int:
    """Mark every pending recommendation of *project_id* whose ``(rule, target,
    action)`` is not in *keep* — its condition no longer holds — ``superseded``."""
    from server.db.models import OwnerRecommendation
    keep = set(keep)
    n = 0
    for row in (await db.execute(select(OwnerRecommendation).where(
            OwnerRecommendation.project_id == project_id,
            OwnerRecommendation.status == "pending"))).scalars().all():
        if (row.rule, row.target_ref, row.action) not in keep:
            row.status, row.resolved_at, row.resolved_by = "superseded", now, HYGIENE_ACTOR
            n += 1
    await db.flush()
    return n


async def record_samples(db, counts: Mapping[str, dict], project_ids: Mapping[str, str], *,
                         now: datetime) -> list[str]:
    """Set ``viberpit_pm_needs_you{product}`` for every product in *counts*
    (``pm_tree.product_attention_counts``: slug → ``{needsYou, waiting}``) and write
    an ``AttentionSample`` for each whose last one is ``hygiene.SAMPLE_INTERVAL`` old.
    Answers the slugs sampled."""
    from server.db.models import AttentionSample
    from server.obs.metrics import pm_needs_you
    latest = {pid: at for pid, at in (await db.execute(
        select(AttentionSample.project_id, func.max(AttentionSample.sampled_at))
        .group_by(AttentionSample.project_id))).all()}
    written: list[str] = []
    for slug, c in sorted(counts.items()):
        pm_needs_you.labels(slug).set(float(c["needsYou"]))
        pid = project_ids.get(slug)
        if pid is None or not h.sample_due(latest.get(pid), now):
            continue
        db.add(AttentionSample(project_id=pid, sampled_at=now, needs_you=int(c["needsYou"]),
                               waiting=dict(c.get("waiting") or {})))
        written.append(slug)
    await db.flush()
    return written


# --------------------------------------------------------------------------------------
# Automation + loop
# --------------------------------------------------------------------------------------

class NeedsYouHygiene:
    id = AUTOMATION_ID
    name = "Needs-you Hygiene"
    work_class = "maintenance"
    description = ("Tidies what a machine can, so Needs you counts only what a person must "
                   "decide: closes issues the board has shown as Done for a day, moves "
                   "stale Blocked cards back to the queue, ends system holds the queue "
                   "already skips, prunes resolved dependency edges, and has a helper "
                   "model assess each merged-PR close-out hold: close it when the PR did "
                   "the work, hand it back to the runner when an agent can finish it, "
                   "else narrow it to the checklist only you can do. Products with "
                   "cautious risk tolerance get a recommendation to approve instead. Reviews "
                   "parked issues weekly and recommends closing (fixed or obsolete) or "
                   "unparking them — only ever a recommendation. Sends one daily owner "
                   "digest push per product, and samples each product's needs-you count "
                   "hourly.")
    modes: tuple[str, ...] = NEEDS_YOU_AUTOMATION_MODES
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema: list = []

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "needs_you_hygiene_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        text = (f"{counts['applied']} applied · {counts['recommended']} recommended · "
                f"{counts['dryRun']} dry-run · {counts['failed']} failed")
        busy = counts["applied"] or counts["recommended"] or counts["dryRun"]
        return AutomationStatus(text=text, level="active" if busy else "idle",
                                counts=dict(counts))

    async def on_state_change(self, *, enabled: bool, mode: str | None, config: dict,
                              ctx: Any) -> None:
        ...  # the loop reads its mode each tick


class NeedsYouHygieneLoop:
    def __init__(self, ctx: Any, *,
                 read_config: Callable[[], Awaitable[tuple[str, dict]]],
                 load: Callable[[], Awaitable[list[h.ProductInputs]]],
                 apply_action: Callable[[HygieneAction], Awaitable[dict]],
                 record_recommendation: Callable[[str, HygieneAction], Awaitable[bool]],
                 supersede_recommendations: Callable[[str, set], Awaitable[int]],
                 record_samples: Callable[[], Awaitable[Any]],
                 assess: Callable[[QueueRow], Awaitable[Mapping | None]] | None = None,
                 review_parked: Callable[[QueueRow], Awaitable[Mapping | None]] | None = None,
                 record_parked_run: Callable[[str, dict], Awaitable[None]] | None = None,
                 digest: "DigestSeams | None" = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._load = load
        self._apply_action = apply_action
        self._record_recommendation = record_recommendation
        self._supersede = supersede_recommendations
        self._record_samples = record_samples
        #: The close-out assessor (§3): assesses one held row, persists and answers
        #: its ``closeout_assess.assessment_state`` — or ``None`` when the call
        #: could not be made (retried next tick). ``None``: no assessments.
        self._assess = assess
        #: The parked reviewer (§4): reviews one parked row, persists and answers
        #: its ``parked_review.review_state`` — ``None`` when the call could not be
        #: made (retried next tick). With ``record_parked_run`` (persists a
        #: product's run state). ``None``: no parked review.
        self._review_parked = review_parked
        self._record_parked_run = record_parked_run
        #: The daily owner digest push (§5). ``None``: no push.
        self._digest = digest
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        #: Dry-run findings already announced (one line per finding, not per tick).
        self._dry_announced: set[tuple[str, tuple[str, str, str]]] = set()
        #: (product, action key) -> when it last failed (``FAILED_RETRY``).
        self._failed: dict[tuple[str, tuple[str, str, str]], datetime] = {}

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:
                observation_failed(self._ctx)
                log.warning("needs_you_hygiene.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, _config = await self._read_config()
        if mode == "off":
            return
        now = self._now()
        counts = {"applied": 0, "recommended": 0, "dryRun": 0, "failed": 0}
        products = await self._assess_closeouts(await self._load(), mode, now)
        products = await self._review_parked_items(products, mode, now)
        for product in products:
            try:
                await self._product(product, mode, now, counts)
            except Exception:
                # One product's bad data must not silence every other product.
                log.warning("needs_you_hygiene.product_failed slug=%s", product.slug,
                            exc_info=True)
        try:
            await self._record_samples()
        except Exception:
            log.warning("needs_you_hygiene.sample_failed", exc_info=True)
        try:
            await self._send_digests(products, mode, now)
        except Exception:
            log.warning("needs_you_hygiene.digest_failed", exc_info=True)
        publish_observation(self._ctx, "needs_you_hygiene_counts", counts)

    async def _assess_closeouts(self, products: list[h.ProductInputs], mode: str,
                                now: datetime) -> list[h.ProductInputs]:
        """Assess up to ``ASSESSMENTS_PER_TICK`` close-out holds fleet-wide that have
        no assessment of their current body, in dry run too (an assessment writes
        nothing to GitHub; it is what a dry run reports). Answers *products* with
        the new assessments folded in, so this tick's ``plan`` acts on them."""
        if self._assess is None:
            return products
        budget = h.ASSESSMENTS_PER_TICK
        out: list[h.ProductInputs] = []
        for p in products:
            eff = h.effective_mode(mode, str(p.policy.get("needs_you_automation", "off")))
            pending = h.closeout_pending(p, now=now) if eff != "off" and budget > 0 else []
            states = dict(p.closeout_states)
            for row in pending[:budget]:
                budget -= 1
                try:
                    state = await self._assess(row)
                except Exception:
                    log.warning("needs_you_hygiene.assess_crashed slug=%s ref=%s", p.slug,
                                row.ref, exc_info=True)
                    continue
                if state is None:
                    continue
                states[row.ref] = state
                await self._ctx.emit(
                    "info", f"{p.slug}: assessed the close-out hold on {row.ref} — "
                    f"{state.get('decision') or 'no usable verdict'}",
                    kind="hygiene_assessed", target=row.ref,
                    detail={"decision": state.get("decision"), "summary": state.get("summary")})
            out.append(replace(p, closeout_states=states) if pending else p)
        return out

    async def _review_parked_items(self, products: list[h.ProductInputs], mode: str,
                                   now: datetime) -> list[h.ProductInputs]:
        """The parked review (§4). A product's run starts when ``parked_review.run_due``
        (weekly, or daily while more than its ``parked_review_threshold`` are parked)
        and covers up to ``REVIEWS_PER_RUN`` parked issues, the least recently
        reviewed first; at most ``REVIEWS_PER_TICK`` helper calls a tick fleet-wide,
        so a run spans ticks (its ``pending`` refs persist). Runs in dry run too: a
        review writes nothing to GitHub. Answers *products* with the new reviews
        folded in, so this tick's ``plan`` recommends from them."""
        if self._review_parked is None or self._record_parked_run is None:
            return products
        budget = pr.REVIEWS_PER_TICK
        out: list[h.ProductInputs] = []
        for p in products:
            eff = h.effective_mode(mode, str(p.policy.get("needs_you_automation", "off")))
            if eff == "off":
                out.append(p)
                continue
            parked = {r.ref: r for r in pr.parked_rows(r for r in p.rows if r.product == p.slug)}
            run = dict(p.parked_run)
            pending = [ref for ref in run.get("pending") or () if ref in parked]
            changed = pending != list(run.get("pending") or ())
            threshold = int(p.policy.get("parked_review_threshold") or 0)
            if not pending and pr.run_due(pr.parse_at(run.get("startedAt")), len(parked),
                                          threshold, now=now):
                pending = [r.ref for r in pr.pick(list(parked.values()), p.parked_states,
                                                  pr.REVIEWS_PER_RUN)]
                run = {"startedAt": now.isoformat()}
                changed = True
                if pending:
                    await self._ctx.emit(
                        "info", f"{p.slug}: reviewing {len(pending)} of {len(parked)} parked "
                        "issue(s)", kind="hygiene_parked_review", target=p.slug,
                        detail={"pending": pending, "parked": len(parked)})
            states = dict(p.parked_states)
            while pending and budget > 0:
                ref = pending[0]
                budget -= 1
                try:
                    state = await self._review_parked(parked[ref])
                except Exception:
                    log.warning("needs_you_hygiene.parked_review_crashed slug=%s ref=%s",
                                p.slug, ref, exc_info=True)
                    state = None
                if state is None:
                    break   # no helper (or the read failed): the run waits for a later tick
                pending.pop(0)
                changed = True
                states[ref] = state
                await self._ctx.emit(
                    "info", f"{p.slug}: reviewed parked {ref} — "
                    f"{state.get('verdict') or 'no usable verdict'}",
                    kind="hygiene_parked_reviewed", target=ref,
                    detail={"verdict": state.get("verdict"), "summary": state.get("summary")})
            if changed:
                run["pending"] = pending
                await self._record_parked_run(p.slug, run)
            out.append(replace(p, parked_states=states, parked_run=run))
        return out

    async def _send_digests(self, products: list[h.ProductInputs], mode: str,
                            now: datetime) -> None:
        """The daily owner digest push (§5): one ``decision_needed`` line per product
        whose digest is non-empty and whose ``needs_you_automation`` is not off, at
        most once per ``digest.DIGEST_INTERVAL`` (deduped on its ``pm.digest.sent``
        audit row) and never inside quiet hours (deferred, not dropped)."""
        if self._digest is None:
            return
        on = {p.slug for p in products if h.effective_mode(
            mode, str(p.policy.get("needs_you_automation", "off"))) != "off"}
        if not on or await self._digest.in_quiet_hours(now):
            return
        last = await self._digest.last_sent()
        if not any(dg.digest_due(last.get(slug), now) for slug in on):
            return   # the composition is a full map read: skip it until a send is due
        view = await self._digest.load()
        for product in view.get("products") or ():
            slug = product.get("slug")
            items = list(product.get("items") or ())
            if slug not in on or not items or not dg.digest_due(last.get(slug), now):
                continue
            with_rec = sum(1 for i in items if i.get("recommendation"))
            title, body = dg.summary_text(slug, len(items), with_rec)
            try:
                await self._digest.notify(title, body, slug=slug, url=dg.digest_path(slug))
                await self._digest.record_sent(slug, {"slug": slug, "total": len(items),
                                                      "withRecommendation": with_rec,
                                                      "ids": [i.get("id") for i in items][:50]})
            except Exception:
                log.warning("needs_you_hygiene.digest_send_failed slug=%s", slug, exc_info=True)
                continue
            await self._ctx.emit("info", f"{slug}: sent the owner digest — {body}",
                                 kind="hygiene_digest_sent", target=slug,
                                 detail={"total": len(items), "withRecommendation": with_rec})

    async def _product(self, p: h.ProductInputs, mode: str, now: datetime,
                       counts: dict) -> None:
        eff = h.effective_mode(mode, str(p.policy.get("needs_you_automation", "off")))
        if eff == "off":
            return
        actions = h.plan(p, now=now)
        if eff == "dry_run":
            for a in h.capped(actions):
                counts["dryRun"] += 1
                if (p.slug, a.key) in self._dry_announced:
                    continue
                self._dry_announced.add((p.slug, a.key))
                await self._ctx.emit("info", f"{p.slug}: would {_describe(a)} — {a.evidence}",
                                     kind="hygiene_dry_run", target=a.ref, detail=a.to_wire())
            return
        # Recommend-only findings (the parked review) never apply, whatever the risk.
        applying = h.applies(p.policy)
        recommend = [a for a in actions if not applying or a.rule in h.RECOMMEND_ONLY_RULES]
        for a in recommend:
            counts["recommended"] += 1
            if await self._record_recommendation(p.project_id, a):
                await self._ctx.emit(
                    "info", f"{p.slug}: recommended — {_describe(a)} ({a.evidence})",
                    kind="hygiene_recommended", target=a.ref, detail=a.to_wire())
        await self._supersede(p.project_id, {a.key for a in recommend})
        if not applying:
            return
        for a in h.capped(a for a in actions if a.rule not in h.RECOMMEND_ONLY_RULES):
            failed_at = self._failed.get((p.slug, a.key))
            if failed_at is not None and now - failed_at < FAILED_RETRY:
                continue
            try:
                result = await self._apply_action(a)
            except Exception:
                log.warning("needs_you_hygiene.apply_crashed slug=%s ref=%s action=%s",
                            p.slug, a.ref, a.action, exc_info=True)
                self._failed[(p.slug, a.key)] = now
                counts["failed"] += 1
                continue
            if result.get("ok"):
                self._failed.pop((p.slug, a.key), None)
                counts["applied"] += 1
                await self._ctx.emit("info", f"{p.slug}: {_describe(a)} — {a.evidence}",
                                     kind="hygiene_applied", target=a.ref, detail=a.to_wire())
                continue
            # Expected: GitHub refused, the mirror moved, the item changed. INFO, never
            # WARNING (which auto-files an issue); backed off so a close never re-comments.
            self._failed[(p.slug, a.key)] = now
            counts["failed"] += 1
            log.info("needs_you_hygiene.refused slug=%s ref=%s action=%s step=%s: %s",
                     p.slug, a.ref, a.action, result.get("step"), result.get("error"))
            await self._ctx.emit(
                "info", f"{p.slug}: could not {_describe(a)} — {result.get('error')}",
                kind="hygiene_failed", target=a.ref,
                detail={**a.to_wire(), "step": result.get("step"), "error": result.get("error")})
