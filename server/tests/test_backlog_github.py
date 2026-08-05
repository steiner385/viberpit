"""GitHub I/O for the Backlog Runner — candidate query + atomic claim/unclaim/reconcile.

Every gh call is routed through the injected ``gh_run`` seam, so these tests use
``FakeGh`` and never touch the network.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from server.automations.backlog_github import (
    claim_batch,
    label_needs_human,
    query_candidates,
    query_claimed_refs,
    heal_orphan_claims,
    reconcile_dangling,
    unclaim_batch,
)
from server.automations.backlog_selection import Batch
from server.tests.fakes.fake_backlog import FakeGh


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# query_candidates                                                             #
# --------------------------------------------------------------------------- #

def _graphql_stdout(nodes: list[dict]) -> str:
    return json.dumps({"data": {"issues": {"nodes": nodes}}})


def test_query_candidates_builds_filtered_query_and_parses():
    nodes = [
        {"number": 7, "title": "Fix login", "url": "https://github.com/o/r/issues/7",
         "repository": {"nameWithOwner": "o/r"}},
        {"number": 12, "title": "Add search", "url": "https://github.com/o/r/issues/12",
         "repository": {"nameWithOwner": "o/r"}},
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})
    config = {"enabled_repos": ["o/r"], "eligible_label": "ready"}

    out = query_candidates(config, gh_run=fake)

    # The graphql read happened.
    assert fake.calls, "expected a gh api graphql call"
    argv = fake.calls[0]
    assert argv[:2] == ["api", "graphql"]
    query = next(a for a in argv if a.startswith("query="))
    # The selection fetches url so the candidate can link out (#467).
    assert "url" in query
    # Carries the eligible label and excludes already-claimed / needs-human issues.
    assert "label:ready" in query or 'label:"ready"' in query
    assert "-label:in-progress" in query or '-label:"in-progress"' in query
    assert "-label:needs-human" in query or '-label:"needs-human"' in query
    # Scoped to the enabled repo, open, unassigned issues.
    assert "repo:o/r" in query
    assert "is:open" in query
    assert "no:assignee" in query

    # Parsed into the candidate shape the grouping optimizer consumes.
    assert out == [
        {"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix login",
         "body": None, "url": "https://github.com/o/r/issues/7"},
        {"ref": "o/r#12", "number": 12, "repo": "o/r", "title": "Add search",
         "body": None, "url": "https://github.com/o/r/issues/12"},
    ]


def test_query_candidates_synthesizes_url_when_api_omits_it():
    """If the GraphQL node lacks a url, fall back to the canonical issue path so
    the ActionableItem still links out (#467)."""
    nodes = [{"number": 9, "title": "No url", "repository": {"nameWithOwner": "o/r"}}]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})
    out = query_candidates({"enabled_repos": ["o/r"], "eligible_label": "ready"}, gh_run=fake)
    assert out == [{"ref": "o/r#9", "number": 9, "repo": "o/r", "title": "No url",
                    "body": None, "url": "https://github.com/o/r/issues/9"}]


def test_query_candidates_empty_enabled_repos_returns_empty_no_call():
    fake = FakeGh()
    out = query_candidates({"enabled_repos": [], "eligible_label": "ready"}, gh_run=fake)
    assert out == []
    assert fake.calls == []


def test_query_candidates_gh_failure_returns_empty():
    fake = FakeGh({("api", "graphql"): _proc(returncode=1, stderr="boom")})
    out = query_candidates({"enabled_repos": ["o/r"], "eligible_label": "ready"}, gh_run=fake)
    assert out == []


def test_query_candidates_blank_label_drops_the_label_gate():
    """A blank eligible_label means "no label gate" — any open, unassigned issue
    is a candidate. The query must NOT carry a `label:` qualifier (and must not
    silently fall back to `ready`), while the other gates stay."""
    nodes = [{"number": 7, "title": "Fix login", "url": "https://github.com/o/r/issues/7",
              "repository": {"nameWithOwner": "o/r"}}]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})

    out = query_candidates(
        {"enabled_repos": ["o/r"], "eligible_label": "", "needs_human_retry_days": 0},
        gh_run=fake)

    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "label:ready" not in query          # no silent default
    assert "label:\\\"ready\\\"" not in query
    # No positive label gate at all (the only label quals are the exclusions).
    assert "label:in-progress" in query and "label:needs-human" in query
    assert " label:" not in query.replace("-label:", "")  # no bare `label:<x>` gate
    # Other gates still present; the issue is still parsed.
    assert "no:assignee" in query and "is:open" in query
    assert out == [{"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix login",
                    "body": None, "url": "https://github.com/o/r/issues/7"}]


