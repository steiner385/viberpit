"""Input identity for a durable feature-map disposition."""
from server.db.models import TriageProposal, WorkItem
from server.pm.outcomes import body_fingerprint


def map_resolution_inputs(proposal: TriageProposal, item: WorkItem) -> dict:
    """Scope evidence to one accepted decision and the work it actually evaluated."""
    from server.pm.triage_accept import _aware
    resolved = _aware(proposal.resolved_at)
    return {"repo": item.repo, "number": item.number, "kind": item.kind,
            "suggestion": proposal.no_fit_suggestion,
            "bodyFingerprint": body_fingerprint(item.title, item.body),
            "resolvedAt": resolved.isoformat() if resolved else None}
