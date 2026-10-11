"""Input identity for a durable feature-map disposition, and the map-diff key."""
from server.db.models import TriageProposal, WorkItem
from server.pm.outcomes import body_fingerprint

#: The separator in a map-diff session's ``origin_ref``/``dedupe_key``.
_MAP_DIFF_SEP = ":map-diff:"


def map_diff_key(project_slug: str, suggestion: str) -> str:
    """THE map-diff dedupe key, ``<project>:map-diff:<suggestion>``.

    One capability of one product maps to one key, so the spawn dedupe, the
    drift latch and the disposition observer all agree on which sessions and
    PRs belong to it (#2926)."""
    return f"{project_slug}{_MAP_DIFF_SEP}{suggestion}"


def parse_map_diff_key(ref: str | None) -> tuple[str, str] | None:
    """``(project_slug, suggestion)`` from a :func:`map_diff_key`, or ``None``
    for anything else, including a key with an empty project or suggestion."""
    project, sep, suggestion = str(ref or "").partition(_MAP_DIFF_SEP)
    project, suggestion = project.strip(), suggestion.strip()
    if not sep or not project or not suggestion:
        return None
    return project, suggestion


def map_resolution_inputs(proposal: TriageProposal, item: WorkItem) -> dict:
    """Scope evidence to one accepted decision and the work it actually evaluated."""
    from server.pm.triage_accept import _aware
    resolved = _aware(proposal.resolved_at)
    return {"repo": item.repo, "number": item.number, "kind": item.kind,
            "suggestion": proposal.no_fit_suggestion,
            "bodyFingerprint": body_fingerprint(item.title, item.body),
            "resolvedAt": resolved.isoformat() if resolved else None}
