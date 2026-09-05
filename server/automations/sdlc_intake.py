"""SDLC Intake — the front-of-loop automation that turns a trigger into a
proposed ``intent.md`` PR.

Two objects, following the ``BacklogRunner`` mold exactly (constitution VI —
one substrate, no parallel engine):

* ``SdlcIntake`` — the ``Automation`` contract surface (id/name/modes/config +
  ``status``/``on_state_change``). Metadata only; it holds no I/O.
* ``SdlcIntakeLoop`` — the supervised control loop. Every I/O dependency is
  INJECTED (read config, read pending triggers, ensure the Initiative row,
  record the intent artifact, spawn the session, clock) so the loop runs with no
  network and no real DB in tests. The loop never *decides* the stage itself: it
  delegates to the pure ``sdlc.engine.next_action`` and seeds the session with
  the pure ``sdlc.prompts.build_intent_prompt``.

**Propose-only v1 (design §3.4):** the loop turns each pending trigger into one
committed ``intent.md`` PR (the spawned session does the commit + non-draft PR),
records the artifact, and stops. Advancing the Initiative to the next stage is a
human tap — the engine returns ``AwaitApproval`` for any initiative whose intent
artifact already exists, so a re-tick never re-proposes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from server.sdlc import engine
from server.sdlc.prompts import artifact_path, build_intent_prompt

from .base import AutomationStatus, ConfigField
from .engine_policy import EnginePolicy

# A trigger's per-day spawn ceiling is enforced over a rolling 24h window.
_DAY_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------------- #
# Trigger — the loop's input view (constructed by the orchestrator from a       #
# brainstorm session or, in Phase 2, a triage item; NOT an ORM model).          #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Trigger:
    """A pending intake trigger. ``slug`` is the stable Initiative identity;
    ``text`` is the brainstorm/triage content that seeds the intent prompt."""
    slug: str
    text: str
    title: str = ""
    repo: str = ""
    origin: str = "human"
    origin_ref: str | None = None


# --------------------------------------------------------------------------- #
# Automation contract surface                                                   #
# --------------------------------------------------------------------------- #

class SdlcIntake:
    id = "sdlc-intake"
    name = "SDLC Intake"
    description = (
        "Turns a brainstorm/triage trigger into a proposed intent.md PR — the "
        "front of the AI-native SDLC loop. Propose-only: a human advance is the gate.")
    modes: tuple[str, ...] = ("off", "propose", "auto")
    # Audited engine ceiling (Phase 7, Task 7.1). This origin has no Codex
    # acceptance evidence, so it launches Claude only. Widening it is a
    # reviewable one-line change gated on its acceptance suite passing.
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema = [
        ConfigField("enabled_repos", "Repositories", "repo-multiselect", [],
                    help="Repositories this intake automation may open intent PRs in."),
        ConfigField("max_spawns_per_day", "Max intent sessions / day", "int", 10,
                    help="Safety cap on intent-generation sessions spawned per rolling 24h."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "sdlc_intake_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        pending = counts.get("pending", 0)
        awaiting = counts.get("awaiting", 0)
        text = f"{pending} pending · {awaiting} awaiting approval"
        return AutomationStatus(text=text, level="active" if pending else "idle",
                                counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        # No side effects on toggle; the loop reacts on its next tick.
        return None


# --------------------------------------------------------------------------- #
# Injected-seam type aliases (mirror BacklogRunnerLoop's typed seams)           #
# --------------------------------------------------------------------------- #

ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ReadTriggers = Callable[[], Awaitable[list[Trigger]]]
EnsureInitiative = Callable[[Trigger], Awaitable[tuple[engine.InitiativeView,
                                                       list[engine.ArtifactView]]]]
RecordIntent = Callable[[str, str, str | None], Awaitable[None]]
Spawn = Callable[[Trigger, str], Awaitable[Any]]


class SdlcIntakeLoop:
    """The Intake control loop. One ``tick()`` reads pending triggers and, for
    each one the pure engine says to ``Propose('intent')``, spawns an intent
    session and records the artifact. All I/O is injected."""

    def __init__(
        self,
        ctx: Any,
        *,
        read_config: ReadConfig,
        read_triggers: ReadTriggers,
        ensure_initiative: EnsureInitiative,
        record_intent: RecordIntent,
        spawn: Spawn,
        clock: Callable[[], float],
    ) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._read_triggers = read_triggers
        self._ensure_initiative = ensure_initiative
        self._record_intent = record_intent
        self._spawn = spawn
        self._clock = clock
        # Rolling-window spawn timestamps (in-memory, like BacklogRunnerLoop).
        self._spawns_today: list[float] = []

    async def tick(self) -> None:
        """One control-loop iteration. Best-effort: swallows exceptions so a
        transient failure never crashes the supervising driver."""
        try:
            await self._run()
        except Exception:  # pragma: no cover — belt+braces, mirrors the mold
            import logging
            logging.getLogger(__name__).warning("sdlc_intake.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash_counts(0, 0)
            return

        triggers = await self._read_triggers()
        self._prune_day()
        cap = int(config.get("max_spawns_per_day", 10))
        awaiting = 0

        for trigger in triggers:
            view, artifacts = await self._ensure_initiative(trigger)
            action = engine.next_action(view, artifacts, mode, config)

            if isinstance(action, engine.Propose) and action.kind == "intent":
                if len(self._spawns_today) >= cap:
                    await self._ctx.emit(
                        "info", "daily intent-session cap reached; deferring",
                        kind="ceiling", target=trigger.slug)
                    continue
                await self._propose_intent(trigger)
            elif isinstance(action, engine.AwaitApproval):
                awaiting += 1
            # Any other action (Done / HandoffToBuild / NeedsHuman) is not the
            # intake stage's concern — leave it for the human / later stages.

        self._stash_counts(len(triggers), awaiting)

    async def _propose_intent(self, trigger: Trigger) -> None:
        # The intent instructions state a commit trailer, the one engine-specific
        # line in them, so the engine has to be known before the prompt is built
        # (Phase 7, Task 7.1). A refusal is recorded by the context and skips the
        # proposal rather than seeding a session for an unaudited engine.
        plan = await self._ctx.launch_plan_or_refuse(f"{trigger.slug}:intent")
        if plan is None:
            return
        launch_engine, _config = plan
        prompt = build_intent_prompt(trigger.text, slug=trigger.slug, repo=trigger.repo,
                                     engine=launch_engine)
        session = await self._spawn(trigger, prompt)
        session_id = getattr(session, "id", None)
        path = artifact_path(trigger.slug, "intent")
        await self._record_intent(trigger.slug, path, session_id)
        self._spawns_today.append(self._clock())
        await self._ctx.emit(
            "info", f"proposed intent for {trigger.slug}",
            kind="proposed", target=trigger.slug, session_id=session_id,
            detail={"path": path})

    def _prune_day(self) -> None:
        """Drop spawn timestamps older than the rolling 24h window."""
        cutoff = self._clock() - _DAY_SECONDS
        self._spawns_today = [t for t in self._spawns_today if t >= cutoff]

    def _stash_counts(self, pending: int, awaiting: int) -> None:
        # Surfaced by SdlcIntake.status() on the automation card.
        self._ctx.sdlc_intake_counts = {"pending": pending, "awaiting": awaiting}
