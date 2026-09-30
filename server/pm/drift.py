"""Drift audit for the 003 mirror: where GitHub's declared planning state and the
conventions ViberPit consumes disagree (spec 003 Phase A deliverable)."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
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


def reverse_blockers(edges: Iterable[DepEdgeView]) -> dict[str, set[str]]:
    """THE reverse dependency index: blocker ref -> the refs it blocks (spec
    2026-09-30 section 3). Active edges only (an inactive row is a dep that was removed
    or never resolved) and a self-edge is a parser artefact, not a block. Shared by the
    deps summary here and the escalation read model (`server/pm/escalation.py`)."""
    out: dict[str, set[str]] = {}
    for e in edges:
        if not e.active or not e.src_ref or not e.dst_ref or e.src_ref == e.dst_ref:
            continue
        out.setdefault(e.dst_ref, set()).add(e.src_ref)
    return out


def top_blockers(edges: Iterable[DepEdgeView], open_refs: Set[str], *, n: int = 10,
                 states: Mapping[str, str | None] | None = None) -> list[dict]:
    """The `n` refs that block the most OPEN issues, for the deps summary."""
    rev = reverse_blockers(edges)
    rows: list[dict] = [{"ref": ref, "blocks": len(srcs & open_refs),
                         "state": (states or {}).get(ref)} for ref, srcs in rev.items()]
    rows = [r for r in rows if r["blocks"]]
    rows.sort(key=lambda r: (-r["blocks"], r["ref"]))
    return rows[:n]
