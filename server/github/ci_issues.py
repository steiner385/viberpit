"""GitHub issue/comment writes (REST mutations — no GraphQL path in gh).

Born as the CI/CD Doctor's issue filer; now also the single place the
``POST /api/github/issues`` and ``POST /api/prs/…/comments`` endpoints write
through (spec v2 Pack G), so the assistant and the doctor share one gh surface
rather than growing a second client.

Best-effort throughout: a failed write returns None. For the doctor that means
the loop retries next tick (the finding row keeps issue_number NULL); for the
endpoints it means a 502 the caller can report.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from urllib.parse import quote

from viberpit import brand, gitrepos

from server.pm.github_sync import with_automation_marker

from .host_failures import log_gh_failure

log = logging.getLogger(__name__)

LABEL = "ci-doctor"

_REPO_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

# Longest issue/comment body the write endpoints accept. GitHub's own limit is far
# higher (65 536); this is a ViberPit-side guard so a runaway assistant body can't be
# posted publicly, and it is the one both endpoints' pydantic models point at.
BODY_MAX_CHARS = 10_000


def valid_repo(repo: str) -> bool:
    """Whether `repo` is a safe ``owner/name`` to interpolate into a gh api path.

    The doctor's repos come from this project's own config, but the write endpoints take
    theirs from a request body, and every function here drops it straight into
    ``repos/<repo>/…``. A segment of ``..`` would retarget the write at another
    resource, so segments are checked against GitHub's own charset AND rejected when
    they are a relative-path element (``foo..bar`` is a legal repo name; ``..`` is not
    a repo at all).
    """
    parts = repo.split("/")
    return len(parts) == 2 and all(
        _REPO_SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts)


def _ensure_label(repo: str, name: str, run) -> None:
    # 422 already_exists is the steady state; any failure is non-fatal.
    description = (f"Filed by the {brand.NAME} CI/CD Doctor" if name == LABEL
                   else f"Applied by {brand.NAME}")
    run(["api", f"repos/{repo}/labels", "-f", f"name={name}",
         "-f", "color=1d76db", "-f", f"description={description}"])


def issue_url(repo: str, number: int) -> str:
    """The canonical html_url for an issue. Derived rather than read back from the
    create response so the caller needs no second request; GitHub numbers issues and
    PRs from one sequence per repo, so ``/issues/<n>`` always resolves (it redirects
    to ``/pull/<n>`` when the number happens to be a PR)."""
    return f"https://github.com/{repo}/issues/{number}"


def with_authorship(body: str, *, via: str | None, identity: str) -> str:
    """Sign a body posted through the assistant, and only then.

    ``via`` is ``request.state.via``, which ``IdentityMiddleware`` sets to
    ``"assistant"`` only for a ``ccas1:`` bearer call. A human posting the same body
    from the UI is already attributed by the gh account doing the write, so signing
    it would be noise; an assistant-authored comment is not obviously machine-written
    and must say so (spec v2 §4.6).
    """
    if via != "assistant":
        return body
    return f"{body}\n\n— posted by the {brand.NAME} assistant on behalf of {identity}"


def create_issue(repo: str, title: str, body: str, *,
                 labels: Sequence[str] | None = None,
                 run=gitrepos._run_gh) -> int | None:
    """File an issue, returning its number.

    ``labels`` distinguishes three cases deliberately: ``None`` (the default) applies
    the ci-doctor label, because the doctor is the caller that predates this argument
    and identifies its own issues by it; ``[]`` applies none, which is what a
    user- or assistant-filed issue wants; a list applies exactly those, creating any
    that the repo does not have yet (best-effort — a label that can't be created is
    dropped by GitHub rather than failing the create).
    """
    names = [LABEL] if labels is None else [str(name) for name in labels]
    try:
        for name in names:
            _ensure_label(repo, name, run)
        args = ["api", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        for name in names:
            args += ["-f", f"labels[]={name}"]
        proc = run(args)
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr,
                           "cicd.issue_create_failed repo=%s: %s", repo, proc.stderr.strip())
            return None
        return int(json.loads(proc.stdout)["number"])
    except Exception:
        log.warning("cicd.issue_create_failed repo=%s", repo, exc_info=True)
        return None


def comment_on_pr(repo: str, number: int, body: str, *,
                  run=gitrepos._run_gh) -> str | None:
    """Comment on a pull request, returning the comment's html_url.

    Posts through ``repos/{repo}/issues/{n}/comments`` — the endpoint ``gh pr comment``
    itself shells to — rather than the porcelain command: it matches the other
    mutations in this module, and it answers with JSON carrying the url the caller
    hands back to the user instead of a bare line of stdout.
    """
    try:
        proc = run(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "github.pr_comment_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return None
        return str(json.loads(proc.stdout)["html_url"])
    except Exception:
        log.warning("github.pr_comment_failed repo=%s #%s", repo, number, exc_info=True)
        return None


def comment_on_issue(repo: str, number: int, body: str, *, automation: bool = True,
                     run=gitrepos._run_gh) -> str | None:
    """Comment on an issue, returning the comment's html_url; None on failure.

    Same endpoint as ``comment_on_pr`` (GitHub shares one comment API across
    issues and PRs) — kept as its own name so call sites in the filers read as
    what they are. Reached through ``issue_filing.apply`` by all three filers:
    error triage, the session-feedback filer (#1434) and the CI/CD Doctor (#1433).

    Marked ``AUTOMATION_COMMENT_MARKER`` so the mirror never reads it as a person's
    comment re-arming a not-needed issue (#2683 review). ``automation=False`` is for
    a comment a PERSON asked for (the assistant's ``comment_on_issue`` and the note
    ``close_issue`` posts): that is the operator's word on the issue, and re-arming on
    it is exactly right — so, like ``comment_on_pr``, it goes out unmarked.
    """
    text = with_automation_marker(body) if automation else body
    try:
        proc = run(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={text}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "github.issue_comment_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return None
        return str(json.loads(proc.stdout)["html_url"])
    except Exception:
        log.warning("github.issue_comment_failed repo=%s #%s", repo, number, exc_info=True)
        return None


def issue_states(repo: str, numbers: Sequence[int], *,
                 run=gitrepos._run_gh) -> dict[int, tuple[bool, str | None]]:
    """``{number: (closed, state_reason)}`` for up to 100 issues in ONE GraphQL call.

    ``state_reason`` is GitHub's ``stateReason`` lowercased ("completed",
    "not_planned", "reopened") or None. Issues that do not exist are omitted.
    Returns {} on any failure — callers treat "unknown" as "unchanged".
    """
    try:
        wanted = [int(n) for n in numbers][:100]
        if not wanted:
            return {}
        owner, name = repo.split("/", 1)
        aliases = " ".join(f"i{n}: issue(number: {n}) {{ number closed stateReason }}"
                           for n in wanted)
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {aliases} }} }}'
        proc = run(["api", "graphql", "-f", f"query={query}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr,
                           "github.issue_states_failed repo=%s: %s", repo, proc.stderr.strip())
            return {}
        repo_data = (json.loads(proc.stdout).get("data") or {}).get("repository") or {}
        out: dict[int, tuple[bool, str | None]] = {}
        for value in repo_data.values():
            if not value:
                continue
            reason = value.get("stateReason")
            out[int(value["number"])] = (bool(value.get("closed")),
                                         str(reason).lower() if reason else None)
        return out
    except Exception:
        log.warning("github.issue_states_failed repo=%s", repo, exc_info=True)
        return {}


def remove_label(repo: str, number: int, name: str, *, run=gitrepos._run_gh) -> bool:
    """Remove label *name* (as spelled on the issue) from an issue; True on success.
    The label is percent-encoded into the ``gh api`` path (a label may hold spaces or
    a slash) and a relative-path name is refused, for the same reason ``valid_repo``
    exists."""
    if not valid_repo(repo) or not name.strip() or name in (".", ".."):
        return False
    try:
        proc = run(["api", "-X", "DELETE",
                    f"repos/{repo}/issues/{number}/labels/{quote(name, safe='')}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "github.label_remove_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return False
        return True
    except Exception:
        log.warning("github.label_remove_failed repo=%s #%s", repo, number, exc_info=True)
        return False


def label_placer(repo: str, number: int, name: str, *, run=gitrepos._run_gh) -> str | None:
    """The GitHub login that LAST applied label *name* to an issue (its latest
    ``labeled`` event, compared case-insensitively), or None when unknown — no such
    event, or the read failed. Bot logins keep their ``[bot]`` suffix (REST shape)."""
    if not valid_repo(repo):
        return None
    jq = (".[] | select(.event == \"labeled\" and ((.label.name // \"\") | ascii_downcase) == "
          f"{json.dumps(name.lower())}) | .actor.login // empty")
    try:
        proc = run(["api", "--paginate", f"repos/{repo}/issues/{number}/events", "--jq", jq])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "github.label_events_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return None
        logins = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return logins[-1] if logins else None
    except Exception:
        log.warning("github.label_events_failed repo=%s #%s", repo, number, exc_info=True)
        return None


def viewer_login(*, run=gitrepos._run_gh) -> str | None:
    """The login of the gh account this host writes to GitHub as — the operator's own
    account on a single-operator install — or None when it cannot be read."""
    try:
        proc = run(["api", "user", "--jq", ".login"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "github.viewer_login_failed: %s", proc.stderr.strip())
            return None
        return proc.stdout.strip() or None
    except Exception:
        log.warning("github.viewer_login_failed", exc_info=True)
        return None


def close_issue(repo: str, number: int, *, run=gitrepos._run_gh) -> bool:
    try:
        proc = run(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
                    "-f", "state=closed"])
        return proc.returncode == 0
    except Exception:
        log.warning("cicd.issue_close_failed repo=%s #%s", repo, number, exc_info=True)
        return False


# No reopen_issue: every filer now files a linked "[regression]" issue when a
# closed fingerprint comes back, and none of them reopens (issue_filing.decide,
# #1433). The doctor's reopen path (#448) was this module's only caller.


def edit_issue_body(repo: str, number: int, body: str, *, run=gitrepos._run_gh) -> bool:
    try:
        proc = run(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
                    "-f", f"body={body}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "cicd.issue_edit_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return False
        return True
    except Exception:
        log.warning("cicd.issue_edit_failed repo=%s #%s", repo, number, exc_info=True)
        return False


def add_label(repo: str, number: int, name: str, *, run=gitrepos._run_gh) -> bool:
    """Add label *name* to an issue, creating it in the repo first if needed."""
    if not valid_repo(repo) or not name.strip():
        return False
    try:
        _ensure_label(repo, name, run)
        proc = run(["api", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={name}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "cicd.label_add_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return False
        return True
    except Exception:
        log.warning("cicd.label_add_failed repo=%s #%s", repo, number, exc_info=True)
        return False


def issue_snapshot(repo: str, number: int, *, run=gitrepos._run_gh) -> dict | None:
    """``{"state", "body", "labels"}`` for one issue; None on any failure."""
    try:
        proc = run(["api", f"repos/{repo}/issues/{number}"])
        if proc.returncode != 0:
            log_gh_failure(log, proc.stderr, "cicd.issue_read_failed repo=%s #%s: %s",
                           repo, number, proc.stderr.strip())
            return None
        data = json.loads(proc.stdout)
        return {"state": str(data.get("state") or ""), "body": data.get("body") or "",
                "labels": [str(lb.get("name")) for lb in data.get("labels") or []
                           if isinstance(lb, dict) and lb.get("name")]}
    except Exception:
        log.warning("cicd.issue_read_failed repo=%s #%s", repo, number, exc_info=True)
        return None