# --------------------------------------------------------------------------- #
# needs-human retry pool                                                       #
# --------------------------------------------------------------------------- #

def _graphql_stdout_with_retry(fresh: list[dict], retry: list[dict]) -> str:
    return json.dumps({"data": {"issues": {"nodes": fresh}, "retry": {"nodes": retry}}})


_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_query_candidates_retry_pool_includes_cooled_needs_human_issues():
    """With ``needs_human_retry_days`` > 0 the query carries a SECOND search for
    ``needs-human`` issues untouched since the cutoff (escalation bumps
    ``updatedAt``, so every give-up re-arms the cooldown). Retry candidates are
    appended AFTER fresh ones — fresh work always outranks a retry."""
    fresh = [{"number": 7, "title": "Fresh", "url": "https://github.com/o/r/issues/7",
              "repository": {"nameWithOwner": "o/r"}}]
    retry = [{"number": 3, "title": "Cooled", "url": "https://github.com/o/r/issues/3",
              "repository": {"nameWithOwner": "o/r"}}]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout_with_retry(fresh, retry))})
    config = {"enabled_repos": ["o/r"], "eligible_label": "", "needs_human_retry_days": 7}

    out = query_candidates(config, gh_run=fake, now=_NOW)

    query = next(a for a in fake.calls[0] if a.startswith("query="))
    # The retry search: needs-human ON, still unclaimed/unassigned, cooled off.
    assert "retry:" in query
    assert "label:needs-human" in query.replace("-label:needs-human", "")
    assert "updated:<2026-07-02" in query  # now (2026-07-09) minus 7 days
    # Both pools parsed; fresh first.
    assert [c["ref"] for c in out] == ["o/r#7", "o/r#3"]


def test_query_candidates_retry_search_keeps_claim_and_assignee_gates():
    """The retry search must still exclude ``in-progress`` (the claim lock) and
    assigned issues, and honor the eligible-label gate when one is set."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout_with_retry([], []))})
    config = {"enabled_repos": ["o/r"], "eligible_label": "ready",
              "needs_human_retry_days": 7}

    query_candidates(config, gh_run=fake, now=_NOW)

    query = next(a for a in fake.calls[0] if a.startswith("query="))
    retry_part = query[query.index("retry:"):]
    assert "-label:in-progress" in retry_part
    assert "no:assignee" in retry_part
    assert "label:ready" in retry_part


def test_query_candidates_retry_days_zero_disables_retry_pool():
    """``needs_human_retry_days: 0`` is the opt-out: needs-human stays a
    permanent handoff and the query carries no retry search."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout([]))})
    config = {"enabled_repos": ["o/r"], "eligible_label": "", "needs_human_retry_days": 0}

    query_candidates(config, gh_run=fake, now=_NOW)

    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "retry:" not in query
    assert "updated:<" not in query


