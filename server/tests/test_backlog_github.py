"""GitHub I/O for the Backlog Runner — pre-dispatch verification, the claim /
needs-human announcements and the merged-PR close-out.

Phase D (#994) retired the ``in-progress``/``needs-human`` labels: a claim is the
runner's durable target-state row and a handoff is its ``gave_up`` row, so what
is left here is announcements (comments) plus the close-out. No test in this file
may expect a label write.

Every gh call is routed through the injected ``gh_run`` seam, so these tests use
``FakeGh`` and never touch the network.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from typing import Callable

import pytest

from viberpit import gitrepos
from server.automations.backlog_github import (
    announce_claim,
    announce_needs_human,
    issue_state_live,
    verify_ready_live,
)
from server.automations.backlog_selection import Batch
from server.tests.fakes.fake_backlog import FakeGh


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@contextlib.contextmanager
def _app_tokens(mapping: dict[str, str] | None = None):
    """Register a fake GitHub App token provider for the duration of the block —
    ``owner -> "tok-<owner>"`` by default, or an explicit ``{owner: token}`` map
    (missing owners resolve to ``None``, i.e. ambient auth). Always restores the
    global provider afterward so tests never leak state to each other."""
    fn: "Callable[[str], str | None]"
    if mapping is None:
        fn = lambda owner: f"tok-{owner}"  # noqa: E731
    else:
        fn = lambda owner: mapping.get(owner)  # noqa: E731
    gitrepos.set_token_provider(fn)
    try:
        yield
    finally:
        gitrepos.set_token_provider(None)


# --------------------------------------------------------------------------- #
# verify_ready_live — spec 003 §7a safety net, fails closed (§10)              #
# --------------------------------------------------------------------------- #

def _ready_issue(*, state: str = "OPEN", assignees: int = 0,
                 blockers: tuple[str, ...] = (), total: int | None = None) -> dict:
    """One ``verify_ready_live`` issue node. ``total`` overrides the ``blockedBy``
    ``totalCount`` (default: the page is complete) so a test can model GitHub
    reporting more blockers than the page returned."""
    return {"state": state, "assignees": {"totalCount": assignees},
            "blockedBy": {"totalCount": len(blockers) if total is None else total,
                          "nodes": [{"state": s} for s in blockers]}}


def _verify_payload(repos: dict[str, dict[str, dict]]) -> str:
    """``{"o/r": {"5": issue_node, ...}}`` → the aliased response body
    ``verify_ready_live`` expects (``r<i>`` per repo, ``i<n>`` per issue)."""
    data = {}
    for i, (_repo, issues) in enumerate(repos.items()):
        data[f"r{i}"] = {f"i{n}": node for n, node in issues.items()}
    return json.dumps({"data": data})


def test_verify_ready_live_true_when_every_issue_is_open_unassigned_unblocked():
    gh = FakeGh({("api", "graphql"): _proc(stdout=_verify_payload(
        {"o/r": {"5": _ready_issue(blockers=("CLOSED",)), "6": _ready_issue()}}))})
    with _app_tokens():
        assert verify_ready_live(["o/r#5", "o/r#6"], gh_run=gh) is True
    assert len(gh.calls) == 1, "one query per owner"
    query = next(a for a in gh.calls[0] if a.startswith("query="))
    assert "i5: issue(number: 5)" in query and "i6: issue(number: 6)" in query
    assert ("blockedBy(first: 50) { totalCount nodes { state } }" in query
            and "assignees(first: 1)" in query)
    assert gh.tokens == ["tok-o"]


def test_verify_ready_live_false_on_open_blocker():
    gh = FakeGh({("api", "graphql"): _proc(stdout=_verify_payload(
        {"o/r": {"5": _ready_issue(blockers=("CLOSED", "OPEN"))}}))})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is False


def test_verify_ready_live_false_on_blocker_with_unreadable_state():
    """Same polarity as the mirror-side gate (``backlog_deps._is_done``): a
    blocker whose state cannot be read is blocking, never clear."""
    gh = FakeGh({("api", "graphql"): _proc(stdout=json.dumps({"data": {"r0": {"i5": {
        "state": "OPEN", "assignees": {"totalCount": 0},
        "blockedBy": {"nodes": [{"state": None}, None]}}}}}))})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is False


def test_verify_ready_live_none_when_blockers_are_truncated():
    """More blockers than the page returned: the ones past the page are unknown,
    and unknown is not clear — §10 fails closed, so this is ``None``, not True."""
    gh = FakeGh({("api", "graphql"): _proc(stdout=_verify_payload(
        {"o/r": {"5": _ready_issue(blockers=("CLOSED",) * 50, total=51)}}))})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is None


def test_verify_ready_live_false_on_assignee():
    gh = FakeGh({("api", "graphql"): _proc(stdout=_verify_payload(
        {"o/r": {"5": _ready_issue(assignees=1)}}))})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is False


def test_verify_ready_live_false_when_issue_no_longer_open():
    gh = FakeGh({("api", "graphql"): _proc(stdout=_verify_payload(
        {"o/r": {"5": _ready_issue(state="CLOSED")}}))})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is False


def test_verify_ready_live_none_on_gh_failure():
    gh = FakeGh({("api", "graphql"): _proc(returncode=1, stderr="boom")})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is None


def test_verify_ready_live_none_on_unparseable_payload():
    gh = FakeGh({("api", "graphql"): _proc(stdout="<html>rate limited</html>")})
    assert verify_ready_live(["o/r#5"], gh_run=gh) is None


def test_verify_ready_live_none_when_a_ref_is_missing_from_the_response():
    # GitHub returns ``null`` for an alias it could not resolve (deleted or
    # transferred issue, permissions) — that is not "ready", it is "unknown".
    gh = FakeGh({("api", "graphql"): _proc(stdout=_verify_payload(
        {"o/r": {"5": _ready_issue(), "6": None}}))})  # type: ignore[dict-item]
    assert verify_ready_live(["o/r#5", "o/r#6"], gh_run=gh) is None


def test_verify_ready_live_none_when_batch_is_empty_or_malformed():
    gh = FakeGh()
    assert verify_ready_live([], gh_run=gh) is None
    assert verify_ready_live(["not-a-ref"], gh_run=gh) is None
    assert gh.calls == []


def test_verify_ready_live_consults_every_owner_with_its_own_token():
    """Refs across two owners → one call per owner with that owner's token;
    a not-ready issue under EITHER owner fails the whole batch."""
    class ByOwner:
        def __init__(self, ready_other: bool):
            self.calls: list[list[str]] = []
            self.tokens: list[str | None] = []
            self._ready_other = ready_other

        def __call__(self, args, token=None):
            self.calls.append(list(args))
            self.tokens.append(token)
            query = next(a for a in args if a.startswith("query="))
            if 'owner: "acme"' in query:
                return _proc(stdout=_verify_payload({"acme/r1": {"1": _ready_issue()}}))
            node = _ready_issue() if self._ready_other else _ready_issue(assignees=1)
            return _proc(stdout=_verify_payload({"other/r2": {"2": node}}))

    with _app_tokens():
        gh = ByOwner(ready_other=True)
        assert verify_ready_live(["acme/r1#1", "other/r2#2"], gh_run=gh) is True
        assert len(gh.calls) == 2
        assert set(gh.tokens) == {"tok-acme", "tok-other"}
        gh = ByOwner(ready_other=False)
        assert verify_ready_live(["acme/r1#1", "other/r2#2"], gh_run=gh) is False


# --------------------------------------------------------------------------- #
# issue_state_live — how an ended INVESTIGATION is settled (spec §5)           #
# --------------------------------------------------------------------------- #

def test_issue_state_live_reads_the_live_state_with_the_repos_token():
    gh = FakeGh({("issue", "view"): _proc(stdout=json.dumps({"state": "CLOSED"}))})
    with _app_tokens():
        assert issue_state_live("o/r#5", gh_run=gh) == "closed"
    assert gh.calls == [["issue", "view", "5", "--repo", "o/r", "--json", "state"]]
    assert gh.tokens == ["tok-o"]


def test_issue_state_live_reads_open():
    gh = FakeGh({("issue", "view"): _proc(stdout=json.dumps({"state": "OPEN"}))})
    assert issue_state_live("o/r#5", gh_run=gh) == "open"


def test_issue_state_live_is_none_when_the_answer_is_unknown():
    """Unknown must never collapse to "open" — that is the hand-off case, and the
    caller retries instead of giving a human an issue because gh was down."""
    failed = FakeGh({("issue", "view"): _proc(returncode=1, stderr="boom")})
    assert issue_state_live("o/r#5", gh_run=failed) is None
    unparseable = FakeGh({("issue", "view"): _proc(stdout="<html>rate limited</html>")})
    assert issue_state_live("o/r#5", gh_run=unparseable) is None
    unexpected = FakeGh({("issue", "view"): _proc(stdout=json.dumps({"state": "MERGED"}))})
    assert issue_state_live("o/r#5", gh_run=unexpected) is None


def test_issue_state_live_never_calls_gh_for_a_malformed_ref():
    gh = FakeGh()
    assert issue_state_live("not-a-ref", gh_run=gh) is None
    assert issue_state_live("o/r#abc", gh_run=gh) is None
    assert gh.calls == []


# --------------------------------------------------------------------------- #
# find_referencing_pr                                                          #
# --------------------------------------------------------------------------- #

def _timeline_stdout(sources: list[dict]) -> str:
    return json.dumps({"data": {"repository": {"issue": {"timelineItems": {
        "nodes": [{"source": s} for s in sources]}}}}})


def _pr_node(number: int, state: str, repo: str = "o/r", *, is_draft: bool = False,
             auto_merge: bool = False, branch: str = "b",
             closes: tuple[int, ...] = ()) -> dict:
    return {"number": number, "url": f"https://github.com/{repo}/pull/{number}",
            "title": f"pr {number}", "state": state, "isDraft": is_draft,
            "headRefName": branch,
            "autoMergeRequest": {"enabledAt": "x"} if auto_merge else None,
            "repository": {"nameWithOwner": repo},
            "closingIssuesReferences": {"nodes": [
                {"number": n, "repository": {"nameWithOwner": repo}} for n in closes]}}


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


def test_find_referencing_pr_prefers_linked_over_merged_mention():
    """Regression for #2201: a merged PR that only *mentions* the issue (no
    ``closingIssuesReferences`` entry — e.g. a batch docs/re-review PR) must
    never outrank an OPEN PR that is actually linked to close it, even though
    MERGED normally beats OPEN."""
    from server.automations.backlog_github import find_referencing_pr
    sources = [
        _pr_node(16109, "MERGED", branch="docs/batch"),      # mentions only
        _pr_node(16440, "OPEN", branch="fix/it", closes=(15952,)),  # linked
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_timeline_stdout(sources))})

    info = find_referencing_pr("o/r#15952", gh_run=fake)

    assert info is not None
    assert (info.number, info.state, info.merged) == (16440, "open", False)


def test_find_referencing_pr_merged_wins_within_the_linked_tier():
    """When more than one PR is linked, MERGED still beats OPEN within that tier."""
    from server.automations.backlog_github import find_referencing_pr
    sources = [
        _pr_node(1, "OPEN", closes=(3,)),
        _pr_node(2, "MERGED", closes=(3,)),
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_timeline_stdout(sources))})

    info = find_referencing_pr("o/r#3", gh_run=fake)

    assert info is not None
    assert (info.number, info.state) == (2, "merged")


def test_find_referencing_pr_falls_back_to_mentions_when_nothing_is_linked():
    """No cross-referencing PR is linked (the common case pre-#2201): the old
    MERGED-beats-OPEN-beats-nothing ranking still applies to mentions."""
    from server.automations.backlog_github import find_referencing_pr
    sources = [_pr_node(1, "OPEN"), _pr_node(2, "MERGED")]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_timeline_stdout(sources))})

    info = find_referencing_pr("o/r#3", gh_run=fake)

    assert info is not None
    assert (info.number, info.state) == (2, "merged")


def test_find_referencing_pr_passes_repo_app_token():
    from server.automations.backlog_github import find_referencing_pr
    fake = FakeGh({("api", "graphql"): _proc(stdout=_timeline_stdout([]))})
    with _app_tokens():
        find_referencing_pr("acme/r1#3", gh_run=fake)
    assert fake.tokens == ["tok-acme"]


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
        self.tokens: list[str | None] = []
        self._pr = pr_proc
        self._timelines = dict(timeline_procs or {})  # issue number -> proc

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        self.tokens.append(token)
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


def test_close_batch_issues_passes_repo_app_token_to_every_call():
    """Every lookup and mutation close_batch_issues makes (PR linkage, per-issue
    signals, the issue close) is scoped to the batch's repo ``o/r`` —
    every gh call must carry o's installation token."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(closing=[8, 9])))

    with _app_tokens():
        outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    assert gh.calls
    assert all(tok == "tok-o" for tok in gh.tokens)


def test_close_batch_issues_body_and_commit_references_count_as_linkage():
    """A `#N` reference in the PR body or a commit message is linkage."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(
        body="Fixes #8 by rewiring the sweep.", messages=["tidy up (#9)"])))

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


def test_close_batch_issues_partial_linkage_closes_linked_and_returns_rest():
    """PR references one batch issue but not the other: close the linked one,
    leave the verified-unlinked one OPEN with a comment saying so (the runner's
    row is what returns it to the pool — Phase D has no claim label to strip)."""
    from server.automations.backlog_github import close_batch_issues
    gh = RoutedGh(_proc(stdout=_linkage_stdout(closing=[8])),
                  timeline_procs={9: _proc(stdout=_crossref_stdout([]))})

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert [c[2] for c in closes] == ["8"]
    comments = [c for c in gh.calls if c[:2] == ["issue", "comment"]]
    assert [c[2] for c in comments] == ["9"]
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"]]


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
# close_batch_issues — human gate: action issues & unchecked acceptance (#784/#2681)
# --------------------------------------------------------------------------- #

def _signals_stdout(title="", body="", labels=(), sub_issue_states=()) -> str:
    """An issue-signals GraphQL response: title + body + labels + sub-issues."""
    return json.dumps({"data": {"repository": {"issue": {
        "title": title, "body": body,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "subIssues": {"nodes": [{"state": s} for s in sub_issue_states]}}}}})


class GateGh:
    """Routes the three graphql shapes close_batch_issues issues once linkage is
    verified: PR linkage (``closingIssuesReferences``), per-issue signals (``issue(number: N)`` with ``labels(``), and the timeline
    cross-ref (``timelineItems``). Non-graphql calls succeed."""

    def __init__(self, *, pr, signals=None):
        self.calls: list[list[str]] = []
        self.tokens: list[str | None] = []
        self._pr = pr
        self._signals = dict(signals or {})  # issue number -> proc

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        self.tokens.append(token)
        if args[:2] != ["api", "graphql"]:
            return _proc()
        q = args[-1]
        if "pullRequest(" in q:
            return self._pr
        if "timelineItems" in q:
            return _proc(stdout=_crossref_stdout([]))
        if "issue(number" in q:
            for num, proc in self._signals.items():
                if f"issue(number: {num})" in q:
                    return proc
            return _proc(stdout=_signals_stdout())
        return _proc()


def _batch8() -> Batch:
    return Batch(primary="o/r#8", issues=["o/r#8"], repo="o/r")


def test_close_batch_issues_reports_escalated_and_edits_no_labels():
    """A verifiably-linked PR must NOT auto-close an action/ops issue (a manual
    console step). The #784 incident: the issue is handed to a human and left
    OPEN — a PR can never perform the real-world action. Phase D reports that as
    the ``"escalated"`` outcome (the sweep turns it into a ``gave_up`` row); the
    only GitHub write is the explaining comment."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        signals={8: _proc(stdout=_signals_stdout(
            title="Remove Classroom scopes on the GCP consent screen"))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    # The outcome names what is left (#2681): the matched action phrase.
    assert outcome.startswith("escalated: ") and "'consent screen'" in outcome
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert not any("--add-label" in c or "--remove-label" in c for c in gh.calls)
    assert [c for c in gh.calls if c[:2] == ["issue", "comment"]]


