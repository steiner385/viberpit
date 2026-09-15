"""SDLC Synthesizer automation (#800): intent→spec→plan artifact PRs.

Carries initiatives through the design and plan stages by proposing spec and
plan artifacts. Enrichment (pr_number/commit_sha fill-in) is handled here for
ALL artifact kinds so there is exactly one writer (constitution VI).

The loop's mode is ``off | on`` (spec 2026-09-14 D8): whether a gate resolves
itself is the product's ``risk_tolerance``, not a global mode. Under
``cautious`` an initiative parks at ``awaiting_human`` after its artifact
session exactly as before. All I/O is injected (fakes in tests, orchestrator
in prod) — the ``BacklogRunnerLoop`` idiom."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import datetime, timezone
from typing import Any

from server.sdlc.engine import (
    STAGE_ARTIFACT, Advance, ArtifactView, AwaitApproval, InitiativeView, Materialize,
    MergeArtifact, NeedsHuman, Propose, next_action)
from server.sdlc.gates import auto_gate_record, effective_tolerance
from server.sdlc.prompts import artifact_path

from .base import AutomationStatus, ConfigField
from .context import observation_failed, publish_observation
from .model_pick import AUTOMATION_MODEL_CHOICES
from .engine_policy import EnginePolicy
from .product_policy import effective_policy

log = logging.getLogger(__name__)

#: How many decompositions one initiative may spend before the loop stops trying
#: (C4-R19c). A decompose session that keeps producing unreadable output now ends
#: as a rejected proposal rather than a blocked initiative, which is what lets the
#: next tick try again — so something has to say when "again" stops. Three attempts
#: is the same shape as the reconciler's park-on-the-second-failure rule: enough to
#: ride out a bad turn, few enough that a plan the model cannot decompose costs
#: three sessions, not a session every tick forever.
MATERIALIZE_MAX_ATTEMPTS = 3

#: Modes the row could carry before spec 2026-09-14 D8 folded them into ``on``.
LEGACY_SYNTH_MODES: tuple[str, ...] = ("propose", "auto")


def fold_synth_mode(mode: str) -> str:
    """ONE fold, shared by the read path (``_sdlc_synth_read_config``) and the
    first-tick row rewrite (``_sdlc_synth_fold_stored_mode``). A stored ``off``
    survives verbatim: it is a human's explicit park."""
    return "on" if mode in LEGACY_SYNTH_MODES else mode


ListInitiatives = Callable[[], Awaitable[list[dict]]]
ListArtifacts = Callable[[str], Awaitable[list[dict]]]
RecordArtifact = Callable[..., Awaitable[None]]
SetStatus = Callable[[str, str], Awaitable[None]]
Spawn = Callable[[dict, str], Awaitable[Any]]
SpawnMaterialize = Callable[[dict], Awaitable[Any]]
HasPendingMaterialization = Callable[[str], Awaitable[bool]]
MaterializationAttempts = Callable[[str], Awaitable[int]]
PrLookup = Callable[[dict], Awaitable["dict | None"]]
UpdateArtifact = Callable[..., Awaitable[None]]
ReadConfig = Callable[[], Awaitable["tuple[str, dict]"]]
ReadPolicies = Callable[[], Awaitable["dict[str, dict]"]]
LiveSessions = Callable[[Sequence[str]], Awaitable["set[str]"]]
#: (initiative_id, from_stage, to_stage, last_auto_gate record) → advanced?
#: False means the compare-and-set found a newer human decision (spec §15).
AdvanceStage = Callable[[str, str, str, dict], Awaitable[bool]]
#: The process-wide kill switch (spec D9), read per tick.
AutoresolveEnabled = Callable[[], bool]


