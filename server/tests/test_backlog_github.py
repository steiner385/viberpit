"""GitHub I/O for the Backlog Runner — pre-dispatch verification, atomic
claim/unclaim/reconcile, and the per-owner auth routing they share.

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
    claim_batch,
    ensure_claim_labels,
    label_needs_human,
    query_claimed_refs,
    heal_orphan_claims,
    reconcile_dangling,
    unclaim_batch,
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
# _graphql_per_owner — one call per owner, per-owner failure isolation          #
# --------------------------------------------------------------------------- #

class OwnerRoutedGh:
    """Routes graphql calls by which owner's repo the query targets, returning a
    distinct node set per owner and recording (args, token) per call — used to
    verify multi-owner grouping issues exactly one call per owner with that
    owner's token, and merges every owner's results."""

    def __init__(self, nodes_by_owner: dict[str, list[dict]], *, data_key: str = "claimed"):
        self.calls: list[list[str]] = []
        self.tokens: list[str | None] = []
        self._nodes_by_owner = nodes_by_owner
        self._data_key = data_key

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        self.tokens.append(token)
        query = next(a for a in args if a.startswith("query="))
        for owner, nodes in self._nodes_by_owner.items():
            if f"repo:{owner}/" in query:
                return _proc(stdout=json.dumps({"data": {self._data_key: {"nodes": nodes}}}))
        raise AssertionError(f"query did not target a known owner: {query}")


def test_per_owner_grouping_issues_one_call_per_owner_and_merges_results():
    """Repos spanning two owners must never send one owner's token against
    another owner's repo (it would 404) — group by owner, one call per owner,
    each with that owner's own token, merged into one result."""
    gh = OwnerRoutedGh({
        "acme": [{"number": 1, "repository": {"nameWithOwner": "acme/r1"}}],
        "other": [{"number": 2, "repository": {"nameWithOwner": "other/r2"}}],
    })
    with _app_tokens():
        out = query_claimed_refs(["acme/r1", "other/r2"], gh_run=gh)

    assert len(gh.calls) == 2, "one call per owner, not one call spanning both"
    assert set(gh.tokens) == {"tok-acme", "tok-other"}
    for call_args, token in zip(gh.calls, gh.tokens):
        query = next(a for a in call_args if a.startswith("query="))
        if "repo:acme/r1" in query:
            assert token == "tok-acme"
            assert "repo:other/r2" not in query
        else:
            assert "repo:other/r2" in query
            assert token == "tok-other"
            assert "repo:acme/r1" not in query
    assert set(out) == {"acme/r1#1", "other/r2#2"}


class OwnerFailureGh:
    """Routes graphql calls by owner; the named ``failing_owner`` gets a
    non-zero-returncode failure, every other owner gets its scripted nodes —
    used to verify ``_graphql_per_owner``'s per-owner failure isolation (one
    bad owner must skip-and-continue, never blank out the rest)."""

    def __init__(self, nodes_by_owner: dict[str, list[dict]], *, failing_owner: str,
                 data_key: str = "claimed"):
        self.calls: list[list[str]] = []
        self._nodes_by_owner = nodes_by_owner
        self._failing_owner = failing_owner
        self._data_key = data_key

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        query = next(a for a in args if a.startswith("query="))
        if f"repo:{self._failing_owner}/" in query:
            return _proc(returncode=1, stderr="boom")
        for owner, nodes in self._nodes_by_owner.items():
            if f"repo:{owner}/" in query:
                return _proc(stdout=json.dumps({"data": {self._data_key: {"nodes": nodes}}}))
        raise AssertionError(f"query did not target a known owner: {query}")


def test_one_owner_gh_failure_does_not_blank_the_other_owners():
    """``_graphql_per_owner`` must tolerate ONE owner's gh call failing while
    other owners succeed: a failing owner is logged and skipped (``continue``),
    not treated as a reason to give up on the whole call (``return []``) —
    which would silently drop every healthy owner's results too. Here that
    would strand every other owner's claims as untracked orphans."""
    gh = OwnerFailureGh({
        "other": [{"number": 2, "repository": {"nameWithOwner": "other/r2"}}],
    }, failing_owner="acme")
    with _app_tokens():
        out = query_claimed_refs(["acme/r1", "other/r2"], gh_run=gh)

    assert set(out) == {"other/r2#2"}


# --------------------------------------------------------------------------- #
# verify_ready_live — spec 003 §7a safety net, fails closed (§10)              #
# --------------------------------------------------------------------------- #

