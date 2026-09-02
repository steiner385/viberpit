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
A stale ``GITHUB_TOKEN``/``GH_TOKEN`` env var shadows the account ``gh`` would
otherwise use, and the daemon shares one global gh config with every session — a
KinDash bot login into that config hijacks the active identity and makes the
user's own private repos vanish (``NOT_FOUND``).  EVERY ``gh`` subprocess therefore
runs with those tokens stripped and, when ``CCSWITCH_GH_CONFIG_DIR`` is set, pinned
to a dedicated config dir authed only as the ccswitch identity (see :func:`_gh_env`).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class GhError(Exception):
    """Raised when a ``gh`` invocation fails (missing binary / unauthed / API error)."""


class CloneError(Exception):
    """Raised when a clone could not be created."""


# ---------------------------------------------------------------------------
# Stale-credential registry (issue #670)
# ---------------------------------------------------------------------------
#
# A ``git fetch`` inside :func:`ensure_clone` is best-effort — on failure the
# clone keeps operating off its stale ref. When that failure is an *auth-class*
# error (a revoked/expired token, not a transient network blip), silently
# proceeding is dangerous: a backlog-runner worktree branched off a stale
# ``origin/master`` can be hundreds of commits behind, so the runner implements
# against ancient code without anyone knowing.
#
# We keep a small, process-local registry of repos whose LAST fetch failed with
# an auth-class error. A successful fetch clears the entry (the "auto-clear once
# a fetch succeeds" contract). The registry drives two consumers: the
# backlog-runner pauses worktree creation for a listed repo, and the dashboard
# surfaces a "git credential refresh needed" notification. It lives in the
# daemon process (where ``ensure_clone`` runs); the edge reads it via an
# orchestrator RPC.

# Substrings git/gh print when credentials are stale, revoked, or missing —
# distinct from a transient network failure (offline, DNS, timeout), which is
# NOT a credential problem and must not raise a "refresh your token" alert.
_AUTH_ERROR_MARKERS = (
    "invalid username or token",
    "password authentication is not supported",
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "http 403",
    "403 forbidden",
    "remote: forbidden",
    "fatal: authentication",
)

_TOKEN_SETTINGS_URL = "https://github.com/settings/tokens"
_APP_SETTINGS_URL = "https://github.com/settings/installations"

_cred_lock = threading.Lock()
# name_with_owner -> {"since": float, "error": str, "via_app": bool}
_stale_credentials: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# App-installation-token provider (issue #887)
# ---------------------------------------------------------------------------
#
# git network operations authenticate with a GitHub App *installation* token
# minted per owner, rather than whatever the machine's ambient credential helper
# happens to hold. The core cannot import the minting code: ``server.github``
# depends on httpx/cryptography and the src/ -> server/ direction is never
# imported (the CLI must run with no daemon). So the daemon registers a provider
# at startup, exactly as it registers ``accounts.set_auth_failure_sink`` and
# ``accounts.set_copilot_usage_reader``.

_token_provider: "Callable[[str], str | None] | None" = None


def set_token_provider(fn: "Callable[[str], str | None] | None") -> None:
    """Register (or clear) the provider that mints a git token for a repo owner.

    The core stays HTTP-free: the daemon registers
    ``server.github.app_auth.token_for_owner`` at startup. When unset (CLI usage)
    git keeps using the machine's ambient credential helper.

    The provider is called once per network git invocation, so it MUST be cheap
    and idempotent — ``token_for_owner`` caches the installation token until near
    expiry, which is what makes that true.
    """
    global _token_provider
    _token_provider = fn


