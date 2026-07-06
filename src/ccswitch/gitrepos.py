"""GitHub repo discovery + ccswitch-owned clones.

ccswitch must NEVER touch the user's real ``~/GitHub`` checkouts.  All git
activity it performs — clones AND worktrees — lives under
``<repo_root>/.ccswitch/``:

  * clones:    ``<repo_root>/.ccswitch/clones/<owner>/<name>``
  * worktrees: ``<repo_root>/.ccswitch/worktrees/<name>/<session>`` (see
    :mod:`ccswitch.worktrees`)

The "New session" repo picker lists the GitHub repos the user's ``gh``
credentials can access (via the GraphQL API), and clones-on-select into the
ccswitch clone namespace.  A fresh clone is left on a DETACHED head so that
selecting a branch later yields a worktree literally on that branch (rather
than a child branch off whatever the clone happened to check out).

gh auth gotcha
--------------
A stale ``GITHUB_TOKEN`` env var shadows the keyring login that ``gh`` would
otherwise use.  EVERY ``gh`` subprocess therefore runs with ``GITHUB_TOKEN``
stripped from the environment (see :func:`_gh_env`).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class GhError(Exception):
    """Raised when a ``gh`` invocation fails (missing binary / unauthed / API error)."""


class CloneError(Exception):
    """Raised when a clone could not be created."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def clones_base(repo_root: str) -> Path:
    """ccswitch-owned root for all clones, under the user's repo root."""
    return Path(repo_root).expanduser() / ".ccswitch" / "clones"


def clone_path(repo_root: str, name_with_owner: str) -> Path:
    """Absolute clone path for ``<owner>/<name>`` under the clones base."""
    owner, _, name = name_with_owner.partition("/")
    if not owner or not name:
        raise ValueError(f"expected '<owner>/<name>', got {name_with_owner!r}")
    return clones_base(repo_root) / owner / name


# ---------------------------------------------------------------------------
# gh subprocess plumbing
# ---------------------------------------------------------------------------


def _gh_env() -> dict[str, str]:
    """Environment for gh subprocesses with the shadowing GITHUB_TOKEN removed."""
    return {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}


