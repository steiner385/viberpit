"""AutomationContext — the sole outward seam for an automation: activity feed,
per-target state, and (Task 4) de-duplicated session spawning."""
from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ccswitch import brand
from server.db import db_session
from server.db.models import (
    DISPATCH_KINDS,
    DISPATCH_OUTCOMES,
    AutomationEvent,
    AutomationTargetState,
    DispatchOutcome,
    WorkItem,
)
from server.pm.outcomes import (
    body_fingerprint,
    bounces_stmt,
    prior_attempts as _prior_attempts,
    project_id_for_slug,
    rows_to_bounces,
)
from server.pm.refs import split_ref

from .base import AutomationStatus

log = logging.getLogger(__name__)


def publish_observation(ctx: Any, attribute: str, counts: dict, *,
                        degraded: bool = False) -> None:
    """Publish a measured summary and its success time as one synchronous update.

    Producers call this only after a real refresh, never for disabled/skipped
    ticks. Contexts remain duck-typed, as with the existing summary attributes.
    """
    setattr(ctx, attribute, counts)
    ctx.observed_at = datetime.now(timezone.utc).isoformat()
    ctx.last_refresh_failed_at = None
    ctx.observation_degraded = degraded


def observation_failed(ctx: Any) -> None:
    """Invalidate current telemetry without losing the last successful timestamp."""
    ctx.last_refresh_failed_at = datetime.now(timezone.utc).isoformat()


def observed_status(ctx: Any, status: AutomationStatus) -> AutomationStatus:
    """Attach producer evidence without advancing freshness during UI polling."""
    observed_at = getattr(ctx, "observed_at", None)
    failed_at = getattr(ctx, "last_refresh_failed_at", None)
    if failed_at:
        return AutomationStatus(
            "Runtime observation unavailable — last refresh failed", level="unknown",
            observed_at=observed_at, last_refresh_failed_at=failed_at,
            waiting_reason=status.waiting_reason)
    if getattr(ctx, "observation_degraded", False):
        return replace(status, observed_at=observed_at,
                       level=status.level if status.level in ("error", "unknown") else "warn",
                       text=status.text + "; some observations unavailable")
    return replace(status, observed_at=observed_at)


# What an automation-spawned session runs as when nothing else says otherwise.
#
# NOT "interactive" (create_session's default, which is right for a human at a
# keyboard): an automation session has no one watching it, so the first Edit or
# Bash blocks forever on a prompt nobody will answer — a Backlog Runner needs git,
# gh and pytest, so unattended runs stalled indefinitely (#750).
#
# "full-auto" is ViberPit's OWN existing mode (PermissionRegistry.request, and the
# canonical list in web/src/session/permissionModes.ts). It approves every tool
# without prompting, INCLUDING shell. That is a deliberate, owner-approved trade:
# there is no middle tier that permits edits and shell while gating anything else —
# "auto" still prompts on both, since Bash and Edit are on the DANGEROUS denylist.
_AUTOMATION_FALLBACK_PERMISSION_MODE = "full-auto"


def _elapsed_s(started_at: datetime | None, now: datetime) -> float | None:
    """Seconds between a dispatch's start and *now*, or None when it never
    recorded a start. The ``dispatch_outcomes`` datetime columns are
    ``DateTime(timezone=True)``: Postgres hands back aware values, SQLite hands
    back naive ones, so a naive value is re-stamped UTC before the subtraction
    rather than compared against an aware ``now`` (that raises)."""
    if started_at is None:
        return None
    started = (started_at if started_at.tzinfo
               else started_at.replace(tzinfo=timezone.utc))
    return (now - started).total_seconds()


def _check_dispatch_kind(kind: str) -> str:
    """THE ``DISPATCH_KINDS`` check, for BOTH writers of the column — the
    spawn-time ``record_outcome`` insert and ``finalize_outcome``'s §13 recovery
    insert, which takes its kind from the claim row. An unknown kind is a
    programming error and raises even though the rest of each write is
    best-effort; the column is nullable free text, so nothing downstream would
    catch a value the vocabulary forbids."""
    if kind not in DISPATCH_KINDS:
        raise ValueError(f"unknown dispatch kind {kind!r}")
    return kind


async def _fingerprint_for(db, target: str) -> str | None:
    """The D8 body fingerprint for ``owner/repo#N`` off its mirror row, or None when
    the ref is unparseable or unmirrored — a row with no fingerprint can never be
    counted against a body nobody has seen. THE reading for both INSERT paths
    (spawn-time ``record_outcome`` and ``finalize_outcome``'s §13 fallback): a
    second copy would let a recovered row be counted against a different body than
    the one the dispatch actually ran on."""
    parts = split_ref(target)
    if parts is None:
        return None
    repo, number = parts
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.repo == repo, WorkItem.number == number,
        WorkItem.kind == "issue"))).scalars().first()
    return body_fingerprint(wi.title, wi.body) if wi is not None else None


