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
(``STAGE_ARTIFACT``). ``next_action`` takes the product's risk *tolerance* (spec
2026-09-14 §5): under ``cautious`` every artifact gate returns ``AwaitApproval``
and a human tap advances; ``balanced``/``bold`` return ``MergeArtifact``/``Advance``
for the gates the registry (``server/sdlc/gates.py``) says they cover. The engine
never performs the write; the synthesizer loop does.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.sdlc.gates import auto_resolves

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
    # The initiative's epic, "owner/repo#N", once one exists (adopted from GitHub
    # or created by materialization); None means the plan has not been decomposed
    # yet. Internal to the engine — the API's wire `epic` key is a progress
    # object, not this ref.
    epic: str | None = None
    # Sub-issue progress of the epic (the build stage's completion signal for the
    # ``stage.advance_built`` gate); 0/0 when there is no epic.
    epic_total: int = 0
    epic_done: int = 0
    # Whether the epic issue itself is closed on GitHub (its mirrored state).
    epic_closed: bool = False

    @property
    def epic_finished(self) -> bool:
        """Every sub-issue of the epic is closed (and there is at least one)."""
        return self.epic is not None and 0 < self.epic_total <= self.epic_done


@dataclass(frozen=True)
class ProgramView:
    """A program (an initiative with phases) for the lifecycle sweep (#3108). Its
    stage is derived from its phases, so the only decision is whether it is over:
    ``epic_total``/``epic_done`` count its OWN epic's direct sub-issues, and
    ``phases_total``/``phases_done`` its phase initiatives."""
    slug: str
    status: str
    stage: str
    epic: str | None = None
    epic_closed: bool = False
    epic_total: int = 0
    epic_done: int = 0
    phases_total: int = 0
    phases_done: int = 0


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
class Materialize:
    """Terminal action of the ``plan`` stage (spec 003 §7b): the plan artifact is
    merged but the initiative has no epic yet, so the plan must be decomposed
    into an epic + sub-issues before anything downstream can happen."""
    kind: str = "plan"


@dataclass(frozen=True)
class MergeArtifact:
    """The stage artifact PR is open and the tolerance covers ``artifact.merge``:
    the loop arms auto-merge on it (its one GitHub write). ``pr_number`` is
    always set — with no number known yet the engine waits instead."""
    kind: str
    pr_number: int | None = None


@dataclass(frozen=True)
class Advance:
    """The stage is complete (artifact merged; at plan, the epic exists; at build,
    the epic is fully closed) and the tolerance covers the advance gate: move to
    ``to_stage`` through the same write the human Advance tap uses. The NEXT tick
    proposes the next artifact — one write per tick, one decision per action."""
    to_stage: str


@dataclass(frozen=True)
class CloseEpic:
    """Every sub-issue of the epic is closed but the epic issue is still open, and
    the tolerance covers ``epic.close``: close it on GitHub (with a comment). The
    NEXT tick, seeing it closed, closes the initiative."""
    epic: str


@dataclass(frozen=True)
class CloseInitiative:
    """The work is over — the epic and every sub-issue are closed (for a program,
    every phase is done too) — and the tolerance covers ``initiative.close``: move
    the row to the terminal stage and status, as the human Close does."""


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


StageAction = (Propose | AwaitApproval | MergeArtifact | Advance | Materialize | CloseEpic
               | CloseInitiative | HandoffToBuild | Done | NeedsHuman)

#: Stages past build, where the lifecycle sweep (#3108) closes a shipped epic and
#: then the initiative. Build itself first advances under ``stage.advance_built``.
_CLOSING_STAGES = ("deploy", "maintain")


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


def step_back_stage(stage: str) -> str | None:
    """Predecessor stage for the human step-back tap; None at the first stage.

    ``done`` steps back to ``maintain`` — nothing is absorbing in this direction,
    a human undoing a mis-tap is the whole point. Raises ``ValueError`` on an
    unknown stage, as ``advance_stage`` does."""
    if stage not in STAGE_SEQUENCE:
        raise ValueError(f"unknown stage {stage!r}")
    idx = STAGE_SEQUENCE.index(stage)
    return STAGE_SEQUENCE[idx - 1] if idx > 0 else None


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
    tolerance: str,
    *,
    framework_block: str | None = None,
) -> StageAction:
    """Decide the next SDLC action for *initiative*. Pure + total.

    ``mode`` is the owning automation's mode: ``"off"`` parks everything; any
    other value proceeds (the synthesizer's is ``off | on``; intake still passes
    its legacy ``propose``/``auto``). ``tolerance`` is the product's
    ``risk_tolerance`` (``server/sdlc/gates.py:RISK_TOLERANCES``); the caller
    resolves it, this function only consults ``auto_resolves``.

    ``framework_block`` is the caller's ``framework_gate`` reason for the
    artifact PR: it turns an artifact merge or advance into ``NeedsHuman`` that
    no tolerance can bypass. The build-stage ``stage.advance_built`` is not
    gated here; code PRs are governed by the merge-boundary required check.
    """
    action = _decide(initiative, artifacts, mode, tolerance)
    if framework_block and (isinstance(action, MergeArtifact) or (
            isinstance(action, Advance) and initiative.stage in STAGE_ARTIFACT)):
        return NeedsHuman(reason=f"framework evidence required: {framework_block}")
    return action


