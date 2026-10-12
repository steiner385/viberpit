"""Stale dependency edges: active ``WorkItemDep`` pairs that no longer describe a real
block (gap log 2026-10-11). Pure — the API loads the rows and applies the result.

Since #2979 a resolved blocker is already inert (``drift.is_open_blocker`` is what every
surface asks), so a stale edge blocks nothing: pruning one is hygiene for the dependency
map and its counts, never an unblock.

Three kinds, each with an honest answer to "can it be pruned so it STAYS pruned?":

- ``native_resolved`` — a GitHub blocked-by edge whose blocker is closed/merged.
  Prunable: removing it on GitHub (``dep_remove``) records the applied
  ``remove_blocked_by`` key that keeps the mirror from re-deriving the pair.
- ``prose_vanished`` — a ``prose`` edge whose ``Depends on`` text is no longer in the
  source issue's body (the mirror just has not re-synced). Prunable locally: the next
  relations sync would deactivate it too, and cannot re-create it, because the text the
  mirror derives prose edges from is gone.
- ``derived_resolved`` — a ``prose``/``order`` edge whose blocker is resolved but which
  the mirror still derives on every sync (the text is still in the body, or the epic's
  sibling order still implies it). NOT prunable here: a local deactivation would be
  re-activated on the next sync. Editing the issue body (or the epic order) is the fix.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass

from server.automations.backlog_deps import parse_body_dep_refs

from .drift import DepEdgeView, is_open_blocker

STALE_KINDS: tuple[str, ...] = ("native_resolved", "prose_vanished", "derived_resolved")
PRUNABLE_KINDS: frozenset[str] = frozenset({"native_resolved", "prose_vanished"})
# Edges per prune call: each native one is a GitHub write, and the confirmation card
# lists every edge. Matches the assistant's bulk cap (tools.py `_BATCH_MAX`).
DEP_PRUNE_MAX = 20


@dataclass(frozen=True)
class StaleEdge:
    src: str
    blocker: str
    kind: str
    blocker_state: str | None
    sources: tuple[str, ...]

    @property
    def prunable(self) -> bool:
        return self.kind in PRUNABLE_KINDS

    def to_wire(self) -> dict:
        return {"src": self.src, "blocker": self.blocker, "kind": self.kind,
                "blockerState": self.blocker_state, "sources": list(self.sources),
                "prunable": self.prunable}


def classify_stale(edges: Iterable[DepEdgeView], *, open_srcs: Set[str],
                   bodies: Mapping[str, str | None]) -> list[StaleEdge]:
    """Stale pairs among the ACTIVE edges whose source is an open issue (a closed
    source's edges gate nothing). ``bodies`` maps a source ref to its mirrored body;
    a missing or ``None`` body is unknown text and never reads as "vanished"."""
    pairs: dict[tuple[str, str], tuple[set[str], str | None]] = {}
    for e in edges:
        if not e.active or not e.src_ref or not e.dst_ref or e.src_ref not in open_srcs:
            continue
        sources, _ = pairs.get((e.src_ref, e.dst_ref), (set(), None))
        sources.add(e.source)
        pairs[(e.src_ref, e.dst_ref)] = (sources, e.dst_state)
    out: list[StaleEdge] = []
    for (src, dst), (sources, state) in sorted(pairs.items()):
        resolved = not is_open_blocker(state)
        kind: str | None = None
        if "native" in sources:
            kind = "native_resolved" if resolved else None
        else:
            body = bodies.get(src)
            prose_gone = ("prose" in sources and body is not None
                          and dst not in parse_body_dep_refs(body, src.rsplit("#", 1)[0]))
            still_derived = "order" in sources or ("prose" in sources and not prose_gone)
            if prose_gone and not still_derived:
                kind = "prose_vanished"
            elif resolved:
                kind = "derived_resolved"
        if kind is not None:
            out.append(StaleEdge(src, dst, kind, state, tuple(sorted(sources))))
    return out
