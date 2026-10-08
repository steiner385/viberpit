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


@dataclass(frozen=True)
class FrameworkPolicySnapshot:
    """Read-only declaration and GitHub provenance snapshot, never gate authority.

    Git object IDs identify the bytes observed; they are deliberately distinct
    from agentctl's canonical framework digest. Subject head is never inferred
    from the default-branch policy revision or an adopted artifact declaration.
    """

    repo: str
    status: str
    policy_revision: str | None = None
    contract_oid: str | None = None
    lock_oid: str | None = None
    frameworks: tuple[dict, ...] = ()
    head_sha: str | None = None
    author: str | None = None
    base_sha: str | None = None
    framework_digest: str | None = None
    policy_digest: str | None = None
    changed_paths: tuple[str, ...] = ()
    scope_complete: bool = False
    reviews: tuple[dict, ...] = ()
    reviews_complete: bool = False


@dataclass(frozen=True)
class FrameworkGate:
    """Whether required framework evidence holds an SDLC artifact transition.

    ``reasons`` are short, stable, sorted ``<framework>/<concern>: ...`` strings
    so a feed event and a latch can key on them."""

    blocking: bool
    reasons: tuple[str, ...] = ()


#: Admission states whose in-scope concerns must never pass a non-observe gate.
_WITHDRAWN = frozenset({"rejected", "revoked"})

#: THE block reason when a gating policy may apply but cannot be judged: the
#: default-branch policy read failed, or the PR's scope/reviews did not load.
FRAMEWORK_EVIDENCE_UNREADABLE = "framework evidence unreadable"


def _exception_waives(entry: dict, concern: str, snapshot: FrameworkPolicySnapshot,
                      now: datetime, pull_request: int | None) -> bool:
    """A declared exception waives one concern on one PR until it expires, and
    only when its owner is a known principal other than the PR author: an
    author cannot excuse their own change."""
    from server.sdlc.framework_policy import policy

    author = snapshot.author
    if pull_request is None or not author:
        return False
    when = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    for item in (entry.get("observation") or {}).get("exceptions", []):
        if item["concern"] != concern or item["pull_request"] != pull_request:
            continue
        if policy.same_principal(item["owner"], author):
            continue
        if when < policy.parse_expiry(item["expires_at"]):
            return True
    return False


def _obligations(snapshot: FrameworkPolicySnapshot, now: datetime, pull_request: int | None):
    """THE per-obligation evaluation both the gate and the observation payload
    read, so the displayed ``blocking`` can never disagree with the decision.

    Yields ``(entry, concern, rule, mode, applicability, evidence_status,
    block_reason)``; ``block_reason`` is None when the obligation does not block.
    """
    from server.sdlc.framework_policy import policy
    from server.sdlc.framework_evidence import review_observation

    for entry in snapshot.frameworks:
        for concern, rule in (entry.get("observation") or {}).get("concerns", {}).items():
            mode = policy.effective_mode(entry, concern)
            applicability = policy.applicability(rule, snapshot.changed_paths, snapshot.scope_complete)
            status = "missing"
            if rule["tier"] == "required_review" and applicability == "applicable":
                status = review_observation(snapshot, entry, concern)
            reason, waivable = None, True
            # Only a loaded default-branch policy is authority; observe never
            # blocks and advisory findings never masquerade as failures.
            if snapshot.status == "loaded" and mode != "observe" and rule["tier"] != "advisory":
                withdrawn = entry["state"] in _WITHDRAWN
                if applicability == "unknown":
                    # Unknown scope needs triage, never an automatic skip.
                    if mode == "enforced":
                        reason = "framework revoked" if withdrawn else "applicability unknown (needs triage)"
                elif applicability == "applicable":
                    if withdrawn:
                        # Revocation never turns into a pass, even with a review.
                        reason = "framework revoked"
                    elif rule["tier"] == "deterministic":
                        reason = "deterministic evidence unavailable (no trusted runner ingestion)"
                    elif status == "invalid":
                        # A trusted principal's malformed or withdrawn record is a
                        # finding to fix, not an absence an exception may excuse.
                        reason, waivable = "required review invalid", False
                    elif status == "stale":
                        reason = "required review stale"
                    elif status != "review_recorded":
                        reason = "required review missing"
                if reason is not None and waivable and _exception_waives(
                        entry, concern, snapshot, now, pull_request):
                    reason = None
            yield (entry, concern, rule, mode, applicability, status,
                   None if reason is None else f"{entry['id']}/{concern}: {reason}")


def framework_gate(snapshot: FrameworkPolicySnapshot, now: datetime, *,
                   pull_request: int | None) -> FrameworkGate:
    """Pure gate over default-branch declarations and the subject PR's scope and
    reviews. ``no_contract``/``legacy_contract``/``unavailable`` know no
    obligation, so they never block here; a loaded policy whose PR scope could
    not be read reaches this as incomplete scope and blocks per mode."""
    reasons = sorted({reason for *_, reason in _obligations(snapshot, now, pull_request)
                      if reason is not None})
    return FrameworkGate(blocking=bool(reasons), reasons=tuple(reasons))


def _unread(snapshot: FrameworkPolicySnapshot) -> bool:
    """A failed or moving PR read leaves no head or an empty, incomplete scope:
    an unread subject, not an "unknown" one evidence_required may let through."""
    return (snapshot.status != "loaded" or snapshot.head_sha is None
            or (not snapshot.scope_complete and not snapshot.changed_paths))


