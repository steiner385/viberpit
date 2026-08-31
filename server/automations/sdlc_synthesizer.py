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

from server.sdlc.engine import ArtifactView, InitiativeView, Propose, next_action
from server.sdlc.prompts import artifact_path

from .base import AutomationStatus, ConfigField
from .model_pick import AUTOMATION_MODEL_CHOICES

log = logging.getLogger(__name__)

ListInitiatives = Callable[[], Awaitable[list[dict]]]
ListArtifacts = Callable[[str], Awaitable[list[dict]]]
RecordArtifact = Callable[..., Awaitable[None]]
SetStatus = Callable[[str, str], Awaitable[None]]
Spawn = Callable[[dict, str], Awaitable[Any]]
PrLookup = Callable[[dict], Awaitable["dict | None"]]
UpdateArtifact = Callable[..., Awaitable[None]]
ReadConfig = Callable[[], Awaitable["tuple[str, dict]"]]


def _to_views(ini: dict, artifacts: list[dict]) -> tuple[InitiativeView, list[ArtifactView]]:
    """Adapt the injected dict rows into the engine's frozen views. ``issue`` is
    the origin_ref — the GitHub issue the Build stage hands to backlog_runner."""
    return (
        InitiativeView(slug=str(ini.get("slug") or ""),
                       stage=str(ini.get("stage") or ""),
                       status=str(ini.get("status") or ""),
                       repo=str(ini.get("repo") or ""),
                       issue=ini.get("origin_ref")),
        [ArtifactView(kind=str(a.get("kind") or ""),
                      commit_sha=a.get("commit_sha"),
                      pr_number=a.get("pr_number"),
                      pr_url=a.get("pr_url")) for a in artifacts],
    )


class SdlcSynthesizer:
    id = "sdlc-synthesizer"
    name = "SDLC Synthesizer"
    description = "Carries initiatives intent→spec→plan as artifact PRs (propose-only v1)."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="default_model", label="Model", type="enum",
                    default="default", choices=list(AUTOMATION_MODEL_CHOICES),
                    help="Model for spawned spec/plan sessions ('auto' assesses per task)."),
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
    ) -> None:
        self._ctx = ctx
        self._list_initiatives = list_initiatives
        self._list_artifacts = list_artifacts
        self._record_artifact = record_artifact
        self._set_status = set_status
        self._spawn = spawn
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
            await self._enrich(artifacts)

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
        try:
            await self._ctx.emit("info", f"proposed {kind} for {slug}",
                                 kind="proposed", target=slug,
                                 session_id=getattr(rec, "id", None))
        except Exception:  # best-effort — the feed must not break the loop
            log.debug("sdlc_synthesizer.emit_failed", exc_info=True)