def _to_views(ini: dict, artifacts: list[dict]) -> tuple[InitiativeView, list[ArtifactView]]:
    """Adapt the injected dict rows into the engine's frozen views. ``issue`` is
    the origin_ref — the GitHub issue the Build stage hands to backlog_runner;
    ``epic`` folds the epic pointer into the ``owner/repo#N`` ref the engine
    tests for when deciding whether a merged plan still needs materializing."""
    return (
        InitiativeView(slug=str(ini.get("slug") or ""),
                       stage=str(ini.get("stage") or ""),
                       status=str(ini.get("status") or ""),
                       repo=str(ini.get("repo") or ""),
                       issue=ini.get("origin_ref"),
                       epic=(f"{ini['epic_repo']}#{ini['epic_number']}"
                             if ini.get("epic_number") else None),
                       epic_total=int(ini.get("epic_total") or 0),
                       epic_done=int(ini.get("epic_done") or 0)),
        [ArtifactView(kind=str(a.get("kind") or ""),
                      commit_sha=a.get("commit_sha"),
                      pr_number=a.get("pr_number"),
                      pr_url=a.get("pr_url")) for a in artifacts],
    )


def is_producing(stage: str | None, artifacts: list[dict], live: Collection[str]) -> bool:
    """Whether an initiative currently holds a front-chain slot (spec 2026-09-14
    §6, amended after the phase 1 review): its CURRENT stage is ``design`` or
    ``plan``, that stage's artifact row is unmerged (``commit_sha is None``) AND
    that row's session is still live.

    Everything the rule excludes, it excludes on purpose. ``intake`` never counts
    — sdlc-intake's own daily budget governs it, and letting an open intent PR
    hold a slot starves the spec/plan work this cap exists to pace. A session that
    died without opening a PR never leaks a slot: nothing else would ever clear
    its ``commit_sha is None`` row. And an open artifact PR waiting on a human
    merge does not count either: under ``cautious`` the cap bounds concurrent
    machine work, never the human merge queue. (A ``blocked`` initiative is
    excluded by the caller, which is where status lives.)"""
    kind = STAGE_ARTIFACT.get(stage or "")
    if kind is None or stage not in ("design", "plan"):
        return False
    return any(a.get("kind") == kind and a.get("commit_sha") is None
               and a.get("session_id") in live for a in artifacts)


