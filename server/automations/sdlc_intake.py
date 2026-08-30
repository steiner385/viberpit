"""SDLC Intake automation (#799): trigger → committed ``intent.md`` PR.

v1 triggers are human-created Initiatives (``POST /api/sdlc/initiatives``);
Phase 2 (#804) adds triage items. Propose-only: after spawning the artifact
session the Initiative parks at ``awaiting_human`` until the human advance tap.
All I/O is injected (fakes in tests, orchestrator in prod) — the
``BacklogRunnerLoop`` idiom."""
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


class SdlcIntake:
    id = "sdlc-intake"
    name = "SDLC Intake"
    description = "Turns triggers into committed intent.md artifact PRs (propose-only v1)."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="default_model", label="Model", type="enum",
                    default="default", choices=list(AUTOMATION_MODEL_CHOICES),
                    help="Model for spawned intent sessions ('auto' assesses per task)."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        return AutomationStatus(text="proposes intent.md artifacts for new initiatives")

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        return None


class SdlcIntakeLoop:
    """One tick: for every Initiative the engine wants an ``intent`` proposed
    for, spawn the artifact session (deduped downstream on ``<slug>:intent``),
    record the artifact index row, and park at ``awaiting_human``."""

    def __init__(
        self,
        ctx: Any,
        *,
        list_initiatives: ListInitiatives,
        list_artifacts: ListArtifacts,
        record_artifact: RecordArtifact,
        set_status: SetStatus,
        spawn: Spawn,
        read_config: ReadConfig,
        clock: Callable[[], float],
    ) -> None:
        self._ctx = ctx
        self._list_initiatives = list_initiatives
        self._list_artifacts = list_artifacts
        self._record_artifact = record_artifact
        self._set_status = set_status
        self._spawn = spawn
        self._read_config = read_config
        self._clock = clock
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run()
            except Exception:
                log.warning("sdlc_intake.tick_failed", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        for ini in await self._list_initiatives():
            state, arts = _to_views(ini, await self._list_artifacts(ini["id"]))
            action = next_action(state, arts, mode, config)
            if action != Propose("intent"):
                continue
            rec = await self._spawn(ini, "intent")
            if rec is None:
                continue
            slug = str(ini.get("slug") or "")
            await self._record_artifact(
                initiative_id=ini["id"], kind="intent",
                repo_path=artifact_path(slug, "intent"),
                session_id=getattr(rec, "id", None),
                model=getattr(rec, "model", None))
            await self._set_status(ini["id"], "awaiting_human")
            await self._emit_proposed(slug, rec)

    async def _emit_proposed(self, slug: str, rec: Any) -> None:
        try:
            await self._ctx.emit("info", f"proposed intent for {slug}",
                                 kind="proposed", target=slug,
                                 session_id=getattr(rec, "id", None))
        except Exception:  # best-effort — the feed must not break the loop
            log.debug("sdlc_intake.emit_failed", exc_info=True)
