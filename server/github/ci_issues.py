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

from ccswitch import brand, gitrepos

log = logging.getLogger(__name__)

LABEL = "ci-doctor"

_REPO_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

# Longest issue/comment body the write endpoints accept. GitHub's own limit is far
# higher (65 536); this is a ccswitch-side guard so a runaway assistant body can't be
# posted publicly, and it is the one both endpoints' pydantic models point at.
BODY_MAX_CHARS = 10_000


def valid_repo(repo: str) -> bool:
    """Whether `repo` is a safe ``owner/name`` to interpolate into a gh api path.

    The doctor's repos come from ccswitch's own config, but the write endpoints take
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
            log.warning("cicd.issue_create_failed repo=%s: %s", repo, proc.stderr.strip())
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
            log.warning("github.pr_comment_failed repo=%s #%s: %s",
                        repo, number, proc.stderr.strip())
            return None
        return str(json.loads(proc.stdout)["html_url"])
    except Exception:
        log.warning("github.pr_comment_failed repo=%s #%s", repo, number, exc_info=True)
        return None


def close_issue(repo: str, number: int, *, run=gitrepos._run_gh) -> bool:
    try:
        proc = run(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
                    "-f", "state=closed"])
        return proc.returncode == 0
    except Exception:
        log.warning("cicd.issue_close_failed repo=%s #%s", repo, number, exc_info=True)
        return False


def reopen_issue(repo: str, number: int, *, run=gitrepos._run_gh) -> bool:
    """Reopen a previously-filed ci-doctor issue when its finding recurs, instead
    of filing a brand-new duplicate each flap. Best-effort — False on failure so
    the caller can fall back to filing fresh."""
    try:
        proc = run(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
                    "-f", "state=open"])
        return proc.returncode == 0
    except Exception:
        log.warning("cicd.issue_reopen_failed repo=%s #%s", repo, number, exc_info=True)
        return False