class SdlcSynthesizer:
    id = "sdlc-synthesizer"
    name = "SDLC Synthesizer"
    work_class = "development"
    description = ("Carries initiatives intent→spec→plan as artifact PRs; resolves stage "
                   "gates under each product's risk tolerance.")
    modes: tuple[str, ...] = ("off", "on")
    # Audited engine ceiling (Phase 7, Task 7.3; widened by #1266). Same gate and
    # same evidence as sdlc-intake — the offline suite in
    # `server/tests/test_sdlc_codex_acceptance.py` plus the live intent → spec →
    # plan run in `server/tests/test_sdlc_codex_live_artifact.py`.
    #
    # This ceiling is independent of sdlc-intake's on purpose: the handoff between
    # stages is the committed artifact FILE, not the producing session, so an
    # initiative whose intent ran on Claude can have its spec run on Codex and
    # neither stage inherits the other's engine.
    #
    # `codex_allowed` grants the option, not the traffic; see the sdlc_intake
    # comment for the three gates that still ship off.
    engine_policy: EnginePolicy = EnginePolicy.CODEX_ALLOWED
    config_schema = [
        ConfigField(key="default_model", label="Model", type="enum",
                    default="default", choices=list(AUTOMATION_MODEL_CHOICES),
                    help="Model for spawned spec/plan/decompose sessions "
                         "('auto' assesses per task)."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "sdlc_synthesizer_counts", None)
        if counts is None:
            return AutomationStatus(text="Initiative observations unavailable", level="unknown")
        awaiting = counts["awaiting"]
        proposed = counts["proposed"]
        queued = counts.get("queued", 0)
        auto_resolved = counts.get("auto_resolved", 0)
        text = f"{proposed} proposed this refresh · {awaiting} awaiting human action"
        if auto_resolved:
            text += f" · {auto_resolved} auto-resolved"
        if queued:
            text += f" · {queued} queued behind the cap"
        return AutomationStatus(
            text=text,
            level="active" if proposed else "idle", counts=counts,
            waiting_reason="human" if awaiting else None)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


class SdlcSynthesizerLoop:
    """One tick: for every Initiative the engine wants a ``spec`` or ``plan``
    proposed, spawn the artifact session (deduped downstream), record the
    artifact index row, and park at ``awaiting_human``. Also enriches all
    artifacts with PR metadata once the session's PR exists."""

    def __init__(
        self,
        ctx: Any,
        *,
        list_initiatives: ListInitiatives,
        list_artifacts: ListArtifacts,
        record_artifact: RecordArtifact,
        set_status: SetStatus,
        spawn: Spawn,
        pr_lookup: PrLookup,
        update_artifact: UpdateArtifact,
        read_config: ReadConfig,
        clock: Callable[[], float],
        spawn_materialize: SpawnMaterialize | None = None,
        has_pending_materialization: HasPendingMaterialization | None = None,
        materialization_attempts: MaterializationAttempts | None = None,
        read_policies: ReadPolicies | None = None,
        live_sessions: LiveSessions | None = None,
        advance_stage: AdvanceStage | None = None,
        autoresolve_enabled: AutoresolveEnabled | None = None,
    ) -> None:
        self._ctx = ctx
        self._list_initiatives = list_initiatives
        self._list_artifacts = list_artifacts
        self._record_artifact = record_artifact
        self._set_status = set_status
        self._spawn = spawn
        self._spawn_materialize = spawn_materialize
        self._has_pending_materialization = has_pending_materialization
        self._materialization_attempts = materialization_attempts
        #: slugs already warned about a missing materialize seam — once per
        #: initiative, or an unwired deployment would warn on every tick forever.
        self._materialize_unwired: set[str] = set()
        #: slugs already warned about spending their decomposition attempts. The
        #: `blocked` write below normally stops the second tick from ever asking,
        #: but a human who un-blocks without fixing anything must not get the warn
        #: on every tick from then on.
        self._materialize_exhausted: set[str] = set()
        self._pr_lookup = pr_lookup
        self._update_artifact = update_artifact
        self._read_config = read_config
        self._read_policies = read_policies
        self._live_sessions = live_sessions
        self._advance_stage = advance_stage
        #: No seam = no switch = `cautious` everywhere (fail-closed: an unwired
        #: deployment must never resolve a gate by accident).
        self._autoresolve_enabled = autoresolve_enabled
        #: slugs already warned that the engine wanted an advance this
        #: deployment cannot write — once, like the materialize seam.
        self._advance_unwired: set[str] = set()
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:
                observation_failed(self._ctx)
                log.warning("sdlc_synthesizer.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, _config = await self._read_config()
        if mode == "off":
            return
        initiatives = await self._list_initiatives()
        policies = await self._read_policies() if self._read_policies is not None else {}
        # The kill switch is read once per tick (spec §15: a flip takes effect on
        # the next tick, nothing in flight is interrupted).
        enabled = bool(self._autoresolve_enabled()) if self._autoresolve_enabled is not None else False
        counts = {"initiatives": len(initiatives), "proposed": 0, "awaiting": 0,
                  "queued": 0, "auto_resolved": 0}
        # Front-chain slots per product, counted before any spawn this tick so
        # the cap is judged against the whole pool, not the rows visited so far.
        artifacts_by_id = {ini["id"]: await self._list_artifacts(ini["id"]) for ini in initiatives}
        # One batched liveness read for every unmerged artifact session: a slot is
        # a session this daemon still holds, never a dead one (see `is_producing`).
        sids = [a["session_id"] for arts in artifacts_by_id.values() for a in arts
                if a.get("session_id") and a.get("commit_sha") is None]
        live = await self._live_sessions(sids) if self._live_sessions is not None and sids else set()
        producing: dict[str | None, int] = {}
        for ini in initiatives:
            if ini.get("status") == "blocked":
                continue          # the loop refuses to act on it, so it is not *actively* producing
            if is_producing(ini.get("stage"), artifacts_by_id[ini["id"]], live):
                key = ini.get("project_slug")
                producing[key] = producing.get(key, 0) + 1
        #: products already told about their ceiling this tick — five queued rows
        #: are one operator-visible fact, not five.
        warned: set[str | None] = set()
        for ini in initiatives:
            artifacts = artifacts_by_id[ini["id"]]
            state, arts = _to_views(ini, artifacts)
            key = ini.get("project_slug")
            policy = effective_policy(policies.get(key) if key else None)
            tolerance = effective_tolerance(policy.get("risk_tolerance"), enabled=enabled)
            action = next_action(state, arts, mode, tolerance)
            if isinstance(action, AwaitApproval | MergeArtifact) or (
                    isinstance(action, NeedsHuman)
                    and (state.status == "blocked" or state.stage == "build")):
                # `MergeArtifact` counts as awaiting until the arm-auto-merge
                # seam lands (phase 2b): the PR still needs a human merge today.
                counts["awaiting"] += 1
            if isinstance(action, Advance):
                if await self._advance(ini, state, action, tolerance):
                    counts["auto_resolved"] += 1
                else:
                    counts["awaiting"] += 1
            if isinstance(action, Propose) and action.kind in ("spec", "plan"):
                cap = int(policy["max_initiatives_in_flight"])
                if producing.get(key, 0) >= cap:
                    counts["queued"] += 1
                    if key not in warned:
                        warned.add(key)
                        await self._emit(
                            "info", f"{key or 'unassigned'}: front-chain cap {cap} "
                                    f"reached; queuing {ini.get('slug') or ''}",
                            kind="ceiling", target=str(ini.get("slug") or ""))
                else:
                    rec = await self._spawn(ini, action.kind)
                    if rec is not None:
                        producing[key] = producing.get(key, 0) + 1
                        slug = str(ini.get("slug") or "")
                        await self._record_artifact(
                            initiative_id=ini["id"], kind=action.kind,
                            repo_path=artifact_path(slug, action.kind),
                            session_id=getattr(rec, "id", None),
                            model=getattr(rec, "model", None))
                        await self._set_status(ini["id"], "awaiting_human")
                        await self._emit_proposed(slug, action.kind, rec)
                        counts["proposed"] += 1
            elif isinstance(action, Materialize):
                outcome = await self._materialize(ini)
                if outcome is not None:
                    counts[outcome] += 1
            await self._enrich(artifacts)
        publish_observation(self._ctx, "sdlc_synthesizer_counts", counts)

    async def _advance(self, ini: dict, state: InitiativeView, action: Advance,
                       tolerance: str) -> bool:
        """Execute the engine's ``Advance`` (spec §6): the gate the tolerance
        covered is ``stage.advance_built`` from ``build`` and ``stage.advance``
        elsewhere. The write is the injected compare-and-set; ``False`` from it
        means a human moved the row first (§15) and the loop just moves on.
        Returns whether a gate was resolved this tick."""
        slug = str(ini.get("slug") or "")
        if self._advance_stage is None:
            if slug not in self._advance_unwired:
                self._advance_unwired.add(slug)
                await self._emit("warn", f"{slug}: the stage would advance under "
                                 f"{tolerance} but stage advance is not wired — advance it by hand",
                                 kind="advance_unwired", target=slug)
            return False
        gate_id = "stage.advance_built" if state.stage == "build" else "stage.advance"
        record = auto_gate_record(gate_id, tolerance, datetime.now(timezone.utc),
                                  detail={"from": state.stage, "to": action.to_stage})
        advanced = await self._advance_stage(ini["id"], state.stage, action.to_stage, record)
        if not advanced:
            log.info("sdlc_synthesizer.advance_stale slug=%s from=%s", slug, state.stage)
            return False
        await self._emit("info", f"{slug}: advanced {state.stage} → {action.to_stage} "
                         f"under {tolerance} (Step back reverses it)",
                         kind="auto_resolved", target=slug,
                         detail={"gate": gate_id, "tolerance": tolerance,
                                 "from": state.stage, "to": action.to_stage})
        return True

    async def _materialize(self, ini: dict) -> str | None:
        """Spawn the decompose session for a merged plan (spec 003 §7b).

        The park signal here is NOT ``Initiative.status``: the plan Propose
        already parked the initiative at ``awaiting_human`` and only the human
        Advance tap unparks it — past the plan stage — so a status gate would
        mean no plan is ever materialized. What "already handled" actually means
        is a pending ``MaterializationProposal`` (waiting on the human gate) or a
        live decompose session; the first is asked here, the second inside the
        spawn seam, which answers None when its own session is still running so a
        tick during the decompose neither re-parks, re-emits nor re-chains.

        The status write after a real spawn is kept (idempotent — it is normally
        already parked) so a materialization reached from any other status still
        reads as awaiting the human."""
        slug = str(ini.get("slug") or "")
        if self._spawn_materialize is None:
            if slug not in self._materialize_unwired:
                self._materialize_unwired.add(slug)
                await self._emit("warn", f"{slug}: plan is merged but plan "
                                 "materialization is not wired — decompose it by hand",
                                 kind="materialization_unwired", target=slug)
            return "awaiting"
        if (self._has_pending_materialization is not None
                and await self._has_pending_materialization(ini["id"])):
            return "awaiting"
        # Attempts are counted only once nothing is pending: a proposal waiting on
        # the human gate is not a spent attempt, it is the outcome this loop wanted.
        if self._materialization_attempts is not None:
            attempts = await self._materialization_attempts(ini["id"])
            if attempts >= MATERIALIZE_MAX_ATTEMPTS:
                if slug not in self._materialize_exhausted:
                    self._materialize_exhausted.add(slug)
                    await self._emit(
                        "warn", f"{slug}: {attempts} decomposition attempts have "
                                "produced nothing to review — decompose the plan by "
                                "hand, or clear the rejected proposals to retry",
                        kind="materialization_attempts_exhausted", target=slug,
                        detail={"attempts": attempts,
                                "max": MATERIALIZE_MAX_ATTEMPTS})
                # Blocked is the right end state HERE (unlike a single parse
                # failure): the loop has exhausted what it can do by itself, so it
                # hands the initiative to a human instead of spending a session a
                # tick on a plan it cannot decompose.
                await self._set_status(ini["id"], "blocked")
                return "awaiting"
        rec = await self._spawn_materialize(ini)
        if rec is None:  # no headroom / refused launch — retry next tick
            return None
        await self._set_status(ini["id"], "awaiting_human")
        sid = getattr(rec, "id", None)
        await self._emit("info", f"decomposing the merged plan for {slug} (session {sid})",
                         kind="materialization_proposed", target=slug, session_id=sid)
        return "proposed"

    async def _enrich(self, artifacts: list[dict]) -> None:
        """Fill pr_number/pr_url once the artifact session's PR exists, and
        commit_sha once it merges. Owns enrichment for ALL kinds (incl. intent)
        so there is exactly one writer (constitution VI)."""
        for art in artifacts:
            if not art.get("session_id") or art.get("commit_sha"):
                continue
            info = await self._pr_lookup(art)
            if info is None:
                continue
            if art.get("pr_number") is None and info.get("number") is not None:
                await self._update_artifact(art["id"], pr_number=info["number"],
                                            pr_url=info.get("url"))
            elif info.get("state") == "merged" and info.get("sha"):
                await self._update_artifact(art["id"], commit_sha=info["sha"])

    async def _emit_proposed(self, slug: str, kind: str, rec: Any) -> None:
        await self._emit("info", f"proposed {kind} for {slug}", kind="proposed",
                         target=slug, session_id=getattr(rec, "id", None))

    async def _emit(self, level: str, summary: str, **kw: Any) -> None:
        try:
            await self._ctx.emit(level, summary, **kw)
        except Exception:  # best-effort — the feed must not break the loop
            log.debug("sdlc_synthesizer.emit_failed", exc_info=True)
