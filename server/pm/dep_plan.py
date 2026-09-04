"""Pure dependency-observation planner for the 003 mirror (read-only in Phase A).

Prose parsing is delegated to backlog_deps — the single source of truth for
`Depends on #N` conventions. Order edges chain consecutive sub-issue siblings,
mirroring the "phase order = sibling blocked-by" convention of spec 003 §4.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from server.automations.backlog_deps import parse_body_dep_refs

from .gh_projects import IssueRelView


@dataclass(frozen=True)
class ObservedDep:
    src_ref: str
    dst_ref: str
    source: str


@dataclass(frozen=True)
class DepRowView:
    id: str
    src_ref: str
    dst_ref: str
    source: str
    active: bool


@dataclass
class DepPlan:
    create: list[ObservedDep]
    reactivate: list[str]
    deactivate: list[str]


def observed_deps(repo: str, rels: list[IssueRelView],
                  bodies: Mapping[str, str | None]) -> list[ObservedDep]:
    out: list[ObservedDep] = []
    seen: set[tuple[str, str, str]] = set()

    def add(src: str, dst: str, source: str) -> None:
        if src == dst:
            return
        key = (src, dst, source)
        if key not in seen:
            seen.add(key)
            out.append(ObservedDep(src, dst, source))

    for rel in rels:
        src = f"{rel.repo}#{rel.number}"
        for dst in rel.blocked_by:
            add(src, dst, "native")
        # Cross-repo sub-issues are excluded from Phase A order edges (src-prefix
        # scoping in mirror_store requires same-repo src); Phase B compiler revisits.
        same_repo_subs = [ref for ref in rel.sub_issue_refs
                          if ref.startswith(f"{rel.repo}#")]
        for prev, cur in zip(same_repo_subs, same_repo_subs[1:]):
            add(cur, prev, "order")
    for src, body in bodies.items():
        for dst in parse_body_dep_refs(body, repo):
            add(src, dst, "prose")
    return out


def plan_dep_rows(observed: Iterable[ObservedDep],
                  existing: Iterable[DepRowView]) -> DepPlan:
    wanted = {(d.src_ref, d.dst_ref, d.source): d for d in observed}
    plan = DepPlan(create=[], reactivate=[], deactivate=[])
    for row in existing:
        key = (row.src_ref, row.dst_ref, row.source)
        if key in wanted:
            wanted.pop(key)
            if not row.active:
                plan.reactivate.append(row.id)
        elif row.active:
            plan.deactivate.append(row.id)
    plan.create = list(wanted.values())
    return plan
