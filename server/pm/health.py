"""Roadmap drift readout (5S plan, Sustain). Pure: the API loads rows and
calls `roadmap_health`; nothing here decides or mutates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

#: Lifecycle states that claim the capability works today, so should be proven
#: by at least one `tests:` path (#2162).
_TESTED_STATES = frozenset({"live", "delivering"})


@dataclass(frozen=True)
class RoadmapHealth:
    unparented_capabilities: tuple[str, ...]
    unlinked_initiatives: tuple[str, ...]
    stale_proposed: tuple[str, ...]
    stale_inbox: int
    unindexed_docs: tuple[tuple[str, str], ...] = ()
    # pm_sync's classified constitution status for the product's primary repo
    # (issue #2165) — None/"missing"/"template"/"no_version_footer", passed
    # through unchanged from Project.constitution_status; see
    # server/sdlc/constitution_status.classify_document for what each value means.
    governance: str | None = None
    # Capability evidence (#2162), from pm-sync's feature_evidence rows.
    # (slug, kind, path) for every cited path gone from the default branch.
    dangling_evidence: tuple[tuple[str, str, str], ...] = ()
    # live/delivering capabilities that cite no `tests:` path.
    untested_capabilities: tuple[str, ...] = ()
    # capabilities citing `code:` but no `specs:`.
    code_without_specs: tuple[str, ...] = ()
    # capabilities whose doc (and so its `state`) last changed before their
    # newest linked spec did — the spec moved on, the state may not have.
    state_behind_spec: tuple[str, ...] = ()
    # Artifact frontmatter contract (issue #2161). Initiative slugs whose
    # committed artifact names an `epic:` that's closed while the initiative
    # itself isn't `done` — the file and the board disagree about whether the
    # work is finished.
    artifacts_epic_closed_not_done: tuple[str, ...] = ()
    # (initiative slug, capability slug) for every frontmatter `capabilities:`
    # entry that names no indexed Feature in the project.
    artifacts_unknown_capability: tuple[tuple[str, str], ...] = ()
    # Initiative slugs with a committed artifact whose last frontmatter read
    # found no `---` fence at all.
    artifacts_missing_frontmatter: tuple[str, ...] = ()


def _evidence_health(
    caps: list[tuple[str, str | None, str | None, datetime | None]],
    evidence: Iterable[tuple[str, str, str, bool | None, datetime | None]],
    doc_committed: Mapping[str, datetime | None],
) -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    known = {s for s, _, _, _ in caps}
    kinds: dict[str, set[str]] = {}
    newest_spec: dict[str, datetime] = {}
    dangling: list[tuple[str, str, str]] = []
    for slug, kind, path, missing, when in evidence:
        if slug not in known:
            continue  # orphaned capability — not on the map, not drifting
        kinds.setdefault(slug, set()).add(kind)
        if missing:
            dangling.append((slug, kind, path))
        elif kind == "specs" and when is not None:
            cur = newest_spec.get(slug)
            newest_spec[slug] = _aware(when) if cur is None else max(cur, _aware(when))
    untested = tuple(sorted(s for s, _, st, _ in caps
                            if st in _TESTED_STATES and "tests" not in kinds.get(s, ())))
    unspecced = tuple(sorted(s for s, _, st, _ in caps if st != "sunset"
                             and "code" in kinds.get(s, ()) and "specs" not in kinds.get(s, ())))
    behind = tuple(sorted(
        s for s, spec_at in newest_spec.items()
        if (doc_at := doc_committed.get(s)) is not None and _aware(doc_at) < spec_at))
    return tuple(sorted(dangling)), untested, unspecced, behind


def _artifact_health(
    known_capabilities: set[str],
    done_by_slug: Mapping[str, bool],
    artifacts: Iterable[tuple[str, bool | None, str | None, tuple[str, ...]]],
    epic_state: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    epic_closed: set[str] = set()
    unknown_cap: set[tuple[str, str]] = set()
    missing_fm: set[str] = set()
    for slug, present, epic, caps in artifacts:
        if present is False:
            missing_fm.add(slug)
        if epic and epic_state.get(epic) == "closed" and not done_by_slug.get(slug, False):
            epic_closed.add(slug)
        for cap in caps:
            if cap not in known_capabilities:
                unknown_cap.add((slug, cap))
    return tuple(sorted(epic_closed)), tuple(sorted(unknown_cap)), tuple(sorted(missing_fm))


def roadmap_health(*, capabilities: Iterable[tuple[str, str | None, str | None, datetime | None]],
                   initiatives: Iterable[tuple[str, int, bool, bool]],
                   proposals: Iterable[datetime | None],
                   now: datetime, stale_days: int = 30, inbox_days: int = 7,
                   unindexed_docs: Iterable[tuple[str, str]] = (),
                   governance: str | None = None,
                   evidence: Iterable[tuple[str, str, str, bool | None, datetime | None]] = (),
                   doc_committed: Mapping[str, datetime | None] | None = None,
                   artifacts: Iterable[tuple[str, bool | None, str | None, tuple[str, ...]]] = (),
                   epic_state: Mapping[str, str] | None = None,
                   ) -> RoadmapHealth:
    caps = list(capabilities)
    inits = list(initiatives)
    has_child = {p for _, p, _, _ in caps if p}
    tree_in_use = any(p is None and s in has_child for s, p, _, _ in caps)
    unparented = tuple(sorted(s for s, p, _, _ in caps
                              if tree_in_use and p is None and s not in has_child))
    # A program (`prog`) is judged by its phases, never itself — and a `done`
    # initiative (leaf or program) is finished, not drifting, so neither
    # belongs in the "needs a capability" readout.
    unlinked = tuple(sorted(s for s, n, prog, done in inits if n == 0 and not prog and not done))
    cutoff = now - timedelta(days=stale_days)
    stale = tuple(sorted(s for s, _, st, upd in caps
                         if st == "proposed" and upd is not None and _aware(upd) < cutoff))
    inbox_cutoff = now - timedelta(days=inbox_days)
    stale_inbox = sum(1 for c in proposals if c is not None and _aware(c) < inbox_cutoff)
    unindexed = tuple(sorted(unindexed_docs))
    dangling, untested, unspecced, behind = _evidence_health(caps, evidence, doc_committed or {})
    done_by_slug = {s: done for s, _, _, done in inits}
    known_caps = {s for s, _, _, _ in caps}
    epic_closed, unknown_cap, missing_fm = _artifact_health(
        known_caps, done_by_slug, artifacts, epic_state or {})
    return RoadmapHealth(unparented, unlinked, stale, stale_inbox, unindexed, governance,
                         dangling, untested, unspecced, behind,
                         epic_closed, unknown_cap, missing_fm)


def _aware(dt: datetime) -> datetime:
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