def test_query_candidates_retry_defaults_on_when_key_missing():
    """A pre-existing stored config has no ``needs_human_retry_days`` key — the
    schema default (7 days) must apply so deployed runners pick up the retry
    pool without a config re-save."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout_with_retry([], []))})
    config = {"enabled_repos": ["o/r"], "eligible_label": ""}

    query_candidates(config, gh_run=fake, now=_NOW)

    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "retry:" in query
    assert "updated:<2026-07-02" in query


def test_query_candidates_dedupes_ref_across_pools():
    """A ref surfacing in both searches (label edited mid-query) must appear once."""
    dup = {"number": 3, "title": "Dup", "url": "https://github.com/o/r/issues/3",
           "repository": {"nameWithOwner": "o/r"}}
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout_with_retry([dup], [dup]))})
    config = {"enabled_repos": ["o/r"], "eligible_label": "", "needs_human_retry_days": 7}

    out = query_candidates(config, gh_run=fake, now=_NOW)

    assert [c["ref"] for c in out] == ["o/r#3"]


# --------------------------------------------------------------------------- #
# bot / meta-issue filtering (#637)                                            #
# --------------------------------------------------------------------------- #

def test_query_candidates_query_fetches_author_and_labels():
    """The candidate query must request ``author { login }`` and issue labels so
    the parse loop can filter out unimplementable bot/meta issues (#637)."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout([]))})
    query_candidates({"enabled_repos": ["o/r"], "eligible_label": "ready"}, gh_run=fake)
    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "author" in query and "login" in query
    assert "labels" in query


def test_query_candidates_skips_bot_authored_issues():
    """Issues opened by a bot/app account (login ends in ``[bot]``) are automated
    meta-tickets a Claude session cannot implement — Renovate's Dependency
    Dashboard is the motivating case (#637). Filter them out."""
    nodes = [
        {"number": 7, "title": "Fix login", "url": "https://github.com/o/r/issues/7",
         "author": {"login": "alice"}, "repository": {"nameWithOwner": "o/r"}},
        {"number": 8, "title": "Dependency Dashboard",
         "url": "https://github.com/o/r/issues/8",
         "author": {"login": "renovate[bot]"}, "repository": {"nameWithOwner": "o/r"}},
        {"number": 9, "title": "Bump deps", "url": "https://github.com/o/r/issues/9",
         "author": {"login": "dependabot[bot]"}, "repository": {"nameWithOwner": "o/r"}},
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})
    out = query_candidates({"enabled_repos": ["o/r"], "eligible_label": "ready"}, gh_run=fake)
    assert [c["ref"] for c in out] == ["o/r#7"]


def test_query_candidates_skips_dependency_labels_and_dashboard_title():
    """Defense in depth: even a human-authored issue is skipped when it carries a
    dependency-bot label or the canonical ``Dependency Dashboard`` title (#637)."""
    nodes = [
        {"number": 1, "title": "Real feature", "url": "https://github.com/o/r/issues/1",
         "author": {"login": "alice"}, "labels": {"nodes": [{"name": "enhancement"}]},
         "repository": {"nameWithOwner": "o/r"}},
        {"number": 2, "title": "Update lockfile", "url": "https://github.com/o/r/issues/2",
         "author": {"login": "alice"}, "labels": {"nodes": [{"name": "dependencies"}]},
         "repository": {"nameWithOwner": "o/r"}},
        {"number": 3, "title": "Renovate config", "url": "https://github.com/o/r/issues/3",
         "author": {"login": "alice"}, "labels": {"nodes": [{"name": "Renovate"}]},
         "repository": {"nameWithOwner": "o/r"}},
        {"number": 4, "title": "  Dependency Dashboard  ",
         "url": "https://github.com/o/r/issues/4",
         "author": {"login": "alice"}, "repository": {"nameWithOwner": "o/r"}},
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})
    out = query_candidates({"enabled_repos": ["o/r"], "eligible_label": ""}, gh_run=fake)
    assert [c["ref"] for c in out] == ["o/r#1"]


def test_query_candidates_keeps_human_issues_without_author_field():
    """A node missing the author field (older cache / partial payload) must NOT be
    dropped — absence of a bot signal means keep it."""
    nodes = [{"number": 5, "title": "Fix bug", "url": "https://github.com/o/r/issues/5",
              "repository": {"nameWithOwner": "o/r"}}]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})
    out = query_candidates({"enabled_repos": ["o/r"], "eligible_label": "ready"}, gh_run=fake)
    assert [c["ref"] for c in out] == ["o/r#5"]


# --------------------------------------------------------------------------- #
# claim_batch                                                                  #
# --------------------------------------------------------------------------- #

def test_claim_batch_labels_every_issue_returns_true():
    fake = FakeGh()
    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2", "o/r#3"], repo="o/r")

    ok = claim_batch(batch, gh_run=fake)

    assert ok is True
    edits = [c for c in fake.calls if c[:2] == ["issue", "edit"]]
    labeled = {(c[2], c[c.index("--repo") + 1]) for c in edits}
    assert labeled == {("1", "o/r"), ("2", "o/r"), ("3", "o/r")}
    for c in edits:
        assert "--add-label" in c
        assert c[c.index("--add-label") + 1] == "in-progress"


