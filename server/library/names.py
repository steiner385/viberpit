"""Identifier validation for library entities (path-traversal defense).

Every validator uses ``fullmatch``: ``re.match`` with ``$`` also accepts a trailing newline."""
from __future__ import annotations

import re
from pathlib import Path

_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MCP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Case is accepted and significant: 2.1.318 installs ``Hello@MixedCase-Mkt`` and answers
# ``not_found`` for ``hello@mixedcase-mkt`` (probed, D-16c), so lower-casing would be wrong.
_PLUGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def valid_skill_name(name: str) -> bool:
    return bool(_SKILL_RE.fullmatch(name))


def valid_command_path(path: str) -> bool:
    if not path or path.startswith("/") or path.endswith("/"):
        return False
    segments = path.split("/")
    if not 1 <= len(segments) <= 4:
        return False
    for seg in segments:
        if seg in (".", "..") or seg.endswith(".md") or not _SEGMENT_RE.fullmatch(seg):
            return False
    return True


def valid_mcp_name(name: str) -> bool:
    return bool(_MCP_RE.fullmatch(name))


def valid_plugin_id(pid: str) -> bool:
    return bool(_PLUGIN_RE.fullmatch(pid))


def resolve_under(base: Path, *parts: str) -> Path | None:
    """Join parts under base and resolve; None unless the result stays inside base.

    Resolves symlinks, so a link pointing outside base is rejected too.
    """
    candidate = base.joinpath(*parts).resolve()
    base_resolved = base.resolve()
    if candidate == base_resolved:
        return None
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    return candidate
