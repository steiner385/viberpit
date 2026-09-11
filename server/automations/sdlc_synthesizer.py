"""SDLC Synthesizer automation (#800): intent→spec→plan artifact PRs.

Carries initiatives through the design and plan stages by proposing spec and
plan artifacts. Enrichment (pr_number/commit_sha fill-in) is handled here for
ALL artifact kinds so there is exactly one writer (constitution VI).

Propose-only v1: after spawning the artifact session the Initiative parks at
``awaiting_human`` until the human advance tap. All I/O is injected (fakes in
tests, orchestrator in prod) — the ``BacklogRunnerLoop`` idiom."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from server.sdlc.engine import (
    ArtifactView, InitiativeView, Materialize, Propose, next_action)
from server.sdlc.prompts import artifact_path

from .base import AutomationStatus, ConfigField
from .model_pick import AUTOMATION_MODEL_CHOICES
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)

#: How many decompositions one initiative may spend before the loop stops trying
#: (C4-R19c). A decompose session that keeps producing unreadable output now ends
#: as a rejected proposal rather than a blocked initiative, which is what lets the
#: next tick try again — so something has to say when "again" stops. Three attempts
#: is the same shape as the reconciler's park-on-the-second-failure rule: enough to
#: ride out a bad turn, few enough that a plan the model cannot decompose costs
#: three sessions, not a session every tick forever.
MATERIALIZE_MAX_ATTEMPTS = 3

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
                             if ini.get("epic_number") else None)),
        [ArtifactView(kind=str(a.get("kind") or ""),
                      commit_sha=a.get("commit_sha"),
                      pr_number=a.get("pr_number"),
                      pr_url=a.get("pr_url")) for a in artifacts],
    )


class SdlcSynthesizer:
    id = "sdlc-synthesizer"
    name = "SDLC Synthesizer"
    work_class = "development"
    description = "Carries initiatives intent→spec→plan as artifact PRs (propose-only v1)."
    modes: tuple[str, ...] = ("off", "propose", "auto")
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
        return AutomationStatus(text="proposes spec/plan artifacts for in-progress initiatives")

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
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:
                log.warning("sdlc_synthesizer.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        initiatives = await self._list_initiatives()
        for ini in initiatives:
            artifacts = await self._list_artifacts(ini["id"])
            state, arts = _to_views(ini, artifacts)
            action = next_action(state, arts, mode, config)
            if isinstance(action, Propose) and action.kind in ("spec", "plan"):
                rec = await self._spawn(ini, action.kind)
                if rec is not None:
                    slug = str(ini.get("slug") or "")
                    await self._record_artifact(
                        initiative_id=ini["id"], kind=action.kind,
                        repo_path=artifact_path(slug, action.kind),
                        session_id=getattr(rec, "id", None),
                        model=getattr(rec, "model", None))
                    await self._set_status(ini["id"], "awaiting_human")
                    await self._emit_proposed(slug, action.kind, rec)
            elif isinstance(action, Materialize):
                await self._materialize(ini)
            await self._enrich(artifacts)

    async def _materialize(self, ini: dict) -> None:
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
            return
        if (self._has_pending_materialization is not None
                and await self._has_pending_materialization(ini["id"])):
            return
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
                return
        rec = await self._spawn_materialize(ini)
        if rec is None:  # no headroom / refused launch — retry next tick
            return
        await self._set_status(ini["id"], "awaiting_human")
        sid = getattr(rec, "id", None)
        await self._emit("info", f"decomposing the merged plan for {slug} (session {sid})",
                         kind="materialization_proposed", target=slug, session_id=sid)

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
