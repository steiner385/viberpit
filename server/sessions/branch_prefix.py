"""The configurable session-branch prefix (D-22, owner decision D22b).

``AppSettings.branch_prefix`` (NULL = the built-in ``brand.BRANCH_PREFIX``) names the
branches NEW sessions and forks are created on. Changing it can never make a user's
branch deletable: deletion requires a recorded creation
(``server/sessions/branch_ownership.py``), not a name. Validation still refuses a
prefix that would collide with branches nobody can vouch for, so a new session branch
never lands in — or confusingly next to — the user's own namespace.

THE resolver for "which prefix does a new branch get" is :func:`effective_branch_prefix`;
spawn and fork read it the way they read ``effective_repo_root``.
"""
from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import select

from viberpit import brand, gitrepos

log = logging.getLogger(__name__)

BRANCH_PREFIX_INVALID = "BRANCH_PREFIX_INVALID"

#: Common branch namespaces a session prefix may never take over: these are where
#: people (and their bots) keep branches of their own, so a session branch there
#: would read as theirs. Refused outright, whatever the clones currently hold.
RESERVED_PREFIXES: frozenset[str] = frozenset({
    "feature/", "feat/", "fix/", "bugfix/", "hotfix/", "release/", "dependabot/",
    "renovate/", "main/", "master/", "pm/", "gh-pages/",
})

MIN_LEN = 2
MAX_LEN = 40
#: How many clashing branches a refusal names.
CLASH_EXAMPLES = 3


class BranchPrefixInvalid(ValueError):
    """A prefix the settings PUT refuses; ``reason`` is the user-facing sentence."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def format_problem(prefix: str) -> str | None:
    """Why *prefix* is not a well-formed, allowed prefix, or None. Blocking: runs
    ``git check-ref-format --branch <prefix>x`` as an argv list (never a shell)."""
    if not prefix.endswith("/"):
        return "The prefix must end with /."
    if not MIN_LEN <= len(prefix) <= MAX_LEN:
        return f"The prefix must be {MIN_LEN}–{MAX_LEN} characters long."
    if prefix.lower() in RESERVED_PREFIXES:
        return f"{prefix} is a common branch namespace; pick one of your own."
    try:
        r = subprocess.run(["git", "check-ref-format", "--branch", f"{prefix}x"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "The prefix could not be checked with git."
    if r.returncode != 0:
        return f"{prefix} is not a valid git branch prefix."
    return None


def _clones(repo_root: str) -> list[Path]:
    """This host's clones under *repo_root*: every managed clone
    (``<clones base>/<owner>/<name>``, both namespace spellings) and every checkout
    directly in the root."""
    found: list[Path] = []
    for base in gitrepos.clones_bases(repo_root):
        if base.is_dir():
            found += [c for owner in base.iterdir() if owner.is_dir()
                      for c in owner.iterdir() if (c / ".git").is_dir()]
    root = Path(repo_root)
    if root.is_dir():
        found += [c for c in root.iterdir() if (c / ".git").is_dir()]
    return found


def _local_branches(clone: Path) -> list[str]:
    try:
        r = subprocess.run(["git", "-C", str(clone), "for-each-ref",
                            "--format=%(refname:short)", "refs/heads/"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return r.stdout.split() if r.returncode == 0 else []


def clashing_branches(prefix: str, repo_root: str,
                      recorded: dict[str, frozenset[str]]) -> list[str]:
    """Blocking: local branches in this host's clones the prefix would claim but no
    record vouches for — a branch under it, or one named like the prefix itself
    (``foo`` blocks ``foo/…`` in git). *recorded* maps a clone key to its recorded
    branch names. Returns ``<clone name>:<branch>`` entries, sorted."""
    from server.sessions.branch_ownership import clone_key
    stem = prefix[:-1]
    hits: list[str] = []
    for clone in _clones(repo_root):
        key = clone_key(str(clone))
        mine = recorded.get(key or "", frozenset())
        hits += [f"{clone.name}:{b}" for b in _local_branches(clone)
                 if (b.startswith(prefix) or b == stem) and b not in mine]
    return sorted(set(hits))


def _name_examples(hits: Iterable[str]) -> str:
    hits = list(hits)
    shown = ", ".join(hits[:CLASH_EXAMPLES])
    more = len(hits) - CLASH_EXAMPLES
    return shown + (f" and {more} more" if more > 0 else "")


def validate(prefix: str, repo_root: str, recorded: dict[str, frozenset[str]]) -> None:
    """Raise :class:`BranchPrefixInvalid` unless *prefix* may become the prefix.
    Blocking (git). A built-in prefix is always accepted: its branches are matched by
    the legacy rule already and nothing new becomes deletable."""
    if prefix in brand.BRANCH_PREFIXES:
        return
    problem = format_problem(prefix)
    if problem is not None:
        raise BranchPrefixInvalid(problem)
    hits = clashing_branches(prefix, repo_root, recorded)
    if hits:
        raise BranchPrefixInvalid(
            f"Branches already use {prefix} that {brand.NAME} did not create: "
            f"{_name_examples(hits)}. Pick a prefix no existing branch uses.")


async def recorded_by_clone(host: str) -> dict[str, frozenset[str]]:
    """*host*'s recorded branch names by clone key (for :func:`validate`). A failed read
    raises: the validation must not pass on records it could not see."""
    from server.db import db_session
    from server.db.models import SessionBranch
    async with db_session() as db:
        rows = (await db.execute(select(SessionBranch.clone, SessionBranch.branch)
                                 .where(SessionBranch.host == host))).all()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r.clone, set()).add(r.branch)
    return {k: frozenset(v) for k, v in out.items()}


def normalise(value: str | None) -> str | None:
    """The stored form of a PUT value: blank or None clears to NULL (the built-in)."""
    v = (value or "").strip()
    return v or None


async def effective_branch_prefix() -> str:
    """DB ``AppSettings.branch_prefix`` → the built-in ``brand.BRANCH_PREFIX``. THE
    resolver for a new session or fork branch's prefix; any read failure falls back
    to the built-in (always a valid, ownership-gated prefix)."""
    from server.db import db_session
    from server.db.models import AppSettings
    try:
        async with db_session() as db:
            val = (await db.execute(select(AppSettings.branch_prefix)
                                    .where(AppSettings.id == "singleton"))
                   ).scalar_one_or_none()
    except Exception:
        log.debug("settings: branch_prefix read failed", exc_info=True)
        return brand.BRANCH_PREFIX
    return val or brand.BRANCH_PREFIX

