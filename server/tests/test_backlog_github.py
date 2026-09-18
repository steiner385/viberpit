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

from ccswitch import gitrepos
from server.automations.backlog_github import (
    announce_claim,
    announce_needs_human,
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
    signals, PR files, the issue close) is scoped to the batch's repo ``o/r`` —
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
# close_batch_issues — human gate: action-required issues & docs-only PRs (#784)#
# --------------------------------------------------------------------------- #

def _signals_stdout(title="", body="", labels=(), sub_issue_states=()) -> str:
    """An issue-signals GraphQL response: title + body + labels + sub-issues."""
    return json.dumps({"data": {"repository": {"issue": {
        "title": title, "body": body,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "subIssues": {"nodes": [{"state": s} for s in sub_issue_states]}}}}})


def _files_stdout(paths) -> str:
    """A PR changed-files GraphQL response."""
    return json.dumps({"data": {"repository": {"pullRequest": {
        "files": {"nodes": [{"path": p} for p in paths]}}}}})


class GateGh:
    """Routes the four graphql shapes close_batch_issues issues once linkage is
    verified: PR linkage (``closingIssuesReferences``), PR files (``files(``),
    per-issue signals (``issue(number: N)`` with ``labels(``), and the timeline
    cross-ref (``timelineItems``). Non-graphql calls succeed."""

    def __init__(self, *, pr, files=None, signals=None):
        self.calls: list[list[str]] = []
        self.tokens: list[str | None] = []
        self._pr = pr
        self._files = files
        self._signals = dict(signals or {})  # issue number -> proc

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        self.tokens.append(token)
        if args[:2] != ["api", "graphql"]:
            return _proc()
        q = args[-1]
        if "pullRequest(" in q and "files(" in q:
            return self._files or _proc(stdout=_files_stdout([]))
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
        files=_proc(stdout=_files_stdout(["src/oauth.py"])),  # even a CODE PR
        signals={8: _proc(stdout=_signals_stdout(
            title="Remove Classroom scopes on the GCP consent screen"))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "escalated"
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
        files=_proc(stdout=_files_stdout(["src/reconcile.py"])),
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
        files=_proc(stdout=_files_stdout(["src/reconcile.py"])),
        signals={8: _proc(stdout=_signals_stdout(
            title="Epic: done", sub_issue_states=["CLOSED", "CLOSED"]))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    assert [c for c in gh.calls if c[:2] == ["issue", "close"]]


def test_close_batch_issues_docs_only_pr_does_not_satisfy_non_doc_issue():
    """A docs-only diff (runbook/guide) must not close a non-documentation
    issue — a guide for X is not doing X (#784). Escalate to a human instead."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        files=_proc(stdout=_files_stdout(["docs/runbook.md", "README.md"])),
        signals={8: _proc(stdout=_signals_stdout(title="Rotate the signing key"))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "escalated"
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    body = next(c for c in gh.calls if c[:2] == ["issue", "comment"])
    assert "only documentation" in body[body.index("--body") + 1]


def test_close_batch_issues_docs_only_pr_closes_a_documentation_issue():
    """A docs-only PR legitimately satisfies a documentation issue — the gate
    must not over-fire and block real doc work."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        files=_proc(stdout=_files_stdout(["docs/setup.md"])),
        signals={8: _proc(stdout=_signals_stdout(
            title="Document the setup flow", labels=["documentation"]))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert [c[2] for c in closes] == ["8"]


def test_close_batch_issues_code_pr_normal_issue_still_closes():
    """The happy path is unchanged: a code PR for an ordinary code issue closes
    it — the human gate only fires on action issues or docs-only diffs."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        files=_proc(stdout=_files_stdout(["src/sweep.py", "server/tests/test_sweep.py"])),
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
        files=_proc(stdout=_files_stdout(["src/x.py"])),
        signals={8: _proc(returncode=1, stderr="boom")})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"
    assert [c for c in gh.calls if c[:2] == ["issue", "close"]]


# --------------------------------------------------------------------------- #
# Action-required / docs-only predicates (#784)                                #
# --------------------------------------------------------------------------- #

def test_is_action_required_matches_phrases_and_labels():
    from server.automations.backlog_github import _issue_is_action_required
    assert _issue_is_action_required({"title": "Verify domain in Search Console",
                                      "body": "", "labels": []})
    assert _issue_is_action_required({"title": "Cleanup",
                                      "body": "Record a demo video and upload it",
                                      "labels": []})
    assert _issue_is_action_required({"title": "misc", "body": "",
                                      "labels": ["action-required"]})
    assert not _issue_is_action_required({"title": "Fix null deref",
                                         "body": "guard the pointer", "labels": []})


def test_is_docs_only_classifies_paths():
    from server.automations.backlog_github import _is_docs_only
    assert _is_docs_only(["docs/a.md", "README.md", "web/docs/b.mdx"]) is True
    assert _is_docs_only(["docs/a.md", "src/main.py"]) is False
    assert _is_docs_only([]) is None          # unknown, not "docs-only"
    assert _is_docs_only(None) is None


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
    assert [c[2] for c in calls] == ["7", "9"]

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
    # The comment must name a board value that EXISTS today: the Status option is
    # still "Needs human" (the rename to "Held" is a later item in the series), and
    # it must list the three release signals (spec §7).
    assert "Board Status is Needs human" in text and "Held" not in text
    assert "Ready or Backlog" in text and "Retry" in text
    assert not any("label" in a for c in gh.calls for a in c)