def test_claim_batch_provisions_claim_labels_before_adding():
    """The runner self-provisions ``in-progress``/``needs-human`` (idempotent,
    ``--force``) BEFORE the add-label edits — otherwise a repo that never had the
    label rejects ``--add-label`` and no issue can ever be claimed (the silent
    "runs but never spawns" failure). Deduped per repo."""
    fake = FakeGh()
    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")

    ok = claim_batch(batch, gh_run=fake)

    assert ok is True
    creates = [c for c in fake.calls if c[:2] == ["label", "create"]]
    made = {(c[2], c[c.index("--repo") + 1]) for c in creates}
    assert made == {("in-progress", "o/r"), ("needs-human", "o/r")}  # deduped per repo
    for c in creates:
        assert "--force" in c  # idempotent: create-or-update
    # All provisioning precedes the first add-label edit.
    first_edit = next(i for i, c in enumerate(fake.calls) if c[:2] == ["issue", "edit"])
    last_create = max(i for i, c in enumerate(fake.calls) if c[:2] == ["label", "create"])
    assert last_create < first_edit


def test_claim_batch_with_comment_posts_comment_per_issue():
    fake = FakeGh()
    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")

    ok = claim_batch(batch, gh_run=fake, comment="working on it")

    assert ok is True
    comments = [c for c in fake.calls if c[:2] == ["issue", "comment"]]
    numbers = {c[2] for c in comments}
    assert numbers == {"1", "2"}
    for c in comments:
        assert "--body" in c
        assert c[c.index("--body") + 1] == "working on it"


def test_claim_batch_rolls_back_on_partial_failure():
    # Fail on issue #2's edit; #1 already labeled must be rolled back.
    def gh_run(args, token=None):
        gh_run.calls.append(list(args))
        if args[:2] == ["issue", "edit"] and args[2] == "2" and "--add-label" in args:
            return _proc(returncode=1, stderr="rate limited")
        return _proc()
    gh_run.calls = []

    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2", "o/r#3"], repo="o/r")
    ok = claim_batch(batch, gh_run=gh_run)

    assert ok is False
    # Issue 1 was added then rolled back (remove-label in-progress).
    removes = [
        c for c in gh_run.calls
        if c[:2] == ["issue", "edit"] and "--remove-label" in c
        and c[c.index("--remove-label") + 1] == "in-progress"
    ]
    rolled_back = {c[2] for c in removes}
    assert "1" in rolled_back
    # Issue 3 was never claimed (failure aborted before it), so nothing to undo for it.
    assert "3" not in rolled_back


# --------------------------------------------------------------------------- #
# unclaim_batch / label_needs_human                                           #
# --------------------------------------------------------------------------- #

def test_unclaim_batch_removes_label_every_issue():
    fake = FakeGh()
    batch = Batch(primary="o/r#5", issues=["o/r#5", "o/r#6"], repo="o/r")

    unclaim_batch(batch, gh_run=fake)

    removes = [c for c in fake.calls if c[:2] == ["issue", "edit"] and "--remove-label" in c]
    numbers = {c[2] for c in removes}
    assert numbers == {"5", "6"}
    for c in removes:
        assert c[c.index("--remove-label") + 1] == "in-progress"


def test_unclaim_batch_continues_on_individual_failure():
    def gh_run(args, token=None):
        gh_run.calls.append(list(args))
        if args[2] == "5":
            return _proc(returncode=1, stderr="nope")
        return _proc()
    gh_run.calls = []

    batch = Batch(primary="o/r#5", issues=["o/r#5", "o/r#6"], repo="o/r")
    unclaim_batch(batch, gh_run=gh_run)

    # Both issues were attempted despite #5 failing.
    numbers = {c[2] for c in gh_run.calls}
    assert numbers == {"5", "6"}


def test_label_needs_human_adds_needs_human_removes_in_progress():
    fake = FakeGh()
    batch = Batch(primary="o/r#8", issues=["o/r#8", "o/r#9"], repo="o/r")

    label_needs_human(batch, gh_run=fake)

    edits = [c for c in fake.calls if c[:2] == ["issue", "edit"]]
    by_issue: dict[str, list[str]] = {}
    for c in edits:
        by_issue.setdefault(c[2], []).extend(c)
    assert set(by_issue) == {"8", "9"}
    for _num, flat in by_issue.items():
        assert "needs-human" in flat
        assert "--add-label" in flat
        assert "--remove-label" in flat
        assert "in-progress" in flat


