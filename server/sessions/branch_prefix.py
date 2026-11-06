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
import time
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

#: Leading path segments that make a branch name ambiguous with git's own ref
#: namespaces or a remote: ``heads/x`` is ``refs/heads/heads/x``, which
#: ``rev-parse --abbrev-ref`` (how a worktree's branch is read at cleanup) may print
#: differently from the name the record holds — so cleanup could never match the
#: record, and the branch would leak. A different concern from RESERVED_PREFIXES (a
#: team convention), so a different list. Matched on the FIRST segment, any case.
AMBIGUOUS_REF_NAMESPACES: frozenset[str] = frozenset({
    "refs/", "heads/", "tags/", "remotes/", "head/", "origin/", "upstream/",
})

#: Wall-clock bound on the whole clash scan, so a PUT never hangs on a slow host.
SCAN_DEADLINE_S = 10.0

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
    first = prefix.split("/", 1)[0] + "/"
    if first.lower() in AMBIGUOUS_REF_NAMESPACES:
        return (f"{first} is one of git's own ref namespaces; a branch under it would be "
                "ambiguous. Pick another prefix.")
    try:
        r = subprocess.run(["git", "check-ref-format", "--branch", f"{prefix}x"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "The prefix could not be checked with git."
    if r.returncode != 0:
        return f"{prefix} is not a valid git branch prefix."
    return None


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        """Seconds left; refuses (fail closed) once none are."""
        left = self.end - time.monotonic()
        if left <= 0:
            raise BranchPrefixInvalid(
                "Checking existing branches took too long; try again.")
        return left


def _clones(repo_root: str, deadline: _Deadline) -> list[Path]:
    """This host's clones under *repo_root*: every managed clone
    (``<clones base>/<owner>/<name>``, both namespace spellings) and every checkout
    directly in the root. A directory that cannot be read refuses (fail closed)."""
    found: list[Path] = []
    try:
        for base in gitrepos.clones_bases(repo_root):
            if base.is_dir():
                for owner in base.iterdir():
                    deadline.left()
                    if owner.is_dir():
                        found += [c for c in owner.iterdir() if (c / ".git").is_dir()]
        root = Path(repo_root)
        if root.is_dir():
            found += [c for c in root.iterdir() if (c / ".git").is_dir()]
    except OSError as exc:
        raise BranchPrefixInvalid(
            f"Couldn't check existing branches under {repo_root}: "
            f"{exc.strerror or type(exc).__name__}.") from exc
    return found


def _local_branches(clone: Path, deadline: _Deadline) -> list[str]:
    """Every local branch of *clone*; refuses (fail closed) when git cannot list them
    in time — an unlisted clone could hold the very branch the prefix clashes with."""
    try:
        r = subprocess.run(["git", "-C", str(clone), "for-each-ref",
                            "--format=%(refname:short)", "refs/heads/"],
                           capture_output=True, text=True, timeout=deadline.left())
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is None or r.returncode != 0:
        raise BranchPrefixInvalid(f"Couldn't check existing branches in {clone.name}.")
    return r.stdout.split()


def _parent_dirs(prefix: str) -> list[str]:
    """Every folder the prefix nests in, as a branch name git would refuse to nest
    under: ``team/agent/`` → ``team``, ``team/agent``."""
    parts = prefix[:-1].split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def clashing_branches(prefix: str, repo_root: str,
                      recorded: dict[str, frozenset[str]], *,
                      deadline_s: float = SCAN_DEADLINE_S) -> list[str]:
    """Blocking: local branches in this host's clones that clash with *prefix*:

    * a branch under it that no record vouches for (*recorded* maps a clone key to its
      recorded branch names); and
    * a branch named like any folder of the prefix (``team`` or ``team/agent`` for
      ``team/agent/``) — recorded or not, since git refuses ``team/agent/x`` next to
      a branch ``team`` either way.

    Bounded by *deadline_s* overall and fail closed: a clone it cannot list, or a scan
    past the deadline, raises :class:`BranchPrefixInvalid`. Returns
    ``<clone name>:<branch>`` entries, sorted."""
    from server.sessions.branch_ownership import clone_key
    deadline = _Deadline(deadline_s)
    parents = set(_parent_dirs(prefix))
    hits: list[str] = []
    for clone in _clones(repo_root, deadline):
        key = clone_key(str(clone), timeout=deadline.left())
        if key is None:
            raise BranchPrefixInvalid(f"Couldn't check existing branches in {clone.name}.")
        mine = recorded.get(key, frozenset())
        hits += [f"{clone.name}:{b}" for b in _local_branches(clone, deadline)
                 if b in parents or (b.startswith(prefix) and b not in mine)]
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

