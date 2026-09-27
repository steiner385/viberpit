"""A stored repo slug must be GitHub's canonical ``nameWithOwner``, not an alias.

GitHub resolves ``repository(owner:, name:)`` through renames, so a query for a
pre-rename slug still SUCCEEDS and returns the canonical repository. Nothing
404s — which is exactly why a stale alias survives unnoticed and silently
doubles every string-keyed lane downstream. the alias and the canonical name are one repository, but pr-doctor's job key is
``"owner/repo#N"``, so both slugs in ``enabled_repos`` spawned two concurrent
sessions on the same pull request.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from server.github import repo_canon


_FIELD = re.compile(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


def _gh_ok(mapping: dict[str, str]):
    """A fake ``gh`` that answers the query it was actually given.

    It parses the aliases out of the document rather than assuming an ordering,
    so alias numbering stays a detail of the code under test. A slug absent from
    *mapping* answers ``null``, which is how GitHub reports a repo it cannot
    resolve. Records every invocation so a test can assert the call COUNT.
    """
    calls: list[list[str]] = []

    def run(args, **kw):
        calls.append(list(args))
        query = kw.get("input") or ""
        data = {}
        for alias, owner, name in _FIELD.findall(query):
            canonical = mapping.get(f"{owner}/{name}")
            data[alias] = {"nameWithOwner": canonical} if canonical else None

        class P:
            returncode = 0
            stdout = json.dumps({"data": data})
            stderr = ""

        return P()

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_alias_slug_resolves_to_its_canonical_name():
    gh = _gh_ok({"acme/old-name": "acme/new-name"})
    assert repo_canon.canonical_slugs(["acme/old-name"], gh_run=gh) == {
        "acme/old-name": "acme/new-name"
    }


def test_canonicalize_repo_list_rewrites_the_alias_and_drops_the_duplicate():
    """The alias and the canonical name in one list are ONE repo, and the
    canonical name keeps the alias's position so the operator's ordering
    survives a rename."""
    gh = _gh_ok({
        "acme/old-name": "acme/new-name",
        "cairnea/KinDash": "cairnea/KinDash",
        "acme/new-name": "acme/new-name",
    })
    out = repo_canon.canonicalize_repo_list(
        ["acme/old-name", "cairnea/KinDash", "acme/new-name"], gh_run=gh
    )
    assert out == ["acme/new-name", "cairnea/KinDash"]


def test_many_slugs_cost_one_batched_query():
    """N slugs must not mean N subprocesses — the shared 5,000-req/hr limit is
    the reason this is batched (repo norm: GraphQL for reads)."""
    gh = _gh_ok({f"o/n{i}": f"o/n{i}" for i in range(12)})
    repo_canon.canonical_slugs([f"o/n{i}" for i in range(12)], gh_run=gh)
    assert len(gh.calls) == 1  # type: ignore[attr-defined]


def test_unreachable_github_leaves_every_slug_untouched():
    """Fail OPEN: a config write must never be blocked, and a list must never be
    emptied, because GitHub was briefly unreachable."""
    def boom(args, **kw):
        raise OSError("network down")

    repos = ["acme/old-name", "cairnea/KinDash"]
    assert repo_canon.canonicalize_repo_list(repos, gh_run=boom) == repos


def test_a_slug_github_cannot_resolve_is_kept_verbatim():
    """A typo'd or deleted repo answers ``null``. Keep the operator's text —
    dropping it would silently disable an automation for that repo."""
    gh = _gh_ok({"cairnea/KinDash": "cairnea/KinDash"})
    out = repo_canon.canonicalize_repo_list(
        ["steiner385/does-not-exist", "cairnea/KinDash"], gh_run=gh
    )
    assert out == ["steiner385/does-not-exist", "cairnea/KinDash"]


@pytest.mark.parametrize("bad", ["no-slash", "a/b/c"])
def test_a_malformed_slug_is_kept_but_never_sent_to_github(bad):
    """``owner/name`` or nothing — an unsplittable value would build a broken
    query and fail the whole batch, taking the valid slugs down with it. It is
    still the operator's text, so it survives unchanged."""
    gh = _gh_ok({"cairnea/KinDash": "cairnea/KinDash"})
    out = repo_canon.canonicalize_repo_list([bad, "cairnea/KinDash"], gh_run=gh)
    assert out == [bad, "cairnea/KinDash"]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_entry_is_dropped(blank):
    gh = _gh_ok({"cairnea/KinDash": "cairnea/KinDash"})
    assert repo_canon.canonicalize_repo_list(
        [blank, "cairnea/KinDash"], gh_run=gh
    ) == ["cairnea/KinDash"]


def test_repeated_lookup_failures_are_throttled_not_refiled(caplog):
    """A still-unreachable ``gh`` is one standing condition. Re-logging a fresh
    WARNING on every retry (a 120s polling loop hits this constantly) is what
    turned one transient rate-limit window into a stream of auto-error issues
    (#2513) — the second call within the throttle window must demote to DEBUG."""
    def boom(args, **kw):
        raise OSError("network down")

    repo_canon._LOOKUP_FAILED_THROTTLE.clear()
    with caplog.at_level(logging.DEBUG, logger="server.github.repo_canon"):
        repo_canon.canonical_slugs(["acme/old-name"], gh_run=boom)
        repo_canon.canonical_slugs(["acme/old-name"], gh_run=boom)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert len(debugs) == 1