def test_close_batch_issues_tracking_issue_with_open_sub_issues_is_released_not_closed():
    """A merged PR that references a tracking epic must not close it while any
    sub-issue is still open — the #997 incident: PR #1004 said ``Refs #997``, a
    bare mention counted as linkage, and ten open children were orphaned under
    a closed parent. The epic is left open with an explaining comment; it is NOT
    escalated either, because nothing about it needs a human — its open
    sub-issues are the remaining work."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),  # even a real Fixes #8
        signals={8: _proc(stdout=_signals_stdout(
            title="Epic: 003 rollout", sub_issue_states=["CLOSED", "OPEN", "OPEN"]))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"  # the PR IS the batch's; the epic is just not closeable
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert not any("--add-label" in c or "--remove-label" in c for c in gh.calls)
    comment = next(c for c in gh.calls if c[:2] == ["issue", "comment"])
    assert "2 open sub-issue" in comment[comment.index("--body") + 1]


def test_close_batch_issues_epic_whose_sub_issues_are_all_done_still_closes():
    """The gate is on OPEN children only: an epic whose sub-issues are all
    closed is ordinary completed work."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        signals={8: _proc(stdout=_signals_stdout(
            title="Epic: done", sub_issue_states=["CLOSED", "CLOSED"]))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    assert [c for c in gh.calls if c[:2] == ["issue", "close"]]


