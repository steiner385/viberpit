"""Pure diff engine for the 003 reconciler (spec §6 steps 2–3). No I/O."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import datetime

from .desired_state import DesiredItem


@dataclass(frozen=True)
class Mutation:
    kind: str
    target_node_id: str
    field: str | None
    old_value: object
    new_value: object
    initiated_by: str

    def key(self) -> str:
        raw = "|".join([self.kind, self.target_node_id, self.field or "",
                        json.dumps(self.new_value, sort_keys=True, default=str)])
        return hashlib.sha1(raw.encode()).hexdigest()


@dataclass(frozen=True)
class MirrorItem:
    ref: str
    item_node_id: str
    status: str | None
    score: float | None
    stage: str | None
    gh_updated_at: datetime | None


@dataclass(frozen=True)
class DepView:
    src_ref: str
    dst_ref: str
    source: str
    active: bool


def should_push(decided_at: datetime | None, gh_updated_at: datetime | None,
                mirrored_value: object) -> bool:
    if mirrored_value is None:
        return True
    if decided_at is None or gh_updated_at is None:
        return decided_at is not None
    return decided_at > gh_updated_at


def _same(a: object, b: object) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    return a == b


def diff_fields(desired: DesiredItem, mirror: MirrorItem) -> list[Mutation]:
    out: list[Mutation] = []
    for field, decision, current, tag in (
            ("Status", desired.status, mirror.status, "status"),
            ("Score", desired.score, mirror.score, "score"),
            ("Stage", desired.stage, mirror.stage, "stage")):
        if decision.value is None or _same(decision.value, current):
            continue
        if not should_push(decision.decided_at, mirror.gh_updated_at, current):
            continue
        out.append(Mutation("set_field", mirror.item_node_id, field, current,
                            decision.value, f"{tag}:{desired.ref}"))
    return out


def membership(open_issue_nodes: Mapping[str, str], mirrored_refs: Set[str],
               project_node_id: str) -> list[Mutation]:
    return [Mutation("add_item", project_node_id, None, None, node, f"membership:{ref}")
            for ref, node in sorted(open_issue_nodes.items()) if ref not in mirrored_refs]


def compile_edges(deps: Iterable[DepView], node_ids: Mapping[str, str],
                  already: Set[str]) -> list[Mutation]:
    deps = list(deps)
    native = {(d.src_ref, d.dst_ref) for d in deps if d.source == "native" and d.active}
    out: list[Mutation] = []
    seen: set[tuple[str, str]] = set()
    for d in deps:
        pair = (d.src_ref, d.dst_ref)
        if not d.active or d.source == "native" or pair in native or pair in seen:
            continue
        src, dst = node_ids.get(d.src_ref), node_ids.get(d.dst_ref)
        if not src or not dst:
            continue
        m = Mutation("add_blocked_by", src, None, None, dst,
                     f"compile:{d.source}:{d.src_ref}->{d.dst_ref}")
        if m.key() in already:
            continue
        seen.add(pair)
        out.append(m)
    return out
