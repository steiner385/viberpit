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
from typing import Callable, Iterable

from ccswitch import gitrepos

log = logging.getLogger(__name__)

GhRun = Callable[..., "object"]

IN_PROGRESS_LABEL = "in-progress"
NEEDS_HUMAN_LABEL = "needs-human"


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

def _build_candidates_query(repos: list[str], eligible_label: str) -> str:
    """A GitHub search-issues GraphQL doc for eligible, unclaimed OPEN issues.

    The search string carries every gate: the eligible label (OMITTED when
    ``eligible_label`` is blank — then *any* open, unassigned issue is a
    candidate), unassigned (``no:assignee``), and NOT already ``in-progress`` /
    ``needs-human``.  Repos are OR'd via ``repo:`` qualifiers.  Strings are
    JSON-encoded so a label or repo name can never break out of the search
    literal.
    """
    quals = [f"repo:{r}" for r in repos]
    quals += ["is:issue", "is:open", "no:assignee"]
    if eligible_label:
        quals.append(f"label:{eligible_label}")
    quals += [
        f"-label:{IN_PROGRESS_LABEL}",
        f"-label:{NEEDS_HUMAN_LABEL}",
    ]
    search = " ".join(quals)
    return (
        "query {\n"
        f"  issues: search(query: {json.dumps(search)}, type: ISSUE, first: 100) {{\n"
        "    nodes { ... on Issue { number title url repository { nameWithOwner } } }\n"
        "  }\n"
        "}"
    )


def query_candidates(config: dict, *, gh_run: GhRun = gitrepos._run_gh) -> list[dict]:
    """Return eligible, unclaimed OPEN issues as candidate dicts.

    Candidate shape (what :func:`rank_and_batch` consumes)::

        {"ref": "owner/repo#N", "number": N, "repo": "owner/repo", "title": ...,
         "url": "https://github.com/owner/repo/issues/N"}

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
    query = _build_candidates_query(repos, eligible_label)

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

    nodes = (
        (payload.get("data") or {})
        .get("issues", {})
        .get("nodes", [])
    ) or []
    out: list[dict] = []
    for node in nodes:
        if not node:
            continue
        number = node.get("number")
        repo = (node.get("repository") or {}).get("nameWithOwner")
        if number is None or not repo:
            continue
        out.append({
            "ref": f"{repo}#{number}",
            "number": number,
            "repo": repo,
            "title": node.get("title"),
            # url lets the ActionableItem link out to the issue; fall back to the
            # canonical /issues/N path if the API somehow omitted it (#467).
            "url": node.get("url") or f"https://github.com/{repo}/issues/{number}",
        })
    return out


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