def test_close_batch_issues_docs_only_pr_closes_an_issue_with_nothing_left():
    """#2681: a merged, verified-linked PR closes the issue even when its diff is
    documentation only — the pre-#2681 docs-only gate held exactly these, and each
    hold's only remaining step was a person closing the issue."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        signals={8: _proc(stdout=_signals_stdout(title="Weekly failed — 2026-09-13"))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert [c[2] for c in closes] == ["8"]
    # No PR-files lookup any more: the diff's shape gates nothing.
    assert not any("files(" in c[-1] for c in gh.calls if c[:2] == ["api", "graphql"])


def test_close_batch_issues_unchecked_acceptance_items_keep_the_issue_and_say_so():
    """An unchecked task-list item is work the issue itself says is open, so the
    close-out keeps it and the outcome quotes what is left (the hold reason)."""
    from server.automations.backlog_github import close_batch_issues
    body = ("Acceptance:\n- [x] API checks pass\n- [ ] Exercise the browser return path "
            "on QA\n* [ ] Record the audit anchor\n")
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        signals={8: _proc(stdout=_signals_stdout(title="finish acceptance", body=body))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == ("escalated: 2 unchecked acceptance item(s) remain on the issue "
                       "(first: 'Exercise the browser return path on QA')")
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    comment = next(c for c in gh.calls if c[:2] == ["issue", "comment"])
    assert "2 unchecked acceptance item(s)" in comment[comment.index("--body") + 1]


def test_close_batch_issues_names_each_gated_ref_in_a_multi_issue_batch():
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8, 9])),
        signals={8: _proc(stdout=_signals_stdout(title="ok")),
                 9: _proc(stdout=_signals_stdout(title="t", labels=["manual"]))})

    outcome = close_batch_issues(Batch(primary="o/r#8", issues=["o/r#8", "o/r#9"], repo="o/r"),
                                 _merged_info(), gh_run=gh)

    assert outcome == ("escalated: o/r#9: it is labelled 'manual': a manual/operational "
                       "step a merge cannot perform")
    assert [c[2] for c in gh.calls if c[:2] == ["issue", "close"]] == ["8"]


def test_close_batch_issues_refs_only_reference_is_kept_open_and_escalated():
    """#2984 (cairnea/KinDash#15941): the PR referenced the tracker with ``Refs``
    and a "Why this is not `Fixes #N`" section, deliberately; the close-out closed
    it anyway. A Refs-only reference is the session's "not done" — keep it open
    and hand it to a person, never close it."""
    from server.automations.backlog_github import close_batch_issues
    body = ("Refs #8\n\n## Why this is not `Fixes #8`\n\nThe tracker stays open "
            "until its sub-issues land.")
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(body=body)))

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome.startswith("escalated: ") and "`Refs`" in outcome
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    comment = next(c for c in gh.calls if c[:2] == ["issue", "comment"])
    assert "left it open on purpose" in comment[comment.index("--body") + 1]


@pytest.mark.parametrize("text", [
    "Refs #8, Fixes #9",                       # each ref judged on its own keyword
    "Part of #8; fixes #9",
    "Related to https://github.com/o/r/issues/8\n\nCloses #9",
])
def test_close_batch_issues_refs_keeps_only_the_refs_issue_open(text):
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(body=text)))

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome.startswith("escalated: o/r#8: ")
    assert [c[2] for c in gh.calls if c[:2] == ["issue", "close"]] == ["9"]


def test_close_batch_issues_deferred_follow_up_is_kept_open_and_escalated():
    """#3099 (cairnea/KinDash#19385): the merged PR listed the issue under deferred
    follow-ups ("blocked by Train 6") and the runner closed it "Implemented by"."""
    from server.automations.backlog_github import close_batch_issues
    body = ("Ships the time sheet.\n\n## Deferred\n"
            "- #8: the Android slice (chip, cards). It is blocked by Train 6.")
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(body=body)))

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome.startswith("escalated: ") and "deferred/blocked" in outcome
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]


def test_close_batch_issues_deferral_does_not_override_a_closing_keyword():
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(
        body="Fixes #8\n\nFollow-up for #8 docs is deferred.")))

    assert close_batch_issues(_batch8(), _merged_info(), gh_run=gh) == "closed"


@pytest.mark.parametrize("pr", [
    {"closing": [8], "body": "Refs #8"},                        # GitHub closes it
    {"body": "Refs #8", "messages": ["fix: guard (fixes #8)"]},  # a commit closes it
    {"body": "Implements #8."},                                  # no keyword: forgot
])
def test_close_batch_issues_refs_does_not_hold_a_closed_or_forgotten_issue(pr):
    """Only a Refs-ONLY reference holds: a closing keyword anywhere (GitHub's own
    closingIssuesReferences, a commit message), or a bare mention from a session
    that simply forgot ``Fixes``, still closes — that is what the close-out is for."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(**pr)))

    assert close_batch_issues(_batch8(), _merged_info(), gh_run=gh) == "closed"
    assert [c[2] for c in gh.calls if c[:2] == ["issue", "close"]] == ["8"]


