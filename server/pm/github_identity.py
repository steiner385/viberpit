"""A GitHub login → its immutable user id (#3147).

Trust in an issue author is keyed on the GitHub ``databaseId`` because a login can be
renamed and the freed name re-registered by anyone. ``trusted_authors`` policy entries
are bound to that id when they are saved (``product_policy.patch_project_policy``); this
module is the one lookup that binding goes through.

``GET /users/{login}`` is public, so the ambient ``gh`` credential (through
``viberpit.gitrepos._run_gh``, the server's canonical ``gh`` runner) is enough: there is
no owning repository whose installation token would fit better.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from collections.abc import Callable
from typing import Any

from viberpit import gitrepos
from viberpit.gitrepos import GhError

log = logging.getLogger(__name__)

# GitHub's own login rule: alphanumerics and hyphens, not starting with a hyphen, at
# most 39 characters. Checked before the lookup so no entry can steer the request at a
# different API path (``users/../repos/…``).
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

_TIMEOUT_S = 20.0


class GitHubLookupError(RuntimeError):
    """GitHub could not be asked (transport, auth or an unreadable answer) — distinct
    from a definite "no such user", which is ``None``."""


async def resolve_github_user_id(login: str, *,
                                 run: Callable[..., Any] | None = None) -> int | None:
    """The id of the GitHub USER *login*, or ``None`` when there is no such account or it
    is an Organization/Bot (neither may be trusted as a person). Raises ``ValueError`` for
    text that is not a GitHub login and ``GitHubLookupError`` when GitHub cannot be asked.
    """
    if not GITHUB_LOGIN_RE.match(login or ""):
        raise ValueError(f"{login!r} is not a GitHub login")
    # Resolved per call, not bound as a default, so a patched seam is honoured.
    run = run or gitrepos._run_gh
    args = ["api", f"users/{login}"]
    try:
        proc = await asyncio.to_thread(run, args, timeout=_TIMEOUT_S)
    except (subprocess.TimeoutExpired, GhError, OSError) as exc:
        raise GitHubLookupError(f"GitHub user lookup for {login!r} failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or "gh api failed"
        if "HTTP 404" in err:
            return None
        raise GitHubLookupError(f"GitHub user lookup for {login!r} failed: {err}")
    try:
        body = json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise GitHubLookupError(f"GitHub user lookup for {login!r}: unparseable answer") from exc
    if not isinstance(body, dict):
        raise GitHubLookupError(f"GitHub user lookup for {login!r}: unexpected answer")
    uid = body.get("id")
    if body.get("type") != "User" or not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        return None
    return uid