def _app_token(owner: str) -> str | None:
    """The registered provider's token for ``owner``, or None to use ambient auth.

    Never raises: a provider that fails (no installation, a blip minting the
    token) must degrade to the previous ambient-credential behavior rather than
    break every fetch in the daemon."""
    fn = _token_provider
    if fn is None:
        return None
    try:
        return fn(owner) or None
    except Exception:
        log.warning("git.token_provider_failed owner=%s", owner, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# App-installation *repo enumeration* provider (issue #912)
# ---------------------------------------------------------------------------
#
# The repo chooser must list the repos the pr-dashy GitHub App can reach, NOT the
# ambient gh user's ``viewer.repositories`` (which needs a user OAuth token — an
# App installation token has no ``viewer``, and when the machine's stored gh
# credential expires the query silently drops to anonymous and the chooser blanks).
#
# ``set_token_provider`` cannot serve this: it takes an owner as *input*, so it can
# mint a token for a known owner but cannot *discover* which owners/repos the App
# can access. Repo enumeration needs that discovery up front, so it gets its own
# seam that returns the full repo list (via ``/installation/repositories``). Like
# the token provider, the daemon (and edge) register it at startup; unset in CLI
# usage, where the viewer path still applies.

_installations_provider: "Callable[[], list[dict] | None] | None" = None


def set_installations_provider(fn: "Callable[[], list[dict] | None] | None") -> None:
    """Register (or clear) the provider that lists every repo the pr-dashy App can reach.

    Distinct from :func:`set_token_provider`: that seam takes an owner as INPUT and so
    cannot discover owners, which repo enumeration needs up front. The provider returns
    raw GitHub REST repo objects (from ``/installation/repositories``), ``None`` when no
    App is configured (CLI → viewer fallback), or RAISES :class:`GhError` when an App is
    configured but broken — so the chooser fails loudly rather than degrading to the
    ambient user credential it was migrated off (issue #912)."""
    global _installations_provider
    _installations_provider = fn


def _installation_repos() -> "list[dict] | None":
    """The registered installations provider's repo list, or None for viewer fallback.

    Unlike :func:`_app_token` this deliberately does NOT swallow provider errors: a
    configured-but-broken App must surface as :class:`GhError` (fail loud) instead of
    silently falling through to the ambient user credential (issue #912)."""
    fn = _installations_provider
    if fn is None:
        return None
    return fn()


def is_auth_error(stderr: str | None) -> bool:
    """True if ``stderr`` looks like a git/gh *authentication* failure.

    Matches the substrings GitHub emits for a revoked/expired token or a
    prompt-disabled non-interactive fetch. Deliberately does NOT match generic
    network failures — those are transient, not a credential problem."""
    low = (stderr or "").lower()
    return any(marker in low for marker in _AUTH_ERROR_MARKERS)


def token_settings_url(name_with_owner: str = "", *, via_app: bool = False) -> str:
    """The GitHub page where the user fixes the credential behind a failed fetch.

    The link must name the credential that ACTUALLY failed. When the fetch
    authenticated with a GitHub App installation token (``via_app``), no personal
    access token is involved and sending the user to the PAT page is a dead end —
    the actionable page is the App installation.

    ``name_with_owner`` is accepted for a future per-owner deep-link but stays
    unused: the org-scoped page (``/organizations/<owner>/settings/installations``)
    404s for a user-owned repo, and telling org from user needs an API call, so we
    emit the one URL that resolves for both."""
    if via_app:
        return _APP_SETTINGS_URL
    return _TOKEN_SETTINGS_URL


def record_fetch_result(
    name_with_owner: str, *, ok: bool, stderr: str = "", via_app: bool = False
) -> None:
    """Update the stale-credential registry from a fetch/clone outcome.

    ``ok`` clears any prior alert for the repo (credentials work again). A
    failure records/refreshes an alert ONLY when ``stderr`` is auth-class — a
    transient (offline) failure leaves the registry untouched. The ``since``
    timestamp is preserved across repeated auth failures so the notification
    reflects when the problem first appeared, not the latest retry.

    ``via_app`` records whether the failing fetch authenticated with an App
    installation token, so the notification can link to the page that actually
    fixes it. Unlike ``since`` it is NOT preserved across retries — it describes
    the most recent attempt, and the credential in play can change between them."""
    with _cred_lock:
        if ok:
            _stale_credentials.pop(name_with_owner, None)
            return
        if not is_auth_error(stderr):
            return
        first_line = ""
        if stderr and stderr.strip():
            first_line = stderr.strip().splitlines()[0][:200]
        existing = _stale_credentials.get(name_with_owner)
        _stale_credentials[name_with_owner] = {
            "since": existing["since"] if existing else time.time(),
            "error": first_line,
            "via_app": via_app,
        }


def has_stale_credentials(name_with_owner: str) -> bool:
    """Whether ``name_with_owner``'s last fetch failed with an auth-class error."""
    with _cred_lock:
        return name_with_owner in _stale_credentials


def stale_credential_repos() -> list[dict]:
    """Snapshot of every repo currently flagged with stale credentials.

    Each entry: ``{"repo", "since", "error", "settingsUrl"}`` — the wire shape
    the dashboard banner and the backlog-runner notification both consume."""
    with _cred_lock:
        return [
            {
                "repo": repo,
                "since": info["since"],
                "error": info["error"],
                "settingsUrl": token_settings_url(
                    repo, via_app=bool(info.get("via_app"))
                ),
                "viaApp": bool(info.get("via_app")),
            }
            for repo, info in sorted(_stale_credentials.items())
        ]


def clear_stale_credentials(name_with_owner: str) -> bool:
    """Drop a repo's alert (a successful fetch or an explicit reset). Returns
    True if an entry was present."""
    with _cred_lock:
        return _stale_credentials.pop(name_with_owner, None) is not None


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
    """Environment for gh subprocesses, isolated from shared/mutable gh auth state.

    The daemon and every managed session run as the same OS user and share one global
    gh config (``~/.config/gh/hosts.yml``). A KinDash session that logs the
    ``kindash-claude-sessions[bot]`` App installation into that config — or exports a
    bot ``GITHUB_TOKEN``/``GH_TOKEN`` — would otherwise hijack the active gh identity,
    making every gh call for the user's own private repos (e.g. ``steiner385/*``) fail
    with ``NOT_FOUND``. Two defenses:

    * Strip both ``GITHUB_TOKEN`` and ``GH_TOKEN`` — either env var overrides the
      pinned account (a per-owner App token is re-set explicitly by :func:`_run_gh`).
    * When ``CCSWITCH_GH_CONFIG_DIR`` is set, point gh at a dedicated config dir authed
      only as the ccswitch identity, so mutations to the default ``~/.config/gh`` can
      never leak in.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GITHUB_TOKEN", "GH_TOKEN")
    }
    config_dir = os.environ.get("CCSWITCH_GH_CONFIG_DIR")
    if config_dir:
        env["GH_CONFIG_DIR"] = os.path.expanduser(config_dir)
    return env


def _run_gh(
    args: list[str],
    token: str | None = None,
    *,
    input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command. Ambient ``GITHUB_TOKEN``/``GH_TOKEN`` are always stripped
    and the config dir is pinned (see :func:`_gh_env`); when ``token`` is given (a
    GitHub App installation token), ``GH_TOKEN`` is re-set so gh uses that owner's token.

    ``input`` is written to the child's stdin. Use it (with ``gh api ... -F
    field=@-``) to pass a large value that would otherwise overflow the per-argument
    kernel limit (MAX_ARG_STRLEN, 128 KiB) and raise ``OSError: Argument list too
    long`` — e.g. the batched PR-status GraphQL query.

    ``timeout`` bounds the child (seconds); on expiry ``subprocess`` raises
    ``TimeoutExpired``, which callers that want a soft failure must catch themselves."""
    env = _gh_env()
    if token:
        env["GH_TOKEN"] = token
    # Only pass ``timeout`` when set, so the common (timeout-less) call keeps its exact
    # kwarg shape — the many tests that mock ``subprocess.run`` rely on that signature.
    run_kwargs: dict = {"capture_output": True, "text": True, "env": env, "input": input}
    if timeout is not None:
        run_kwargs["timeout"] = timeout
    try:
        return subprocess.run(["gh", *args], **run_kwargs)
    except FileNotFoundError as exc:  # gh not installed
        raise GhError("the `gh` CLI is not installed or not on PATH") from exc


# A git credential helper that answers from the environment. The token is read
# from $CCSWITCH_GIT_TOKEN at call time and is deliberately NOT baked into the
# config value, which git echoes back in traces and `git config --list`.
_CRED_HELPER = (
    '!f() { echo username=x-access-token; echo "password=$CCSWITCH_GIT_TOKEN"; }; f'
)


def _git_env(owner: str | None) -> dict[str, str] | None:
    """Environment for a git subprocess that authenticates as ``owner``'s App install.

    Returns None when there is no App token to use — no provider registered (CLI
    usage), the owner has no installation, or minting failed. None means "inherit
    the ambient environment", i.e. exactly the behavior that predates this.

    When a token IS available the token reaches git through the environment ONLY:

    * NOT argv — ``git -c credential.helper=...`` puts the value in the command
      line, which is world-readable via ``ps``.
    * NOT the remote URL — embedding it would persist the token to
      ``.git/config`` on disk, outliving the hour it is valid for.

    ``GIT_CONFIG_COUNT`` first RESETS the helper list (an empty ``credential.helper``
    is git's documented reset) so a broken or hijacked ambient helper cannot answer
    first, then installs ours. ``GITHUB_TOKEN``/``GH_TOKEN`` are stripped for the
    same reason :func:`_gh_env` strips them — a stale bot token in the environment
    must never outrank the token we just minted.
    """
    if not owner:
        return None
    token = _app_token(owner)
    if token is None:
        return None
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GITHUB_TOKEN", "GH_TOKEN")
    }
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""
    env["GIT_CONFIG_KEY_1"] = "credential.helper"
    env["GIT_CONFIG_VALUE_1"] = _CRED_HELPER
    env["CCSWITCH_GIT_TOKEN"] = token
    return env