@pytest.mark.parametrize("signals,needle", [
    # An `epic` LABEL is deferred, not held (#3107) — see the left-open tests below.
    ({"labels": ["permanent-singleton"]}, "'permanent-singleton'"),
    ({"labels": ["Epic", "permanent-singleton"]}, "'epic'"),
    ({"body": "> ⚠️ **Permanent singleton — do not claim or close.**"},
     "'permanent singleton'"),
    ({"body": "Reference it with `Refs #16546`, never `Fixes`."}, "'never fixes'"),
])
def test_close_batch_issues_issue_that_forbids_closing_is_escalated(signals, needle):
    """#2984 (cairnea/KinDash#16685, #16677): an epic or a self-declared
    do-not-close issue is never a merged PR's to close, even with ``Fixes #N``."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(closing=[8])),
                signals={8: _proc(stdout=_signals_stdout(title="t", **signals))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome.startswith("escalated: ") and needle in outcome
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]


def test_keep_open_phrases_ignore_an_ordinary_do_not_close_ui_bug():
    from server.automations.backlog_github import _human_gate_reason
    assert _human_gate_reason({"title": "Dialog", "labels": [],
                               "body": "Do not close the dialog on Escape."}) is None


# --------------------------------------------------------------------------- #
# close_batch_issues — the session's declared left-open reason (#3107)          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reason", [
    "epic", "remaining_work", "awaiting_deploy_or_condition",
    "needs_physical_verification", "needs_owner_record"])
@pytest.mark.parametrize("template", [
    "Refs #8 (left-open: {r})",
    "Refs #8 — left-open: {r}",
    "refs #8 - Left Open: {r}",
    "Part of o/r#8 (left-open: {r})",
    "Related to https://github.com/o/r/issues/8 (left-open: {r})",
])
def test_declared_left_open_reason_parses_every_reason_and_spelling(reason, template):
    from server.automations.backlog_github import LEFT_OPEN_REASONS, declared_left_open_reason
    assert reason in LEFT_OPEN_REASONS
    text = "Some summary.\n\n" + template.format(r=reason) + "\n"
    assert declared_left_open_reason(text, "o/r", 8) == reason


@pytest.mark.parametrize("text", [
    "`Refs #8 (left-open: epic)`",                  # quoted in a code span
    "```\nRefs #8 (left-open: epic)\n```",           # quoted in a fence
    "Refs #8 (left-open: because_reasons)",          # unknown token
    "Refs #80 (left-open: epic)",                    # #8 is not #80
    "Refs #8",                                       # no declaration
    "Refs other/repo#8 (left-open: epic)",           # another repo's #8
    "Fixes #8 (left-open: epic)",                    # not a non-closing keyword
])
def test_declared_left_open_reason_ignores_what_is_not_a_declaration(text):
    from server.automations.backlog_github import declared_left_open_reason
    assert declared_left_open_reason(text, "o/r", 8) is None


def test_left_open_disposition_maps_every_reason():
    from server.automations.backlog_github import LEFT_OPEN_REASONS, left_open_disposition
    assert {r: left_open_disposition(r) for r in LEFT_OPEN_REASONS} == {
        "epic": "defer", "awaiting_deploy_or_condition": "defer",
        "remaining_work": "requeue",
        "needs_physical_verification": "hold", "needs_owner_record": "hold"}
    assert left_open_disposition(None) == "hold"
    assert left_open_disposition("nonsense") == "hold"


def _comment_bodies(gh) -> list[str]:
    return [c[c.index("--body") + 1] for c in gh.calls if c[:2] == ["issue", "comment"]]


@pytest.mark.parametrize("reason", ["epic", "awaiting_deploy_or_condition"])
def test_close_batch_issues_declared_defer_reason_is_deferred_without_a_hold(reason):
    from server.automations.backlog_github import DEFERRED, close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(body=f"Refs #8 (left-open: {reason})")))

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == DEFERRED
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    body, = _comment_bodies(gh)
    assert "Handed to a human" not in body and "no hold" in body


def test_close_batch_issues_declared_remaining_work_is_requeued():
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(
        body="Refs #8 (left-open: remaining_work)")))

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"   # no hold, no window: the pool takes it back
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    body, = _comment_bodies(gh)
    assert "Handed to a human" not in body and "returned it to the backlog pool" in body


@pytest.mark.parametrize("reason,words", [
    ("needs_physical_verification", "needs physical verification"),
    ("needs_owner_record", "needs owner record"),
])
def test_close_batch_issues_declared_hold_reason_names_the_reason(reason, words):
    from server.automations.backlog_github import (
        CLOSEOUT_HOLD_PREFIX, close_batch_issues, closeout_hold_reason, escalation_detail)
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(body=f"Refs #8 (left-open: {reason})")))

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    detail = escalation_detail(outcome)
    assert detail == f"{words} (declared by the session)"
    assert closeout_hold_reason(detail).startswith(CLOSEOUT_HOLD_PREFIX)
    body, = _comment_bodies(gh)
    assert "Handed to a human" in body


def test_close_batch_issues_epic_label_without_a_reason_is_deferred():
    """#3107 acceptance: the owner digest no longer shows epics. KinDash #19347 was
    labelled `epic` and still held."""
    from server.automations.backlog_github import DEFERRED, close_batch_issues
    for pr in ({"closing": [8]}, {"body": "Refs #8"},
               {"body": "Refs #8 (left-open: needs_owner_record)"}):
        gh = GateGh(pr=_proc(stdout=_linkage_stdout(**pr)),
                    signals={8: _proc(stdout=_signals_stdout(title="t", labels=["Epic"]))})
        assert close_batch_issues(_batch8(), _merged_info(), gh_run=gh) == DEFERRED
        assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
        assert all("Handed to a human" not in b for b in _comment_bodies(gh))


def test_close_batch_issues_undeclared_refs_still_escalates():
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(body="Refs #8 (left-open: whatever)")))
    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)
    assert outcome.startswith("escalated: ") and "`Refs`" in outcome


def test_close_batch_issues_hold_outranks_a_deferred_sibling():
    from server.automations.backlog_github import close_batch_issues, escalation_detail
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(
        body="Refs #8 (left-open: awaiting_deploy_or_condition)\n"
             "Refs #9 (left-open: needs_owner_record)")))

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert escalation_detail(outcome) == "o/r#9: needs owner record (declared by the session)"
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]


def test_close_batch_issues_deferred_member_beside_a_closed_one_is_deferred():
    from server.automations.backlog_github import DEFERRED, close_batch_issues, is_deferred
    gh = GateGh(pr=_proc(stdout=_linkage_stdout(
        body="Refs #8 (left-open: awaiting_deploy_or_condition)\nFixes #9")))

    outcome = close_batch_issues(_batch89(), _merged_info(), gh_run=gh)

    assert outcome == DEFERRED and is_deferred(outcome) and not is_deferred("closed")
    assert [c[2] for c in gh.calls if c[:2] == ["issue", "close"]] == ["9"]


def test_escalation_outcome_round_trips():
    from server.automations.backlog_github import escalated_outcome, escalation_detail
    assert escalation_detail(escalated_outcome(["a", "b"])) == "a; b"
    assert escalation_detail("escalated") == ""          # a legacy bare outcome
    assert escalation_detail(escalated_outcome([])) == ""
    assert escalation_detail("closed") is None and escalation_detail(None) is None


def test_close_batch_issues_code_pr_normal_issue_still_closes():
    """The happy path is unchanged: a code PR for an ordinary code issue closes
    it — the human gate only fires on action issues or unchecked acceptance items."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        signals={8: _proc(stdout=_signals_stdout(title="Fix the sweep off-by-one"))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert [c[2] for c in closes] == ["8"]
    assert not any("--add-label" in c or "--remove-label" in c for c in gh.calls)


