"""Gate registry (spec 2026-09-14-roadmap-auto-execution-design §4).

Every point where the roadmap waits on a human is a named gate with a risk
class. A product's ``risk_tolerance`` (policy) covers a class or it does not;
``auto_resolves`` is the ONE rule the loops ask. ``high`` never auto-resolves.

Pure: no I/O, no imports from server.db / server.automations, so the stage
engine can import it and stay total.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


#: How long the roadmap shows what the machine did with a gate (spec D5): long
#: enough to be seen on the next working day, short enough that a row is not
#: labelled by its history forever.
AUTO_RESOLVED_VISIBLE_S = 24 * 3600


def auto_gate_record(gate_id: str, tolerance: str, at: datetime, *,
                     outcome: str = "resolved", detail: dict | None = None) -> dict:
    """The ``Initiative.last_auto_gate`` payload (spec §8) — ONE shape, built here
    so every loop that resolves a gate and every reader agree on the keys.
    ``outcome`` is ``resolved`` or ``refused`` (GitHub declined the write)."""
    return {"gate": gate_id, "at": at.astimezone(timezone.utc).isoformat(),
            "tolerance": tolerance, "outcome": outcome, "detail": dict(detail or {})}


def _summary(record: dict) -> str:
    detail = record.get("detail") or {}
    gate = record.get("gate")
    if record.get("outcome") == "refused":
        return "auto-merge unavailable" if gate == "artifact.merge" else "refused"
    if gate in ("stage.advance", "stage.advance_built") and detail.get("to"):
        return f"advanced to {detail['to']}"
    if gate == "artifact.merge" and detail.get("kind"):
        return f"merged {detail['kind']}"
    if gate == "materialization.accept":
        return "accepted decomposition"
    spec = GATE_BY_ID.get(str(gate))
    return spec.label.lower() if spec is not None else str(gate)


def auto_resolved_view(record: dict | None, now: datetime) -> dict | None:
    """The roadmap's ``autoResolved`` node field: what the machine did with this
    initiative, only while it is still worth a chip (``AUTO_RESOLVED_VISIBLE_S``
    after ``at``). ``None`` for no record, a malformed one, or an expired one —
    the chip leaving the row is the point, not an error."""
    if not record or not isinstance(record, dict):
        return None
    try:
        at = datetime.fromisoformat(str(record.get("at")))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if now.astimezone(timezone.utc) - at > timedelta(seconds=AUTO_RESOLVED_VISIBLE_S):
        return None
    gate = str(record.get("gate") or "")
    spec = GATE_BY_ID.get(gate)
    return {"gate": gate, "summary": _summary(record),
            "reverse": spec.reverse if spec is not None else None,
            "at": at.isoformat(), "outcome": str(record.get("outcome") or "resolved"),
            "tolerance": record.get("tolerance")}
