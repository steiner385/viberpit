"""Identifier validation for library entities (path-traversal defense)."""
from __future__ import annotations

import re
from pathlib import Path

_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MCP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PLUGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}@[a-z0-9][a-z0-9._-]{0,63}$")


def valid_skill_name(name: str) -> bool:
    return bool(_SKILL_RE.match(name))


def valid_command_path(path: str) -> bool:
    if not path or path.startswith("/") or path.endswith("/"):
        return False
    segments = path.split("/")
    if not 1 <= len(segments) <= 4:
        return False
    for seg in segments:
        if seg in (".", "..") or seg.endswith(".md") or not _SEGMENT_RE.match(seg):
            return False
    return True


def valid_mcp_name(name: str) -> bool:
    return bool(_MCP_RE.match(name))


def valid_plugin_id(pid: str) -> bool:
    return bool(_PLUGIN_RE.match(pid))


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