def test_close_batch_issues_action_signal_lookup_failure_falls_back_to_close():
    """If the issue-signals lookup fails we cannot assert action/ops, so a
    verified-linked PR still closes — a transient read must not wedge close-out
    forever (the linkage check already gates misattribution)."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        signals={8: _proc(returncode=1, stderr="boom")})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    assert [c for c in gh.calls if c[:2] == ["issue", "close"]]


# --------------------------------------------------------------------------- #
# Human-gate predicates (#784/#2681)                                          #
# --------------------------------------------------------------------------- #

def test_human_gate_reason_names_phrases_labels_and_unchecked_items():
    from server.automations.backlog_github import _human_gate_reason
    assert "'search console'" in _human_gate_reason(
        {"title": "Verify domain in Search Console", "body": "", "labels": []})
    assert "'demo video'" in _human_gate_reason(
        {"title": "Cleanup", "body": "Record a demo video and upload it", "labels": []})
    assert "'action-required'" in _human_gate_reason(
        {"title": "misc", "body": "", "labels": ["action-required"]})
    assert "'deployed acceptance'" in _human_gate_reason(
        {"title": "test(family): finish deployed acceptance", "body": "", "labels": []})
    assert _human_gate_reason({"title": "Fix null deref", "body": "guard the pointer",
                               "labels": []}) is None
    # A CHECKED item is done; only an unchecked one gates.
    assert _human_gate_reason({"title": "t", "body": "- [x] done", "labels": []}) is None
    long_item = "x" * 200
    reason = _human_gate_reason({"title": "t", "body": f"1. [ ] {long_item}", "labels": []})
    assert reason.startswith("1 unchecked acceptance item(s)") and reason.endswith("…')")
    assert _human_gate_reason(None) is None


# --------------------------------------------------------------------------- #
# Shared timeline fixture                                                      #
# --------------------------------------------------------------------------- #

def _crossref_stdout(prs: list[dict]) -> str:
    nodes = [{"source": {**pr, "repository": {"nameWithOwner": "o/r"}}} for pr in prs]
    return json.dumps({"data": {"repository": {"issue": {"timelineItems": {"nodes": nodes}}}}})


# --------------------------------------------------------------------------- #
# announce_claim / announce_needs_human (Phase D: comments, never labels)      #
# --------------------------------------------------------------------------- #

def test_announce_claim_comments_every_issue_and_never_raises():
    """The claim itself is the durable row; this is only its human-visible
    trace, so a failing comment on one issue must neither raise nor stop the
    rest — and nothing here may touch a label."""
    gh = FakeGh({("issue", "comment", "9"): _proc(returncode=1, stderr="boom")})
    announce_claim(Batch(primary="o/r#7", issues=["o/r#7", "o/r#9"], repo="o/r"),
                   "Claimed by X", gh_run=gh)
    cmds = [c for c in gh.calls if c[:2] == ["issue", "comment"]]
    assert [c[2] for c in cmds] == ["7", "9"] and all("--body" in c for c in cmds)
    assert not any("--add-label" in c or "--remove-label" in c for c in gh.calls)


def test_announce_claim_passes_repo_app_token():
    gh = FakeGh()
    with _app_tokens():
        announce_claim(Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r"),
                       "Claimed by X", gh_run=gh)
    assert gh.calls
    assert all(tok == "tok-o" for tok in gh.tokens)


def test_announce_helpers_survive_a_raising_seam():
    """"Never raises" has to cover a seam that RAISES, not just one that returns
    non-zero: minting the repo's App token does network I/O, so a failure on the
    first ref must not abort the caller or skip the bundle's remaining issues."""
    def raising_gh(args, token=None):
        calls.append(list(args))
        if args[:3] == ["issue", "comment", "7"]:
            raise RuntimeError("token mint failed")
        return _proc()

    batch = Batch(primary="o/r#7", issues=["o/r#7", "o/r#9"], repo="o/r")
    calls: list[list[str]] = []
    announce_claim(batch, "Claimed by X", gh_run=raising_gh)
    assert [c[2] for c in calls if c[:2] == ["issue", "comment"]] == ["7", "9"]

    calls = []
    announce_needs_human(batch, "max attempts", gh_run=raising_gh)
    assert [c[2] for c in calls] == ["7", "9"]


