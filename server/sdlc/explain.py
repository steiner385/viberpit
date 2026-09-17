"""Consequence explainer (spec 2026-09-17-roadmap-consequence-guidance-design §3).

Pure: no I/O, no imports from server.db / server.automations / server.sessions.
Every sentence the UI shows about what a control does is computed here by running
the real stage engine on the hypothetical post-tap state and describing the
result — so the guidance cannot drift from the behaviour. ``describe`` is the ONE
place a ``StageAction`` becomes words.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from server.sdlc.engine import (
    STAGE_ARTIFACT,
    STAGE_SEQUENCE,
    Advance,
    ArtifactView,
    AwaitApproval,
    Done,
    HandoffToBuild,
    InitiativeView,
    Materialize,
    MergeArtifact,
    NeedsHuman,
    Propose,
    StageAction,
    advance_stage,
    next_action,
    step_back_stage,
)
from server.sdlc.gates import GATE_BY_ID, GATES, RISK_TOLERANCES, auto_resolves
from server.sdlc.paths import artifact_path

WHEN_NOW = "now"
WHEN_NEXT_TICK = "next_tick"
WHEN_NEVER = "never"

ACTOR_MACHINE = "machine"
ACTOR_YOU = "you"
ACTOR_NOBODY = "nobody"

#: Every human control the initiative page carries, in the order the wire lists them.
CONTROLS: tuple[str, ...] = ("advance", "step_back", "unblock", "materialize_accept", "materialize_reject")

_PROGRAM_REASON = "A program is driven by its phases; act on each phase instead."


# --------------------------------------------------------------------------- #
# Model                                                                        #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExplainContext:
    initiative: InitiativeView
    artifacts: list[ArtifactView]
    mode: str                     # synthesizer automation mode: "on" | "off"
    tolerance: str                # EFFECTIVE (after the kill switch)
    configured_tolerance: str     # the policy value as stored
    autoresolve_enabled: bool
    has_children: bool
    proposal_pending: bool
    proposal_tasks: int
    dispatch: str                 # product policy `dispatch`: off | propose | auto
    queue_min_status: str         # product policy `queue_min_status`
    session_model: str            # product policy `default_model`
    tick_seconds: int
    #: The unblock endpoint's third 409: the synthesizer gave up decomposing this
    #: plan after MATERIALIZE_MAX_ATTEMPTS. Defaulted so existing callers keep working.
    materialize_exhausted: bool = False


@dataclass(frozen=True)
class Step:
    text: str
    spawns_session: bool = False
    writes_github: bool = False


@dataclass(frozen=True)
class Consequence:
    available: bool
    unavailable_reason: str | None
    immediate: tuple[Step, ...]
    then: tuple[Step, ...]
    when: str
    reversal: str | None
    gate: str | None


@dataclass(frozen=True)
class MachineOutlook:
    actor: str
    when: str
    summary: str
    steps: tuple[Step, ...]
    because: str


@dataclass(frozen=True)
class Guidance:
    machine: MachineOutlook | None
    actions: dict[str, Consequence] = field(default_factory=dict)
    tolerance: dict = field(default_factory=dict)
    tick_seconds: int = 60


# --------------------------------------------------------------------------- #
# Words                                                                        #
# --------------------------------------------------------------------------- #

def capitalize(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def dispatch_sentence(dispatch: str, queue_min_status: str) -> str:
    """THE phrasing for what the Backlog Runner does with a board item under the
    product's ``dispatch`` — shared by the build-stage outlook and the inbox.
    Lowercase start so a caller can continue a sentence with it."""
    if dispatch == "auto":
        return (f"the Backlog Runner may dispatch it to a coding session once it is "
                f"at or above '{queue_min_status}' on the board.")
    if dispatch == "propose":
        return "the Backlog Runner may include it in a proposed batch; you approve each batch."
    return "nothing is dispatched: this product's Dispatch is off."


def _upstream(kind: str) -> str | None:
    stage = next((s for s, k in STAGE_ARTIFACT.items() if k == kind), None)
    if stage is None:
        return None
    idx = STAGE_SEQUENCE.index(stage)
    prev = STAGE_SEQUENCE[idx - 1] if idx > 0 else None
    return STAGE_ARTIFACT.get(prev) if prev is not None else None


def _artifact_of(artifacts: list[ArtifactView], kind: str) -> ArtifactView | None:
    for art in artifacts:
        if art.kind == kind:
            return art
    return None


def _next_of(stage: str) -> str | None:
    try:
        return advance_stage(stage)
    except ValueError:
        return None


def _board_step(ctx: ExplainContext) -> Step:
    return Step("Nothing more from the roadmap loop. The epic's sub-issues are on the board; "
                + dispatch_sentence(ctx.dispatch, ctx.queue_min_status),
                spawns_session=ctx.dispatch == "auto")


def _needs_human_step(reason: str, ctx: ExplainContext, view: InitiativeView) -> Step:
    if view.status == "blocked":
        return Step("Blocked: unblock or step back.")
    if ctx.mode == "off":
        return Step("SDLC automation is off: nothing moves until it is turned on from the "
                    "Delivery page's Automation tab.")
    if view.stage == "build":
        # The engine says NeedsHuman when no origin issue is set, but the loop takes
        # no action at build either way — the board does (spec §3.3).
        return _board_step(ctx)
    if view.stage in ("deploy", "maintain"):
        return Step("Deploy and maintain are carried by the pipeline and control bands, "
                    "not the roadmap loop.")
    return Step(f"Needs a human decision: stage {view.stage!s} is not one the roadmap loop drives.")


def describe(action: StageAction, ctx: ExplainContext, view: InitiativeView,
             artifacts: list[ArtifactView], *, depth: int = 0) -> tuple[Step, ...]:
    """Turn one engine decision into the steps a human reads. ``depth`` bounds the
    lookahead after an automatic advance to ONE further decision."""
    if isinstance(action, Propose):
        if action.kind == "intent":
            # The synthesizer's `_run` only spawns spec and plan. An intent comes
            # from the trigger-driven intake loop (server/automations/sdlc_intake.py),
            # which reacts to a triage incident or an adopted epic and never visits
            # a hand-created initiative — so promising a session here would lie.
            return (Step("The intent is proposed by the intake loop when a triage incident or an "
                         "adopted epic reaches this initiative; nothing starts on its own."),)
        # Only `spec` and `plan` reach here, and both take "a" (the intent split
        # above is what keeps a vowel-initial kind out of this sentence).
        up = _upstream(action.kind)
        reads = f" that reads the merged {up}" if up else ""
        return (Step(f"Spawns a {action.kind} session on {ctx.session_model}{reads} and opens "
                     f"a PR adding {artifact_path(view.slug, action.kind)}.",
                     spawns_session=True),)
    if isinstance(action, AwaitApproval):
        art = _artifact_of(artifacts, action.kind)
        if art is not None and art.commit_sha is None:
            return (Step(f"The {action.kind} PR is waiting for you to review and merge it; "
                         f"under {ctx.tolerance} the loop does not merge it."),)
        return (Step(f"The {action.kind} is merged; tap Advance to move to {_next_of(view.stage)}."),)
    if isinstance(action, MergeArtifact):
        return (Step(f"Arms GitHub auto-merge on the {action.kind} PR #{action.pr_number}; "
                     "CI stays the gate.", writes_github=True),)
    if isinstance(action, Advance):
        steps = [Step(f"Advances to {action.to_stage} automatically.")]
        if depth == 0:
            after = replace(view, stage=action.to_stage, status="active")
            steps.extend(describe(next_action(after, artifacts, ctx.mode, ctx.tolerance),
                                  ctx, after, artifacts, depth=1))
        return tuple(steps)
    if isinstance(action, Materialize):
        if ctx.proposal_pending:
            return (Step("A decomposition is waiting for your Accept or Reject."),)
        return (Step("Spawns a decompose session that proposes an epic and sub-issues for you "
                     "to accept or reject.", spawns_session=True),)
    if isinstance(action, HandoffToBuild):
        return (_board_step(ctx),)
    if isinstance(action, Done):
        return (Step("Nothing left to do."),)
    if isinstance(action, NeedsHuman):
        return (_needs_human_step(action.reason, ctx, view),)
    return (Step("No next action."),)  # pragma: no cover — union is closed


def _loop_acts(action: StageAction, ctx: ExplainContext) -> bool:
    """Whether the machine (synthesizer or Backlog Runner) executes *action* without
    a human — the one rule behind ``when`` and ``actor``."""
    if ctx.mode == "off":
        return False
    if isinstance(action, Propose):
        # The synthesizer spawns `spec` and `plan` only (sdlc_synthesizer._run);
        # `intent` belongs to the trigger-driven intake loop, which no tick visits.
        return action.kind in ("spec", "plan")
    if isinstance(action, (MergeArtifact, Advance)):
        return True
    if isinstance(action, Materialize):
        return not ctx.proposal_pending
    if isinstance(action, HandoffToBuild):
        return ctx.dispatch == "auto"
    if isinstance(action, NeedsHuman) and ctx.initiative.stage == "build" \
            and ctx.initiative.status != "blocked":
        return ctx.dispatch == "auto"
    return False


def _because(ctx: ExplainContext) -> str:
    if ctx.mode == "off":
        return "SDLC automation is off, so nothing moves until it is turned on."
    if ctx.configured_tolerance != ctx.tolerance:
        return (f"Risk tolerance is {ctx.configured_tolerance}, but auto-resolve is switched off "
                "for this deployment, so every gate waits for you.")
    # Only the FIRST character: `str.lower()` would flatten an acronym inside a
    # label ("Merge the PR" → "pr").
    covered = [g.label[:1].lower() + g.label[1:] for g in GATES if auto_resolves(g.id, ctx.tolerance)]
    if not covered:
        return f"Risk tolerance {ctx.tolerance}: every gate waits for you."
    return (f"Risk tolerance {ctx.tolerance}: the machine resolves these gates itself: "
            + "; ".join(covered) + ".")


# --------------------------------------------------------------------------- #
# Controls                                                                     #
# --------------------------------------------------------------------------- #

def _unavailable(reason: str, gate: str | None = None) -> Consequence:
    return Consequence(False, reason, (), (), WHEN_NEVER, None, gate)


def _then(ctx: ExplainContext, after: InitiativeView) -> tuple[tuple[Step, ...], str]:
    action = next_action(after, ctx.artifacts, ctx.mode, ctx.tolerance)
    steps = describe(action, ctx, after, ctx.artifacts)
    return steps, (WHEN_NEXT_TICK if _loop_acts(action, replace(ctx, initiative=after)) else WHEN_NEVER)


def _advance(ctx: ExplainContext) -> Consequence:
    gate = "stage.advance"
    view = ctx.initiative
    if view.status == "blocked":
        return _unavailable("Clear the block first.", gate)
    if view.stage == "done":
        return _unavailable("Already at the final stage.", gate)
    nxt = _next_of(view.stage)
    if nxt is None:
        return _unavailable("Unknown stage.", gate)
    after = replace(view, stage=nxt, status="active")
    then, when = _then(ctx, after)
    return Consequence(True, None, (Step(f"Moves the gate to {nxt}."),), then, when,
                       GATE_BY_ID[gate].reverse, gate)


def _step_back(ctx: ExplainContext) -> Consequence:
    view = ctx.initiative
    if view.status == "blocked":
        return _unavailable("Clear the block first.")
    if view.stage not in STAGE_SEQUENCE:
        return _unavailable("Unknown stage.")
    prev = step_back_stage(view.stage)
    if prev is None:
        return _unavailable("Already at the first stage.")
    after = replace(view, stage=prev, status="active")
    then, when = _then(ctx, after)
    return Consequence(True, None, (Step(f"Moves the gate back to {prev}."),), then, when,
                       "Advance", None)


def _unblock(ctx: ExplainContext) -> Consequence:
    gate = "initiative.blocked"
    view = ctx.initiative
    if view.status != "blocked":
        return _unavailable("Not blocked.", gate)
    if ctx.proposal_pending:
        return _unavailable("Accept or reject the pending materialization first.", gate)
    if ctx.materialize_exhausted:
        # The endpoint's third 409 (`materialize_exhausted`): the synthesizer wrote
        # this block after MATERIALIZE_MAX_ATTEMPTS and would re-block on the next
        # tick, so the button must say so rather than 409 on tap.
        return _unavailable("Decomposition gave up after repeated attempts; reject or remove "
                            "the stale proposals first.", gate)
    after = replace(view, status="awaiting_human")
    then, when = _then(ctx, after)
    immediate: tuple[Step, ...] = (Step(f"Clears the block; the gate stays at {view.stage}."),)
    if view.stage == "plan" and view.epic is None:
        # A block at plan with no epic is the failed-materialize shape: the apply
        # may have left issues on GitHub that a later decomposition will duplicate.
        immediate += (Step("If a failed materialize left an epic or sub-issues on GitHub, close "
                           "them first; a later decomposition creates a fresh set."),)
    return Consequence(True, None, immediate, then, when, None, gate)


def _materialize_accept(ctx: ExplainContext) -> Consequence:
    gate = "materialization.accept"
    if not ctx.proposal_pending:
        return _unavailable("No decomposition is waiting.", gate)
    view = ctx.initiative
    n = ctx.proposal_tasks
    after = replace(view, stage="build", status="active", epic=view.epic or "pending")
    then, when = _then(ctx, after)
    immediate = (
        Step(f"Creates the epic and {n} sub-issue{'' if n == 1 else 's'} on GitHub; this cannot "
             "be undone here.", writes_github=True),
        Step("Moves the gate to build."),
        Step("The sub-issues land on the board already classified."),
    )
    return Consequence(True, None, immediate, then, when, GATE_BY_ID[gate].reverse, gate)


def _materialize_reject(ctx: ExplainContext) -> Consequence:
    if not ctx.proposal_pending:
        return _unavailable("No decomposition is waiting.")
    freed = replace(ctx, proposal_pending=False)
    then, when = _then(freed, ctx.initiative)
    return Consequence(True, None,
                       (Step("Rejects this decomposition; nothing is created on GitHub."),),
                       then, when, None, None)


def _outlook(ctx: ExplainContext) -> MachineOutlook:
    view = ctx.initiative
    action = next_action(view, ctx.artifacts, ctx.mode, ctx.tolerance)
    steps = describe(action, ctx, view, ctx.artifacts)
    if isinstance(action, Done) or ctx.mode == "off":
        actor, when = ACTOR_NOBODY, WHEN_NEVER
    elif _loop_acts(action, ctx):
        actor, when = ACTOR_MACHINE, WHEN_NEXT_TICK
    else:
        actor, when = ACTOR_YOU, WHEN_NEVER
    return MachineOutlook(actor, when, steps[0].text, steps, _because(ctx))


def explain(ctx: ExplainContext) -> Guidance:
    """The whole guidance payload for one initiative. Total: an unknown stage or
    status degrades to unavailable controls and an honest outlook, never a raise."""
    tolerance = {"effective": ctx.tolerance, "configured": ctx.configured_tolerance,
                 "autoresolveEnabled": ctx.autoresolve_enabled, "synthesizerMode": ctx.mode}
    if ctx.has_children:
        return Guidance(None, {c: _unavailable(_PROGRAM_REASON) for c in CONTROLS},
                        tolerance, ctx.tick_seconds)
    actions = {
        "advance": _advance(ctx),
        "step_back": _step_back(ctx),
        "unblock": _unblock(ctx),
        "materialize_accept": _materialize_accept(ctx),
        "materialize_reject": _materialize_reject(ctx),
    }
    return Guidance(_outlook(ctx), actions, tolerance, ctx.tick_seconds)


# --------------------------------------------------------------------------- #
# Wire                                                                         #
# --------------------------------------------------------------------------- #

def step_to_wire(s: Step) -> dict:
    return {"text": s.text, "spawnsSession": s.spawns_session, "writesGithub": s.writes_github}


def consequence_to_wire(c: Consequence) -> dict:
    return {"available": c.available, "unavailableReason": c.unavailable_reason,
            "immediate": [step_to_wire(s) for s in c.immediate],
            "then": [step_to_wire(s) for s in c.then],
            "when": c.when, "reversal": c.reversal, "gate": c.gate}


def guidance_to_wire(g: Guidance) -> dict:
    machine = None if g.machine is None else {
        "actor": g.machine.actor, "when": g.machine.when, "summary": g.machine.summary,
        "steps": [step_to_wire(s) for s in g.machine.steps], "because": g.machine.because}
    return {"machine": machine,
            "actions": {k: consequence_to_wire(v) for k, v in g.actions.items()},
            "tolerance": dict(g.tolerance), "tickSeconds": g.tick_seconds}


def gates_to_wire(*, autoresolve_enabled: bool) -> dict:
    """The registry for ``GET /api/sdlc/gates``: every gate with whether each
    tolerance level resolves it (spec D5)."""
    return {"gates": [{"id": g.id, "label": g.label, "risk": g.risk, "action": g.action,
                       "reverse": g.reverse,
                       "autoResolvesUnder": {t: auto_resolves(g.id, t) for t in RISK_TOLERANCES}}
                      for g in GATES],
            "tolerances": list(RISK_TOLERANCES),
            "autoresolveEnabled": autoresolve_enabled}
