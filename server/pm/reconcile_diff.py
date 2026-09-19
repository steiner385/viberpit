"""Pure diff engine for the 003 reconciler (spec §6 steps 2–3). No I/O."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import datetime

from .desired_state import DesiredItem, is_held_status


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


def _same(a: object, b: object, *, field: str | None) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    # Rollout tolerance (spec 2026-09-18 §9): a board whose Status option is still
    # spelled "Needs human" already holds the card our decision calls "Held" — the
    # two are ONE column. Without this the diff would re-plan the same mutation on
    # every tick, and `_render` would keep resolving it to that same option id.
    # Status-scoped on purpose: no other field carries the alias.
    if (field == "Status" and isinstance(a, str) and isinstance(b, str)
            and is_held_status(a) and is_held_status(b)):
        return True
    return a == b


def diff_fields(desired: DesiredItem, mirror: MirrorItem,
                own_values: Mapping[tuple[str, str], object] = {}) -> list[Mutation]:
    """Plan the field mutations for one item.

    ``own_values`` maps ``(item_node_id, field)`` → the value the reconciler last
    APPLIED (``journal.own_applied_values``). The ownership rule (§3) is "human
    edits win", and our own past write is not a human decision — but it DID stamp
    the item's ``gh_updated_at`` with our write time, which would out-rank every
    older decision forever (e.g. we push "In progress" for a live session; the
    session ends; Status falls through to Ready carrying the older ``classified_at``
    and can never be pushed again). So a mirrored value that still equals our own
    last write is treated as UNSET for the ownership check. Nothing else changes:
    a value a human has since altered no longer matches, and the initial-fill rule
    in ``should_push`` is untouched."""
    out: list[Mutation] = []
    for field, decision, current, tag in (
            ("Status", desired.status, mirror.status, "status"),
            ("Score", desired.score, mirror.score, "score"),
            ("Stage", desired.stage, mirror.stage, "stage")):
        if decision.value is None or _same(decision.value, current, field=field):
            continue
        own = own_values.get((mirror.item_node_id, field))
        mirrored = None if (own is not None and _same(current, own, field=field)) else current
        if not should_push(decision.decided_at, mirror.gh_updated_at, mirrored):
            continue
        out.append(Mutation("set_field", mirror.item_node_id, field, current,
                            decision.value, f"{tag}:{desired.ref}"))
    return out


def membership(open_issue_nodes: Mapping[str, str], mirrored_refs: Set[str],
               project_node_id: str) -> list[Mutation]:
    return [Mutation("add_item", project_node_id, None, None, node, f"membership:{ref}")
            for ref, node in sorted(open_issue_nodes.items()) if ref not in mirrored_refs]


def remove_blocked_by_mutation(blocked_node: str, blocker_node: str, *,
                               initiated_by: str) -> Mutation:
    """The inverse of an `add_blocked_by` edge, as the reconciler's own `Mutation`.

    Same field layout as the add (target = the blocked issue, new_value = the
    blocker) so ONE key shape covers both directions, and `compile_edges` can
    recognise an applied removal by key and stop re-compiling the edge from a
    prose dep that still names it in the issue body."""
    return Mutation("remove_blocked_by", blocked_node, None, None, blocker_node, initiated_by)


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
        # A human deleted this edge from the app (`dep_remove`): the applied
        # removal is the newer decision, and the body's prose does not re-add it.
        if remove_blocked_by_mutation(src, dst, initiated_by="").key() in already:
            continue
        seen.add(pair)
        out.append(m)
    return out
