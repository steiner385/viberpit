"""GitHub I/O for the Backlog Runner — pre-dispatch verification, claim
announcements, needs-human handoff notes and close-out.

Candidate DISCOVERY no longer lives here: the runner selects from the global
priority queue over the mirrored Projects v2 boards (spec 003 §7a,
``SessionOrchestrator._backlog_query_queue``). What remains is the GitHub side
of acting on a pick — re-verifying it against live GitHub immediately before the
claim (:func:`verify_ready_live`, fails closed), announcing the claim and the
needs-human handoff, and closing out on a merged PR.

Every GitHub call is routed through an injected ``gh_run`` seam (default
:func:`ccswitch.gitrepos._run_gh`) so tests never hit the network.  The seam is
``gh_run(args: list[str]) -> subprocess.CompletedProcess`` — the ``gh`` binary
with ``GITHUB_TOKEN`` stripped, exactly as the rest of ViberPit shells it.

Phase D (#994) retired the ``in-progress``/``needs-human`` labels: a claim is the
runner's durable target-state row (plus board Status ``In progress`` from the
reconciler) and a handoff is its ``gave_up`` row. So nothing in this module takes
a lock or can fail a spawn — every write here is a best-effort COMMENT, the
human-visible trace of a decision the rows already carry.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Iterable

from ccswitch import brand, gitrepos

from . import backlog_deps

log = logging.getLogger(__name__)

GhRun = Callable[..., "object"]

# Human-gate signals (#784). Linkage (#642) proves a PR *references* an issue;
# it does NOT prove the PR *did the work*. Two issue classes a merged PR can
# never legitimately complete, so the runner must escalate them to a human
# instead of auto-closing:
#
#   1. Action/ops issues — real-world steps no diff performs (verify a domain in
#      Search Console, prune scopes on the GCP consent screen, record a demo
#      video, submit to Google for review). Detected by an ops label or a curated
#      high-signal phrase in the title/body. Bare ambiguous words ("submit",
#      "console") are matched ONLY inside these multi-word phrases — a bare
#      keyword would escalate ordinary code issues ("fix console.log").
#   2. Any non-documentation issue "closed" by a DOCS-ONLY PR — a runbook/guide
#      for X is not doing X. The docs-only diff gate is the catch-all for action
#      issues whose wording the phrase list didn't enumerate.
#
# The gate fails SAFE: a false positive just asks a human to close manually; a
# false negative is the false-completion bug itself. So the phrase list can be
# generous and the docs-only gate broad.
ACTION_ISSUE_LABELS = frozenset({
    "action-required", "action", "ops", "operations", "manual", "manual-step"})
DOC_ISSUE_LABELS = frozenset({"documentation", "docs"})
ACTION_ISSUE_PHRASES = (
    "search console", "consent screen", "consent-screen", "oauth consent",
    "verify domain", "verify the domain", "domain verification",
    "demo video", "record a demo", "record and upload", "record + upload",
    "gcp console", "google cloud console", "cloud console",
    "google search console", "app verification", "verification submission",
    "submit for verification", "submit for review", "submit to google",
    "publishing status",
)
# A PR whose diff touches ONLY these is documentation, not implementation.
DOC_PATH_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt")


# --------------------------------------------------------------------------- #
# Ref helpers                                                                  #
# --------------------------------------------------------------------------- #

def _split_ref(ref: str) -> tuple[str, str]:
    """``"owner/repo#123"`` → ``("owner/repo", "123")``."""
    repo, _, number = ref.partition("#")
    return repo, number


def _group_repos_by_owner(repos: Iterable[str]) -> list[tuple[str, list[str]]]:
    """Group ``"owner/name"`` repos by owner, preserving first-seen order.

    A single GraphQL search spanning repos owned by different owners can only
    ever be authenticated with one installation token — so any multi-repo query
    must issue one call per owner (never send one owner's token against
    another owner's repo; it would 404 and look like a missing repo)."""
    groups: dict[str, list[str]] = {}
    for repo in repos:
        owner = repo.partition("/")[0]
        groups.setdefault(owner, []).append(repo)
    return list(groups.items())


# --------------------------------------------------------------------------- #
# Epic gate                                                                    #
# --------------------------------------------------------------------------- #

def _open_sub_issue_count(issue: dict | None) -> int:
    """How many of *issue*'s native sub-issues are still open — the tracking-issue
    signal. Reads the ``subIssues { nodes { state } }`` shape of an issue-signals
    payload; its one caller is the close gate (the candidate search that also fed
    it went away with the board-mirror queue, spec 003 §7a).

    An issue with open sub-issues is an epic under spec 003 D1: GitHub primitives
    are the planning truth, and the children ARE the work. The runner must
    neither claim such an issue (a session has nothing to implement) nor close it
    on a PR merge (ten children orphaned under a closed parent — #997). Once
    every child is done the count is 0 and the issue is ordinary work again.
    """
    nodes = (((issue or {}).get("subIssues") or {}).get("nodes")) or []
    return sum(1 for n in nodes if n and not backlog_deps._is_done(n.get("state")))


# --------------------------------------------------------------------------- #
# Pre-dispatch live re-verification (spec 003 §7a safety net)                  #
# --------------------------------------------------------------------------- #

def verify_ready_live(refs: list[str], *, gh_run: GhRun = gitrepos._run_gh) -> bool | None:
    """Re-check a batch's issues against live GitHub right before the claim.

    The runner picks from the board mirror, which can lag GitHub by a sync
    interval — an issue closed, assigned, or newly blocked in that window would
    otherwise be claimed and a session spawned on stale work. One aliased query
    per owner (``r<i>`` per repo, ``i<n>`` per issue; each owner's own App token,
    never one owner's token against another's repo — it would 404).

    Returns ``True`` only when EVERY ref is ``OPEN``, has no assignee and no
    ``OPEN`` node in ``blockedBy``; ``False`` as soon as one is not; ``None`` on
    any transport/parse failure, a malformed ref, a ref the response does not
    resolve (GitHub returns ``null`` for an alias it cannot serve), or a
    ``blockedBy`` list longer than the page requested (the blockers past the page
    are unknown, and unknown is never clear). Spec 003 §10:
    the check FAILS CLOSED — the caller treats ``None`` exactly like ``False``.
    A failing owner must therefore abort the whole check rather than being
    skipped, which would turn "unknown" into "no failing issue found".
    """
    by_repo: dict[str, list[str]] = {}
    for ref in refs:
        repo, number = _split_ref(ref)
        if not repo or not number.isdigit():
            log.warning("backlog_github.verify_ready_malformed_ref ref=%s", ref)
            return None
        by_repo.setdefault(repo, []).append(number)
    if not by_repo:
        return None

    for owner, owner_repos in _group_repos_by_owner(list(by_repo)):
        parts = ["query {"]
        for i, repo in enumerate(owner_repos):
            _owner, _, name = repo.partition("/")
            lookups = " ".join(
                f"i{n}: issue(number: {n}) {{ state assignees(first: 1) {{ totalCount }} "
                "blockedBy(first: 50) { totalCount nodes { state } } }"
                for n in by_repo[repo])
            parts.append(
                f"r{i}: repository(owner: {json.dumps(_owner)}, name: {json.dumps(name)}) "
                f"{{ {lookups} }}")
        parts.append("}")
        query = "\n".join(parts)
        proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                      token=gitrepos.app_token_for_owner(owner))
        if getattr(proc, "returncode", 1) != 0:
            log.warning("backlog_github.verify_ready_failed owner=%s err=%s",
                        owner, (getattr(proc, "stderr", "") or "").strip())
            return None
        try:
            data = json.loads(getattr(proc, "stdout", "") or "{}").get("data")
        except (json.JSONDecodeError, AttributeError):
            log.warning("backlog_github.verify_ready_unparseable owner=%s", owner,
                        exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        for i, repo in enumerate(owner_repos):
            repo_obj = data.get(f"r{i}")
            if not isinstance(repo_obj, dict):
                return None
            for n in by_repo[repo]:
                issue = repo_obj.get(f"i{n}")
                if not isinstance(issue, dict):
                    log.warning("backlog_github.verify_ready_unresolved ref=%s#%s", repo, n)
                    return None
                if issue.get("state") != "OPEN":
                    return False
                if (issue.get("assignees") or {}).get("totalCount", 0):
                    return False
                # Same polarity as the mirror-side gate (backlog_deps): a
                # blocker whose state cannot be read is BLOCKING, not clear.
                blocked_by = issue.get("blockedBy") or {}
                blockers = blocked_by.get("nodes") or []
                # More blockers than the page returned: the ones past the page are
                # unknown, and unknown is not clear (§10 fails closed).
                total = blocked_by.get("totalCount")
                if isinstance(total, int) and total > len(blockers):
                    log.warning("backlog_github.verify_ready_blockers_truncated "
                                "ref=%s#%s total=%s page=%s", repo, n, total, len(blockers))
                    return None
                if any(not backlog_deps._is_done(b.get("state") if isinstance(b, dict) else None)
                       for b in blockers):
                    return False
    return True


# --------------------------------------------------------------------------- #
# Cross-reference PR lookup                                                    #
# --------------------------------------------------------------------------- #

def _issue_crossref_pr_nodes(ref: str, *, gh_run: GhRun) -> list[dict] | None:
    """Same-repo PR source nodes from *ref*'s CROSS_REFERENCED_EVENT timeline.

    Returns ``None`` when the lookup failed (gh error / unparseable payload) —
    callers must treat that as *unknown*, never as "no references". Cross-repo
    references are dropped here so every consumer shares the same-repo scoping.
    """
    repo, number = _split_ref(ref)
    owner, _, name = repo.partition("/")
    if not owner or not name or not number.isdigit():
        return None
    query = (
        "query {\n"
        f"  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    issue(number: {int(number)}) {{\n"
        "      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 50) {\n"
        "        nodes { ... on CrossReferencedEvent { source { ... on PullRequest {\n"
        "          number url title state isDraft headRefName\n"
        "          autoMergeRequest { enabledAt }\n"
        "          repository { nameWithOwner }\n"
        "        } } } }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                  token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.find_referencing_pr_failed ref=%s err=%s",
                    ref, (getattr(proc, "stderr", "") or "").strip())
        return None
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.find_referencing_pr_unparseable ref=%s", ref)
        return None
    nodes = ((((payload.get("data") or {}).get("repository") or {})
              .get("issue") or {}).get("timelineItems") or {}).get("nodes") or []
    out: list[dict] = []
    for node in nodes:
        src = (node or {}).get("source") or {}
        if ((src.get("repository") or {}).get("nameWithOwner")) != repo:
            continue
        out.append(src)
    return out


def find_referencing_pr(ref: str, *, gh_run: GhRun = gitrepos._run_gh):
    """The best same-repo PR cross-referencing issue *ref*, or ``None``.

    The runner's sessions don't reliably record their branch (shared-clone
    reflog discovery misses worktree-created branches) and don't reliably put
    ``Fixes #N`` in the PR body — so when the cached-branch lookup misses, this
    asks GitHub directly which PRs mention the issue (a PR titled ``… (#N)``
    creates the cross-reference even without a closing keyword).

    Preference: MERGED beats OPEN (the work landed); a later reference beats an
    earlier one. CLOSED-unmerged and cross-repo references are ignored — a
    drive-by mention from a closed PR must never read as "PR closed unmerged →
    give up". Any gh failure or unparseable payload returns ``None``.
    """
    from server.github.pr_status import PullRequestInfo

    srcs = _issue_crossref_pr_nodes(ref, gh_run=gh_run)
    if not srcs:
        return None
    merged_pr = open_pr = None
    for src in srcs:
        state = src.get("state")
        if state == "MERGED":
            merged_pr = src        # last reference wins within a priority tier
        elif state == "OPEN":
            open_pr = src
    best = merged_pr or open_pr
    if best is None:
        return None
    merged = best.get("state") == "MERGED"
    return PullRequestInfo(
        number=best.get("number") or 0,
        url=best.get("url") or "",
        state="merged" if merged else ("draft" if best.get("isDraft") else "open"),
        checks="none",
        title=best.get("title") or "",
        is_draft=bool(best.get("isDraft")),
        merged=merged,
        head_ref=best.get("headRefName") or "",
        auto_merge_enabled=best.get("autoMergeRequest") is not None,
    )


# --------------------------------------------------------------------------- #
# Comments: the claim / handoff announcements                                  #
# --------------------------------------------------------------------------- #

def _issue_comment(ref: str, body: str, *, gh_run: GhRun) -> bool:
    repo, number = _split_ref(ref)
    proc = gh_run(["issue", "comment", number, "--repo", repo, "--body", body],
                  token=gitrepos.app_token_for_repo(repo))
    return getattr(proc, "returncode", 1) == 0


def announce_claim(batch, comment: str, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Human-visible trace of a claim: one comment per issue. The claim itself is
    the runner's durable target-state row (records-before-anything) plus board
    Status ``In progress`` from the reconciler — Phase D retired the
    ``in-progress`` label, so nothing here can fail a spawn: a comment that does
    not land is logged and the batch proceeds.

    The per-ref guard is what makes "never raises" true: the comment path can
    RAISE as well as return non-zero (minting the repo's App token does network
    I/O), and an escaping exception would both abort the caller and skip the
    bundle's remaining issues."""
    for ref in batch.issues:
        try:
            posted = _issue_comment(ref, comment, gh_run=gh_run)
        except Exception:
            log.warning("backlog_github.announce_claim_error ref=%s", ref, exc_info=True)
            continue
        if not posted:
            log.warning("backlog_github.announce_claim_failed ref=%s", ref)


def announce_needs_human(batch, reason: str, *,
                         gh_run: GhRun = gitrepos._run_gh) -> None:
    """Human-visible trace of a give-up: one comment per issue carrying the
    reason a human needs in order to pick the target up. The handoff SIGNAL is
    the runner's durable ``gave_up`` row (which the reconciler turns into board
    Status ``Needs human`` and the queue into a retry cooldown); this only says
    so on the issue. Best-effort, exactly like :func:`announce_claim`."""
    body = (f"Handed to a human by {brand.NAME} Backlog Runner: {reason}. "
            "Board Status is Needs human; the runner retries after the "
            "configured window.")
    for ref in batch.issues:
        try:
            posted = _issue_comment(ref, body, gh_run=gh_run)
        except Exception:
            log.warning("backlog_github.announce_needs_human_error ref=%s", ref,
                        exc_info=True)
            continue
        if not posted:
            log.warning("backlog_github.announce_needs_human_failed ref=%s", ref)


# --------------------------------------------------------------------------- #
# PR↔issue linkage verification (fail closed, #642)                            #
# --------------------------------------------------------------------------- #

def _pr_linkage(repo: str, pr_number: int, *, gh_run: GhRun) -> dict | None:
    """PR-side linkage evidence for ``repo`` PR ``pr_number``.

    Returns ``{"closing": set[int], "text": str}`` — the same-repo issue numbers
    the PR's ``closingIssuesReferences`` will auto-close, plus the searchable
    text (body + commit messages). ``None`` when the lookup failed: *unknown*,
    never "no linkage".
    """
    owner, _, name = repo.partition("/")
    if not owner or not name or not pr_number:
        return None
    query = (
        "query {\n"
        f"  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    pullRequest(number: {int(pr_number)}) {{\n"
        "      body\n"
        "      closingIssuesReferences(first: 50) {\n"
        "        nodes { number repository { nameWithOwner } }\n"
        "      }\n"
        "      commits(first: 100) { nodes { commit { message } } }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                  token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.pr_linkage_failed repo=%s pr=%s err=%s",
                    repo, pr_number, (getattr(proc, "stderr", "") or "").strip())
        return None
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.pr_linkage_unparseable repo=%s pr=%s", repo, pr_number)
        return None
    pr = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not pr:
        return None
    closing = {
        node.get("number")
        for node in (((pr.get("closingIssuesReferences") or {}).get("nodes")) or [])
        if node and ((node.get("repository") or {}).get("nameWithOwner")) == repo
    }
    texts = [pr.get("body") or ""]
    for node in (((pr.get("commits") or {}).get("nodes")) or []):
        texts.append(((node or {}).get("commit") or {}).get("message") or "")
    return {"closing": closing, "text": "\n".join(texts)}


def _text_references_issue(text: str, repo: str, number: int) -> bool:
    """True when *text* carries a ``#N`` token (not part of a longer number,
    e.g. ``#89`` is not ``#8``) or a same-repo ``…/issues/N`` URL."""
    if re.search(rf"(?<![\w#])#{number}(?!\d)", text):
        return True
    return re.search(rf"{re.escape(repo)}/issues/{number}(?!\d)", text) is not None


def _pr_issue_linked(ref: str, pr_number: int, linkage: dict | None, *,
                     gh_run: GhRun) -> bool | None:
    """Whether merged PR ``pr_number`` verifiably references issue *ref*.

    - ``True`` — linked: the PR's ``closingIssuesReferences`` names the issue,
      its body/commits reference it, or the issue's own cross-reference
      timeline names the PR.
    - ``False`` — verified unlinked: the issue's timeline was fetched and does
      NOT contain the PR. Any PR-side mention of an issue creates a
      CROSS_REFERENCED_EVENT on that issue, so timeline absence is decisive.
    - ``None`` — unverifiable (lookups failed): fail closed, decide nothing.
    """
    repo, number_s = _split_ref(ref)
    if not number_s.isdigit():
        return None
    number = int(number_s)
    if linkage is not None:
        if number in linkage["closing"]:
            return True
        if _text_references_issue(linkage["text"], repo, number):
            return True
    srcs = _issue_crossref_pr_nodes(ref, gh_run=gh_run)
    if srcs is None:
        return None
    return True if any(src.get("number") == pr_number for src in srcs) else False


def _close_issue(ref: str, comment: str, *, gh_run: GhRun) -> bool:
    """Close one issue with a linking comment. Best-effort; returns success."""
    repo, number = _split_ref(ref)
    try:
        proc = gh_run(["issue", "close", number, "--repo", repo,
                       "--comment", comment], token=gitrepos.app_token_for_repo(repo))
        if getattr(proc, "returncode", 1) != 0:
            log.warning("backlog_github.close_issue_failed ref=%s", ref)
            return False
        return True
    except Exception:  # pragma: no cover — best-effort
        log.warning("backlog_github.close_issue_error ref=%s", ref, exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Human gate: action-required issues & docs-only PRs (fail safe, #784)         #
# --------------------------------------------------------------------------- #

def _issue_signals(ref: str, *, gh_run: GhRun) -> dict | None:
    """Title, body, label names and open sub-issue count for issue *ref* — the
    close-gate inputs.

    Returns ``{"title": str, "body": str, "labels": list[str],
    "open_sub_issues": int}`` or ``None`` when the lookup failed / the issue is
    gone (caller treats ``None`` as *can't assess* → don't block the close)."""
    repo, number = _split_ref(ref)
    owner, _, name = repo.partition("/")
    if not owner or not name or not number.isdigit():
        return None
    query = (
        "query {\n"
        f"  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    issue(number: {int(number)}) {{\n"
        "      title body\n"
        "      labels(first: 50) { nodes { name } }\n"
        "      subIssues(first: 100) { nodes { state } }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                  token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.issue_signals_failed ref=%s err=%s",
                    ref, (getattr(proc, "stderr", "") or "").strip())
        return None
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.issue_signals_unparseable ref=%s", ref)
        return None
    issue = ((payload.get("data") or {}).get("repository") or {}).get("issue")
    if not issue:
        return None
    labels = [((n or {}).get("name") or "")
              for n in (((issue.get("labels") or {}).get("nodes")) or [])]
    return {"title": issue.get("title") or "",
            "body": issue.get("body") or "", "labels": labels,
            "open_sub_issues": _open_sub_issue_count(issue)}


def _pr_changed_files(repo: str, pr_number: int, *, gh_run: GhRun) -> list[str] | None:
    """Paths changed by ``repo`` PR ``pr_number`` (first 100). ``None`` on lookup
    failure (unknown — never assume docs-only)."""
    owner, _, name = repo.partition("/")
    if not owner or not name or not pr_number:
        return None
    query = (
        "query {\n"
        f"  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    pullRequest(number: {int(pr_number)}) {{\n"
        "      files(first: 100) { nodes { path } }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                  token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.pr_files_failed repo=%s pr=%s err=%s",
                    repo, pr_number, (getattr(proc, "stderr", "") or "").strip())
        return None
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.pr_files_unparseable repo=%s pr=%s", repo, pr_number)
        return None
    pr = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not pr:
        return None
    nodes = ((pr.get("files") or {}).get("nodes")) or []
    return [((n or {}).get("path") or "") for n in nodes if (n or {}).get("path")]


def _issue_is_action_required(signals: dict) -> bool:
    """True when *signals* describe a real-world action no diff performs (#784):
    an ops label, or a curated action phrase in the title/body."""
    labels = {lbl.strip().lower() for lbl in (signals.get("labels") or ())}
    if labels & ACTION_ISSUE_LABELS:
        return True
    haystack = f"{signals.get('title') or ''}\n{signals.get('body') or ''}".lower()
    return any(phrase in haystack for phrase in ACTION_ISSUE_PHRASES)


def _issue_is_documentation(signals: dict) -> bool:
    """True when the issue is itself documentation work — a docs-only PR then
    legitimately satisfies it, so the docs-only gate must not fire."""
    labels = {lbl.strip().lower() for lbl in (signals.get("labels") or ())}
    if labels & DOC_ISSUE_LABELS:
        return True
    title = (signals.get("title") or "").lower()
    return "document" in title or "readme" in title or "changelog" in title


def _is_doc_path(path: str) -> bool:
    p = path.strip().lower()
    return (p.startswith("docs/") or "/docs/" in p or p.endswith(DOC_PATH_SUFFIXES))


def _is_docs_only(files: list[str] | None) -> bool | None:
    """True when every changed path is documentation, False when at least one is
    not, ``None`` when the file list is unknown/empty (never assume docs-only)."""
    if not files:
        return None
    return all(_is_doc_path(p) for p in files)


def _human_gate_reason(signals: dict | None, docs_only: bool | None) -> str | None:
    """Why a verified-linked PR must NOT auto-close this issue, or ``None`` to
    close it. Fails safe: unknown signals → close (linkage already gated it)."""
    if signals is None:
        return None
    if _issue_is_action_required(signals):
        return ("this looks like a manual/operational action a merged PR cannot "
                "perform (verification, console change, recording, submission)")
    if docs_only and not _issue_is_documentation(signals):
        return ("the merged PR changed only documentation — a runbook/guide for "
                "the work is not the work itself")
    return None


def _escalate_issue(ref: str, reason: str, pr_link: str, *, gh_run: GhRun) -> None:
    """Say on the issue why a merged PR did NOT close it. The escalation itself
    is the ``"escalated"`` outcome this feeds back to the sweep, which writes the
    durable ``gave_up`` row (Phase D) — the row, not this comment, is what keeps
    the issue out of the pool for the retry window. Best-effort."""
    _issue_comment(
        ref,
        f"Batch PR {pr_link} merged, but this issue was **not** auto-closed: "
        f"{reason}. Handed to a human by {brand.NAME} Backlog "
        f"Runner — please complete the real work and close manually.",
        gh_run=gh_run)


def _release_tracking_issue(ref: str, open_children: int, pr_link: str, *,
                            gh_run: GhRun) -> None:
    """Say on a tracking epic a merged PR referenced why it was left OPEN. Unlike
    :func:`_escalate_issue` this is NOT an escalation: nothing about the epic
    needs a human — its open sub-issues are the remaining work, and the candidate
    gate keeps the epic itself out of the pool. Best-effort."""
    _issue_comment(
        ref,
        f"PR {pr_link} merged but this is a tracking issue with {open_children} open "
        f"sub-issue(s), so {brand.NAME} Backlog Runner left it open (claim released). "
        f"It closes when its sub-issues do.",
        gh_run=gh_run)


def close_batch_issues(batch, info, *, gh_run: GhRun = gitrepos._run_gh) -> str:
    """Close the batch's issues after its PR merged — FAIL CLOSED on linkage.

    The implement prompt asks for ``Fixes #N`` in the PR body, but sessions
    don't reliably comply, so this close-out exists to close the issues a merged
    PR really did implement without one. But the branch→PR association that got
    us here can be WRONG: shared-clone branch discovery can attribute another session's
    merged PR to this batch (#642 — a dep-bump PR closed an unrelated epic
    twice). So each issue is closed only with verified linkage to this PR
    (:func:`_pr_issue_linked`); one PR-side lookup covers the batch, plus one
    timeline lookup per issue the PR side didn't resolve.

    Returns the close-out outcome for the sweep:

    - ``"closed"``   — the PR is the batch's (≥1 issue verifiably linked) and at
      least one issue was closed. Linked issues are closed with a linking comment
      (best-effort: an individual failure is logged and the rest still close)
      UNLESS the human gate (:func:`_human_gate_reason`, #784) says a merged PR
      can't legitimately complete them — an action/ops issue, or a non-doc issue
      with a docs-only diff — in which case they are left OPEN with an explaining
      comment. Verified-unlinked issues are left OPEN with a comment; the runner's
      row is what returns them to the pool.
    - ``"escalated"`` — the human gate fired and NOTHING was closed: the merge
      landed but a human has to finish and close the work. The sweep turns this
      into a durable ``gave_up`` row (Phase D's needs-human signal) instead of
      deleting the row as a completion.
    - ``"unlinked"`` — every issue is verified-unlinked: a misattributed PR.
      Nothing is closed; the caller must ignore/denylist the PR.
    - ``"retry"``    — linkage unverifiable (lookups failed) for at least one
      issue and none verified linked: decide nothing, retry next sweep.
    """
    pr_number = int(getattr(info, "number", 0) or 0)
    repo = getattr(batch, "repo", "") or (
        batch.issues[0].split("#", 1)[0] if batch.issues else "")
    linkage = _pr_linkage(repo, pr_number, gh_run=gh_run)
    verdicts = {ref: _pr_issue_linked(ref, pr_number, linkage, gh_run=gh_run)
                for ref in batch.issues}
    linked = [ref for ref, v in verdicts.items() if v is True]
    unlinked = [ref for ref, v in verdicts.items() if v is False]
    if not linked:
        if unlinked and len(unlinked) == len(verdicts):
            log.warning("backlog_github.close_batch_unlinked pr=%s refs=%s",
                        pr_number, list(verdicts))
            return "unlinked"
        log.warning("backlog_github.close_batch_unverified pr=%s refs=%s",
                    pr_number, list(verdicts))
        return "retry"
    pr_link = getattr(info, "url", "") or f"PR #{pr_number}"
    comment = (f"Implemented by {pr_link} (merged) — "
               f"closed by {brand.NAME} Backlog Runner.")
    # A verified-linked PR still must not falsely complete an action/ops issue or
    # be satisfied by a docs-only diff (#784). Fetch the diff once (same PR for
    # the whole batch), then gate each linked issue on its own signals.
    docs_only = _is_docs_only(_pr_changed_files(repo, pr_number, gh_run=gh_run))
    escalated = False
    any_closed = False
    for ref in linked:
        signals = _issue_signals(ref, gh_run=gh_run)
        open_children = (signals or {}).get("open_sub_issues") or 0
        if open_children:  # tracking epic — never closeable by one PR (#997)
            log.info("backlog_github.close_batch_release_tracking ref=%s pr=%s open=%d",
                     ref, pr_number, open_children)
            _release_tracking_issue(ref, open_children, pr_link, gh_run=gh_run)
            continue
        reason = _human_gate_reason(signals, docs_only)
        if reason:
            log.info("backlog_github.close_batch_escalate ref=%s pr=%s reason=%s",
                     ref, pr_number, reason)
            _escalate_issue(ref, reason, pr_link, gh_run=gh_run)
            escalated = True
        elif _close_issue(ref, comment, gh_run=gh_run):
            any_closed = True
    # The PR is the batch's, but it never referenced these issues — closing
    # them would be exactly the #642 bug. Leave them open and say so; the
    # runner's row is what returns them to the pool.
    for ref in unlinked:
        _issue_comment(
            ref,
            f"Batch PR {pr_link} merged without referencing this issue — "
            f"returned to the backlog pool (left open) by {brand.NAME} Backlog Runner.",
            gh_run=gh_run)
    # Nothing closed and the human gate fired: this merge is a handoff, not a
    # completion, and the sweep must keep a row rather than free the target.
    return "escalated" if escalated and not any_closed else "closed"
