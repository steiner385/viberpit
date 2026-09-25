"""Stable requirement/acceptance-criteria ID parsing (issue #2163).

``build_spec_prompt`` (``server/sdlc/prompts.py``) asks a spec session to give
every functional requirement and acceptance criterion a stable ID —
``FR-NNN``/``AC-NNN``/``US-N`` — and ``build_materialize_prompt`` (this
package's ``materialize.py``) asks a decompose session to copy the IDs a task
covers onto a ``Covers: FR-…, AC-…`` line in that task's issue body. This
module is the one place that reads that line back out, so the requirement
coverage endpoint (``server/api/sdlc.py``) and anything else downstream parse
it the same way (constitution VI: one definition)."""
from __future__ import annotations

import re

_COVERS_LINE_RE = re.compile(r"(?im)^\s*covers:\s*(.+)$")
_ID_RE = re.compile(r"\b(?:FR|AC|US)-\d+\b", re.IGNORECASE)


def parse_covers(body: str | None) -> tuple[str, ...]:
    """The stable requirement IDs named on a ``Covers:`` line in *body*.

    Deduplicated, first-seen order; IDs are upper-cased so ``fr-003`` and
    ``FR-003`` are the same ID. A body with no ``Covers:`` line, or one with no
    recognizable ID on it, returns an empty tuple rather than raising — a task
    body predating this convention (or one the session skipped, as the prompt
    allows for an unmapped phase) is not an error.
    """
    if not body:
        return ()
    seen: set[str] = set()
    ids: list[str] = []
    for line_match in _COVERS_LINE_RE.finditer(body):
        for id_match in _ID_RE.finditer(line_match.group(1)):
            token = id_match.group(0).upper()
            if token not in seen:
                seen.add(token)
                ids.append(token)
    return tuple(ids)


def sort_key(requirement_id: str) -> tuple[str, int, str]:
    """Sort ``FR-``/``AC-``/``US-`` IDs by prefix then numerically, so
    ``FR-002`` sorts before ``FR-010`` (a plain string sort would not)."""
    prefix, _, number = requirement_id.partition("-")
    return (prefix, int(number) if number.isdigit() else 0, requirement_id)