def _account_defaults_for(account: str) -> dict:
    """This account's ledger defaults, or {} — the same read the human create path does.

    Mirrors server/api/sessions.py's account-default resolution (same ledger call,
    same lock) so the two cannot disagree about what an operator pinned.
    """
    from ccswitch import ledger as _ledger

    with _ledger.locked_ledger() as _led:
        return _ledger.get_account_defaults(_led, account) or {}


def resolve_permission_mode(create_kwargs: dict) -> str:
    """Permission mode for an automation-spawned session, highest precedence first.

    1. an explicit ``permission_mode`` the automation asked for
    2. the account's pinned ``defaultPermissionMode``, when an account is pinned
    3. ``full-auto``

    Step 2 is what keeps step 3 from being a blanket escalation: ``full-auto`` is a
    FALLBACK, so an operator who pins an account to a restrictive mode genuinely
    constrains the unattended sessions running on it.

    A ledger read failure degrades to the fallback rather than raising — an
    automation that cannot read a preference should still run, not crash the tick.
    """
    explicit = create_kwargs.get("permission_mode")
    if explicit:
        return str(explicit)
    account = create_kwargs.get("account")
    if account:
        try:
            pinned = _account_defaults_for(str(account)).get("defaultPermissionMode")
        except Exception:
            log.warning("automation: account-default lookup failed for %s; "
                        "falling back to %s", account,
                        _AUTOMATION_FALLBACK_PERMISSION_MODE, exc_info=True)
            pinned = None
        if pinned:
            return str(pinned)
    return _AUTOMATION_FALLBACK_PERMISSION_MODE


