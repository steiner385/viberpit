"""Needs-you Hygiene (design ``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md``
§2): the mechanical auto-resolves — so "needs you" stops counting things a machine can
tidy — and the hourly needs-you sample.

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
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import func, select

from server.automations.base import AutomationStatus
from server.automations.context import observation_failed, publish_observation
from server.automations.engine_policy import EnginePolicy
from server.automations.product_policy import NEEDS_YOU_AUTOMATION_MODES
from server.pm import hygiene as h
from server.pm.hygiene import HygieneAction

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
         "release the hold on", h.ACTION_PRUNE_EDGE: "prune the edge"}


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


def _describe(action: HygieneAction) -> str:
    if action.action == h.ACTION_SET_STATUS:
        return f"move {action.ref} to {action.detail.get('status')}"
    return f"{_VERB.get(action.action, action.action)} {action.ref}"


def close_comment(action: HygieneAction) -> str:
    """The one-line note a hygiene close posts before closing (design §2 rule 1)."""
    return (f"Closing as completed: {action.evidence}. "
            "Reopen it if there is still work to do here.")


async def _close(action: HygieneAction, seams: IssueSeams) -> dict:
    from server.github import ci_issues
    from server.pm.refs import split_ref
    parts = split_ref(action.ref)
    if parts is None or not action.work_item_id:
        return {"ok": False, "step": "load", "error": f"{action.ref} is not a mirrored issue"}
    # Comment first, then close — ``POST /api/pm/issues/close``'s order: an issue is
    # never closed without its explanation.
    url = await asyncio.to_thread(ci_issues.comment_on_issue, parts[0], parts[1],
                                  close_comment(action), automation=True)
    if url is None:
        return {"ok": False, "step": "comment",
                "error": "GitHub rejected the comment; the issue was not closed"}
    result = await seams.pm_issue_close(action.work_item_id, {"reason": "completed"})
    return {**result, "commentUrl": url}


async def _release(action: HygieneAction, *, actor: str, via: str) -> dict:
    from server.db import db_session
    from server.pm import holds
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
    the policy-skip hold release (system holds only), and ``dep_prune.prune``.
    Answers ``{"ok", "step", "error", …}``; a refusal is data, not an exception.
    Every attempt is audited ``pm.hygiene.<rule>`` with its outcome."""
    from server.audit import record_audit
    from server.db import db_session
    if action.action == h.ACTION_CLOSE:
        result = await _close(action, seams)
    elif action.action == h.ACTION_SET_STATUS:
        result = await seams.pm_board_status(str(action.work_item_id), {
            "status": str(action.detail.get("status")), "add_if_missing": False})
    elif action.action == h.ACTION_RELEASE_HOLD:
        result = await _release(action, actor=actor, via=via)
    elif action.action == h.ACTION_PRUNE_EDGE:
        result = await _prune(action, seams, identity=identity, via=via)
    else:
        result = {"ok": False, "step": "unknown_action", "error": f"unknown {action.action!r}"}
    async with db_session() as db:
        await record_audit(db, f"pm.hygiene.{action.rule}", identity, {
            **action.to_wire(), "ok": bool(result.get("ok")), "step": result.get("step"),
            "error": result.get("error"), "commentUrl": result.get("commentUrl")}, via=via)
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
                   "already skips, and prunes resolved dependency edges. Products with "
                   "cautious risk tolerance get a recommendation to approve instead. Also "
                   "samples each product's needs-you count hourly.")
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
                 now: Callable[[], datetime] | None = None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._load = load
        self._apply_action = apply_action
        self._record_recommendation = record_recommendation
        self._supersede = supersede_recommendations
        self._record_samples = record_samples
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
        for product in await self._load():
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
        publish_observation(self._ctx, "needs_you_hygiene_counts", counts)

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
        if not h.applies(p.policy):
            for a in actions:
                counts["recommended"] += 1
                if await self._record_recommendation(p.project_id, a):
                    await self._ctx.emit(
                        "info", f"{p.slug}: recommended — {_describe(a)} ({a.evidence})",
                        kind="hygiene_recommended", target=a.ref, detail=a.to_wire())
            await self._supersede(p.project_id, {a.key for a in actions})
            return
        await self._supersede(p.project_id, set())
        for a in h.capped(actions):
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
