"""Dependency-readiness gate for Backlog Runner candidates (pure, no I/O).

An issue is *blocked* — not ready for a session — while any dependency is
still open. Three OR'd signal sources (:func:`blocked_refs`):

1. **Native relationships** — the GitHub ``blockedBy`` connection (the
   "Relationships → Blocked by" field). States arrive inline with the
   candidate search, so this source never needs an extra API call.
2. **Body text** — ``Depends on #N`` / ``Blocked by #N`` markers in the
   candidate's own body (cross-repo ``owner/repo#N`` supported). These refs
   carry no state; the caller resolves them (batched) and passes ``states``.
3. **Epic ordering** — list order is execution order (a deliberate product
   decision): a sub-issue is blocked while any *earlier* sibling in its
   ``parent``'s sub-issue list is open, and a task-list child is blocked
   while any earlier checklist item in a tracking epic's body is open.

"Done" is any non-OPEN state (``CLOSED``, and ``MERGED`` for a PR resolved
via ``issueOrPullRequest``). An unresolvable state fails CLOSED — treated as
blocking — because honoring declared order is the feature's whole point and
the sweep re-evaluates every tick; the skip is logged by the caller.
"""
from __future__ import annotations

import re
from typing import Mapping

_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"

# ``Depends on`` / ``Blocked by`` marker, optionally followed by a colon.
_MARKER_RE = re.compile(r"\b(?:depends\s+on|blocked\s+by)\b:?", re.IGNORECASE)

# One issue ref following a marker: separators (commas / "and") then
# ``#N`` or ``owner/repo#N``. Matched repeatedly, anchored, to consume a run.
_REF_TOKEN_RE = re.compile(rf"\s*(?:(?:,|\band\b)\s*)?((?:{_REPO})?#\d+)")

# A markdown task-list item: ``- [ ] ...`` / ``* [x] ...`` (also ``+``).
_CHECKLIST_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.*)$")

# First issue ref inside a checklist item's text.
_REF_RE = re.compile(rf"((?:{_REPO})?#\d+)")


def _canonical(ref: str, repo: str) -> str:
    """``#12`` → ``owner/repo#12``; already-qualified refs pass through."""
    return ref if "/" in ref else f"{repo}{ref}"


def _node_repo(node: dict) -> str:
    return ((node.get("repository") or {}).get("nameWithOwner")) or ""


def _node_ref(node: dict) -> str:
    return f"{_node_repo(node)}#{node.get('number')}"


def _is_done(state: "str | None") -> bool:
    """Any known non-OPEN state (CLOSED, MERGED). Unknown is NOT done."""
    return state is not None and state != "OPEN"


def parse_body_dep_refs(body: "str | None", repo: str) -> list[str]:
    """Canonical refs named by ``Depends on`` / ``Blocked by`` markers in *body*.

    Each marker may introduce a run of refs (``Depends on #1, #2 and o/r#3``).
    A bare ``#N`` mention without a marker is NOT a dependency. Order of
    appearance is preserved; duplicates are dropped.
    """
    if not body:
        return []
    out: list[str] = []
    for marker in _MARKER_RE.finditer(body):
        pos = marker.end()
        while (m := _REF_TOKEN_RE.match(body, pos)) is not None:
            ref = _canonical(m.group(1), repo)
            if ref not in out:
                out.append(ref)
            pos = m.end()
    return out


def parse_checklist_refs(body: "str | None", repo: str) -> "list[tuple[str, bool]]":
    """Ordered ``(canonical_ref, checked)`` for task-list items in *body*.

    Items without an issue ref (plain-text checklist lines) are skipped —
    only issues participate in execution ordering.
    """
    out: list[tuple[str, bool]] = []
    for line in (body or "").splitlines():
        item = _CHECKLIST_RE.match(line)
        if item is None:
            continue
        ref = _REF_RE.search(item.group(2))
        if ref is None:
            continue
        out.append((_canonical(ref.group(1), repo), item.group(1) in "xX"))
    return out


def body_dep_refs(node: dict) -> set[str]:
    """The candidate's body-text dependency refs — the ones whose open/closed
    state the caller must resolve externally before calling :func:`blocked_refs`."""
    return set(parse_body_dep_refs(node.get("body"), _node_repo(node)))


def _earlier_open_subissue_siblings(node: dict) -> list[str]:
    """Blockers from the native sub-issue list: earlier siblings still open.
    The ``subIssues`` connection preserves the UI's list order."""
    parent = node.get("parent") or {}
    siblings = ((parent.get("subIssues") or {}).get("nodes")) or []
    self_ref = _node_ref(node)
    ordered = [s for s in siblings if s]
    refs = [_node_ref(s) for s in ordered]
    if self_ref not in refs:  # defensive: stale/edited list imposes no order
        return []
    out: list[str] = []
    for sibling, ref in zip(ordered, refs):
        if ref == self_ref:
            break
        if not _is_done(sibling.get("state")):
            out.append(ref)
    return out


def _earlier_open_epic_items(node: dict) -> list[str]:
    """Blockers from task-list epics that track this candidate: checklist items
    listed above it that are not done. State comes from the epic's
    ``trackedIssues`` nodes; an item with no known state falls back to its
    checkbox (checked = done), else fails closed (blocks).
    """
    self_ref = _node_ref(node)
    out: list[str] = []
    for epic in ((node.get("trackedInIssues") or {}).get("nodes")) or []:
        if not epic:
            continue
        epic_repo = _node_repo(epic)
        items = parse_checklist_refs(epic.get("body"), epic_repo)
        if self_ref not in [ref for ref, _ in items]:
            continue  # tracked via a non-checklist mention — no order implied
        tracked = {_node_ref(t): t.get("state")
                   for t in ((epic.get("trackedIssues") or {}).get("nodes")) or [] if t}
        for ref, checked in items:
            if ref == self_ref:
                break
            state = tracked.get(ref)
            done = _is_done(state) if state is not None else checked
            if not done and ref not in out:
                out.append(ref)
    return out


def blocked_refs(node: dict, *, states: "Mapping[str, str] | None" = None) -> list[str]:
    """Refs of *node*'s still-open (or unresolvable — fail closed) blockers.

    Empty list = ready to work. *states* maps canonical refs to their known
    GitHub state (``OPEN``/``CLOSED``/``MERGED``) for body-text deps; native
    ``blockedBy`` / sub-issue / epic states arrive inline on *node*. A node
    missing the dependency fields entirely (older fixture / API shape) has no
    declared dependencies and is ready.
    """
    known = states or {}
    out: list[str] = []

    for dep in ((node.get("blockedBy") or {}).get("nodes")) or []:
        if dep and not _is_done(dep.get("state")):
            ref = _node_ref(dep)
            if ref not in out:
                out.append(ref)

    for ref in parse_body_dep_refs(node.get("body"), _node_repo(node)):
        if not _is_done(known.get(ref)) and ref not in out:
            out.append(ref)

    for ref in _earlier_open_subissue_siblings(node):
        if ref not in out:
            out.append(ref)

    for ref in _earlier_open_epic_items(node):
        if ref not in out:
            out.append(ref)

    return out
