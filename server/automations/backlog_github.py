"""GitHub I/O for the Backlog Runner — pre-dispatch verification + claim lifecycle.

Candidate DISCOVERY no longer lives here: the runner selects from the global
priority queue over the mirrored Projects v2 boards (spec 003 §7a,
``SessionOrchestrator._backlog_query_queue``). What remains is the GitHub side
of acting on a pick — re-verifying it against live GitHub immediately before the
claim (:func:`verify_ready_live`, fails closed), taking and releasing the
``in-progress`` lock, escalating to ``needs-human``, and closing out on a merged
PR.

Every GitHub call is routed through an injected ``gh_run`` seam (default
:func:`ccswitch.gitrepos._run_gh`) so tests never hit the network.  The seam is
``gh_run(args: list[str]) -> subprocess.CompletedProcess`` — the ``gh`` binary
with ``GITHUB_TOKEN`` stripped, exactly as the rest of ccswitch shells it.

Claiming a batch is *atomic and self-healing*: the ``in-progress`` label is the
lock, so if labeling any issue in the batch fails mid-way, the labels already
added in this call are rolled back (best-effort) and the claim reports failure.
A batch is never left partially-claimed but reported as success.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Iterable

from ccswitch import brand, gitrepos

from ..pm.queue import IN_PROGRESS_LABEL, NEEDS_HUMAN_LABEL
from . import backlog_deps

log = logging.getLogger(__name__)

GhRun = Callable[..., "object"]

# The claim-lifecycle labels are the only queue constants this module still
# needs: the bot/meta skip signals (``SKIP_ISSUE_*``) and the needs-human
# cooldown moved with candidate selection into server/pm/queue.py, which ranks
# the board mirror (spec 003 §7a) and is their single source of truth.

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


def _graphql_per_owner(repos: list[str], build_query: "Callable[[list[str]], str]", *,
                       gh_run: GhRun, event: str) -> list[dict]:
    """Run one ``gh api graphql`` call per owner in *repos* (grouped via
    :func:`_group_repos_by_owner`), each authenticated with that owner's own
    App installation token — never one owner's token against another owner's
    repo (it would 404). ``build_query(owner_repos)`` builds the query text for
    one owner's repo subset.

    Returns each owner's parsed ``data`` dict, **in owner order** — callers
    that must preserve a within-payload ordering invariant (e.g. "fresh
    results across ALL owners before any retry result", so grouping by owner
    for auth never reorders product-level scheduling) should extract each
    pool from every returned dict and concatenate pool-by-pool themselves,
    rather than flattening owner-by-owner.

    A gh failure or unparseable payload for one owner is logged (tagged with
    ``event``, matching this function's one caller's prior log names) and
    that owner is skipped — it must never blank out other owners' results.
    """
    out: list[dict] = []
    for owner, owner_repos in _group_repos_by_owner(repos):
        query = build_query(owner_repos)
        proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                      token=gitrepos.app_token_for_owner(owner))
        if getattr(proc, "returncode", 1) != 0:
            log.warning("backlog_github.%s_failed owner=%s err=%s",
                        event, owner, (getattr(proc, "stderr", "") or "").strip())
            continue
        try:
            payload = json.loads(getattr(proc, "stdout", "") or "{}")
        except json.JSONDecodeError:
            log.warning("backlog_github.%s_unparseable owner=%s", event, owner, exc_info=True)
            continue
        out.append(payload.get("data") or {})
    return out


# --------------------------------------------------------------------------- #
# Shared issue-node fragment + epic gate                                       #
# --------------------------------------------------------------------------- #

# The issue-node fragment for this module's issue searches. Its one reader is
# the claimed-refs search (:func:`query_claimed_refs`), which needs ``number``
# and ``repository``; the dependency/epic fields arrived with the candidate
# search that spec 003 §7a replaced with the board-mirror queue. They are left
# in place rather than narrowed: the whole claim-LABEL lifecycle this fragment
# serves is retired in Phase D (#994), and they cost no extra API call.
_ISSUE_NODES = (
    "nodes { ... on Issue { number title body url author { login } "
    "labels(first: 20) { nodes { name } } repository { nameWithOwner } "
    "subIssues(first: 50) { nodes { state } } "
    "blockedBy(first: 20) { nodes { number state repository { nameWithOwner } } } "
    "parent { number repository { nameWithOwner } "
    "subIssues(first: 50) { nodes { number state repository { nameWithOwner } } } } "
    "trackedInIssues(first: 5) { nodes { number body repository { nameWithOwner } "
    "trackedIssues(first: 50) { nodes { number state repository { nameWithOwner } } } } } } }"
)


def _open_sub_issue_count(issue: dict | None) -> int:
    """How many of *issue*'s native sub-issues are still open — the tracking-issue
    signal. Works on both raw GraphQL shapes the runner sees (a candidate node
    and an issue-signals payload), which share ``subIssues { nodes { state } }``.

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
    any transport/parse failure, a malformed ref, or a ref the response does not
    resolve (GitHub returns ``null`` for an alias it cannot serve). Spec 003 §10:
    the check FAILS CLOSED — the caller treats ``None`` exactly like ``False``.
    Deliberately not built on :func:`_graphql_per_owner`, which skips a failed
    owner and would turn "unknown" into "no failing issue found".
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
                "blockedBy(first: 50) { nodes { state } } }"
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
                blockers = (issue.get("blockedBy") or {}).get("nodes") or []
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
# Label mutations                                                             #
# --------------------------------------------------------------------------- #

