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

from ccswitch import brand, gh_credential, gitrepos
from server.obs.logdedupe import LogThrottle, log_event_throttled

log = logging.getLogger(__name__)

LABEL = "ci-doctor"

#: Window for the "this host's gh credential is unauthenticated" record (#2052).
#: Keyed on the condition, never on the target, which is the whole point: the
#: 2026-09-17 burst was 18 WARNINGs naming 18 healthy issues, because each write
#: reported the target it failed to reach rather than the credential it lacked.
_HOST_CREDENTIAL_THROTTLE = LogThrottle()


def _log_gh_failure(stderr: str, msg: str, *args: object) -> None:
    """Report one failed gh call — as the host condition when it is one.

    Two shapes, deliberately:

    * **Host credential missing** (a locked keyring — see the ``gh_credential``
      module and its runbook). Every caller in this module is hitting the
      same wall, so the per-target record is noise: it is demoted to DEBUG and a
      single throttled ``github.host_gh_unauthenticated`` event stands for the
      family. The 401s from writes and the unauthenticated-IP 403s from reads
      collapse together — they are one condition seen from two sides.
    * **Anything else** — logged at WARNING with *exactly* the format string the
      call site always used. :func:`server.obs.error_signature.signature_of`
      fingerprints on the normalized message, so rewording one of these would
      fork its existing auto-error issue into a fresh lineage.
    """
    if gh_credential.classify_gh_failure(stderr or "") == gh_credential.UNAUTHENTICATED:
        log.debug(msg, *args)
        log_event_throttled(
            log, _HOST_CREDENTIAL_THROTTLE, gh_credential.UNAUTHENTICATED,
            logging.WARNING, "github.host_gh_unauthenticated",
            runbook="docs/runbooks/gh-credential-without-a-keyring.md",
        )
        return
    log.warning(msg, *args)


_REPO_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

# Longest issue/comment body the write endpoints accept. GitHub's own limit is far
# higher (65 536); this is a ViberPit-side guard so a runaway assistant body can't be
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
            _log_gh_failure(proc.stderr,
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
            _log_gh_failure(proc.stderr, "github.pr_comment_failed repo=%s #%s: %s",
                            repo, number, proc.stderr.strip())
            return None
        return str(json.loads(proc.stdout)["html_url"])
    except Exception:
        log.warning("github.pr_comment_failed repo=%s #%s", repo, number, exc_info=True)
        return None


def comment_on_issue(repo: str, number: int, body: str, *,
                     run=gitrepos._run_gh) -> str | None:
    """Comment on an issue, returning the comment's html_url; None on failure.

    Same endpoint as ``comment_on_pr`` (GitHub shares one comment API across
    issues and PRs) — kept as its own name so call sites in the filers read as
    what they are. Used by the error-triage recurrence path and, later, the
    session-feedback filer (#1434) and the CI/CD Doctor (#1433).
    """
    try:
        proc = run(["api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
        if proc.returncode != 0:
            _log_gh_failure(proc.stderr, "github.issue_comment_failed repo=%s #%s: %s",
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
            _log_gh_failure(proc.stderr,
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