def framework_gate_for(policy_snapshot: FrameworkPolicySnapshot,
                       pr_snapshot: FrameworkPolicySnapshot | None, now: datetime, *,
                       pull_request: int) -> FrameworkGate:
    """The gate for one subject PR, shared by the synthesizer seam and the API
    observation so their ``blocking`` cannot disagree. A failed policy read, or
    a gating policy whose PR evidence did not load, blocks rather than passes;
    ``no_contract``/``legacy_contract`` are successful reads with no obligation.
    """
    if policy_snapshot.status == "unavailable":
        return FrameworkGate(True, (FRAMEWORK_EVIDENCE_UNREADABLE,))
    if not has_gating_concern(policy_snapshot):
        return FrameworkGate(False)
    if pr_snapshot is None or _unread(pr_snapshot):
        return FrameworkGate(True, (FRAMEWORK_EVIDENCE_UNREADABLE,))
    return framework_gate(pr_snapshot, now, pull_request=pull_request)


def has_gating_concern(snapshot: FrameworkPolicySnapshot) -> bool:
    """Whether a loaded policy configures any non-observe, non-advisory concern —
    the only case worth a PR-scoped read."""
    from server.sdlc.framework_policy import policy

    return snapshot.status == "loaded" and any(
        policy.effective_mode(entry, concern) != "observe" and rule["tier"] != "advisory"
        for entry in snapshot.frameworks
        for concern, rule in (entry.get("observation") or {}).get("concerns", {}).items())


def observe_framework_evidence(
    snapshot: FrameworkPolicySnapshot, claims: list[dict] | None = None, *,
    now: datetime | None = None, pull_request: int | None = None,
) -> dict:
    """Inventory, diagnose untrusted supporting claims, and report the gate.

    Submitted claims never authenticate. Explicit records collected through the
    GitHub reader may attest a configured principal; whether that satisfies a
    gate is ``framework_gate``'s decision, reported here, never re-derived.
    Unknown scope cannot skip an obligation.
    """
    from server.sdlc.framework_policy import policy

    now = now or datetime.now(timezone.utc)
    diagnostics = []
    selected = {entry["id"] for entry in snapshot.frameworks}
    for claim in claims or []:
        reasons = ["unauthenticated_producer"]
        if claim.get("framework") not in selected:
            reasons.append("framework_not_selected")
        if claim.get("repo") != snapshot.repo:
            reasons.append("wrong_repository")
        if snapshot.head_sha is None:
            reasons.append("subject_revision_unknown")
        elif claim.get("headSha") != snapshot.head_sha:
            reasons.append("stale_head")
        if snapshot.policy_revision is None:
            reasons.append("policy_revision_unknown")
        elif claim.get("policyRevision") != snapshot.policy_revision:
            reasons.append("stale_policy")
        reviewer = claim.get("reviewer")
        if snapshot.author and policy.same_principal(reviewer, snapshot.author):
            reasons.append("self_review")
        diagnostics.append({"framework": claim.get("framework"), "trusted": False,
                            "reasons": reasons})
    by_entry: dict[str, list[dict]] = {}
    modes: set[str] = set()
    for entry, concern, rule, mode, applicability, status, reason in _obligations(
            snapshot, now, pull_request):
        modes.add(mode)
        by_entry.setdefault(entry["id"], []).append(
            {"concern": concern, "tier": rule["tier"], "mode": mode,
             "applicability": applicability, "evidenceStatus": status,
             "blocking": reason is not None})
    frameworks = []
    for entry in snapshot.frameworks:
        obligations = by_entry.get(entry["id"], [])
        row = {"id": entry["id"], "state": entry["state"], "concerns": entry["concerns"],
               "obligations": obligations}
        if not obligations:
            row.update(applicability="unknown", tier="unconfigured",
                       evidenceStatus="untrusted" if any(c["framework"] == entry["id"] for c in diagnostics) else "missing")
        # Configured obligations are the sole source of concern-level statuses;
        # an aggregate would hide mixed tiers or contradict individual scopes.
        frameworks.append(row)
    # With a subject PR the one snapshot is both policy and evidence, judged by
    # the same rule the synthesizer applies before an artifact transition.
    decision = (framework_gate(snapshot, now, pull_request=None) if pull_request is None
                else framework_gate_for(snapshot, snapshot, now, pull_request=pull_request))
    mode = "observe" if not modes else modes.pop() if len(modes) == 1 else "mixed"
    reason = ("Observe only. " if mode == "observe" else
              "Non-observe concerns gate SDLC artifact merges and advances. ")
    return {
        "mode": mode, "blocking": decision.blocking, "gateReasons": list(decision.reasons),
        "policyStatus": snapshot.status,
        "repo": snapshot.repo, "policyRevision": snapshot.policy_revision,
        "contractBlobOid": snapshot.contract_oid, "lockBlobOid": snapshot.lock_oid,
        "frameworkDigest": snapshot.framework_digest, "policyDigest": snapshot.policy_digest,
        "subjectHeadSha": snapshot.head_sha, "subjectBaseSha": snapshot.base_sha,
        "scopeComplete": snapshot.scope_complete,
        "scopeDigest": policy.scope_digest(snapshot.changed_paths) if snapshot.scope_complete else None,
        "reviewsComplete": snapshot.reviews_complete,
        "reason": reason + "Recorded reviews attest configured GitHub principals, not execution, correctness or admission.",
        "frameworks": frameworks, "claims": diagnostics,
    }