# --------------------------------------------------------------------------- #
# reconcile_dangling                                                          #
# --------------------------------------------------------------------------- #

def test_reconcile_dangling_removes_label_for_claim_with_no_live_session():
    fake = FakeGh()
    open_claim_refs = ["o/r#1", "o/r#2"]
    # #1 has a live origin_ref; #2 does not.
    live_origin_refs = {"o/r#1"}

    reconcile_dangling(open_claim_refs, live_origin_refs, gh_run=fake)

    removes = [
        c for c in fake.calls
        if c[:2] == ["issue", "edit"] and "--remove-label" in c
        and c[c.index("--remove-label") + 1] == "in-progress"
    ]
    numbers = {c[2] for c in removes}
    assert numbers == {"2"}, "only the dangling claim (#2) should be released"


def test_reconcile_dangling_no_op_when_all_live():
    fake = FakeGh()
    reconcile_dangling(["o/r#1"], {"o/r#1"}, gh_run=fake)
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# find_referencing_pr                                                          #
# --------------------------------------------------------------------------- #

def _timeline_stdout(sources: list[dict]) -> str:
    return json.dumps({"data": {"repository": {"issue": {"timelineItems": {
        "nodes": [{"source": s} for s in sources]}}}}})


def _pr_node(number: int, state: str, repo: str = "o/r", *, is_draft: bool = False,
             auto_merge: bool = False, branch: str = "b") -> dict:
    return {"number": number, "url": f"https://github.com/{repo}/pull/{number}",
            "title": f"pr {number}", "state": state, "isDraft": is_draft,
            "headRefName": branch,
            "autoMergeRequest": {"enabledAt": "x"} if auto_merge else None,
            "repository": {"nameWithOwner": repo}}


def test_find_referencing_pr_prefers_merged_and_filters_repo():
    from server.automations.backlog_github import find_referencing_pr
    sources = [
        _pr_node(1, "MERGED", repo="other/repo"),   # wrong repo — ignored
        _pr_node(2, "CLOSED"),                       # closed unmerged — ignored
        _pr_node(3, "OPEN"),
        _pr_node(4, "MERGED", branch="fix/it"),
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_timeline_stdout(sources))})

    info = find_referencing_pr("o/r#11467", gh_run=fake)

    assert info is not None
    assert (info.number, info.state, info.merged) == (4, "merged", True)
    assert info.head_ref == "fix/it"
    # The query targets the issue's cross-referenced timeline.
    flat = " ".join(fake.calls[0])
    assert "CROSS_REFERENCED_EVENT" in flat
    assert "11467" in flat


def test_find_referencing_pr_open_maps_draft_and_auto_merge():
    from server.automations.backlog_github import find_referencing_pr
    fake = FakeGh({("api", "graphql"): _proc(
        stdout=_timeline_stdout([_pr_node(9, "OPEN", auto_merge=True)]))})

    info = find_referencing_pr("o/r#3", gh_run=fake)

    assert info is not None
    assert (info.number, info.state) == (9, "open")
    assert info.auto_merge_enabled is True


def test_find_referencing_pr_ignores_closed_only_and_failures():
    from server.automations.backlog_github import find_referencing_pr
    only_closed = FakeGh({("api", "graphql"): _proc(
        stdout=_timeline_stdout([_pr_node(2, "CLOSED")]))})
    assert find_referencing_pr("o/r#3", gh_run=only_closed) is None

    gh_fail = FakeGh({("api", "graphql"): _proc(returncode=1, stderr="boom")})
    assert find_referencing_pr("o/r#3", gh_run=gh_fail) is None

    unparseable = FakeGh({("api", "graphql"): _proc(stdout="not json")})
    assert find_referencing_pr("o/r#3", gh_run=unparseable) is None


# --------------------------------------------------------------------------- #
# close_batch_issues — fail-closed linkage verification (#642)                #
# --------------------------------------------------------------------------- #

def _linkage_stdout(closing=(), body="", messages=(), repo="o/r") -> str:
    """A PR-linkage GraphQL response: closingIssuesReferences + body + commits."""
    return json.dumps({"data": {"repository": {"pullRequest": {
        "body": body,
        "closingIssuesReferences": {"nodes": [
            {"number": n, "repository": {"nameWithOwner": repo}} for n in closing]},
        "commits": {"nodes": [{"commit": {"message": m}} for m in messages]},
    }}}})