def git_env_for_remote(url: str) -> dict[str, str] | None:
    """Credential env for a network git op against ``url`` (None = use ambient auth).

    The entry point for callers that hold a remote URL rather than an
    ``<owner>/<name>`` — notably the worktree fetch in :mod:`ccswitch.worktrees`.
    Routes through the same :func:`_git_env` so there is one definition of how a
    token reaches git, not one per call site.
    """
    return _git_env(owner_from_remote_url(url))


def _run_git(
    args: list[str], owner: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a ``git`` command, authenticated as ``owner``'s App installation.

    Pass ``owner`` for NETWORK operations (clone, fetch) only. Local operations
    (``remote get-url``, ``checkout``) need no credential, and handing them one
    would widen the token's blast radius for nothing."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, env=_git_env(owner)
    )


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


def owner_from_remote_url(url: str) -> str | None:
    """The GitHub owner behind a remote URL, or None if there isn't one.

    None for a non-GitHub remote (a local path fixture, a self-hosted host): those
    have no App installation, so the caller falls back to ambient credentials.
    """
    normalized = _normalize_remote_url(url)
    if not normalized:
        return None
    parts = normalized.split("/")
    if len(parts) < 3 or parts[0] != "github.com":
        return None
    return parts[1] or None


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
    ``git fetch --prune origin`` and return it.  Otherwise create the parent
    dirs, look for a local reference to speed up the clone, run ``git clone``
    (with ``--reference-if-able <ref> --dissociate`` when a reference is found),
    then leave the clone on a DETACHED head.

    Raises:
        CloneError: if the clone fails.
    """
    dest = clone_path(repo_root, name_with_owner)
    owner = name_with_owner.partition("/")[0]
    # Recorded on the alert so the notification links to the page that fixes the
    # credential actually in use (App installation vs. personal access token).
    via_app = _app_token(owner) is not None

    if (dest / ".git").exists():
        # Best-effort refresh; ignore failures (offline, etc.). Fetch ONLY
        # `origin` — NOT `--all`: a session that ran in a worktree off this
        # shared clone can leave stray remotes behind, and `--all` fetches
        # every one of them. That inflates spawn latency and, when a stray
        # remote points at a since-deleted path, makes the fetch fail on every
        # future automation spawn (issue #567). `origin` is the only remote
        # ensure_clone itself creates.
        fetch = _run_git(["-C", str(dest), "fetch", "--prune", "origin"], owner=owner)
        if fetch.returncode != 0:
            if is_auth_error(fetch.stderr):
                # Auth-class failure (revoked/expired token) — log at ERROR so it
                # is operationally visible, flag the repo so the backlog runner
                # pauses worktree creation, and let the dashboard surface a
                # credential-refresh notification (issue #670). The clone still
                # returns its stale ref for read-only callers; the registry is
                # what stops new work from branching off it.
                log.error(
                    "clone.fetch_failed path=%s err=%s — git credential refresh "
                    "needed for %s", dest, fetch.stderr.strip(), name_with_owner)
            else:
                log.warning("clone.fetch_failed path=%s err=%s", dest, fetch.stderr.strip())
            record_fetch_result(
                name_with_owner, ok=False, stderr=fetch.stderr, via_app=via_app)
        else:
            record_fetch_result(name_with_owner, ok=True)
        return str(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = ["clone"]
    reference = find_local_reference(repo_root, clone_url)
    if reference:
        log.info("clone.reference repo=%s ref=%s", name_with_owner, reference)
        args += ["--reference-if-able", reference, "--dissociate"]
    args += [clone_url, str(dest)]

    r = _run_git(args, owner=owner)
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

    # A fresh clone succeeding proves the credentials work — clear any stale
    # alert left by an earlier failed fetch of this repo.
    record_fetch_result(name_with_owner, ok=True)
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


def _installation_repo_to_dict(node: dict | None) -> dict | None:
    """Map a GitHub REST repo object (``/installation/repositories``) to the chooser dict.

    The App path returns REST objects, whose field names differ from the GraphQL nodes
    :func:`_repo_node_to_dict` maps (``full_name``/``private``/``html_url``/``pushed_at``
    vs ``nameWithOwner``/``isPrivate``/``url``/``pushedAt``). Both mappers live here so
    the chooser wire shape has a single definition; archived repos are dropped by both.
    """
    if not node or node.get("archived"):
        return None
    name_with_owner = node.get("full_name")
    if not name_with_owner:
        return None
    return {
        "nameWithOwner": name_with_owner,
        "name": node.get("name"),
        "owner": (node.get("owner") or {}).get("login"),
        "isPrivate": bool(node.get("private")),
        "url": node.get("html_url"),
        "defaultBranch": node.get("default_branch"),
        "pushedAt": node.get("pushed_at"),
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
    """List repos ccswitch can clone/branch/PR against, most-recently-pushed first.

    Two paths, selected by whether an installations provider is registered:

    * **App path** (daemon/edge — issue #912): when
      :func:`set_installations_provider` is registered, enumerate the repos the
      pr-dashy GitHub App installation can reach (``/installation/repositories``).
      This is the authoritative source in the hosted product — it authenticates as
      the App, not the machine's ambient gh user (whose stored credential expires
      and silently drops to anonymous, blanking the chooser). A configured-but-broken
      App raises :class:`GhError` (fail loud), never degrading to the ambient user.

    * **Viewer path** (standalone CLI): no daemon registered a provider, so fall
      back to the user's own ``gh`` credentials (``viewer.repositories`` unioned
      with org repos). Archived repos are excluded.

    Returns dicts with keys:
    ``nameWithOwner, name, owner, isPrivate, url, defaultBranch, pushedAt``.

    Raises:
        GhError: on the App path, if App repo enumeration fails; on the viewer
            path, if the primary ``viewer.repositories`` query fails or the
            response can't be parsed.
    """
    installs = _installation_repos()
    if installs is not None:
        # App path (issue #912): the installation-repos provider already
        # authenticated as the App and returned raw REST repo objects. Map, dedupe
        # by nameWithOwner, and re-sort PUSHED_AT-desc to honour the contract.
        app_by_name: dict[str, dict] = {}
        for node in installs:
            repo = _installation_repo_to_dict(node)
            if repo:
                app_by_name.setdefault(repo["nameWithOwner"], repo)
        return sorted(
            app_by_name.values(), key=lambda r: r.get("pushedAt") or "", reverse=True)

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

    # Authenticate as the owner's App installation, not the ambient gh user — same
    # gap as repo enumeration (issue #912): a private repo's branches 404 for an
    # anonymous caller once the stored gh credential expires. None (CLI, or no
    # installation for this owner) falls back to the ambient/pinned gh identity.
    token = _app_token(owner)

    branches_r = _run_gh(
        ["api", f"repos/{owner}/{name}/branches", "--paginate", "-q", ".[].name"],
        token=token,
    )
    if branches_r.returncode != 0:
        raise GhError(
            f"gh api repos/{owner}/{name}/branches failed: "
            f"{branches_r.stderr.strip() or branches_r.stdout.strip()}"
        )
    branches = [b.strip() for b in branches_r.stdout.splitlines() if b.strip()]

    default: str | None = None
    default_r = _run_gh(["api", f"repos/{owner}/{name}", "-q", ".default_branch"], token=token)
    if default_r.returncode == 0:
        default = default_r.stdout.strip() or None

    return {"default": default, "branches": branches}
