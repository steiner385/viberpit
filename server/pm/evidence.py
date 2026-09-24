"""Capability evidence paths (#2162): how a cited path resolves against a tree.

A feature doc's ``code:``/``tests:``/``specs:``/``adrs:`` entries are repo
paths in one of three spellings, and this is the one place that decides what
each means (the daemon's clone read and the tests both call it):

* ``dir/`` — a directory: present if any file lives under it;
* anything containing ``*``/``?``/``[`` — a glob (:func:`fnmatch`, so ``*``
  crosses ``/`` exactly as a git pathspec does);
* otherwise an exact file, or a directory written without its trailing slash.

Pure, no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class PathResolution:
    """What pm-sync learned about one cited path at the default-branch head.
    ``last_commit_at`` is only resolved where a health check needs it (specs,
    and the doc itself); ``None`` there means "not looked up"."""

    exists: bool
    last_commit_at: datetime | None = None


def matches(pattern: str, tree: Iterable[str]) -> list[str]:
    """Every file in *tree* that *pattern* names (see module docstring)."""
    if any(c in _GLOB_CHARS for c in pattern):
        return [p for p in tree if fnmatchcase(p, pattern)]
    if pattern.endswith("/"):
        return [p for p in tree if p.startswith(pattern)]
    prefix = pattern + "/"
    return [p for p in tree if p == pattern or p.startswith(prefix)]
