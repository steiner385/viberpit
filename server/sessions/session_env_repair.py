"""Scrub NUL bytes from a resumed session's SessionStart env files (#3501).

A SessionStart hook writes ``export`` lines into ``$CLAUDE_ENV_FILE``
(``<config_dir>/session-env/<session_id>/sessionstart-hook-<n>.sh``), and the
``claude`` binary prepends those files to every Bash command it runs. On a resume
the same session id is reused, so the hook runs against the previous process's
file; a truncate racing an append can leave a run of NUL bytes in it (the reported
file held 68 — exactly one stale ``export`` line). Node then refuses every Bash
spawn (``args[1] must be a string without null bytes``) and the CLI caches the
content at start, so the only cure is to clean the file before the next start.

NUL is never valid in a shell script, so stripping it loses nothing. The rewrite
is atomic (temp file + rename) so a concurrent reader never sees a half file.
Best-effort: an unreadable dir or file is skipped, never raised.
"""
from __future__ import annotations

import logging
import os
import tempfile

log = logging.getLogger(__name__)

ENV_DIR = "session-env"


def _scrub(path: str) -> bool:
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\x00" not in data:
        return False
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".scrub-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.replace(b"\x00", b""))
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return True


def repair_session_env(config_dir: str, session_id: str) -> int:
    """Strip NUL bytes from every file in ``session-env/<session_id>/``. Returns
    the number of files rewritten."""
    if not config_dir or not session_id or os.path.basename(session_id) != session_id \
            or session_id in (".", ".."):
        return 0
    env_dir = os.path.join(config_dir, ENV_DIR, session_id)
    try:
        names = os.listdir(env_dir)
    except OSError:
        return 0
    fixed = 0
    for name in names:
        path = os.path.join(env_dir, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            if _scrub(path):
                fixed += 1
        except OSError:
            log.warning("session_env_repair: could not scrub %s", path, exc_info=True)
    if fixed:
        log.info("session_env_repair: stripped NUL bytes from %d file(s) in %s",
                 fixed, env_dir)
    return fixed
