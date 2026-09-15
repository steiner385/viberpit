"""Gate registry (spec 2026-09-14-roadmap-auto-execution-design §4).

Every point where the roadmap waits on a human is a named gate with a risk
class. A product's ``risk_tolerance`` (policy) covers a class or it does not;
``auto_resolves`` is the ONE rule the loops ask. ``high`` never auto-resolves.

Pure: no I/O, no imports from server.db / server.automations, so the stage
engine can import it and stay total.
"""
from __future__ import annotations

from dataclasses import dataclass

RISK_CLASSES: tuple[str, ...] = ("low", "medium", "high")
RISK_TOLERANCES: tuple[str, ...] = ("cautious", "balanced", "bold")

#: The highest risk class each tolerance level covers. ``None`` = nothing.
_CEILING: dict[str, str | None] = {"cautious": None, "balanced": "low", "bold": "medium"}


@dataclass(frozen=True)
class GateSpec:
    id: str
    label: str
    #: One of RISK_CLASSES, or None for a gate governed by its own policy knob
    #: (listed so the inventory is complete; never decided by this rule).
    risk: str | None
    #: What auto-resolution does, one sentence, shown in the UI.
    action: str
    #: The human reverse action, shown beside an auto-resolved row.
    reverse: str


GATES: tuple[GateSpec, ...] = (
    GateSpec("artifact.merge", "Merge the stage artifact PR", "low",
             "Arm GitHub auto-merge on the intent, spec or plan PR; CI stays the gate.",
             "Revert the docs commit"),
    GateSpec("stage.advance", "Advance a stage once its artifact merged", "low",
             "Advance the initiative to the next stage.", "Step back"),
    GateSpec("stage.advance_built", "Advance build to deploy once the epic is closed", "low",
             "Advance the initiative from build to deploy.", "Step back"),
    GateSpec("materialization.accept", "Accept a plan decomposition", "medium",
             "Create the epic and sub-issues the decompose session proposed.",
             "Close the created issues"),
    GateSpec("model.upgrade", "Approve a session's request for a costlier model", "medium",
             "Approve the requested tier within the self-switch cap.", "Switch the model back"),
    GateSpec("dispatch", "Dispatch a ready issue to a coding session", None,
             "Governed by the product's dispatch setting.", "Archive the session"),
    GateSpec("triage.accept", "Accept a classification proposal", None,
             "Governed by the product's triage auto-accept tier.", "Undo within the window"),
    GateSpec("issue.needs_spec", "Retry an issue that bounced out", "high",
             "Never automatic.", "Edit the issue body"),
    GateSpec("session.hand_back", "Answer a session that handed back", "high",
             "Never automatic.", "Reply in the session"),
    GateSpec("initiative.blocked", "Unblock a blocked initiative", "high",
             "Never automatic.", "Unblock or step back"),
    GateSpec("triage.no_fit", "Create a capability for a no-fit proposal", "high",
             "Never automatic.", "Reject the proposal"),
    GateSpec("deploy.production", "Deploy to production", "high",
             "Never automatic.", "Roll back"),
    GateSpec("issue.close", "Close an issue as resolved or won't do", "high",
             "Never automatic.", "Reopen the issue"),
)

GATE_BY_ID: dict[str, GateSpec] = {g.id: g for g in GATES}


def auto_resolves(gate_id: str, tolerance: str) -> bool:
    """Whether *tolerance* covers *gate_id*'s risk class. Unknown gates, unknown
    tolerances, policy-owned gates (``risk is None``) and ``high`` all answer False."""
    gate = GATE_BY_ID.get(gate_id)
    ceiling = _CEILING.get(tolerance)
    if gate is None or gate.risk is None or ceiling is None:
        return False
    if gate.risk == "high":
        return False
    return RISK_CLASSES.index(gate.risk) <= RISK_CLASSES.index(ceiling)


def effective_tolerance(value: str | None, *, enabled: bool) -> str:
    """The tolerance a loop should act on: the policy value when it is in the
    vocabulary and the kill switch is on; ``cautious`` otherwise."""
    if not enabled or value not in RISK_TOLERANCES:
        return "cautious"
    return value
