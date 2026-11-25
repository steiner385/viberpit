"""Per-host display target: the one parser for ``Host.display_target`` and the bridge accept list.

Grammar: ``local`` (or empty / None) | ``host:<name>``. There is deliberately no other
scheme (no ``windows:`` prefix): the target is always a fleet host name.
"""
from __future__ import annotations

import re
from typing import Any

_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PREFIX = "host:"


def parse_display_target(value: str | None) -> str | None:
    """Return the target host name, or None for local. Raises ``ValueError`` on bad grammar."""
    if value is None:
        return None
    text = value.strip()
    if not text or text == "local":
        return None
    if not text.startswith(_PREFIX):
        raise ValueError(f"display target {value!r} must be 'local' or 'host:<name>'")
    name = text[len(_PREFIX):]
    if not _HOST_NAME_RE.match(name):
        raise ValueError(f"display target host name {name!r} is not a valid host name")
    return name


def accepted_sources(settings: Any) -> frozenset[str]:
    """Host names allowed to bridge onto this host's desktop (``display_bridge_accept_from``)."""
    raw = getattr(settings, "display_bridge_accept_from", "") or ""
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