def test_announce_needs_human_comments_with_reason():
    gh = FakeGh()
    announce_needs_human(Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r"),
                         "max attempts", gh_run=gh)
    body = next(c for c in gh.calls if c[:2] == ["issue", "comment"])
    text = body[body.index("--body") + 1]
    assert "max attempts" in text
    # The comment must name the board value the reconciler writes — "Held" since the
    # rename (spec 2026-09-18 §9) — and list the three release signals (spec §7).
    assert "Board Status is Held" in text and "Needs human" not in text
    assert "Ready or Backlog" in text and "Retry" in text
    assert not any("label" in a for c in gh.calls for a in c)


# --------------------------------------------------------------------------- #
# retract_claims (#3000)                                                       #
# --------------------------------------------------------------------------- #

def test_retract_claims_edits_each_standing_claim_comment():
    from server.automations.backlog_github import (
        CLAIM_COMMENT_PREFIX, RELEASED_CLAIM_BODY, retract_claims)
    gh = FakeGh({("api", "repos/o/r/issues/7/comments?per_page=100"): _proc("11\n12\n")})
    with _app_tokens():
        retract_claims(["o/r#7", "o/r#9"], gh_run=gh)
    reads = [c for c in gh.calls if c[1].endswith("/comments?per_page=100")]
    assert [c[1] for c in reads] == ["repos/o/r/issues/7/comments?per_page=100",
                                     "repos/o/r/issues/9/comments?per_page=100"]
    jq = reads[0][reads[0].index("--jq") + 1]
    assert CLAIM_COMMENT_PREFIX in jq and "viberpit:automation" in jq
    edits = [c for c in gh.calls if "PATCH" in c]
    assert [c[3] for c in edits] == ["repos/o/r/issues/comments/11",
                                     "repos/o/r/issues/comments/12"]
    body = edits[0][edits[0].index("-f") + 1]
    assert body.startswith(f"body={RELEASED_CLAIM_BODY}") and "viberpit:automation" in body
    # The edited body no longer matches the claim filter, so a retry is a no-op.
    assert not RELEASED_CLAIM_BODY.startswith(CLAIM_COMMENT_PREFIX)
    assert all(tok == "tok-o" for tok in gh.tokens)


