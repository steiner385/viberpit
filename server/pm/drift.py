"""Drift audit for the 003 mirror: where GitHub's declared planning state and the
conventions ccswitch consumes disagree (spec 003 Phase A deliverable)."""
from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass, field

from server.automations.backlog_deps import parse_checklist_refs


@dataclass(frozen=True)
class DepEdgeView:
    src_ref: str
    dst_ref: str
    source: str
    active: bool
    dst_state: str | None


@dataclass
class DriftReport:
    prose_only: list[dict] = field(default_factory=list)
    unresolved_targets: list[dict] = field(default_factory=list)
    missing_from_project: list[str] = field(default_factory=list)
    checklist_epics: list[str] = field(default_factory=list)


def compute_drift(edges: list[DepEdgeView], open_refs: set[str],
                  project_refs: set[str],
                  bodies: Mapping[str, str | None],
                  epic_refs: Set[str] = frozenset()) -> DriftReport:
    report = DriftReport()
    active = [e for e in edges if e.active]
    native = {(e.src_ref, e.dst_ref) for e in active if e.source == "native"}
    seen: set[tuple[str, str]] = set()
    for edge in active:
        pair = (edge.src_ref, edge.dst_ref)
        if edge.source in ("prose", "order") and pair not in native and pair not in seen:
            seen.add(pair)
            report.prose_only.append({"src": edge.src_ref, "dst": edge.dst_ref})
        if edge.dst_state is None:
            report.unresolved_targets.append(
                {"src": edge.src_ref, "dst": edge.dst_ref, "source": edge.source})
    report.missing_from_project = sorted(open_refs - project_refs)
    for ref in sorted(open_refs):
        if ref in epic_refs:
            continue
        repo = ref.rsplit("#", 1)[0]
        if parse_checklist_refs(bodies.get(ref), repo):
            report.checklist_epics.append(ref)
    return report