def _issue_edit(ref: str, *, add: str | None = None, remove: str | None = None,
                gh_run: GhRun) -> bool:
    """Run one ``gh issue edit`` add/remove-label call. Return True on success."""
    repo, number = _split_ref(ref)
    args = ["issue", "edit", number, "--repo", repo]
    if add:
        args += ["--add-label", add]
    if remove:
        args += ["--remove-label", remove]
    proc = gh_run(args, token=gitrepos.app_token_for_repo(repo))
    return getattr(proc, "returncode", 1) == 0


def _issue_comment(ref: str, body: str, *, gh_run: GhRun) -> bool:
    repo, number = _split_ref(ref)
    proc = gh_run(["issue", "comment", number, "--repo", repo, "--body", body],
                  token=gitrepos.app_token_for_repo(repo))
    return getattr(proc, "returncode", 1) == 0


def ensure_claim_labels(repos: Iterable[str], *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Self-provision the runner's claim/handoff labels in each repo.

    The runner OWNS ``in-progress`` (its lock) and ``needs-human`` (its give-up
    handoff). ``gh issue edit --add-label`` fails on a repo that has never had the
    label, so without this a claim can never succeed on a fresh repo and nothing
    is ever spawned. ``gh label create --force`` is idempotent (creates when
    missing, updates otherwise). Best-effort: a provisioning failure must not
    crash the claim — the subsequent add-label call surfaces the real error.
    """
    for repo in dict.fromkeys(repos):  # de-dupe, preserve first-seen order
        token = gitrepos.app_token_for_repo(repo)
        for name in (IN_PROGRESS_LABEL, NEEDS_HUMAN_LABEL):
            try:
                gh_run(["label", "create", name, "--repo", repo, "--color", "ededed",
                        "--description", f"{brand.SLUG} backlog-runner", "--force"],
                      token=token)
            except Exception:  # pragma: no cover — provisioning must never raise
                log.warning("backlog_github.ensure_label_failed repo=%s name=%s", repo, name)


def claim_batch(batch, *, gh_run: GhRun = gitrepos._run_gh,
                comment: str | None = None) -> bool:
    """Atomically claim every issue in ``batch`` by adding ``in-progress``.

    Optionally posts ``comment`` (a linking note) on each issue.  If any gh call
    fails mid-batch, the ``in-progress`` labels already added in *this* call are
    rolled back (best-effort) and ``False`` is returned.  Returns ``True`` only
    when every issue was labeled (and commented, if requested).
    """
    # Self-provision the claim labels first: on a repo that has never had them,
    # `gh issue edit --add-label` would otherwise fail and no issue could ever be
    # claimed (the silent "runs but never spawns" failure).
    ensure_claim_labels([_split_ref(ref)[0] for ref in batch.issues], gh_run=gh_run)
    claimed: list[str] = []
    for ref in batch.issues:
        if not _issue_edit(ref, add=IN_PROGRESS_LABEL, gh_run=gh_run):
            _rollback_claim(claimed, gh_run=gh_run)
            return False
        claimed.append(ref)
        if comment is not None and not _issue_comment(ref, comment, gh_run=gh_run):
            _rollback_claim(claimed, gh_run=gh_run)
            return False
    return True


def _rollback_claim(claimed: list[str], *, gh_run: GhRun) -> None:
    """Best-effort remove ``in-progress`` from refs labeled in a failed claim."""
    for ref in claimed:
        try:
            _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run)
        except Exception:  # pragma: no cover — rollback must never raise
            log.warning("backlog_github.rollback_failed ref=%s", ref, exc_info=True)


def unclaim_batch(batch, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Release every issue in ``batch`` (remove ``in-progress``). Best-effort:
    an individual failure is logged and the sweep continues."""
    for ref in batch.issues:
        try:
            if not _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run):
                log.warning("backlog_github.unclaim_failed ref=%s", ref)
        except Exception:  # pragma: no cover — best-effort
            log.warning("backlog_github.unclaim_error ref=%s", ref, exc_info=True)