def test_retract_claims_never_raises_and_skips_a_failed_read():
    from server.automations.backlog_github import retract_claims

    def gh(args, token=None):
        calls.append(list(args))
        if args[1].startswith("repos/o/r/issues/7/"):
            raise RuntimeError("token mint failed")
        if args[1].startswith("repos/o/r/issues/8/"):
            return _proc(returncode=1, stderr="404")
        return _proc("5\n")

    calls: list[list[str]] = []
    retract_claims(["o/r#7", "o/r#8", "o/r#9"], gh_run=gh)
    assert [c[3] for c in calls if "PATCH" in c] == ["repos/o/r/issues/comments/5"]


def test_announce_claim_retracts_a_standing_claim_before_posting():
    """An issue never shows two live claims (#3000): an earlier claim released by a
    path that did not retract it is taken down when the next one is posted."""
    gh = FakeGh({("api", "repos/o/r/issues/7/comments?per_page=100"): _proc("11\n")})
    announce_claim(Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r"),
                   "Claimed by X", gh_run=gh)
    kinds = ["patch" if "PATCH" in c else c[0] + " " + c[1].split("?")[0][-8:]
             for c in gh.calls]
    assert kinds == ["api comments", "patch", "issue comment"]


# --------------------------------------------------------------------------- #
# gh failures — GitHub's edge timeout is INFO, anything else stays WARNING     #
# (#3017). Every site already answers "unknown"; only the log level differs.   #
# --------------------------------------------------------------------------- #