class RoutedGh:
    """Routes graphql calls by query shape: the PR-side linkage query names
    ``pullRequest(``, the per-issue timeline query names ``issue(number: N)``.
    Non-graphql calls succeed (label edits, closes, comments)."""

    def __init__(self, pr_proc, timeline_procs=None):
        self.calls: list[list[str]] = []
        self._pr = pr_proc
        self._timelines = dict(timeline_procs or {})  # issue number -> proc

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            q = args[-1]
            if "pullRequest(" in q:
                return self._pr
            for num, proc in self._timelines.items():
                if f"issue(number: {num})" in q:
                    return proc
            return _proc(stdout=_crossref_stdout([]))
        return _proc()


def _merged_info(number=42):
    from server.github.pr_status import PullRequestInfo
    return PullRequestInfo(number=number, url=f"https://github.com/o/r/pull/{number}",
                           state="merged", checks="none", title="t", merged=True)


def _batch89() -> Batch:
    return Batch(primary="o/r#8", issues=["o/r#8", "o/r#9"], repo="o/r")


def test_close_batch_issues_closes_each_with_a_linking_comment():
    """Issues the PR's closingIssuesReferences names are closed with the link."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(closing=[8, 9])))

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert {c[2] for c in closes} == {"8", "9"}
    for c in closes:
        assert "--repo" in c and c[c.index("--repo") + 1] == "o/r"
        comment = c[c.index("--comment") + 1]
        assert "https://github.com/o/r/pull/42" in comment


def test_close_batch_issues_body_and_commit_references_count_as_linkage():
    """A `#N` reference in the PR body or a commit message is linkage."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(
        body="Fixes #8 by rewiring the sweep.", messages=["tidy up (refs #9)"])))

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert {c[2] for c in closes} == {"8", "9"}


def test_close_batch_issues_prefix_number_is_not_a_reference():
    """`#89` must not read as a reference to issue 8 (nor `#8` match in `x#89`)."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(body="see #89 and PR#800")),
                  timeline_procs={8: _proc(stdout=_crossref_stdout([])),
                                  9: _proc(stdout=_crossref_stdout([]))})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "unlinked"
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]


def test_close_batch_issues_timeline_crossref_counts_as_linkage():
    """No PR-side evidence, but the issue's own timeline cross-references the
    PR — the weakest sufficient signal (what find_referencing_pr keys on)."""
    from server.automations.backlog_github import close_batch_issues
    pr_node = {"number": 42, "url": "https://github.com/o/r/pull/42", "title": "t",
               "state": "MERGED", "isDraft": False, "headRefName": "b",
               "autoMergeRequest": None}
    gh = RoutedGh(_proc(stdout=_linkage_stdout()),
                  timeline_procs={8: _proc(stdout=_crossref_stdout([pr_node])),
                                  9: _proc(stdout=_crossref_stdout([pr_node]))})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert {c[2] for c in closes} == {"8", "9"}


def test_close_batch_issues_unlinked_pr_closes_nothing():
    """The #642 incident: a misattributed dep-bump PR (zero linkage to any
    batch issue) must close NOTHING and touch no labels — fail closed."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(body="chore: bump brace-expansion")),
                  timeline_procs={8: _proc(stdout=_crossref_stdout([])),
                                  9: _proc(stdout=_crossref_stdout([]))})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "unlinked"
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"]]
    assert not [c for c in gh.calls if c[:2] == ["issue", "comment"]]


def test_close_batch_issues_partial_linkage_closes_linked_and_unclaims_rest():
    """PR references one batch issue but not the other: close the linked one,
    return the verified-unlinked one to the pool OPEN (unclaim + comment)."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(closing=[8])),
                  timeline_procs={9: _proc(stdout=_crossref_stdout([]))})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert [c[2] for c in closes] == ["8"]
    unclaims = [c for c in gh.calls
                if c[:2] == ["issue", "edit"] and "--remove-label" in c]
    assert [c[2] for c in unclaims] == ["9"]
    comments = [c for c in gh.calls if c[:2] == ["issue", "comment"]]
    assert [c[2] for c in comments] == ["9"]


def test_close_batch_issues_unverifiable_reports_retry_and_closes_nothing():
    """Every linkage lookup failed → unknown, not "unlinked": close nothing,
    denylist nothing, let the next sweep retry."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(returncode=1, stderr="boom"),
                  timeline_procs={8: _proc(returncode=1, stderr="boom"),
                                  9: _proc(returncode=1, stderr="boom")})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "retry"
    assert not [c for c in gh.calls if c[0] == "issue"]