def label_needs_human(batch, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Escalate every issue in ``batch``: add ``needs-human`` and remove
    ``in-progress`` (it is no longer actively claimed). Best-effort."""
    for ref in batch.issues:
        try:
            if not _issue_edit(ref, add=NEEDS_HUMAN_LABEL,
                               remove=IN_PROGRESS_LABEL, gh_run=gh_run):
                log.warning("backlog_github.needs_human_failed ref=%s", ref)
        except Exception:  # pragma: no cover — best-effort
            log.warning("backlog_github.needs_human_error ref=%s", ref, exc_info=True)


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
    """Hand an issue to a human instead of closing it: comment why, add
    ``needs-human`` and drop ``in-progress`` (needs-human's retry cooldown keeps
    it out of the pool, so the runner won't churn re-attempting it). Best-effort."""
    _issue_comment(
        ref,
        f"Batch PR {pr_link} merged, but this issue was **not** auto-closed: "
        f"{reason}. Escalated to a human (`needs-human`) by {brand.NAME} Backlog "
        f"Runner — please complete the real work and close manually.",
        gh_run=gh_run)
    _issue_edit(ref, add=NEEDS_HUMAN_LABEL, remove=IN_PROGRESS_LABEL, gh_run=gh_run)


def _release_tracking_issue(ref: str, open_children: int, pr_link: str, *,
                            gh_run: GhRun) -> None:
    """Unclaim a tracking epic a merged PR referenced, leaving it OPEN. Unlike
    :func:`_escalate_issue` this adds NO ``needs-human`` label: nothing about the
    epic needs a human — its open sub-issues are the remaining work, and the
    candidate gate keeps the epic itself out of the pool. Best-effort."""
    _issue_comment(
        ref,
        f"PR {pr_link} merged but this is a tracking issue with {open_children} open "
        f"sub-issue(s), so {brand.NAME} Backlog Runner left it open (claim released). "
        f"It closes when its sub-issues do.",
        gh_run=gh_run)
    _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run)


def close_batch_issues(batch, info, *, gh_run: GhRun = gitrepos._run_gh) -> str:
    """Close the batch's issues after its PR merged — FAIL CLOSED on linkage.

    The implement prompt asks for ``Fixes #N`` in the PR body, but sessions
    don't reliably comply — and an issue a merged PR fixed must not stay open
    wearing ``in-progress``/``needs-human`` (it would be excluded from the
    candidate pools forever). But the branch→PR association that got us here
    can be WRONG: shared-clone branch discovery can attribute another session's
    merged PR to this batch (#642 — a dep-bump PR closed an unrelated epic
    twice). So each issue is closed only with verified linkage to this PR
    (:func:`_pr_issue_linked`); one PR-side lookup covers the batch, plus one
    timeline lookup per issue the PR side didn't resolve.

    Returns the close-out outcome for the sweep:

    - ``"closed"``   — the PR is the batch's (≥1 issue verifiably linked).
      Linked issues are closed with a linking comment (best-effort: an
      individual failure is logged and the rest still close) UNLESS the human
      gate (:func:`_human_gate_reason`, #784) says a merged PR can't legitimately
      complete them — an action/ops issue, or a non-doc issue with a docs-only
      diff — in which case they are escalated to ``needs-human`` and left OPEN.
      Verified-unlinked issues are left OPEN and returned to the pool
      (comment + unclaim).
    - ``"unlinked"`` — every issue is verified-unlinked: a misattributed PR.
      Nothing is closed or relabeled; the caller must ignore/denylist the PR.
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
        else:
            _close_issue(ref, comment, gh_run=gh_run)
    # The PR is the batch's, but it never referenced these issues — closing
    # them would be exactly the #642 bug. Leave them open and unclaim so they
    # re-enter the candidate pool. (Unverifiable refs stay claimed untouched;
    # the orphan heal resolves them once GitHub answers again.)
    for ref in unlinked:
        _issue_comment(
            ref,
            f"Batch PR {pr_link} merged without referencing this issue — "
            f"returned to the backlog pool (left open) by {brand.NAME} Backlog Runner.",
            gh_run=gh_run)
        _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run)
    return "closed"


# --------------------------------------------------------------------------- #
# Orphaned-claim healing (GitHub truth)                                        #
# --------------------------------------------------------------------------- #

def query_claimed_refs(repos: list[str], *, gh_run: GhRun = gitrepos._run_gh) -> list[str]:
    """Refs of OPEN issues wearing ``in-progress`` across *repos* — GitHub's
    view of the runner's outstanding claims.

    The runner owns the ``in-progress`` label, so any labeled issue no target
    state tracks is an orphaned lock (crash, DB loss, or the pre-handed-off
    handoff leak) holding an issue nobody is working. This is the ground-truth
    read the heal pass diffs against. Failures return ``[]``.
    """
    if not repos:
        return []
    # A search spanning repos owned by different owners can't be authenticated
    # with one installation token, so group by owner and issue one call per
    # owner (via the shared _graphql_per_owner helper), merging results. There
    # is one pool here and the heal pass treats every ref alike, so owner order
    # carries no invariant.
    def _build_claimed_query(owner_repos: list[str]) -> str:
        quals = [f"repo:{r}" for r in owner_repos] + [
            "is:issue", "is:open", f"label:{IN_PROGRESS_LABEL}"]
        return (
            "query {\n"
            f"  claimed: search(query: {json.dumps(' '.join(quals))}, type: ISSUE, first: 100) {{\n"
            f"    {_ISSUE_NODES}\n"
            "  }\n"
            "}"
        )

    nodes: list[dict] = []
    for data in _graphql_per_owner(repos, _build_claimed_query, gh_run=gh_run,
                                   event="claimed_query"):
        nodes += (data.get("claimed") or {}).get("nodes") or []

    out: list[str] = []
    for node in nodes:
        if not node:
            continue
        number = node.get("number")
        repo = (node.get("repository") or {}).get("nameWithOwner")
        if number is None or not repo:
            continue
        out.append(f"{repo}#{number}")
    return out


def heal_orphan_claims(refs: list[str], tracked: set[str], *,
                       gh_run: GhRun = gitrepos._run_gh,
                       limit: int = 20) -> list[tuple[str, str]]:
    """Heal claim labels no target state tracks. Returns ``(ref, action)`` pairs.

    Per untracked ref, the referencing PR decides (via
    :func:`find_referencing_pr`, same-repo only):

    - **merged** → the work landed but the close-out never ran (the handoff
      leak): close the issue with a linking comment and strip ``in-progress``
      → ``("closed")``.
    - **open/draft** → live work; leave the lock alone (a later pass heals it
      once the PR merges).
    - **none / closed-unmerged only** → a stale lock with nothing carrying it:
      strip ``in-progress`` so the issue re-enters the candidate pool
      → ``("reclaimed")``.

    ``limit`` caps cross-ref lookups per pass (each is a gh call — rate-limit
    hygiene; the hourly sweep catches the remainder next pass). Best-effort:
    an individual failure skips that ref and the pass continues.
    """
    healed: list[tuple[str, str]] = []
    looked_up = 0
    for ref in refs:
        if ref in tracked:
            continue
        if looked_up >= limit:
            break
        looked_up += 1
        try:
            info = find_referencing_pr(ref, gh_run=gh_run)
            if info is not None and info.state == "merged":
                pr_link = info.url or f"PR #{info.number}"
                # Same human gate as close_batch_issues (#784): never let the heal
                # path auto-close an action/ops issue or one satisfied only by a
                # docs-only diff — escalate to a human instead.
                repo, _ = _split_ref(ref)
                signals = _issue_signals(ref, gh_run=gh_run)
                open_children = (signals or {}).get("open_sub_issues") or 0
                if open_children:  # tracking epic — release, never close (#997)
                    log.info("backlog_github.heal_release_tracking ref=%s pr=%s open=%d",
                             ref, info.number, open_children)
                    _release_tracking_issue(ref, open_children, pr_link, gh_run=gh_run)
                    healed.append((ref, "released"))
                    continue
                docs_only = _is_docs_only(
                    _pr_changed_files(repo, info.number, gh_run=gh_run))
                reason = _human_gate_reason(signals, docs_only)
                if reason:
                    log.info("backlog_github.heal_escalate ref=%s pr=%s reason=%s",
                             ref, info.number, reason)
                    _escalate_issue(ref, reason, pr_link, gh_run=gh_run)
                    healed.append((ref, "escalated"))
                    continue
                comment = (f"Implemented by {pr_link} (merged) — closed by "
                           f"{brand.NAME} Backlog Runner (orphaned-claim heal).")
                if _close_issue(ref, comment, gh_run=gh_run):
                    _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run)
                    healed.append((ref, "closed"))
            elif info is None:
                if _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run):
                    healed.append((ref, "reclaimed"))
                else:
                    log.warning("backlog_github.heal_reclaim_failed ref=%s", ref)
            # open/draft → live work; leave untouched.
        except Exception:  # pragma: no cover — one ref must not stall the pass
            log.warning("backlog_github.heal_orphan_failed ref=%s", ref, exc_info=True)
    return healed


def reconcile_dangling(open_claim_refs: list[str], live_origin_refs, *,
                       gh_run: GhRun = gitrepos._run_gh) -> None:
    """Return orphaned claims to the pool.

    For each ``in-progress``-labeled issue in ``open_claim_refs`` whose owning
    ``origin_ref`` is NOT present in ``live_origin_refs`` (no live session
    carrying it), remove the ``in-progress`` label. Best-effort — an individual
    failure is logged and reconciliation continues.
    """
    live = set(live_origin_refs or ())
    for ref in open_claim_refs:
        if ref in live:
            continue
        try:
            if not _issue_edit(ref, remove=IN_PROGRESS_LABEL, gh_run=gh_run):
                log.warning("backlog_github.reconcile_failed ref=%s", ref)
        except Exception:  # pragma: no cover — best-effort
            log.warning("backlog_github.reconcile_error ref=%s", ref, exc_info=True)
