"""Session ownership of issues (#3076): which live session is already working an item,
so the backlog runner leaves it alone.

A session that the runner dispatched owns its ``origin_ref``. A human-created session
— the usual home of a subagent-driven plan that files its own epic and leaves — has no
``origin_ref``, so it claims issues with ``OWNER_MARKER`` in the issue body instead. A
claim on an epic covers every descendant, and lapses on its own once the session
stops being live: the runner may then pick up whatever the plan left unfinished.

The marker is read from untrusted issue bodies, but it can only ever SUPPRESS a
dispatch, and only while the named session is live — never cause one."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet

from viberpit import brand

OWNER_MARKER_RE = re.compile(
    rf"<!--\s*{brand.SLUG}:owner-session=([A-Za-z0-9-]+)\s*-->")

#: How far up the parent chain ownership is inherited (leaf → epic → initiative …).
#: A cap rather than an unbounded walk: a mirrored parent cycle must not hang a tick.
MAX_ANCESTOR_DEPTH = 5


def owner_marker(session_id: str) -> str:
    """The line a session puts in an issue body to claim it for itself."""
    return f"<!-- {brand.SLUG}:owner-session={session_id} -->"


def marker_owners(body: str | None) -> frozenset[str]:
    """Every session id an issue body's ownership markers name."""
    return frozenset(OWNER_MARKER_RE.findall(body or ""))


def owned_by_live_session(
    item_id: str,
    *,
    nodes: Mapping[str, tuple[str, str | None, str | None]],
    live_refs: AbstractSet[str],
    live_ids: AbstractSet[str],
) -> bool:
    """Whether a live session owns the item or any ancestor of it.

    ``nodes`` maps a work-item id to ``(ref, body, parent_id)``; an id absent from it
    ends the walk. A node is owned when a live session was dispatched on its ref
    (``live_refs``) or its body carries a marker naming a live session (``live_ids``).
    """
    seen: set[str] = set()
    current: str | None = item_id
    for _ in range(MAX_ANCESTOR_DEPTH + 1):
        if current is None or current in seen or current not in nodes:
            return False
        seen.add(current)
        ref, body, parent = nodes[current]
        if ref in live_refs or marker_owners(body) & live_ids:
            return True
        current = parent
    return False


def missing_parents(nodes: Mapping[str, tuple[str, str | None, str | None]],
                    ids: Iterable[str]) -> set[str]:
    """Parent ids of *ids* that ``nodes`` has not loaded yet — the next fetch frontier."""
    return {p for i in ids if (p := nodes[i][2]) and p not in nodes}