_EDGE_504 = "gh: HTTP 504"


def _failing_site(site: str, gh: FakeGh):
    """Run one of the five gh-failure sites against *gh* and return its answer."""
    from server.automations import backlog_github as bg
    if site == "verify_ready":
        return bg.verify_ready_live(["o/r#5"], gh_run=gh)
    if site == "find_referencing_pr":
        return bg.find_referencing_pr("o/r#5", gh_run=gh)
    if site == "issue_state":
        return bg.issue_state_live("o/r#5", gh_run=gh)
    if site == "pr_linkage":
        return bg._pr_linkage("o/r", 7, gh_run=gh)
    if site == "issue_signals":
        return bg._issue_signals("o/r#5", gh_run=gh)
    raise AssertionError(site)


_FAILURE_SITES = ["verify_ready", "find_referencing_pr", "issue_state",
                  "pr_linkage", "issue_signals"]


def _gh_for(site: str, stderr: str) -> FakeGh:
    """A FakeGh that fails the one gh call *site* makes, with *stderr*."""
    key = ("issue", "view") if site == "issue_state" else ("api", "graphql")
    return FakeGh({key: _proc(returncode=1, stderr=stderr)})


@pytest.mark.parametrize("site", _FAILURE_SITES)
def test_gh_edge_timeout_is_info_not_warning(site, caplog):
    with _app_tokens(), caplog.at_level("INFO", logger="server.automations.backlog_github"):
        assert _failing_site(site, _gh_for(site, _EDGE_504)) is None
    records = [r for r in caplog.records if r.name == "server.automations.backlog_github"]
    assert records, "the failure should still be logged"
    assert all(r.levelno < 30 for r in records), [r.getMessage() for r in records]


@pytest.mark.parametrize("site", _FAILURE_SITES)
def test_other_gh_failure_is_still_a_warning(site, caplog):
    with _app_tokens(), caplog.at_level("INFO", logger="server.automations.backlog_github"):
        assert _failing_site(site, _gh_for(site, "boom")) is None
    warnings = [r for r in caplog.records
                if r.name == "server.automations.backlog_github" and r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