class AutomationContext:
    def __init__(self, automation_id: str, *,
                 create_session: Callable[..., Any] | None = None,
                 event_cap: int = 500) -> None:
        self.automation_id = automation_id
        self._create_session = create_session
        self._event_cap = event_cap

    async def emit(self, level: str, summary: str, *, kind: str | None = None,
                   target: str | None = None, session_id: str | None = None,
                   detail: dict | None = None) -> None:
        """Append one activity event, then prune to the cap. Best-effort:
        swallows + logs all errors; never propagates into the caller.

        ``session_id`` is a FK to ``sessions``, and the session an automation is
        acting on can be deleted between its decision and this write (a
        permanent-delete mid-tick). That is an expected race, not a defect: the
        event is kept without the link (the id moves to ``detail.session_gone``) instead of being lost behind a WARNING that auto-files
        an issue (#1815)."""
        try:
            try:
                await self._append(level, summary, kind, target, session_id, detail)
            except IntegrityError:
                if session_id is None:
                    raise
                await self._append(level, summary, kind, target, None,
                                   {**(detail or {}), "session_gone": session_id})
        except Exception:
            log.warning("automation emit failed (automation=%s)", self.automation_id,
                        exc_info=True)
        # Route error/warn activity through the notification rules (#602). Silent
        # unless a rule matches — enabling rules must not spam every warning. Best
        # effort: a notify failure must never propagate into the automation loop.
        # No `orch=` here: AutomationContext has no SessionOrchestrator reference at
        # all, so a matched assistant-channel watch fails open to the unconditional
        # web push only (Task 8.4) — proportionate for a narrow, already-silent-by-
        # default path (fanout_when_unconfigured=False below).
        if level in ("error", "warn"):
            try:
                from server.sessions import push as push_mod
                await push_mod.notify_event(
                    "automation_error" if level == "error" else "automation_warn",
                    f"{brand.NAME} — {self.automation_id}", summary,
                    automation=self.automation_id, fanout_when_unconfigured=False)
            except Exception:
                log.debug("automation notify_event failed", exc_info=True)

    async def _append(self, level: str, summary: str, kind: str | None,
                      target: str | None, session_id: str | None,
                      detail: dict | None) -> None:
        async with db_session() as db:
            db.add(AutomationEvent(
                automation_id=self.automation_id, level=level, summary=summary,
                kind=kind, target=target, session_id=session_id, detail=detail))
            await db.flush()
            await self._prune(db)

    async def _prune(self, db) -> None:
        ids = (await db.execute(
            select(AutomationEvent.id)
            .where(AutomationEvent.automation_id == self.automation_id)
            .order_by(AutomationEvent.ts.desc(), AutomationEvent.id.desc())
            .offset(self._event_cap)
        )).scalars().all()
        if ids:
            await db.execute(delete(AutomationEvent).where(AutomationEvent.id.in_(ids)))

    async def list_events(self, *, limit: int = 100, target: str | None = None,
                          kind: str | None = None) -> list[dict]:
        stmt = (select(AutomationEvent)
                .where(AutomationEvent.automation_id == self.automation_id)
                .order_by(AutomationEvent.ts.desc(), AutomationEvent.id.desc()))
        if target is not None:
            stmt = stmt.where(AutomationEvent.target == target)
        if kind is not None:
            stmt = stmt.where(AutomationEvent.kind == kind)
        stmt = stmt.limit(limit)
        async with db_session() as db:
            rows = (await db.execute(stmt)).scalars().all()
        return [{"ts": r.ts.isoformat(), "level": r.level, "kind": r.kind,
                 "summary": r.summary, "target": r.target, "sessionId": r.session_id,
                 "detail": r.detail}
                for r in rows]

    async def get_target_state(self, target: str) -> dict:
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))).scalar_one_or_none()
            return dict(row.state) if row else {}

    async def set_target_state(self, target: str, state: dict) -> None:
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))).scalar_one_or_none()
            if row is None:
                db.add(AutomationTargetState(
                    automation_id=self.automation_id, target=target, state=state))
            else:
                row.state = state
            await db.flush()

    async def delete_target_state(self, target: str) -> None:
        """Drop the durable state row for *target* (idempotent). Used when a target
        reaches terminal rest (merged / gave-up) so the table stays bounded."""
        async with db_session() as db:
            await db.execute(delete(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id,
                AutomationTargetState.target == target))

    async def claim_target_state(self, target: str, state: dict, *,
                                 expected: dict, source: str,
                                 work_stopped: bool = False) -> dict:
        """Acquire every canonical batch target and persist retry state atomically.

        A deliberate tap may continue an unstarted proposal/no-account claim.
        Active or ambiguous launches are joined by their existing operation, never
        replaced. Legacy coverage is reconciled before entering the transaction.
        """
        from .ownership import OwnershipConflict, ReleaseProof, StaleOwnership, acquire, owned
        from .ownership_reconciliation import reconcile_legacy_targets
        from .target_identity import resolve_targets

        targets = await resolve_targets(state.get("issues") or [target], kind="issue")
        token = expected.get("ownership_token")
        if token:
            async with owned(token) as tx:
                row = await tx.db.get(AutomationTargetState, (self.automation_id, target))
                if row is None or row.state != expected:
                    raise StaleOwnership("target changed while dispatch was deciding")
                if (set(tx.operation.targets) != {t.key for t in targets}
                        or tx.operation.owner != self.automation_id
                        or tx.operation.provenance.get("origin_ref") != target):
                    raise OwnershipConflict("existing work owns this batch")
                if work_stopped and (expected.get("_ended") or expected.get("gave_up")):
                    token = await tx.transfer(
                        owner=self.automation_id, source=source,
                        proof=ReleaseProof.WORK_STOPPED, reason="retry after observed stopped work",
                        provenance={"origin_ref": target})
                elif (source == "automatic"
                      or not (expected.get("proposed") or expected.get("waiting_account"))
                      or tx.operation.phase != "claimed"):
                    raise OwnershipConflict("existing work owns this batch")
                result = {**state, "ownership_token": token}
                row.state = result
            return result
        if expected.get("proposed"):
            existing_targets = await resolve_targets(expected.get("issues") or [target], kind="issue")
            if {t.key for t in existing_targets} != {t.key for t in targets}:
                raise OwnershipConflict("proposal covers a different batch")
        await reconcile_legacy_targets(targets)
        async with acquire(targets, owner=self.automation_id, source=source,
                           reason="issue implementation requested",
                           provenance={"origin_ref": target}) as tx:
            row = await tx.db.get(AutomationTargetState, (self.automation_id, target))
            if (dict(row.state) if row else {}) != expected:
                raise StaleOwnership("target changed while dispatch was deciding")
            # An old claimed row may hide a live native actor. Migration must
            # establish its owner, not overwrite it with a fresh dispatch.
            if ((expected.get("claimed") or expected.get("session_id"))
                    and not expected.get("gave_up")
                    and not (expected.get("proposed") and source != "automatic")):
                raise OwnershipConflict("legacy work needs ownership reconciliation")
            result = {**state, "ownership_token": tx.token}
            if row is None:
                tx.db.add(AutomationTargetState(
                    automation_id=self.automation_id, target=target, state=result))
            else:
                row.state = result
        return result

    async def reserve_target_action(self, target: str, expected: dict, *,
                                    source: str, reason: str, state: dict) -> dict | None:
        """Fence the previous actor before a workflow's external terminal action."""
        from .ownership import ReleaseProof, StaleOwnership, owned

        try:
            async with owned(expected["ownership_token"]) as tx:
                row = await tx.db.get(AutomationTargetState, (self.automation_id, target))
                if row is None or row.state != expected:
                    return None
                if tx.operation.owner == "pr-doctor" and self.automation_id != "pr-doctor":
                    from .pr_accounting import detach
                    if not await detach(tx):
                        return None
                proof = (ReleaseProof.NOT_STARTED if tx.operation.phase == "claimed"
                         else ReleaseProof.WORK_STOPPED)
                token = await tx.transfer(
                    owner=self.automation_id, source=source, proof=proof, reason=reason,
                    provenance={"origin_ref": target,
                                "implementation_session_id": expected.get("implementation_session_id")
                                or expected.get("session_id")}, action_started=True)
                result = {**state, "ownership_token": token}
                row.state = result
            return result
        except StaleOwnership:
            return None

    async def compare_target_state(self, target: str, expected: dict,
                                   state: dict | None, *,
                                   release_proof=None, reason: str = "",
                                   outcome: dict | None = None,
                                   started: dict | None = None,
                                   action_completed: bool = False) -> bool:
        """Commit a sweep/dispatch result only for its original fenced snapshot.

        No-token legacy records remain intact until reconciled. A lost operation
        is an ordinary stale decision; a database error propagates fail closed.
        """
        from .ownership import StaleOwnership, owned

        token = expected.get("ownership_token")
        if not token:
            return False
        try:
            async with owned(token) as tx:
                row = await tx.db.get(AutomationTargetState, (self.automation_id, target))
                if row is None or row.state != expected:
                    return False
                if release_proof is not None and tx.operation.owner == "pr-doctor":
                    if self.automation_id != "pr-doctor":
                        from .pr_accounting import detach
                        if not await detach(tx):
                            return False
                    elif tx.operation.provenance.get("pr_attempt"):
                        from .pr_accounting import account
                        if not await account(tx, tx.operation.provenance["origin_ref"]):
                            return False
                        if row.state != expected:
                            return False  # Accounting committed; refresh the decision.
                if started is not None:
                    try:
                        async with tx.db.begin_nested():
                            await self.record_outcome(target, _db=tx.db, **started)
                    except Exception:
                        log.warning("automation_context.outcome_start_failed target=%s", target,
                                    exc_info=True)
                if outcome is not None:
                    try:
                        async with tx.db.begin_nested():
                            await self.finalize_outcome(target, _db=tx.db, **outcome)
                    except Exception:
                        # History is best-effort; a savepoint prevents its DB
                        # failure from poisoning the fenced state transaction.
                        log.warning("automation_context.outcome_failed target=%s", target,
                                    exc_info=True)
                if action_completed:
                    tx.complete_action()
                if release_proof is not None:
                    tx.release(release_proof, reason)
                if state is None:
                    await tx.db.delete(row)
                else:
                    row.state = {**state, "ownership_token": token}
            return True
        except StaleOwnership:
            return False

    async def release_legacy_target_state(self, target: str, expected: dict) -> bool:
        """Delete a pre-ownership row (no ``ownership_token``) iff it still reads
        *expected*. The ONE write path for such rows: ``compare_target_state``
        refuses them by design (a token is the fence), and the only caller is the
        Backlog Runner sweep with the session-gone evidence it gathered first
        (``BacklogRunnerLoop._release_legacy_claim``). A row that has since been
        acquired (carries a token) or changed is left alone."""
        if expected.get("ownership_token"):
            return False
        async with db_session() as db:
            row = await db.get(AutomationTargetState, (self.automation_id, target))
            if row is None or dict(row.state) != expected:
                return False
            await db.delete(row)
        return True

    async def list_target_states(self) -> dict[str, dict]:
        """All durable target states for this automation, keyed by target."""
        async with db_session() as db:
            rows = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == self.automation_id))).scalars().all()
        return {r.target: dict(r.state) for r in rows}

    async def prior_attempts(self, target_ref: str) -> int:
        """THE ladder's ``attempt_no`` seed (spec 2026-09-18 §4, review R1 I3):
        finalised implement-dispatch history for *target_ref*, read fresh on
        every claim since the claim row itself is deleted on every bounce and
        can no longer carry the count forward on its own. The count itself lives
        in ``server/pm/outcomes.py`` beside ``last_attempt_model`` — the two
        ladder readers share one definition of "a past attempt"; this is only
        the automation-facing session wrapper.

        Best-effort by §13, exactly like ``record_outcome`` below and
        ``outcomes.tier_floor_for_dispatch``: this read sits on the CLAIM path,
        so a history failure must never stop a dispatch. A failed read answers
        0 — the attempt is still recorded and the ladder still climbs off the
        model the attempt ran on; only the human-readable count is short."""
        try:
            async with db_session() as db:
                return await _prior_attempts(db, target_ref)
        except Exception:
            log.warning("automation_context.prior_attempts_failed target=%s", target_ref,
                        exc_info=True)
            return 0

    async def bounces_for(self, target_ref: str) -> int:
        """How many bounces the QUEUE currently counts against *target_ref*'s body
        (spec 2026-09-12 D8), for the sweep's ``give_up_after_bounces`` decision.

        The same ``bounces_stmt`` + ``rows_to_bounces`` composition the queue loader
        runs, over this one ref: the sweep has to predict whether the NEXT dispatch
        would be skipped as ``needs_spec``, and predicting it off a DIFFERENT count
        (the claim's ``attempts``, which is neither fingerprint-scoped nor
        merge-excluding) would make a body edit — the documented way to re-arm the
        dial — stop re-arming the hand-off half of it.

        Counts only FINALISED rows, so the bounce currently being charged (still
        ``running`` at sweep time) is not in it: the caller adds its own.

        Best-effort by §13, like ``prior_attempts``: a failed read answers 0, which
        keeps the item on the ladder rather than handing it to a human on a DB blip.
        An unmirrored ref has no fingerprint and therefore no countable bounce."""
        try:
            async with db_session() as db:
                fingerprint = await _fingerprint_for(db, target_ref)
                if fingerprint is None:
                    return 0
                pairs = [(target_ref, fingerprint)]
                # Row -> plain tuple: ``Result.all()`` is typed ``Sequence[Row[Any]]``,
                # not structurally a ``tuple[str, str, int]`` (mypy), though it
                # unpacks as one — the same conversion ``queue_store`` makes.
                rows = [(ref, fp, n)
                        for ref, fp, n in (await db.execute(bounces_stmt(pairs))).all()]
            return rows_to_bounces(rows, pairs)[target_ref]
        except Exception:
            log.warning("automation_context.bounces_for_failed target=%s", target_ref,
                        exc_info=True)
            return 0

    async def record_outcome(self, target: str, *, product_slug: str | None,
                             session_id: str | None, model: str | None,
                             attempt_no: int, dispatch_kind: str = "implement",
                             _db=None) -> None:
        """Insert the ``running`` outcome row for a dispatch (spec §6, §3/§5). Best-effort:
        history must never block a spawn (§13).

        *dispatch_kind* records what the dispatch was FOR (``DISPATCH_KINDS``): an
        ``investigate`` dispatch never opens a PR by design, so it must never count
        as a bounce (``bounces_stmt`` filters it out). It is checked through the
        shared ``_check_dispatch_kind`` before the ``try`` below, which only guards
        the DB half.

        A new dispatch first closes any row still ``running`` for this target as
        ``superseded``: AT MOST ONE running row per (automation, target) is the
        invariant every reader leans on. Without it a double tap or a manual
        dispatch over a live claim stranded the earlier row ``running`` forever —
        a decoy for the next finalise. ``superseded`` is its own outcome and NOT a
        bounce (R3-9): the dispatch never got to answer, so counting it would let a
        double tap push an issue towards ``needs_spec`` on its own.

        Every import this needs sits at module scope: an ImportError raised inside
        the method would escape the ``try`` below, and "best effort" has to mean
        the whole write."""
        _check_dispatch_kind(dispatch_kind)
        try:
            async with (db_session() if _db is None else nullcontext(_db)) as db:
                now = datetime.now(timezone.utc)
                stale = (await db.execute(select(DispatchOutcome).where(
                    DispatchOutcome.automation_id == self.automation_id,
                    DispatchOutcome.target_ref == target,
                    DispatchOutcome.outcome == "running"))).scalars().all()
                for row in stale:
                    # ``superseded`` is the whole fact, so no ``cause`` echoes it.
                    row.outcome, row.cause = "superseded", None
                    row.ended_at, row.updated_at = now, now
                    row.duration_s = _elapsed_s(row.started_at, now)
                project_id = await project_id_for_slug(db, product_slug)
                fingerprint = await _fingerprint_for(db, target)
                db.add(DispatchOutcome(
                    automation_id=self.automation_id, project_id=project_id,
                    target_ref=target, body_fingerprint=fingerprint, session_id=session_id,
                    model=model, attempt_no=attempt_no, dispatch_kind=dispatch_kind,
                    started_at=datetime.now(timezone.utc), outcome="running"))
        except Exception:
            if _db is not None:
                raise
            log.warning("automation_context.record_outcome_failed target=%s", target,
                        exc_info=True)

    async def finalize_outcome(self, target: str, *, outcome: str, cause: str | None = None,
                               pr_number: int | None = None, pr_url: str | None = None,
                               first_pass_merge: bool | None = None,
                               upgrade_from: tuple[str, ...] = (),
                               fallback: dict | None = None, _db=None) -> None:
        """Finalise the newest ``running`` row for *target* (spec §6). An unknown
        outcome is a programming error and raises; a DB failure is logged and
        swallowed (§13).

        *upgrade_from* is the fallback for a verdict that arrives a SWEEP LATE.
        The runner's give-up branches fire on the tick that charges the last
        attempt, and the tick that charged the previous one already closed that
        same dispatch as ``no_pr`` — nothing re-dispatches in between. With no
        running row to claim, the newest row whose outcome is listed here is
        RE-finalised in place: one row per dispatch, ending at its real verdict,
        never a second bounce for one session (R3-7). ``ended_at``/``duration_s``
        are kept — the dispatch ended when its session did, not when the loop
        noticed — and the PR fields are overwritten only when given, so an upgrade
        that knows nothing new about the PR does not erase what the bounce
        recorded.

        *fallback* is spec §13's recovery for the OTHER way a row can be missing:
        the spawn-time write failed (history is best-effort, dispatch is not), so
        the dispatch really happened but left nothing behind. Given
        ``{"product_slug", "session_id", "model", "attempt_no", "dispatch_kind"}``
        — the claim's own facts, ``dispatch_kind`` defaulting to ``implement`` for a
        claim written before the kind existed — an already-FINALISED row is
        inserted here instead: the fingerprint
        read off the mirror row exactly as ``record_outcome`` reads it (or the
        bounce could never be counted against the body it bounced on),
        ``started_at`` left None because nobody knows when it began (and so
        ``duration_s`` with it), ``ended_at`` now. It is strictly a LAST resort —
        a running row, then an upgradable one, then this — so it can never add a
        second row for one dispatch. Neither a row nor a fallback → still a
        no-op."""
        if outcome not in DISPATCH_OUTCOMES:
            raise ValueError(f"unknown dispatch outcome {outcome!r}")
        # Checked HERE, beside the outcome check and outside the best-effort
        # ``try``, so the recovery insert cannot persist a kind ``record_outcome``
        # would have refused (the column is nullable free text — nothing else
        # would catch it).
        fallback_kind = (None if fallback is None else
                         _check_dispatch_kind(fallback.get("dispatch_kind") or "implement"))
        try:
            async with (db_session() if _db is None else nullcontext(_db)) as db:
                # ``id`` breaks a created_at TIE deterministically: two dispatches
                # of one target can overlap (a propose tap racing the loop's own
                # spawn, or a double tap), and without a tiebreak which of two
                # same-stamped rows gets finalised is whatever the dialect felt
                # like returning.
                def _newest(*outcomes: str):
                    return (select(DispatchOutcome).where(
                        DispatchOutcome.automation_id == self.automation_id,
                        DispatchOutcome.target_ref == target,
                        DispatchOutcome.outcome.in_(outcomes))
                        .order_by(DispatchOutcome.created_at.desc(),
                                  DispatchOutcome.id.desc()).limit(1))

                row = (await db.execute(_newest("running"))).scalars().first()
                upgrade = row is None
                if upgrade and upgrade_from:
                    row = (await db.execute(
                        _newest(*upgrade_from))).scalars().first()
                now = datetime.now(timezone.utc)
                if row is None and fallback is not None:
                    # §13: the spawn-time write was lost — insert the dispatch's
                    # row here, already finalised, so the bounce is not forgotten.
                    #
                    # Guarded by the DISPATCH's identity, which is its session: the
                    # sweep re-runs its "ended with no PR" branch on every tick
                    # until the attempt cap, and each of those finalises found
                    # nothing and was a harmless no-op before. Unguarded, they
                    # would mint a fresh bounce per tick and walk the issue to
                    # ``needs_spec`` on one dispatch. A claim with no recorded
                    # session matches the same way (NULL to NULL), i.e. at most
                    # one recovered row per target for it.
                    sid = fallback.get("session_id")
                    same_session = (DispatchOutcome.session_id.is_(None) if sid is None
                                    else DispatchOutcome.session_id == sid)
                    dispatched = (await db.execute(select(DispatchOutcome.id).where(
                        DispatchOutcome.automation_id == self.automation_id,
                        DispatchOutcome.target_ref == target,
                        same_session))).first()
                    if dispatched is not None:
                        log.info(
                            "automation_context.finalize_outcome_already_recorded target=%s",
                            target)
                        return
                    db.add(DispatchOutcome(
                        automation_id=self.automation_id,
                        project_id=await project_id_for_slug(db, fallback.get("product_slug")),
                        target_ref=target,
                        body_fingerprint=await _fingerprint_for(db, target),
                        session_id=fallback.get("session_id"),
                        model=fallback.get("model"),
                        attempt_no=fallback.get("attempt_no"),
                        dispatch_kind=fallback_kind,
                        started_at=None, ended_at=now, updated_at=now,
                        outcome=outcome, cause=cause,
                        pr_number=pr_number, pr_url=pr_url,
                        first_pass_merge=first_pass_merge))
                    log.info("automation_context.finalize_outcome_recovered_row target=%s",
                             target)
                    return
                if row is None:
                    log.info("automation_context.finalize_outcome_no_running_row target=%s",
                             target)
                    return
                row.outcome, row.cause = outcome, cause
                if pr_number is not None:
                    row.pr_number = pr_number
                if pr_url is not None:
                    row.pr_url = pr_url
                if first_pass_merge is not None:
                    row.first_pass_merge = first_pass_merge
                row.updated_at = now
                if not upgrade:
                    # A live dispatch ends NOW; an upgrade keeps the end it had.
                    row.ended_at = now
                    row.duration_s = _elapsed_s(row.started_at, now)
        except Exception:
            if _db is not None:
                raise
            log.warning("automation_context.finalize_outcome_failed target=%s", target,
                        exc_info=True)

    async def _existing_session(self, key: str):
        """The live session already spawned for *key*, or None.

        Split out of spawn_session so the spawn path's resolution logic is testable
        without standing up a database.
        """
        from server.db.models import INACTIVE_SESSION_STATUSES, Session
        async with db_session() as db:
            return (await db.execute(select(Session).where(
                Session.origin == self.automation_id,
                Session.origin_ref == key,
                Session.status.not_in(INACTIVE_SESSION_STATUSES)))).scalars().first()

    async def launch_plan(self) -> tuple[Any, dict]:
        """``(engine, config)`` this automation's next session would launch with.

        THE engine decision for every automation (Phase 7, Task 7.1). The policy
        ceiling is read off the registry by ``automation_id`` rather than threaded
        through a dozen construction sites, so there is one answer per origin and
        nothing to keep in sync.

        Public because a caller may need the engine BEFORE it can build its seed
        prompt — the prompt states a commit trailer, and a Codex session must not
        be told to co-author its commits as Claude. Such a caller passes the
        engine back in through ``spawn_session(engine=...)`` so the decision is
        made once.

        Raises ``EnginePolicyViolation`` when the only engine this origin is
        audited for cannot be launched right now.
        """
        from server.config import get_settings

        from .config_store import read_effective_config
        from .engine_policy import DEFAULT_ENGINE_POLICY, policy_for, resolve_engine
        from .registry import registry

        try:
            policy = policy_for(registry.get(self.automation_id))
        except KeyError:  # an ad-hoc origin is unaudited by definition
            policy = DEFAULT_ENGINE_POLICY
        config = await read_effective_config(self.automation_id)
        engine = resolve_engine(
            policy, config,
            automations_enabled=bool(
                getattr(get_settings(), "codex_automations_enabled", False)))
        return engine, config

    async def launch_plan_or_refuse(self, origin_ref: str) -> tuple[Any, dict] | None:
        """``launch_plan()``, or ``None`` after recording why the launch is refused.

        The single refusal path, so a caller that resolves the engine early (to
        build its seed prompt) and ``spawn_session`` itself report an out-of-policy
        launch identically."""
        from .engine_policy import EnginePolicyViolation
        try:
            return await self.launch_plan()
        except EnginePolicyViolation as exc:
            await self.emit("error", f"refused spawn for {origin_ref}: {exc.reason}",
                            kind="engine_policy_refused", target=origin_ref,
                            detail={"policy": exc.policy.value})
            return None

    async def spawn_session(self, *, origin_ref: str, dedupe_key: str | None = None,
                            detail: dict | None = None, engine: Any = None,
                            ownership_token: str | None = None,
                            **create_kwargs) -> Any:
        """Find-or-create a session for this automation. If a NON-terminal session
        with the same (origin, dedupe_key) already exists, return it and emit a
        'skipped_duplicate' event instead of creating a second. Otherwise create
        via the injected create_session (which stamps origin/origin_ref), then emit
        a linked 'spawned' event (carrying the optional ``detail`` payload).

        *engine* is an already-resolved answer from ``launch_plan``, for a caller
        that needed it to build the seed prompt; leave it unset and this resolves
        it. Returns ``None`` without spawning when the origin's engine policy
        refuses the launch, when the resolved engine/permission-mode pair
        could not complete an automation's contract (see
        ``unattended_launch_blocker``), or when the fleet has no capacity right now
        (``NoHostAvailable`` — logged as an "info" event, not raised: the caller's
        own schedule retries next tick). Every caller must treat ``None`` as
        "no session exists" and release only a proven unstarted claim.

        With ``ownership_token``, the create boundary joins only the operation's
        reserved session. Exceptions can mean a launch is still running: retain
        the claim for reconciliation, never release it based on a missing row.
        """
        key = dedupe_key or origin_ref
        # Claimed launches are joined only by the durable reserved UUID at the
        # create_session boundary, never by the historical origin/ref lookup.
        existing = await self._existing_session(key) if ownership_token is None else None
        if existing is not None:
            await self.emit("info", f"skipped duplicate spawn for {key}",
                            kind="skipped_duplicate", target=key, session_id=existing.id)
            return existing
        if self._create_session is None:
            raise RuntimeError("AutomationContext has no create_session bound")
        # Engine BEFORE permission mode and before any create_session work: the
        # engine decides which of the caller's launch inputs survive, and a
        # refusal must cost nothing.
        from .engine_policy import (
            sanitize_launch_for_engine,
            unattended_launch_blocker,
        )
        plan = await self.launch_plan_or_refuse(origin_ref)
        if plan is None:
            return None
        resolved, config = plan
        engine = resolved if engine is None else engine
        _kwargs, dropped = sanitize_launch_for_engine(engine, create_kwargs, config=config)
        if dropped:
            # Visible, not silent: the plan forbids quietly routing a Claude-picked
            # account or a Claude model alias into another engine's runner.
            await self.emit(
                "info",
                f"dropped {', '.join(dropped)} — incompatible with engine {engine.value}",
                kind="engine_launch_adjusted", target=origin_ref,
                detail={"engine": engine.value, "dropped": list(dropped)})
        create_kwargs["engine"] = engine
        # An automation session has nobody watching it, so it must not inherit
        # create_session's human-oriented "interactive" default (#750). Resolve the
        # mode the way the human path does: explicit choice → account default →
        # full-auto fallback (see resolve_permission_mode). acceptEdits is NOT used
        # as the automation default: a Backlog Runner's core work is shell (git, gh,
        # pytest), which acceptEdits still gates — so it would stall just the same.
        mode = resolve_permission_mode(create_kwargs)
        create_kwargs["permission_mode"] = mode
        # An automation dispatch has no creating browser, so client_node is always
        # None — resolve_display_mode's locality proof can never fire for it, and
        # every automation session fell back to a pooled Xvfb display even when
        # dispatched on a runner sitting at an attended desktop (#1899). Unlike a
        # browser session (where None means "locality unknown, possibly remote" —
        # e.g. behind `tailscale serve` fronting, see
        # docs/product/features/remote-desktop-vnc-x11.md), None here always means
        # "no browser, ever" — a request-level "native" needs no locality proof
        # (display_policy.resolve_display_mode), so this is safe without touching
        # that shared decision. ``native_available`` still gates it per-host: a
        # runner with no desktop attached still gets virtual. A caller that already
        # has an opinion (none does today) keeps it.
        create_kwargs.setdefault("display", "native")
        # Last gate before the session exists: the engine/mode pair has to be able
        # to produce the one output an automation has (a pushed branch and a PR).
        # Refusing here costs nothing; discovering it at `git push` costs the whole
        # run plus a concurrency slot (Phase 7, Tasks 7.2/7.3).
        blocker = unattended_launch_blocker(engine, mode)
        if blocker is not None:
            await self.emit("error", f"refused spawn for {origin_ref}: {blocker}",
                            kind="engine_launch_refused", target=origin_ref,
                            detail={"engine": engine.value, "permissionMode": mode})
            return None
        if ownership_token is not None:
            create_kwargs["ownership_token"] = ownership_token
        from server.sessions.fleet_router import NoHostAvailable
        try:
            rec = await self._create_session(origin=self.automation_id, origin_ref=origin_ref,
                                             **create_kwargs)
        except NoHostAvailable as exc:
            # The fleet is momentarily saturated — routine and self-healing (the
            # automation's own schedule retries next tick regardless), not a code
            # defect. Left uncaught, this is the ONE shared spawn path every
            # automation uses, so it escaped as a raw exception all the way to each
            # caller's outer `except Exception`, which logs at WARNING and files an
            # auto-error for a condition that clears itself (#2209). `emit("info", …)`
            # records it without routing through notify_event's warn/error channel.
            await self.emit("info", f"spawn deferred for {origin_ref}: {exc}",
                            kind="spawn_deferred", target=origin_ref,
                            detail={"reason": "fleet_at_capacity"})
            return None
        await self.emit("info", f"spawned session for {origin_ref}",
                        kind="spawned", target=origin_ref,
                        session_id=getattr(rec, "id", None), detail=detail)
        return rec