def _ready_issue(*, state: str = "OPEN", assignees: int = 0,
                 blockers: tuple[str, ...] = ()) -> dict:
    return {"state": state, "assignees": {"totalCount": assignees},
            "blockedBy": {"nodes": [{"state": s} for s in blockers]}}


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
    assert "blockedBy(first: 50)" in query and "assignees(first: 1)" in query
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


def test_claim_batch_passes_repo_app_token_to_every_call():
    """Every gh call claim_batch makes (label provisioning, add-label edits, the
    linking comment) is for issues in repo ``o/r`` — every one must carry o's
    installation token, never ambient auth, when an App is configured."""
    fake = FakeGh()
    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")

    with _app_tokens():
        ok = claim_batch(batch, gh_run=fake, comment="claiming")

    assert ok is True
    assert fake.calls, "expected gh calls"
    assert all(tok == "tok-o" for tok in fake.tokens)


def test_ensure_claim_labels_passes_each_repos_own_owner_token():
    """Repos spanning different owners must each get their OWNER's token — never
    one owner's token sent for another owner's repo (it would 404)."""
    fake = FakeGh()
    with _app_tokens():
        ensure_claim_labels(["acme/r1", "other/r2"], gh_run=fake)

    assert fake.calls
    for call_args, token in zip(fake.calls, fake.tokens):
        repo = call_args[call_args.index("--repo") + 1]
        assert token == f"tok-{repo.split('/')[0]}"


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


def test_unclaim_batch_passes_repo_app_token():
    fake = FakeGh()
    batch = Batch(primary="o/r#5", issues=["o/r#5", "o/r#6"], repo="o/r")
    with _app_tokens():
        unclaim_batch(batch, gh_run=fake)
    assert fake.calls
    assert all(tok == "tok-o" for tok in fake.tokens)


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


def test_label_needs_human_passes_repo_app_token():
    fake = FakeGh()
    batch = Batch(primary="o/r#8", issues=["o/r#8", "o/r#9"], repo="o/r")
    with _app_tokens():
        label_needs_human(batch, gh_run=fake)
    assert fake.calls
    assert all(tok == "tok-o" for tok in fake.tokens)


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


def test_reconcile_dangling_passes_repo_app_token():
    fake = FakeGh()
    with _app_tokens():
        reconcile_dangling(["o/r#1", "o/r#2"], {"o/r#1"}, gh_run=fake)
    assert fake.calls
    assert all(tok == "tok-o" for tok in fake.tokens)


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


def test_close_batch_issues_action_required_issue_is_escalated_not_closed():
    """A verifiably-linked PR must NOT auto-close an action/ops issue (a manual
    console step). The #784 incident: the issue is escalated to needs-human and
    left OPEN — a PR can never perform the real-world action."""
    from server.automations.backlog_github import close_batch_issues
    gh = GateGh(
        pr=_proc(stdout=_linkage_stdout(closing=[8])),
        files=_proc(stdout=_files_stdout(["src/oauth.py"])),  # even a CODE PR
        signals={8: _proc(stdout=_signals_stdout(
            title="Remove Classroom scopes on the GCP consent screen"))})

    outcome = close_batch_issues(_batch8(), _merged_info(), gh_run=gh)

    assert outcome == "closed"  # batch handled — some closed, some escalated
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    esc = next(c for c in gh.calls
               if c[:2] == ["issue", "edit"] and "--add-label" in c)
    assert esc[esc.index("--add-label") + 1] == "needs-human"
    assert "--remove-label" in esc and esc[esc.index("--remove-label") + 1] == "in-progress"
    assert [c for c in gh.calls if c[:2] == ["issue", "comment"]]


