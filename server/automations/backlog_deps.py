"""Dependency-marker parsing for the mirrored dependency graph (pure, no I/O).

Two text signals GitHub has no first-class field for, harvested from issue
bodies so the reconciler can compile them into ``WorkItemDep`` edges alongside
the native ``blockedBy`` relationships (spec 003 §7a):

1. **Body markers** — ``Depends on #N`` / ``Blocked by #N`` (cross-repo
   ``owner/repo#N`` supported): :func:`parse_body_dep_refs`.
2. **Epic checklists** — a tracking issue's task list, where list order is
   execution order (a deliberate product decision): :func:`parse_checklist_refs`
   returns the items in order with their checkbox state.

The readers are :mod:`server.pm.dep_plan` (graph compilation) and
:mod:`server.pm.drift`. Gating itself is no longer here: the runner selects
from the queue, which reads the compiled edges (``server/pm/queue.py``), rather
than re-deriving readiness per candidate from a live search.

:func:`_is_done` stays as this module's one shared state predicate — "done" is
any known non-OPEN state (``CLOSED``, and ``MERGED`` for a PR resolved via
``issueOrPullRequest``); an unknown state is NOT done, so a blocker whose state
cannot be read blocks (fail closed).
"""
from __future__ import annotations

import re

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
