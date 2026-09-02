"""GitHub I/O for the Backlog Runner — candidate query + atomic claim/unclaim/reconcile.

Every gh call is routed through the injected ``gh_run`` seam, so these tests use
``FakeGh`` and never touch the network.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from datetime import datetime, timezone
from typing import Callable

from ccswitch import gitrepos
from server.automations.backlog_github import (
    claim_batch,
    ensure_claim_labels,
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


def test_query_candidates_passes_owner_app_token_single_owner():
    """When every enabled repo shares one owner, the single gh call carries that
    owner's installation token (App-authenticated instead of ambient gh)."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout([]))})
    with _app_tokens():
        query_candidates({"enabled_repos": ["acme/r1", "acme/r2"], "eligible_label": ""},
                         gh_run=fake)
    assert fake.calls, "expected a gh api graphql call"
    assert fake.tokens == ["tok-acme"]


def test_query_candidates_no_provider_configured_uses_ambient_auth():
    """No App configured (the default) → token=None on every call, i.e. current
    ambient-gh behaviour is byte-for-byte unchanged."""
    fake = FakeGh({("api", "graphql"): _proc(stdout=_graphql_stdout([]))})
    query_candidates({"enabled_repos": ["acme/r1"], "eligible_label": ""}, gh_run=fake)
    assert fake.tokens == [None]


class OwnerRoutedGh:
    """Routes graphql calls by which owner's repo the query targets, returning a
    distinct node set per owner and recording (args, token) per call — used to
    verify multi-owner grouping issues exactly one call per owner with that
    owner's token, and merges every owner's results."""

    def __init__(self, nodes_by_owner: dict[str, list[dict]], *, data_key: str = "issues"):
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


def test_query_candidates_groups_by_owner_and_merges_results():
    """Enabled repos spanning two owners must never send one owner's token
    against another owner's repo (it would 404) — group by owner, one call per
    owner, each with that owner's own token, merged into one candidate list."""
    gh = OwnerRoutedGh({
        "acme": [{"number": 1, "title": "acme issue",
                  "repository": {"nameWithOwner": "acme/r1"}}],
        "other": [{"number": 2, "title": "other issue",
                   "repository": {"nameWithOwner": "other/r2"}}],
    })
    with _app_tokens():
        out = query_candidates(
            {"enabled_repos": ["acme/r1", "other/r2"], "eligible_label": ""}, gh_run=gh)

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
    assert {c["ref"] for c in out} == {"acme/r1#1", "other/r2#2"}


class OwnerFreshRetryGh:
    """Routes graphql calls by owner, returning that owner's own fresh+retry
    node sets (both pools, per call) — used to verify that grouping the
    candidate query by owner (an AUTH decision) never reorders the product
    invariant that fresh work always outranks a retry."""

    def __init__(self, fresh_retry_by_owner: dict[str, tuple[list[dict], list[dict]]]):
        self.calls: list[list[str]] = []
        self._by_owner = fresh_retry_by_owner

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        query = next(a for a in args if a.startswith("query="))
        for owner, (fresh, retry) in self._by_owner.items():
            if f"repo:{owner}/" in query:
                return _proc(stdout=_graphql_stdout_with_retry(fresh, retry))
        raise AssertionError(f"query did not target a known owner: {query}")


def test_query_candidates_owner_grouping_keeps_fresh_before_retry_globally():
    """Regression for the ordering bug: grouping by owner (an AUTH decision)
    must NOT change scheduling priority. With two owners, each contributing
    one fresh and one retry item, every fresh item across BOTH owners must
    precede every retry item in the merged output — never
    owner1-fresh, owner1-retry, owner2-fresh, owner2-retry, which would let a
    stale needs-human issue in one owner outrank fresh work in another."""
    gh = OwnerFreshRetryGh({
        "acme": (
            [{"number": 1, "title": "acme fresh",
              "repository": {"nameWithOwner": "acme/r1"}}],
            [{"number": 2, "title": "acme retry",
              "repository": {"nameWithOwner": "acme/r1"}}],
        ),
        "other": (
            [{"number": 3, "title": "other fresh",
              "repository": {"nameWithOwner": "other/r2"}}],
            [{"number": 4, "title": "other retry",
              "repository": {"nameWithOwner": "other/r2"}}],
        ),
    })
    config = {"enabled_repos": ["acme/r1", "other/r2"], "eligible_label": "",
              "needs_human_retry_days": 7}

    out = query_candidates(config, gh_run=gh, now=_NOW)

    refs = [c["ref"] for c in out]
    assert set(refs) == {"acme/r1#1", "acme/r1#2", "other/r2#3", "other/r2#4"}
    fresh_refs = {"acme/r1#1", "other/r2#3"}
    retry_refs = {"acme/r1#2", "other/r2#4"}
    last_fresh_index = max(refs.index(r) for r in fresh_refs)
    first_retry_index = min(refs.index(r) for r in retry_refs)
    assert last_fresh_index < first_retry_index, (
        f"a retry item outranked a fresh item across owners: {refs}")


class OwnerFailureGh:
    """Routes graphql calls by owner; the named ``failing_owner`` gets a
    non-zero-returncode failure, every other owner gets its scripted nodes —
    used to verify ``_graphql_per_owner``'s per-owner failure isolation (one
    bad owner must skip-and-continue, never blank out the rest)."""

    def __init__(self, nodes_by_owner: dict[str, list[dict]], *, failing_owner: str,
                 data_key: str = "issues"):
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


def test_query_candidates_one_owner_gh_failure_does_not_blank_others():
    """``_graphql_per_owner`` must tolerate ONE owner's gh call failing while
    other owners succeed: a failing owner is logged and skipped (``continue``),
    not treated as a reason to give up on the whole call (``return []``) —
    which would silently drop every healthy owner's results too."""
    gh = OwnerFailureGh({
        "other": [{"number": 2, "title": "other issue",
                   "repository": {"nameWithOwner": "other/r2"}}],
    }, failing_owner="acme")
    with _app_tokens():
        out = query_candidates(
            {"enabled_repos": ["acme/r1", "other/r2"], "eligible_label": ""}, gh_run=gh)

    assert {c["ref"] for c in out} == {"other/r2#2"}


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

def _signals_stdout(title="", body="", labels=()) -> str:
    """An issue-signals GraphQL response: title + body + labels."""
    return json.dumps({"data": {"repository": {"issue": {
        "title": title, "body": body,
        "labels": {"nodes": [{"name": n} for n in labels]}}}}})


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
