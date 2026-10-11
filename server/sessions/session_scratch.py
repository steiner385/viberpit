"""A per-session scratch directory that survives daemon restarts (#2910).

Long verification runs write logs somewhere; under ``/tmp`` (or a CLI-managed temp
dir) they can be gone by the time a restart-interrupted session resumes and wants
to read them. The directory lives inside the session worktree's OWN git dir
(``<gitdir>/viberpit-scratch/<session_id>``):

* it survives a daemon restart, because the worktree does;
* it is never in ``git status`` or a commit — git does not track its own dir;
* a linked worktree's git dir (``<repo>/.git/worktrees/<name>``) is deleted by
  ``git worktree remove``, so the scratch goes with the session's worktree and
  needs no cleanup path of its own.

Handed to the session by ``session_context`` as ``scratchDir``.
"""
from __future__ import annotations

import logging
import os

from viberpit import brand, worktrees

log = logging.getLogger(__name__)

SCRATCH_DIRNAME = f"{brand.SLUG}-scratch"


def scratch_dir_for(cwd: str | None, session_id: str) -> str | None:
    """Create (if needed) and return the session's scratch dir, or None when *cwd* is
    not inside a git worktree. Never raises."""
    if not cwd or not session_id or os.sep in session_id or session_id in (".", ".."):
        return None
    try:
        resolved = worktrees.git_dir_of(cwd)
        if resolved is None:
            return None
        path = os.path.join(resolved[1], SCRATCH_DIRNAME, session_id)
        os.makedirs(path, mode=0o700, exist_ok=True)
        return path
    except (OSError, ValueError):
        log.info("session_scratch.unavailable session=%s cwd=%s", session_id, cwd,
                 exc_info=True)
        return None