def _run_gh(args: list[str], token: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command. ``GITHUB_TOKEN`` is always stripped; when ``token`` is
    given (a GitHub App installation token), ``GH_TOKEN`` is set so gh uses it."""
    env = _gh_env()
    if token:
        env["GH_TOKEN"] = token
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:  # gh not installed
        raise GhError("the `gh` CLI is not installed or not on PATH") from exc


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# URL normalization + local reference discovery
# ---------------------------------------------------------------------------


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL for equality comparison.

    Treats ``git@github.com:owner/name(.git)`` and
    ``https://github.com/owner/name(.git)`` as equal, lowercases the host, and
    strips a trailing ``.git`` and trailing slashes.
    """
    u = url.strip()
    if not u:
        return ""
    # scp-like syntax: git@host:owner/name
    if u.startswith("git@") or ("@" in u.split("/", 1)[0] and ":" in u and "://" not in u):
        # split off user@host: prefix
        _, _, rest = u.partition("@")  # host:owner/name
        host, _, path = rest.partition(":")
        host = host.lower()
        normalized = f"{host}/{path}"
    elif "://" in u:
        scheme, _, rest = u.partition("://")
        # strip optional userinfo@
        if "@" in rest.split("/", 1)[0]:
            rest = rest.partition("@")[2]
        host, _, path = rest.partition("/")
        host = host.lower()
        normalized = f"{host}/{path}"
    else:
        normalized = u.lower()
    normalized = normalized.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized


def repo_at_path(path: str) -> str | None:
    """Return the GitHub ``owner/name`` for the checkout at ``path``, else None.

    Reads the live git state of a session's working directory — runs
    ``git -C <path> remote get-url origin`` and normalizes the result — so the
    sessions screen's repo indicator reflects what's actually on disk (a
    worktree resolves to its clone's origin) rather than a stored field.

    Returns None when ``path`` is empty, is not a git working tree, or has no
    ``origin`` remote (e.g. a scratch session's isolated temp dir).
    """
    if not path:
        return None
    r = _run_git(["-C", path, "remote", "get-url", "origin"])
    if r.returncode != 0:
        return None
    # _normalize_remote_url yields "host/owner/name"; keep the trailing owner/name.
    normalized = _normalize_remote_url(r.stdout)
    parts = [p for p in normalized.split("/") if p]
    if len(parts) < 3:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def find_local_reference(repo_root: str, clone_url: str) -> str | None:
    """Return a local repo dir whose origin matches ``clone_url``, else None.

    Scans direct, non-hidden child directories of ``repo_root`` (the user's real
    checkouts), reads each ``origin`` remote URL, and returns the first whose
    normalized URL equals the normalized ``clone_url``.  Used to speed up clones
    via ``git clone --reference-if-able``.
    """
    target = _normalize_remote_url(clone_url)
    if not target:
        return None
    root = Path(repo_root).expanduser()
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        r = _run_git(["-C", str(child), "remote", "get-url", "origin"])
        if r.returncode != 0:
            continue
        if _normalize_remote_url(r.stdout) == target:
            return str(child)
    return None


# ---------------------------------------------------------------------------
# Clone management
# ---------------------------------------------------------------------------


def ensure_clone(repo_root: str, name_with_owner: str, clone_url: str) -> str:
    """Ensure a ccswitch-owned clone of ``<owner>/<name>`` exists; return its path.

    If the clone already exists (``<clone_path>/.git``), best-effort
    ``git fetch --all --prune`` and return it.  Otherwise create the parent
    dirs, look for a local reference to speed up the clone, run ``git clone``
    (with ``--reference-if-able <ref> --dissociate`` when a reference is found),
    then leave the clone on a DETACHED head.

    Raises:
        CloneError: if the clone fails.
    """
    dest = clone_path(repo_root, name_with_owner)

    if (dest / ".git").exists():
        # Best-effort refresh; ignore failures (offline, etc.).
        fetch = _run_git(["-C", str(dest), "fetch", "--all", "--prune"])
        if fetch.returncode != 0:
            log.warning("clone.fetch_failed path=%s err=%s", dest, fetch.stderr.strip())
        return str(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = ["clone"]
    reference = find_local_reference(repo_root, clone_url)
    if reference:
        log.info("clone.reference repo=%s ref=%s", name_with_owner, reference)
        args += ["--reference-if-able", reference, "--dissociate"]
    args += [clone_url, str(dest)]

    r = _run_git(args)
    if r.returncode != 0:
        raise CloneError(
            f"git clone failed for {name_with_owner!r}: {r.stderr.strip()}"
        )

    # Detach HEAD so the clone holds NO branch — selecting a branch later yields
    # a worktree literally on that branch (not a child branch off the clone's
    # checked-out branch).  Best-effort.
    detach = _run_git(["-C", str(dest), "checkout", "--detach"])
    if detach.returncode != 0:
        log.warning("clone.detach_failed path=%s err=%s", dest, detach.stderr.strip())

    log.info("clone.create repo=%s path=%s", name_with_owner, dest)
    return str(dest)


# ---------------------------------------------------------------------------
# gh-backed repo + branch listing
# ---------------------------------------------------------------------------


_REPOS_QUERY = """
query($cursor: String) {
  viewer {
    repositories(
      first: 100
      after: $cursor
      affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        name
        owner { login }
        isPrivate
        isArchived
        url
        pushedAt
        defaultBranchRef { name }
      }
    }
  }
}
"""


_ORGS_QUERY = """
query($cursor: String) {
  viewer {
    organizations(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { login }
    }
  }
}
"""


_ORG_REPOS_QUERY = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(
      first: 100
      after: $cursor
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        name
        owner { login }
        isPrivate
        isArchived
        url
        pushedAt
        defaultBranchRef { name }
      }
    }
  }
}
"""


def _repo_node_to_dict(node: dict | None) -> dict | None:
    """Map a GraphQL repo node to the chooser dict, or ``None`` to drop it.

    Archived repos are read-only on GitHub and must never reach the chooser, so
    they map to ``None`` (same rule for viewer- and org-sourced nodes).
    """
    if not node or node.get("isArchived"):
        return None
    name_with_owner = node.get("nameWithOwner")
    if not name_with_owner:
        return None
    default_ref = node.get("defaultBranchRef") or {}
    return {
        "nameWithOwner": name_with_owner,
        "name": node.get("name"),
        "owner": (node.get("owner") or {}).get("login"),
        "isPrivate": bool(node.get("isPrivate")),
        "url": node.get("url"),
        "defaultBranch": default_ref.get("name"),
        "pushedAt": node.get("pushedAt"),
    }


def _paginate_graphql(query: str, extra_args: list[str], path: list[str]):
    """Yield ``nodes`` from a paginated GraphQL connection at ``data.<path>``.

    ``path`` is the key sequence leading to the connection object (which must
    expose ``nodes`` and ``pageInfo {hasNextPage endCursor}``).

    Raises:
        GhError: if gh fails or the response can't be parsed.
    """
    cursor: str | None = None
    while True:
        args = ["api", "graphql", "-f", f"query={query}", *extra_args]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        r = _run_gh(args)
        if r.returncode != 0:
            raise GhError(f"gh api graphql failed: {r.stderr.strip() or r.stdout.strip()}")
        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            raise GhError(f"could not parse gh graphql response: {exc}") from exc

        conn: dict = payload.get("data") or {}
        for key in path:
            conn = (conn or {}).get(key, {})
        conn = conn or {}
        yield from (conn.get("nodes", []) or [])

        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
            continue
        break


def _list_viewer_orgs() -> list[str]:
    """Org logins the viewer belongs to (best-effort; empty on failure)."""
    orgs: list[str] = []
    try:
        for node in _paginate_graphql(_ORGS_QUERY, [], ["viewer", "organizations"]):
            login = (node or {}).get("login")
            if login:
                orgs.append(login)
    except GhError as exc:
        log.warning("repos.org_list_failed err=%s", exc)
    return orgs


def list_accessible_repos() -> list[dict]:
    """List repos the user's gh creds can access, most-recently-pushed first.

    Sources are unioned (deduped by ``nameWithOwner``):

    1. ``viewer.repositories`` — repos the user owns or has an *explicit* tie to
       (added collaborator / team member).
    2. ``organization.repositories`` for every org in ``viewer.organizations`` —
       the ONLY way to surface repos the user can reach solely via org
       base-permission or implicit org-owner admin.  ``viewer.repositories``
       omits those entirely (no affiliation matches) even though a direct
       ``repository(owner,name)`` lookup returns them, so an org-owned repo the
       user never got an explicit grant on would otherwise be invisible.

    Archived repos are excluded -- they are read-only on GitHub and should not
    be offered in the repository chooser.  Org enumeration is best-effort per
    org so one inaccessible org can't blank the whole chooser.

    Returns dicts with keys:
    ``nameWithOwner, name, owner, isPrivate, url, defaultBranch, pushedAt``.

    Raises:
        GhError: if the primary ``viewer.repositories`` query fails or the
            response can't be parsed.
    """
    by_name: dict[str, dict] = {}

    for node in _paginate_graphql(_REPOS_QUERY, [], ["viewer", "repositories"]):
        repo = _repo_node_to_dict(node)
        if repo:
            by_name.setdefault(repo["nameWithOwner"], repo)

    for org in _list_viewer_orgs():
        try:
            for node in _paginate_graphql(
                _ORG_REPOS_QUERY, ["-F", f"org={org}"], ["organization", "repositories"]
            ):
                repo = _repo_node_to_dict(node)
                if repo:
                    by_name.setdefault(repo["nameWithOwner"], repo)
        except GhError as exc:
            log.warning("repos.org_enum_failed org=%s err=%s", org, exc)

    # Merging two PUSHED_AT-desc streams needs a re-sort to honour the contract.
    return sorted(by_name.values(), key=lambda r: r.get("pushedAt") or "", reverse=True)


def list_remote_branches(name_with_owner: str) -> dict:
    """Return ``{"default": str|None, "branches": [str]}`` for a remote repo.

    Works before the repo is cloned (queries the GitHub REST API via gh).

    Raises:
        GhError: if gh is missing/unauthed.
    """
    owner, _, name = name_with_owner.partition("/")
    if not owner or not name:
        raise ValueError(f"expected '<owner>/<name>', got {name_with_owner!r}")

    branches_r = _run_gh(
        ["api", f"repos/{owner}/{name}/branches", "--paginate", "-q", ".[].name"]
    )
    if branches_r.returncode != 0:
        raise GhError(
            f"gh api repos/{owner}/{name}/branches failed: "
            f"{branches_r.stderr.strip() or branches_r.stdout.strip()}"
        )
    branches = [b.strip() for b in branches_r.stdout.splitlines() if b.strip()]

    default: str | None = None
    default_r = _run_gh(["api", f"repos/{owner}/{name}", "-q", ".default_branch"])
    if default_r.returncode == 0:
        default = default_r.stdout.strip() or None

    return {"default": default, "branches": branches}