def test_close_batch_issues_mixed_unlinked_and_unverifiable_retries():
    """One issue verified-unlinked, the other unverifiable → don't denylist the
    PR on partial knowledge; retry next sweep."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout()),
                  timeline_procs={8: _proc(stdout=_crossref_stdout([])),
                                  9: _proc(returncode=1, stderr="boom")})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "retry"
    assert not [c for c in gh.calls if c[0] == "issue"]


# --------------------------------------------------------------------------- #
# query_claimed_refs / heal_orphan_claims (orphaned-claim healing)            #
# --------------------------------------------------------------------------- #

def _claimed_stdout(refs: list[tuple[str, int]]) -> str:
    nodes = [{"number": n, "title": f"t{n}", "url": f"https://github.com/{r}/issues/{n}",
              "repository": {"nameWithOwner": r}} for r, n in refs]
    return json.dumps({"data": {"claimed": {"nodes": nodes}}})


def test_query_claimed_refs_builds_label_query_and_parses():
    fake = FakeGh({("api", "graphql"): _proc(stdout=_claimed_stdout([("o/r", 5), ("o/k", 9)]))})
    out = query_claimed_refs(["o/r", "o/k"], gh_run=fake)
    assert out == ["o/r#5", "o/k#9"]
    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "label:in-progress" in query
    assert "repo:o/r" in query and "repo:o/k" in query
    assert "is:open" in query


def test_query_claimed_refs_empty_repos_and_gh_failure_return_empty():
    assert query_claimed_refs([], gh_run=FakeGh()) == []
    fake = FakeGh({("api", "graphql"): _proc(returncode=1, stderr="boom")})
    assert query_claimed_refs(["o/r"], gh_run=fake) == []


def _crossref_stdout(prs: list[dict]) -> str:
    nodes = [{"source": {**pr, "repository": {"nameWithOwner": "o/r"}}} for pr in prs]
    return json.dumps({"data": {"repository": {"issue": {"timelineItems": {"nodes": nodes}}}}})


def test_heal_orphan_merged_pr_closes_issue_and_strips_label():
    """An orphan whose referencing PR merged → close the issue + remove the claim."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout(
        [{"number": 40, "url": "https://github.com/o/r/pull/40", "title": "fix",
          "state": "MERGED", "isDraft": False, "headRefName": "b",
          "autoMergeRequest": None}]))})
    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == [("o/r#5", "closed")]
    close = next(c for c in fake.calls if c[:2] == ["issue", "close"])
    assert "--comment" in close and "o/r" in close
    edit = next(c for c in fake.calls if c[:2] == ["issue", "edit"])
    assert "--remove-label" in edit and "in-progress" in edit


def test_heal_orphan_open_pr_left_untouched():
    """An orphan with a live OPEN PR is real work — never unclaim or close it."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout(
        [{"number": 41, "url": "u", "title": "wip", "state": "OPEN",
          "isDraft": False, "headRefName": "b", "autoMergeRequest": None}]))})
    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == []
    assert not [c for c in fake.calls if c[:2] in (["issue", "edit"], ["issue", "close"])]


def test_heal_orphan_no_pr_reclaims_label():
    """No referencing PR (or closed-unmerged only) → stale lock; return to pool."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout([]))})
    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == [("o/r#5", "reclaimed")]
    edit = next(c for c in fake.calls if c[:2] == ["issue", "edit"])
    assert "--remove-label" in edit and "in-progress" in edit
    assert not [c for c in fake.calls if c[:2] == ["issue", "close"]]


def test_heal_orphan_skips_tracked_refs_and_honors_limit():
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout([]))})
    healed = heal_orphan_claims(["o/r#1", "o/r#2", "o/r#3"], {"o/r#1"},
                                gh_run=fake, limit=1)
    # #1 is tracked (skipped without a lookup); the cap allows exactly one heal.
    assert healed == [("o/r#2", "reclaimed")]
    assert len([c for c in fake.calls if c[:2] == ["api", "graphql"]]) == 1
