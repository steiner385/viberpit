"""GitHub I/O for the Backlog Runner — candidate discovery + claim lifecycle.

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
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from ccswitch import gitrepos

log = logging.getLogger(__name__)

GhRun = Callable[..., "object"]

IN_PROGRESS_LABEL = "in-progress"
NEEDS_HUMAN_LABEL = "needs-human"

# How long an escalated (``needs-human``) issue must sit untouched before the
# runner reconsiders it. Escalation itself bumps the issue's ``updatedAt``, so
# every give-up re-arms the cooldown — a permanently-failing issue is retried at
# most once per window, never in a tight loop. 0 disables retry entirely
# (needs-human becomes a permanent handoff again).
NEEDS_HUMAN_RETRY_DAYS_DEFAULT = 7


# --------------------------------------------------------------------------- #
# Ref helpers                                                                  #
# --------------------------------------------------------------------------- #

def _split_ref(ref: str) -> tuple[str, str]:
    """``"owner/repo#123"`` → ``("owner/repo", "123")``."""
    repo, _, number = ref.partition("#")
    return repo, number


# --------------------------------------------------------------------------- #
# Candidate query                                                             #
# --------------------------------------------------------------------------- #

# ``body`` feeds the grouping optimizer's relatedness analysis (titles alone
# were what produced over-bundling); it is excerpted before prompting.
_ISSUE_NODES = "nodes { ... on Issue { number title body url repository { nameWithOwner } } }"


def _common_quals(repos: list[str], eligible_label: str) -> list[str]:
    """Gates shared by BOTH candidate pools: repo scope, open, unassigned, the
    optional eligible label, and never ``in-progress`` (the claim lock — a
    claimed issue is live work, no pool may resurface it)."""
    quals = [f"repo:{r}" for r in repos]
    quals += ["is:issue", "is:open", "no:assignee"]
    if eligible_label:
        quals.append(f"label:{eligible_label}")
    quals.append(f"-label:{IN_PROGRESS_LABEL}")
    return quals


def _build_candidates_query(repos: list[str], eligible_label: str,
                            retry_cutoff: str | None) -> str:
    """A GitHub search-issues GraphQL doc for eligible, unclaimed OPEN issues.

    The search string carries every gate: the eligible label (OMITTED when
    ``eligible_label`` is blank — then *any* open, unassigned issue is a
    candidate), unassigned (``no:assignee``), and NOT already ``in-progress`` /
    ``needs-human``.  Repos are OR'd via ``repo:`` qualifiers.  Strings are
    JSON-encoded so a label or repo name can never break out of the search
    literal.

    When ``retry_cutoff`` (a ``YYYY-MM-DD`` date) is given, a second aliased
    search adds the RETRY pool: ``needs-human`` issues whose last update
    precedes the cutoff. Escalation bumps ``updatedAt``, so the qualifier doubles
    as the cooldown clock — and a human commenting on an escalated issue keeps it
    out of the pool for another window.
    """
    fresh = _common_quals(repos, eligible_label) + [f"-label:{NEEDS_HUMAN_LABEL}"]
    parts = [
        "query {\n"
        f"  issues: search(query: {json.dumps(' '.join(fresh))}, type: ISSUE, first: 100) {{\n"
        f"    {_ISSUE_NODES}\n"
        "  }\n"
    ]
    if retry_cutoff:
        retry = _common_quals(repos, eligible_label) + [
            f"label:{NEEDS_HUMAN_LABEL}",
            f"updated:<{retry_cutoff}",
        ]
        parts.append(
            f"  retry: search(query: {json.dumps(' '.join(retry))}, type: ISSUE, first: 100) {{\n"
            f"    {_ISSUE_NODES}\n"
            "  }\n"
        )
    parts.append("}")
    return "".join(parts)


def query_candidates(config: dict, *, gh_run: GhRun = gitrepos._run_gh,
                     now: datetime | None = None) -> list[dict]:
    """Return eligible, unclaimed OPEN issues as candidate dicts.

    Candidate shape (what the grouping optimizer and ``plan_to_batch`` consume)::

        {"ref": "owner/repo#N", "number": N, "repo": "owner/repo", "title": ...,
         "body": ..., "url": "https://github.com/owner/repo/issues/N"}

    Two pools, fresh first: never-escalated issues, then ``needs-human`` issues
    untouched for ``needs_human_retry_days`` (missing key → the schema default,
    so already-stored configs pick the retry pool up without a re-save; explicit
    0 opts out). Fresh-first ordering means a retry can never displace new work.

    For v1, ``enabled_repos`` must be non-empty — an empty list returns ``[]``
    without a gh call (drawing from *all* repos is intentionally deferred).  A gh
    failure or unparseable response returns ``[]`` (the sweep must not crash).
    """
    repos = list(config.get("enabled_repos") or [])
    if not repos:
        return []
    # A blank eligible_label means "no label gate" — consider ALL open,
    # unassigned issues. Do NOT coerce blank to a default here: the schema
    # default ("ready") is applied to a *fresh* config by validate_config, so an
    # explicit blank is the user opting out of the label gate.
    eligible_label = (config.get("eligible_label") or "").strip()
    raw_days = config.get("needs_human_retry_days")
    retry_days = NEEDS_HUMAN_RETRY_DAYS_DEFAULT if raw_days is None else max(0, int(raw_days))
    retry_cutoff = None
    if retry_days:
        moment = now or datetime.now(timezone.utc)
        retry_cutoff = (moment - timedelta(days=retry_days)).strftime("%Y-%m-%d")
    query = _build_candidates_query(repos, eligible_label, retry_cutoff)

    proc = gh_run(["api", "graphql", "-f", f"query={query}"])
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.query_failed err=%s",
                    (getattr(proc, "stderr", "") or "").strip())
        return []
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.query_unparseable", exc_info=True)
        return []

    data = payload.get("data") or {}
    nodes = list((data.get("issues") or {}).get("nodes") or [])
    nodes += list((data.get("retry") or {}).get("nodes") or [])
    out: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        if not node:
            continue
        number = node.get("number")
        repo = (node.get("repository") or {}).get("nameWithOwner")
        if number is None or not repo:
            continue
        ref = f"{repo}#{number}"
        if ref in seen:  # label edited between the two searches — keep first
            continue
        seen.add(ref)
        out.append({
            "ref": ref,
            "number": number,
            "repo": repo,
            "title": node.get("title"),
            # body feeds the grouping optimizer (excerpted before prompting).
            "body": node.get("body"),
            # url lets the ActionableItem link out to the issue; fall back to the
            # canonical /issues/N path if the API somehow omitted it (#467).
            "url": node.get("url") or f"https://github.com/{repo}/issues/{number}",
        })
    return out


# --------------------------------------------------------------------------- #
# Cross-reference PR lookup                                                    #
# --------------------------------------------------------------------------- #

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
    proc = gh_run(["api", "graphql", "-f", f"query={query}"])
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
    merged_pr = open_pr = None
    for node in nodes:
        src = (node or {}).get("source") or {}
        if ((src.get("repository") or {}).get("nameWithOwner")) != repo:
            continue
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
    proc = gh_run(args)
    return getattr(proc, "returncode", 1) == 0


def _issue_comment(ref: str, body: str, *, gh_run: GhRun) -> bool:
    repo, number = _split_ref(ref)
    proc = gh_run(["issue", "comment", number, "--repo", repo, "--body", body])
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
        for name in (IN_PROGRESS_LABEL, NEEDS_HUMAN_LABEL):
            try:
                gh_run(["label", "create", name, "--repo", repo, "--color", "ededed",
                        "--description", "ccswitch backlog-runner", "--force"])
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


def _close_issue(ref: str, comment: str, *, gh_run: GhRun) -> bool:
    """Close one issue with a linking comment. Best-effort; returns success."""
    repo, number = _split_ref(ref)
    try:
        proc = gh_run(["issue", "close", number, "--repo", repo,
                       "--comment", comment])
        if getattr(proc, "returncode", 1) != 0:
            log.warning("backlog_github.close_issue_failed ref=%s", ref)
            return False
        return True
    except Exception:  # pragma: no cover — best-effort
        log.warning("backlog_github.close_issue_error ref=%s", ref, exc_info=True)
        return False


def close_batch_issues(batch, info, *, gh_run: GhRun = gitrepos._run_gh) -> None:
    """Close every issue in ``batch`` after its PR merged, with a linking comment.

    The implement prompt asks for ``Fixes #N`` in the PR body, but sessions
    don't reliably comply — and an issue a merged PR fixed must not stay open
    wearing ``in-progress``/``needs-human`` (it would be excluded from the
    candidate pools forever). Best-effort: an individual failure is logged and
    the rest of the batch is still closed.
    """
    pr_link = getattr(info, "url", "") or f"PR #{getattr(info, 'number', '?')}"
    comment = (f"Implemented by {pr_link} (merged) — "
               f"closed by ccswitch Backlog Runner.")
    for ref in batch.issues:
        _close_issue(ref, comment, gh_run=gh_run)


# --------------------------------------------------------------------------- #
# Orphaned-claim healing (GitHub truth)                                        #
# --------------------------------------------------------------------------- #

def query_claimed_refs(repos: list[str], *, gh_run: GhRun = gitrepos._run_gh) -> list[str]:
    """Refs of OPEN issues wearing ``in-progress`` across *repos* — GitHub's
    view of the runner's outstanding claims.

    The runner owns the ``in-progress`` label, so any labeled issue no target
    state tracks is an orphaned lock (crash, DB loss, or the pre-handed-off
    handoff leak) silently excluded from the candidate pools. This is the
    ground-truth read the heal pass diffs against. Failures return ``[]``.
    """
    if not repos:
        return []
    quals = [f"repo:{r}" for r in repos] + [
        "is:issue", "is:open", f"label:{IN_PROGRESS_LABEL}"]
    query = (
        "query {\n"
        f"  claimed: search(query: {json.dumps(' '.join(quals))}, type: ISSUE, first: 100) {{\n"
        f"    {_ISSUE_NODES}\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"])
    if getattr(proc, "returncode", 1) != 0:
        log.warning("backlog_github.claimed_query_failed err=%s",
                    (getattr(proc, "stderr", "") or "").strip())
        return []
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except json.JSONDecodeError:
        log.warning("backlog_github.claimed_query_unparseable", exc_info=True)
        return []
    nodes = (((payload.get("data") or {}).get("claimed") or {}).get("nodes")) or []
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
                comment = (f"Implemented by {pr_link} (merged) — closed by "
                           f"ccswitch Backlog Runner (orphaned-claim heal).")
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