def _decide(
    initiative: InitiativeView,
    artifacts: list[ArtifactView],
    mode: str,
    tolerance: str,
) -> StageAction:
    # Terminal / override states first (checked before any stage logic).
    if initiative.stage == "done" or initiative.status == "done":
        return Done()
    if initiative.status == "blocked":
        return NeedsHuman(reason="initiative is blocked")
    if mode == "off":
        return NeedsHuman(reason="SDLC automation is off")

    stage = initiative.stage

    # Past build, a finished epic is closed and then the initiative (#3108); a
    # tolerance that covers neither leaves it to the existing flows below.
    if stage in _CLOSING_STAGES and initiative.epic_finished:
        closing = _close_action(initiative.epic, initiative.epic_closed, tolerance)
        if closing is not None:
            return closing

    # Non-artifact stages: a fully built epic may advance to deploy under the
    # `stage.advance_built` gate; otherwise hand off (build) or defer to existing flows.
    if stage not in STAGE_ARTIFACT:
        if (stage == "build" and initiative.epic_total > 0
                and initiative.epic_done >= initiative.epic_total
                and auto_resolves("stage.advance_built", tolerance)):
            return Advance(advance_stage(stage))
        return _enter_stage(stage, initiative)

    # An adopted epic whose sub-issues are already being closed is past this
    # stage's document gate (#3108): the work it would plan is under way, so it
    # advances instead of waiting on (or proposing) a spec or plan.
    if (stage in ("design", "plan") and initiative.epic is not None
            and initiative.epic_done > 0 and auto_resolves("stage.advance", tolerance)):
        return Advance(advance_stage(stage))

    kind = STAGE_ARTIFACT[stage]
    art = _find_artifact(artifacts, kind)

    # Nothing proposed yet → propose generating this stage's artifact.
    if art is None:
        return Propose(kind)

    # Artifact PR open (not merged): arm its merge if the gate allows and the PR
    # number is known (enrichment fills it); otherwise wait for the human.
    if art.commit_sha is None:
        if art.pr_number is not None and auto_resolves("artifact.merge", tolerance):
            return MergeArtifact(kind, art.pr_number)
        return AwaitApproval(kind)

    # Merged plan with no epic yet → materialize (spec 003 §7b), ahead of any
    # advance: until the plan is decomposed there is nothing to advance to.
    if stage == "plan" and initiative.epic is None:
        return Materialize()

    # Artifact merged: advance under the `stage.advance` gate, else await the tap.
    if auto_resolves("stage.advance", tolerance):
        return Advance(advance_stage(stage))
    return AwaitApproval(kind)


def _close_action(epic: str | None, epic_closed: bool,
                  tolerance: str) -> CloseEpic | CloseInitiative | None:
    """THE close sequence for finished work (#3108), shared by leaves and programs:
    close the epic first, then the initiative; None when the tolerance does not
    cover the next step. ``epic`` None (a program without one) skips straight to
    the initiative."""
    if epic is not None and not epic_closed:
        return CloseEpic(epic) if auto_resolves("epic.close", tolerance) else None
    return CloseInitiative() if auto_resolves("initiative.close", tolerance) else None


def program_action(program: ProgramView, mode: str, tolerance: str) -> CloseEpic | CloseInitiative | None:
    """The lifecycle sweep's decision for a program (#3108): once every phase is
    done (and its own epic, if any, has no open sub-issue), close that epic and
    then the program. None for anything still running, blocked or parked —
    a program is otherwise never driven directly. Pure + total."""
    if mode == "off" or program.status in ("done", "blocked") or program.stage == "done":
        return None
    if program.phases_total == 0 or program.phases_done < program.phases_total:
        return None
    if program.epic is not None and program.epic_done < program.epic_total:
        return None
    return _close_action(program.epic, program.epic_closed, tolerance)