def test_close_batch_issues_tracking_issue_with_open_sub_issues_is_released_not_closed():
    """A merged PR that references a tracking epic must not close it while any
    sub-issue is still open — the #997 incident: PR #1004 said ``Refs #997``, a
    bare mention counted as linkage, and ten open children were orphaned under
    a closed parent. The epic is unclaimed and left open; it is NOT escalated to
    needs-human either, because nothing about it needs a human — its open
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
    edits = [c for c in gh.calls if c[:2] == ["issue", "edit"]]
    assert edits and all("--add-label" not in c for c in edits)
    assert any("--remove-label" in c and c[c.index("--remove-label") + 1] == "in-progress"
               for c in edits)
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

    assert outcome == "closed"
    assert not [c for c in gh.calls if c[:2] == ["issue", "close"]]
    assert [c for c in gh.calls
            if c[:2] == ["issue", "edit"] and "--add-label" in c
            and c[c.index("--add-label") + 1] == "needs-human"]


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
    assert not [c for c in gh.calls
                if c[:2] == ["issue", "edit"] and "--add-label" in c]


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


def test_query_claimed_refs_passes_owner_app_token_single_owner():
    fake = FakeGh({("api", "graphql"): _proc(stdout=_claimed_stdout([("acme/r", 5)]))})
    with _app_tokens():
        query_claimed_refs(["acme/r"], gh_run=fake)
    assert fake.tokens == ["tok-acme"]


def test_query_claimed_refs_groups_by_owner_and_merges_results():
    """Repos spanning two owners must never share one search — group by owner,
    one call per owner with that owner's token, merged into one ref list."""
    gh = OwnerRoutedGh({
        "acme": [{"number": 5, "repository": {"nameWithOwner": "acme/r1"}}],
        "other": [{"number": 9, "repository": {"nameWithOwner": "other/r2"}}],
    }, data_key="claimed")
    with _app_tokens():
        out = query_claimed_refs(["acme/r1", "other/r2"], gh_run=gh)

    assert len(gh.calls) == 2, "one call per owner, not one call spanning both"
    assert set(gh.tokens) == {"tok-acme", "tok-other"}
    assert set(out) == {"acme/r1#5", "other/r2#9"}


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


def test_heal_orphan_claims_passes_repo_app_token():
    """Every lookup and mutation heal_orphan_claims makes for issue o/r#5 (the
    cross-ref timeline lookup, the close, the label edit) must carry o's App
    installation token."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_crossref_stdout(
        [{"number": 40, "url": "https://github.com/o/r/pull/40", "title": "fix",
          "state": "MERGED", "isDraft": False, "headRefName": "b",
          "autoMergeRequest": None}]))})
    with _app_tokens():
        healed = heal_orphan_claims(["o/r#5"], set(), gh_run=fake)
    assert healed == [("o/r#5", "closed")]
    assert fake.calls
    assert all(tok == "tok-o" for tok in fake.tokens)


def test_heal_orphan_merged_pr_escalates_action_issue_instead_of_closing():
    """The orphan-heal path shares the #784 human gate: a merged PR that landed
    for an action/ops issue is escalated to needs-human, never auto-closed."""
    pr = {"number": 40, "url": "https://github.com/o/r/pull/40", "title": "docs",
          "state": "MERGED", "isDraft": False, "headRefName": "b",
          "autoMergeRequest": None}

    def router(args, token=None):
        router.calls.append(list(args))
        if args[:2] != ["api", "graphql"]:
            return _proc()
        q = args[-1]
        if "timelineItems" in q:
            return _proc(stdout=_crossref_stdout([pr]))
        if "pullRequest(" in q and "files(" in q:
            return _proc(stdout=_files_stdout(["docs/runbook.md"]))
        if "issue(number" in q:
            return _proc(stdout=_signals_stdout(
                title="Submit to Google for verification"))
        return _proc()
    router.calls = []

    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=router)

    assert healed == [("o/r#5", "escalated")]
    assert not [c for c in router.calls if c[:2] == ["issue", "close"]]
    esc = next(c for c in router.calls
               if c[:2] == ["issue", "edit"] and "--add-label" in c)
    assert esc[esc.index("--add-label") + 1] == "needs-human"


def test_heal_orphan_merged_pr_releases_tracking_issue_instead_of_closing():
    """The orphan-heal path shares the open-sub-issues gate: a merged PR that
    mentioned a tracking epic releases the claim and leaves the epic open,
    without a needs-human label."""
    pr = {"number": 40, "url": "https://github.com/o/r/pull/40", "title": "part 1",
          "state": "MERGED", "isDraft": False, "headRefName": "b",
          "autoMergeRequest": None}

    def router(args, token=None):
        router.calls.append(list(args))
        if args[:2] != ["api", "graphql"]:
            return _proc()
        q = args[-1]
        if "timelineItems" in q:
            return _proc(stdout=_crossref_stdout([pr]))
        if "pullRequest(" in q and "files(" in q:
            return _proc(stdout=_files_stdout(["src/x.py"]))
        if "issue(number" in q:
            return _proc(stdout=_signals_stdout(
                title="Epic: rollout", sub_issue_states=["OPEN"]))
        return _proc()
    router.calls = []

    healed = heal_orphan_claims(["o/r#5"], set(), gh_run=router)

    assert healed == [("o/r#5", "released")]
    assert not [c for c in router.calls if c[:2] == ["issue", "close"]]
    edits = [c for c in router.calls if c[:2] == ["issue", "edit"]]
    assert edits and all("--add-label" not in c for c in edits)


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
