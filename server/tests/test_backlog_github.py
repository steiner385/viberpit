"""GitHub I/O for the Backlog Runner — candidate query + atomic claim/unclaim/reconcile.

Every gh call is routed through the injected ``gh_run`` seam, so these tests use
``FakeGh`` and never touch the network.
"""
from __future__ import annotations

import json
import subprocess

from server.automations.backlog_github import (
    claim_batch,
    label_needs_human,
    query_candidates,
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
        {"number": 7, "title": "Fix login", "repository": {"nameWithOwner": "o/r"}},
        {"number": 12, "title": "Add search", "repository": {"nameWithOwner": "o/r"}},
    ]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})
    config = {"enabled_repos": ["o/r"], "eligible_label": "ready"}

    out = query_candidates(config, gh_run=fake)

    # The graphql read happened.
    assert fake.calls, "expected a gh api graphql call"
    argv = fake.calls[0]
    assert argv[:2] == ["api", "graphql"]
    query = next(a for a in argv if a.startswith("query="))
    # Carries the eligible label and excludes already-claimed / needs-human issues.
    assert "label:ready" in query or 'label:"ready"' in query
    assert "-label:in-progress" in query or '-label:"in-progress"' in query
    assert "-label:needs-human" in query or '-label:"needs-human"' in query
    # Scoped to the enabled repo, open, unassigned issues.
    assert "repo:o/r" in query
    assert "is:open" in query
    assert "no:assignee" in query

    # Parsed into the candidate shape rank_and_batch consumes.
    assert out == [
        {"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix login"},
        {"ref": "o/r#12", "number": 12, "repo": "o/r", "title": "Add search"},
    ]


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
    nodes = [{"number": 7, "title": "Fix login", "repository": {"nameWithOwner": "o/r"}}]
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout(nodes))})

    out = query_candidates({"enabled_repos": ["o/r"], "eligible_label": ""}, gh_run=fake)

    query = next(a for a in fake.calls[0] if a.startswith("query="))
    assert "label:ready" not in query          # no silent default
    assert "label:\\\"ready\\\"" not in query
    # No positive label gate at all (the only label quals are the exclusions).
    assert "label:in-progress" in query and "label:needs-human" in query
    assert " label:" not in query.replace("-label:", "")  # no bare `label:<x>` gate
    # Other gates still present; the issue is still parsed.
    assert "no:assignee" in query and "is:open" in query
    assert out == [{"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix login"}]


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
