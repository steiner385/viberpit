"""Pure SDLC stage-decision engine — no I/O, never touches the DB or orchestrator.

Mirrors ``pr_doctor_engine`` purity: all decision logic is a total function over
plain-dataclass *views* of the Initiative and its artifact chain, returning a
frozen ``StageAction`` the Intake/Synthesizer loops execute. The loops inject the
real I/O (read initiatives, spawn session, commit artifact, open PR); this module
only decides *what should happen next*.

The chain is the front of the AI-Native SDLC loop:

    intake ─▶ design ─▶ plan ─▶ build ─▶ deploy ─▶ maintain ─▶ done
   (intent)   (spec)   (plan)   (issue handoff)   ── existing flows ──

Each front stage produces one committed markdown artifact
(``STAGE_ARTIFACT``). ``next_action`` encodes the **propose-only v1 gate**: once a
stage's artifact PR is proposed, the engine returns ``AwaitApproval`` and NEVER
advances the stage on its own — a human tap (or, later, an auto-advance config)
moves the Initiative to the next stage. See design §3.3.
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Stage / artifact vocabulary (source of truth for the front chain)           #
# --------------------------------------------------------------------------- #

# Full stage order (matches the Initiative.stage enum in the design). Only the
# first three produce artifacts; build hands off; deploy/maintain are carried by
# existing pipeline/monitor flows.
STAGE_SEQUENCE = ("intake", "design", "plan", "build", "deploy", "maintain", "done")

# Which committed artifact each front stage produces. Ordered intake→design→plan.
STAGE_ARTIFACT = {
    "intake": "intent",
    "design": "spec",
    "plan": "plan",
}


# --------------------------------------------------------------------------- #
# Pure input views (constructed by the orchestrator from DB rows — NOT the      #
# ORM models, so this module imports nothing from server.db / automations)     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ArtifactView:
    """A committed-or-proposed artifact in the chain. ``commit_sha`` is None while
    the artifact PR is still open (proposed) and set once merged (approved)."""
    kind: str  # "intent" | "spec" | "plan"
    commit_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class InitiativeView:
    """The state the engine needs to decide the next action. ``issue`` is the
    underlying GitHub issue ref handed to ``backlog_runner`` at the Build stage."""
    slug: str
    stage: str
    status: str  # "proposed" | "active" | "awaiting_human" | "blocked" | "done"
    repo: str = ""
    issue: str | None = None


# --------------------------------------------------------------------------- #
# Stage actions (what the loop should do next)                                  #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Propose:
    """Spawn a session to generate this stage's artifact and open its PR."""
    kind: str  # "intent" | "spec" | "plan"


@dataclass(frozen=True)
class AwaitApproval:
    """The stage's artifact PR is proposed (or merged, not yet advanced) — the
    propose-only gate: do nothing until a human advances the Initiative."""
    kind: str


@dataclass(frozen=True)
class HandoffToBuild:
    """Front chain is complete — hand the underlying issue to backlog_runner."""
    issue: str


@dataclass(frozen=True)
class Done:
    """The Initiative has reached its terminal stage/status."""


@dataclass(frozen=True)
class NeedsHuman:
    """Nothing the engine can safely do automatically; a human must intervene.
    ``reason`` is surfaced in the activity feed."""
    reason: str = ""


StageAction = Propose | AwaitApproval | HandoffToBuild | Done | NeedsHuman


# --------------------------------------------------------------------------- #
# Decision core                                                                 #
# --------------------------------------------------------------------------- #

def _find_artifact(artifacts: list[ArtifactView], kind: str) -> ArtifactView | None:
    """First artifact of *kind*, or None. There is one per kind per Initiative."""
    for art in artifacts:
        if art.kind == kind:
            return art
    return None


def _next_stage(stage: str) -> str | None:
    """The stage following *stage* in ``STAGE_SEQUENCE``, or None past the end."""
    try:
        idx = STAGE_SEQUENCE.index(stage)
    except ValueError:
        return None
    if idx + 1 >= len(STAGE_SEQUENCE):
        return None
    return STAGE_SEQUENCE[idx + 1]


def advance_stage(stage: str) -> str:
    """Successor stage for the human-gate advance tap; ``done`` is absorbing.

    Raises ``ValueError`` on an unknown stage so a corrupt row surfaces loudly
    instead of silently re-entering the sequence."""
    if stage not in STAGE_SEQUENCE:
        raise ValueError(f"unknown stage {stage!r}")
    nxt = _next_stage(stage)
    return nxt if nxt is not None else "done"


def _enter_stage(stage: str, initiative: InitiativeView) -> StageAction:
    """The action to take on *first entering* ``stage`` with no artifact yet.
    Used both for the current stage and (auto-advance) for the next one."""
    if stage == "done":
        return Done()
    if stage == "build":
        if initiative.issue:
            return HandoffToBuild(initiative.issue)
        return NeedsHuman(reason="build stage reached but no underlying issue is set")
    kind = STAGE_ARTIFACT.get(stage)
    if kind is None:
        # deploy / maintain — carried by existing pipeline / control-band flows.
        return NeedsHuman(reason=f"stage {stage!r} is handled by existing flows, not the SDLC engine")
    return Propose(kind)


def next_action(
    initiative: InitiativeView,
    artifacts: list[ArtifactView],
    mode: str,
    config: dict,
) -> StageAction:
    """Decide the next SDLC action for *initiative*. Pure + total.

    ``mode`` ∈ {"off", "propose", "auto"} mirrors the other automations. ``config``
    is reserved for future gating; the one flag consulted today is
    ``propose_only`` (default True) which, when combined with ``mode == "auto"``,
    lets a merged artifact auto-advance to the next stage. v1 default keeps the
    human gate at every stage.
    """
    # Terminal / override states first (checked before any stage logic).
    if initiative.stage == "done" or initiative.status == "done":
        return Done()
    if initiative.status == "blocked":
        return NeedsHuman(reason="initiative is blocked")
    if mode == "off":
        return NeedsHuman(reason="SDLC automation is off")

    stage = initiative.stage

    # Non-artifact stages: hand off (build) or defer to existing flows.
    if stage not in STAGE_ARTIFACT:
        return _enter_stage(stage, initiative)

    kind = STAGE_ARTIFACT[stage]
    art = _find_artifact(artifacts, kind)

    # Nothing proposed yet → propose generating this stage's artifact.
    if art is None:
        return Propose(kind)

    # Artifact PR open (not merged) → propose-only gate: wait for the human.
    if art.commit_sha is None:
        return AwaitApproval(kind)

    # Artifact merged. The engine never advances the stage itself under the
    # propose-only gate — it keeps awaiting the human advance. The only exception
    # is the future auto-advance seam (auto mode + gate explicitly disabled).
    if mode == "auto" and not config.get("propose_only", True):
        nxt = _next_stage(stage)
        if nxt is None:
            return Done()
        return _enter_stage(nxt, initiative)
    return AwaitApproval(kind)
