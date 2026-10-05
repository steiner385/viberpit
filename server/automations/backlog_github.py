"""GitHub I/O for the Backlog Runner — pre-dispatch verification, claim
announcements, needs-human handoff notes and close-out.

Candidate DISCOVERY no longer lives here: the runner selects from the global
priority queue over the mirrored Projects v2 boards (spec 003 §7a,
``SessionOrchestrator._backlog_query_queue``). What remains is the GitHub side
of acting on a pick — re-verifying it against live GitHub immediately before the
claim (:func:`verify_ready_live`, fails closed), announcing the claim and the
needs-human handoff, and closing out on a merged PR.

Every GitHub call is routed through an injected ``gh_run`` seam (default
:func:`viberpit.gitrepos._run_gh`) so tests never hit the network.  The seam is
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

from viberpit import brand, gitrepos
from server.github.host_failures import log_gh_failure

from . import backlog_deps

log = logging.getLogger(__name__)

GhRun = Callable[..., "object"]

# Human-gate signals (#784, narrowed by #2681). Linkage (#642) proves a PR
# *references* an issue; the close-out still keeps an issue OPEN — and hands it to
# a human, naming what is left — only when something remains that no merge does:
#
#   1. Action/ops issues — real-world steps no diff performs (verify a domain in
#      Search Console, prune scopes on the GCP consent screen, record a demo
#      video, submit to Google for review, a deployed-acceptance check).
#      Detected by an ops label or a curated high-signal phrase in the title/body.
#      Bare ambiguous words ("submit", "console") are matched ONLY inside these
#      multi-word phrases — a bare keyword would escalate ordinary code issues
#      ("fix console.log").
#   2. Unchecked acceptance items — a ``- [ ]`` task-list line in the issue body
#      is work the issue itself says is still open.
#
# Anything else closes with a comment (#2681): a hold whose only remaining step is
# closing the issue costs a person a tap and parks nothing real. (The pre-#2681
# docs-only-diff gate held exactly those — every one of the five close-out holds
# in the 2026-10-05 review was it — so it is gone.)
ACTION_ISSUE_LABELS = frozenset({
    "action-required", "action", "ops", "operations", "manual", "manual-step"})
ACTION_ISSUE_PHRASES = (
    "search console", "consent screen", "consent-screen", "oauth consent",
    "verify domain", "verify the domain", "domain verification",
    "demo video", "record a demo", "record and upload", "record + upload",
    "gcp console", "google cloud console", "cloud console",
    "google search console", "app verification", "verification submission",
    "submit for verification", "submit for review", "submit to google",
    "publishing status", "deployed acceptance", "post-deploy verification",
)
#: An unchecked GitHub task-list item: ``- [ ] …`` / ``* [ ] …`` / ``1. [ ] …``.
_UNCHECKED_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]\s+(?P<text>\S.*)$", re.MULTILINE)
#: How much of the first unchecked item the hold reason quotes.
_ITEM_QUOTE_CHARS = 80

#: The close-out's "a human gate fired" outcome. Carries WHAT is left after a
#: colon (``escalated_outcome``) so the runner's hold says it rather than a
#: generic "needs a human close-out" (#2681); a bare ``"escalated"`` (a legacy
#: stub) still reads as one, with no detail.
ESCALATED = "escalated"


def escalated_outcome(reasons: Iterable[str]) -> str:
    """The ``"escalated: <what is left>"`` outcome — the writer half of
    :func:`escalation_detail`."""
    detail = "; ".join(r for r in reasons if r)
    return f"{ESCALATED}: {detail}" if detail else ESCALATED


def escalation_detail(outcome: str | None) -> str | None:
    """``None`` unless *outcome* is an escalation; else what is left (``""`` when
    the outcome names nothing) — the reader half of :func:`escalated_outcome`."""
    if outcome == ESCALATED:
        return ""
    prefix = f"{ESCALATED}: "
    if outcome and outcome.startswith(prefix):
        return outcome[len(prefix):].strip()
    return None


#: The runner's close-out hold reason before #2681 — generic, naming nothing.
#: Kept only so the post-tick pass can find and re-judge those holds.
LEGACY_CLOSEOUT_HOLD_REASON = "human gate: merged PR needs a human close-out"


def closeout_hold_reason(detail: str) -> str:
    """THE close-out hold reason: what is left after the merge (#2681), or the
    legacy generic sentence when the outcome named nothing."""
    return (f"human gate: merged PR left the issue open — {detail}" if detail
            else LEGACY_CLOSEOUT_HOLD_REASON)


def closed_with_nothing_left_comment() -> str:
    """The comment on a close-out-held issue closed once nothing is left for a person."""
    return (f"Its linked PR merged and nothing is left for a person (no unchecked "
            f"acceptance items, no manual step) — closed by {brand.NAME} Backlog Runner.")


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
            stderr = str(getattr(proc, "stderr", "") or "")
            log_gh_failure(log, stderr,
                           "backlog_github.verify_ready_failed owner='%s' err=%s",
                           owner, stderr.strip())
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
        "          closingIssuesReferences(first: 50) {\n"
        "            nodes { number repository { nameWithOwner } }\n"
        "          }\n"
        "        } } } }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    proc = gh_run(["api", "graphql", "-f", f"query={query}"],
                  token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        stderr = str(getattr(proc, "stderr", "") or "")
        log_gh_failure(log, stderr,
                       "backlog_github.find_referencing_pr_failed ref='%s' err=%s",
                       ref, stderr.strip())
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


def _pr_closes_ref(src: dict, repo: str, number: int) -> bool:
    """True when *src* (a cross-referencing PR node) names ``repo#number`` in
    its own ``closingIssuesReferences`` — i.e. it is linked, not a mere mention."""
    nodes = ((src.get("closingIssuesReferences") or {}).get("nodes")) or []
    return any(
        node and node.get("number") == number
        and ((node.get("repository") or {}).get("nameWithOwner")) == repo
        for node in nodes
    )


def find_referencing_pr(ref: str, *, gh_run: GhRun = gitrepos._run_gh):
    """The best same-repo PR cross-referencing issue *ref*, or ``None``.

    The runner's sessions don't reliably record their branch (shared-clone
    reflog discovery misses worktree-created branches) and don't reliably put
    ``Fixes #N`` in the PR body — so when the cached-branch lookup misses, this
    asks GitHub directly which PRs mention the issue (a PR titled ``… (#N)``
    creates the cross-reference even without a closing keyword).

    Preference: a PR whose own ``closingIssuesReferences`` names this issue
    (linked) beats one that merely mentions it in passing — a drive-by mention
    from an unrelated merged PR (e.g. a batch docs PR) must never outrank the
    open-but-failing PR that actually implements the fix (#2201). Within a
    tier, MERGED beats OPEN (the work landed); a later reference beats an
    earlier one. CLOSED-unmerged and cross-repo references are ignored — a
    drive-by mention from a closed PR must never read as "PR closed unmerged →
    give up". Any gh failure or unparseable payload returns ``None``.
    """
    from server.github.pr_status import PullRequestInfo

    repo, number_s = _split_ref(ref)
    srcs = _issue_crossref_pr_nodes(ref, gh_run=gh_run)
    if not srcs:
        return None

    def _best(candidates: list[dict]):
        merged_pr = open_pr = None
        for src in candidates:
            state = src.get("state")
            if state == "MERGED":
                merged_pr = src        # last reference wins within a priority tier
            elif state == "OPEN":
                open_pr = src
        return merged_pr or open_pr

    if number_s.isdigit():
        number = int(number_s)
        linked = [s for s in srcs if _pr_closes_ref(s, repo, number)]
        mentioned = [s for s in srcs if not _pr_closes_ref(s, repo, number)]
    else:
        linked, mentioned = [], srcs
    best = _best(linked) or _best(mentioned)
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


def issue_state_live(ref: str, *, gh_run: GhRun = gitrepos._run_gh) -> str | None:
    """``"open"`` / ``"closed"`` for issue *ref* on live GitHub, or ``None`` when
    that is UNKNOWN (malformed ref, gh failure, unparseable or unexpected payload).

    How an ended INVESTIGATION is settled (spec 2026-09-18 §5): its four exits are
    all expressed on the ISSUE (closed as noise, closed as a duplicate, converted,
    escalated), never on a PR — an investigation opens none. The board mirror lags
    GitHub by a sync interval and the exit happened minutes ago, so this asks
    GitHub, exactly as :func:`verify_ready_live` does before a claim.

    Unknown is a real answer here and never collapses to "open": the caller retries
    on the next tick rather than handing an issue to a human because gh was down.
    """
    repo, number = _split_ref(ref)
    if not repo or not number.isdigit():
        log.warning("backlog_github.issue_state_malformed_ref ref=%s", ref)
        return None
    proc = gh_run(["issue", "view", number, "--repo", repo, "--json", "state"],
                  token=gitrepos.app_token_for_repo(repo))
    if getattr(proc, "returncode", 1) != 0:
        stderr = str(getattr(proc, "stderr", "") or "")
        log_gh_failure(log, stderr,
                       "backlog_github.issue_state_failed ref='%s' err=%s",
                       ref, stderr.strip())
        return None
    try:
        state = json.loads(getattr(proc, "stdout", "") or "{}").get("state")
    except (json.JSONDecodeError, AttributeError):
        log.warning("backlog_github.issue_state_unparseable ref=%s", ref, exc_info=True)
        return None
    if not isinstance(state, str) or state.lower() not in ("open", "closed"):
        log.warning("backlog_github.issue_state_unexpected ref=%s state=%r", ref, state)
        return None
    return state.lower()


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
    the owner_action hold the runner writes on the issue (spec 2026-09-18 §3);
    this only says so on the issue. Best-effort, exactly like :func:`announce_claim`."""
    # "Held" is the board's Status option since the rename (spec 2026-09-18 §9). A board
    # the one-time rename has not reached yet still shows the old column name; the
    # reconciler writes whichever spelling that board has, so the card is parked either way.
    body = (f"Handed to a human by {brand.NAME} Backlog Runner: {reason}. "
            "Board Status is Held until a person releases it: move the card to "
            f"Ready or Backlog, edit the issue body, or tap Retry in {brand.NAME}.")
    for ref in batch.issues:
        try:
            posted = _issue_comment(ref, body, gh_run=gh_run)
        except Exception:
            log.warning("backlog_github.announce_needs_human_error ref=%s", ref,
                        exc_info=True)
            continue
        if not posted:
            log.warning("backlog_github.announce_needs_human_failed ref=%s", ref)


def announce_decision(ref: str, *, label: str, detail: str | None, chosen_by: str,
                      gh_run: GhRun = gitrepos._run_gh) -> None:
    """Human-visible trace of an accepted decision proposal (spec 2026-09-18 §6):
    one comment on the issue naming the option a person picked and who picked it.

    The DECISION itself is the accepted ``decision_proposals`` row — that row is
    what clears the hold and what the next implement run quotes verbatim; this
    only says so where a reader on GitHub will see it. Best-effort and never
    raises, exactly like :func:`announce_claim` and :func:`announce_needs_human`:
    a comment that does not land must not fail the accept that already committed.

    Takes ONE ref rather than a batch because a decision belongs to a single
    issue — the batch announcements above speak for a runner bundle.
    """
    body = (f"Decision accepted: {label} — {detail or label}. "
            f"Chosen by {chosen_by}.")
    try:
        posted = _issue_comment(ref, body, gh_run=gh_run)
    except Exception:
        log.warning("backlog_github.announce_decision_error ref=%s", ref, exc_info=True)
        return
    if not posted:
        log.warning("backlog_github.announce_decision_failed ref=%s", ref)


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
        stderr = str(getattr(proc, "stderr", "") or "")
        log_gh_failure(log, stderr,
                       "backlog_github.pr_linkage_failed repo='%s' pr=%s err=%s",
                       repo, pr_number, stderr.strip())
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
# Human gate: action issues & unchecked acceptance items (#784, #2681)          #
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
        stderr = str(getattr(proc, "stderr", "") or "")
        log_gh_failure(log, stderr,
                       "backlog_github.issue_signals_failed ref='%s' err=%s",
                       ref, stderr.strip())
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


def _action_gate(signals: dict) -> str | None:
    """What real-world action keeps the issue open (#784), named — the matched ops
    label or curated phrase — or ``None``."""
    labels = sorted({lbl.strip().lower() for lbl in (signals.get("labels") or ())}
                    & ACTION_ISSUE_LABELS)
    if labels:
        return f"it is labelled {labels[0]!r}: a manual/operational step a merge cannot perform"
    haystack = f"{signals.get('title') or ''}\n{signals.get('body') or ''}".lower()
    phrase = next((p for p in ACTION_ISSUE_PHRASES if p in haystack), None)
    if phrase:
        return f"it calls for {phrase!r}: a manual/operational step a merge cannot perform"
    return None


def unchecked_items(body: str | None) -> list[str]:
    """The text of every unchecked task-list item in *body*, in order."""
    return [m.group("text").strip() for m in _UNCHECKED_ITEM_RE.finditer(body or "")]


def _human_gate_reason(signals: dict | None) -> str | None:
    """What still needs a person after a verified-linked PR merged, or ``None`` to
    close the issue (#2681). Fails open on unknown signals: linkage already gated
    the close, and holding on a failed lookup is a hold with nothing to say."""
    if signals is None:
        return None
    action = _action_gate(signals)
    if action:
        return action
    items = unchecked_items(signals.get("body"))
    if items:
        first = items[0]
        if len(first) > _ITEM_QUOTE_CHARS:
            first = first[:_ITEM_QUOTE_CHARS - 1].rstrip() + "…"
        return (f"{len(items)} unchecked acceptance item(s) remain on the issue "
                f"(first: {first!r})")
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
        f"Runner — please finish what is left and close manually.",
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

    - ``"closed"`` — every intended issue close succeeded. Explicitly excluded
      tracking and verified-unlinked issues remain open by policy; their comments
      are best-effort and are not completion authority.
    - ``"escalated: <what is left>"`` (:func:`escalated_outcome`) — all intended
      closes succeeded, but at least one issue still needs the explicit human
      gate. Keep the durable human handoff even when other members of this batch
      closed successfully; the detail becomes the hold's reason (#2681).
    - ``"unlinked"`` — every issue is verified-unlinked: a misattributed PR.
      Nothing is closed; the caller must ignore/denylist the PR.
    - ``"retry"`` — at least one linkage or intended close outcome is unknown
      or failed. Keep ownership; the sweep reconciles terminal facts read-only
      rather than assuming completion or replaying uncertain external effects.
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
    # one whose own acceptance list is unfinished (#784, #2681): gate each linked
    # issue on its own signals, and keep what is left for the runner's hold.
    gates: list[str] = []
    incomplete = any(verdict is None for verdict in verdicts.values())
    for ref in linked:
        signals = _issue_signals(ref, gh_run=gh_run)
        open_children = (signals or {}).get("open_sub_issues") or 0
        if open_children:  # tracking epic — never closeable by one PR (#997)
            log.info("backlog_github.close_batch_release_tracking ref=%s pr=%s open=%d",
                     ref, pr_number, open_children)
            _release_tracking_issue(ref, open_children, pr_link, gh_run=gh_run)
            continue
        reason = _human_gate_reason(signals)
        if reason:
            log.info("backlog_github.close_batch_escalate ref=%s pr=%s reason=%s",
                     ref, pr_number, reason)
            _escalate_issue(ref, reason, pr_link, gh_run=gh_run)
            gates.append(f"{ref}: {reason}" if len(batch.issues) > 1 else reason)
        elif not _close_issue(ref, comment, gh_run=gh_run):
            incomplete = True
    # The PR is the batch's, but it never referenced these issues — closing
    # them would be exactly the #642 bug. Leave them open and say so; the
    # runner's row is what returns them to the pool.
    for ref in unlinked:
        _issue_comment(
            ref,
            f"Batch PR {pr_link} merged without referencing this issue — "
            f"returned to the backlog pool (left open) by {brand.NAME} Backlog Runner.",
            gh_run=gh_run)
    # A successful sibling never proves a failed or unobserved write completed.
    # Keep uncertain ownership ahead of human/terminal reporting; reconciliation
    # can later establish that every intended target reached a terminal result.
    if incomplete:
        return "retry"
    return escalated_outcome(gates) if gates else "closed"
