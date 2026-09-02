import contextlib
import json

import pytest

from ccswitch import gitrepos
from server.github.pr_poller import PrStatusPoller
from server.github.pr_status import PullRequestInfo

PAIRS = [("o/r", "br")]


def _good_query(pairs):
    return {"data": {"r0": {"pullRequests": {"nodes": [{
        "number": 5, "url": "https://github.com/o/r/pull/5", "title": "x",
        "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-22T10:00:00Z",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }]}}}}


@pytest.mark.asyncio
async def test_refresh_populates_cache():
    p = PrStatusPoller(query_fn=_good_query)
    assert await p.refresh_once(PAIRS) is True
    info = p.cached("o/r", "br")
    assert isinstance(info, PullRequestInfo) and info.number == 5 and info.state == "open"


@pytest.mark.asyncio
async def test_cached_none_for_unknown_or_null():
    p = PrStatusPoller(query_fn=_good_query)
    assert p.cached("o/r", "br") is None        # nothing fetched yet
    assert p.cached(None, "br") is None
    assert p.cached("o/r", None) is None


@pytest.mark.asyncio
async def test_refresh_failure_keeps_cache():
    p = PrStatusPoller(query_fn=_good_query)
    await p.refresh_once(PAIRS)

    def boom(pairs):
        raise RuntimeError("gh down")

    p._query_fn = boom
    assert await p.refresh_once(PAIRS) is False
    # Last-known value survives a failed refresh.
    assert p.cached("o/r", "br").number == 5


@pytest.mark.asyncio
async def test_refresh_empty_pairs_is_noop_success():
    p = PrStatusPoller(query_fn=_good_query)
    assert await p.refresh_once([]) is True


@pytest.mark.asyncio
async def test_loop_accepts_async_pairs_provider():
    p = PrStatusPoller(query_fn=_good_query)
    async def async_pairs():
        return PAIRS
    p.set_pairs_provider(async_pairs)
    assert await p._resolve_pairs() == PAIRS

@pytest.mark.asyncio
async def test_resolve_pairs_handles_sync_provider():
    p = PrStatusPoller(query_fn=_good_query)
    p.set_pairs_provider(lambda: PAIRS)
    assert await p._resolve_pairs() == PAIRS


# ---------------------------------------------------------------------------
# Task 1 — on_change callback tests (M2b.2)
# ---------------------------------------------------------------------------

def _make_query(checks: str):
    """Return a query_fn that always returns a PR with the given checks value."""
    rollup = {"passing": "SUCCESS", "failing": "FAILURE", "pending": "PENDING"}.get(checks)
    rollup_node = {"state": rollup} if rollup else None

    def _query(pairs):
        return {"data": {"r0": {"pullRequests": {"nodes": [{
            "number": 1, "url": "https://github.com/o/r/pull/1", "title": "t",
            "state": "OPEN", "isDraft": False, "updatedAt": "2026-06-24T10:00:00Z",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup_node}}]},
        }]}}}}

    return _query


@pytest.mark.asyncio
async def test_on_change_callback_fires_on_checks_transition():
    """Second refresh with changed checks calls on_change with (repo, branch, old, new)."""
    p = PrStatusPoller(query_fn=_make_query("passing"))
    calls = []

    def cb(repo, branch, old_checks, new_checks):
        calls.append((repo, branch, old_checks, new_checks))

    p.set_on_change(cb)

    # First refresh — cache was empty, old=None → should fire with old=None
    await p.refresh_once(PAIRS)
    assert len(calls) == 1
    assert calls[0] == ("o/r", "br", None, "passing")

    # Second refresh with same checks — no change → no call
    await p.refresh_once(PAIRS)
    assert len(calls) == 1  # still 1

    # Third refresh with different checks → fires
    p._query_fn = _make_query("failing")
    await p.refresh_once(PAIRS)
    assert len(calls) == 2
    assert calls[1] == ("o/r", "br", "passing", "failing")

    # Fourth refresh with same (failing) → no call
    await p.refresh_once(PAIRS)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_on_change_callback_exception_does_not_break_refresh():
    """A callback that raises must not prevent refresh_once from returning True."""
    p = PrStatusPoller(query_fn=_make_query("passing"))

    def boom(repo, branch, old_checks, new_checks):
        raise RuntimeError("callback exploded")

    p.set_on_change(boom)

    result = await p.refresh_once(PAIRS)
    assert result is True
    # Cache should still be populated despite the callback error
    info = p.cached("o/r", "br")
    assert info is not None and info.checks == "passing"


# ---------------------------------------------------------------------------
# on_cycle sweep hook — fires once per poll cycle AFTER refresh_once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_on_cycle_stores_and_fire_invokes():
    """set_on_cycle stores the fn; _fire_on_cycle invokes it (sync + async)."""
    p = PrStatusPoller(query_fn=_good_query)
    calls = []
    p.set_on_cycle(lambda: calls.append("sync"))
    assert p._on_cycle is not None
    await p._fire_on_cycle()
    assert calls == ["sync"]

    async def async_cb():
        calls.append("async")

    p.set_on_cycle(async_cb)
    await p._fire_on_cycle()
    assert calls == ["sync", "async"]


@pytest.mark.asyncio
async def test_fire_on_cycle_noop_without_hook():
    """No registered hook → _fire_on_cycle is a harmless no-op."""
    p = PrStatusPoller(query_fn=_good_query)
    await p._fire_on_cycle()  # must not raise


# ---------------------------------------------------------------------------
# Chunked refresh — GitHub's GraphQL endpoint enforces a ~10s execution budget,
# so one document with hundreds of aliased repository() blocks 502/504s. That
# starved the cache permanently: _pr_pairs' self-prune needs cache entries to
# shrink the pair list, so an oversized list could never recover (bootstrap
# deadlock observed in prod at 262 pairs / 200 KB query).
# ---------------------------------------------------------------------------

def _query_for(pairs):
    """query_fn that returns an OPEN PR for every requested pair."""
    data = {}
    for i, (_repo, br) in enumerate(pairs):
        data[f"r{i}"] = {"pullRequests": {"nodes": [{
            "number": i + 1, "url": f"https://github.com/o/r/pull/{i + 1}",
            "title": br, "state": "OPEN", "isDraft": False,
            "updatedAt": "2026-06-22T10:00:00Z",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        }]}}
    return {"data": data}


@pytest.mark.asyncio
async def test_refresh_chunks_large_pair_lists():
    """A pair list larger than chunk_size is split across multiple queries."""
    calls = []

    def qfn(pairs):
        calls.append(len(pairs))
        return _query_for(pairs)

    p = PrStatusPoller(query_fn=qfn, chunk_size=40)
    pairs = [("o/r", f"feat/b{i}") for i in range(100)]
    assert await p.refresh_once(pairs) is True
    assert calls == [40, 40, 20]
    # Entries from every chunk land in the cache.
    assert p.cached("o/r", "feat/b0") is not None
    assert p.cached("o/r", "feat/b99") is not None


@pytest.mark.asyncio
async def test_partial_chunk_failure_applies_successes_and_backs_off():
    """One failing chunk must not discard the other chunks' fresh data."""
    def qfn(pairs):
        if any(br == "feat/b1" for _, br in pairs):
            raise RuntimeError("gh: HTTP 504")
        return _query_for(pairs)

    p = PrStatusPoller(query_fn=qfn, chunk_size=1)
    pairs = [("o/r", "feat/b0"), ("o/r", "feat/b1"), ("o/r", "feat/b2")]
    assert await p.refresh_once(pairs) is False
    assert p.cached("o/r", "feat/b0") is not None   # earlier chunk applied
    assert p.cached("o/r", "feat/b1") is None        # failed chunk skipped
    assert p.cached("o/r", "feat/b2") is not None   # later chunk still attempted
    assert p._fail_count == 1                        # one backoff step per cycle


def test_default_chunk_size_is_bounded():
    """Default chunk size stays under the empirically-safe 50-pair ceiling."""
    assert 1 <= PrStatusPoller(query_fn=_good_query)._chunk_size <= 50


@pytest.mark.asyncio
async def test_on_cycle_fires_after_refresh_and_reads_fresh_cache():
    """The sweep hook runs after refresh_once, so it sees the fresh cache."""
    p = PrStatusPoller(query_fn=_good_query)
    seen = []

    async def sweep():
        # Reads the cache the poller just refreshed.
        seen.append(p.cached("o/r", "br"))

    p.set_on_cycle(sweep)
    await p.refresh_once(PAIRS)
    await p._fire_on_cycle()
    assert len(seen) == 1
    assert seen[0] is not None and seen[0].number == 5


@pytest.mark.asyncio
async def test_on_cycle_exception_is_swallowed():
    """A raising sweep hook must not propagate (poll loop survives)."""
    p = PrStatusPoller(query_fn=_good_query)

    def boom():
        raise RuntimeError("sweep exploded")

    p.set_on_cycle(boom)
    await p._fire_on_cycle()  # must not raise

    async def aboom():
        raise RuntimeError("async sweep exploded")

    p.set_on_cycle(aboom)
    await p._fire_on_cycle()  # must not raise


def test_default_query_supplements_evicted_envs(monkeypatch):
    """Per-branch default query re-fetches classified envs evicted from the
    shared deployments window — same truncation fix as the population poller,
    merged into EVERY alias of the affected repo (a repo appears once per
    tracked branch)."""
    import json as _json
    from server.github import pr_poller as PP

    def _dep(env, *, status="SUCCESS"):
        return {"latestEnvironment": env, "commitOid": "c",
                "latestStatus": {"state": status},
                "commit": {"committedDate": "2026-07-18T20:00:00Z"}}

    def _node():
        return {
            "pullRequests": {"nodes": []},
            "deployments": {"nodes": [_dep("kindash / qa")]},
            "environments": {"nodes": [{"name": "kindash / qa"},
                                       {"name": "kindash / production"}]},
        }

    main = {"data": {"r0": _node(), "r1": _node()}}
    supp = {"data": {"r0": {"e0": {"nodes": [_dep("kindash / production")]}}}}
    calls = []

    def fake_run(args, token=None, input=None):
        calls.append(input)
        out = _json.dumps(main if len(calls) == 1 else supp)
        class R: returncode = 0; stdout = out; stderr = ""
        return R()

    monkeypatch.setattr(PP.gitrepos, "_run_gh", fake_run)
    pairs = [("cairnea/KinDash", "br-a"), ("cairnea/KinDash", "br-b")]
    resp = PP._default_query(pairs)
    assert len(calls) == 2
    for alias in ("r0", "r1"):
        envs = [d["latestEnvironment"]
                for d in resp["data"][alias]["deployments"]["nodes"]]
        assert "kindash / production" in envs


# --------------------------------------------------------------------------- #
# _default_query — per-owner App auth                                          #
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _app_tokens(mapping: dict | None = None):
    """Register a fake App token provider (owner → "tok-<owner>") for the test."""
    fn = (lambda owner: mapping.get(owner)) if mapping is not None else (lambda owner: f"tok-{owner}")
    gitrepos.set_token_provider(fn)
    try:
        yield
    finally:
        gitrepos.set_token_provider(None)


def _empty_pr_node():
    return {"pullRequests": {"nodes": []}, "deployments": {"nodes": []},
            "environments": {"nodes": []}}


def _ok_run(node, *, alias="r0"):
    """Return a fake _run_gh that succeeds with a single aliased node."""
    def _run(args, token=None, input=None):
        class R:
            returncode = 0
            stdout = json.dumps({"data": {alias: node}})
            stderr = ""
        return R()
    return _run


def test_default_query_passes_owner_app_token(monkeypatch):
    """Single owner: the gh call carries that owner's App installation token."""
    from server.github import pr_poller as PP

    tokens = []

    def fake_run(args, token=None, input=None):
        tokens.append(token)
        class R:
            returncode = 0
            stdout = json.dumps({"data": {"r0": _empty_pr_node()}})
            stderr = ""
        return R()

    monkeypatch.setattr(PP.gitrepos, "_run_gh", fake_run)
    with _app_tokens():
        PP._default_query([("acme/repo", "main")])

    assert tokens == ["tok-acme"]


def test_default_query_splits_per_owner(monkeypatch):
    """Two owners → two gh calls with distinct per-owner tokens; local r0 aliases
    are remapped back to the global pair positions r0 and r1."""
    from server.github import pr_poller as PP

    tokens = []

    def fake_run(args, token=None, input=None):
        tokens.append(token)
        class R:
            returncode = 0
            stdout = json.dumps({"data": {"r0": _empty_pr_node()}})
            stderr = ""
        return R()

    monkeypatch.setattr(PP.gitrepos, "_run_gh", fake_run)
    with _app_tokens():
        resp = PP._default_query([("acme/repo", "main"), ("other/repo", "main")])

    assert len(tokens) == 2
    assert set(tokens) == {"tok-acme", "tok-other"}
    # Each owner's local r0 must be remapped to its global position.
    assert "r0" in resp["data"]
    assert "r1" in resp["data"]


def test_default_query_tolerates_one_owner_failing(monkeypatch):
    """A failing owner's call is logged and skipped; healthy owners still return data."""
    from server.github import pr_poller as PP

    def fake_run(args, token=None, input=None):
        if token == "tok-bad":
            class Fail:
                returncode = 1; stdout = ""; stderr = "403 Forbidden"
            return Fail()
        class R:
            returncode = 0
            stdout = json.dumps({"data": {"r0": _empty_pr_node()}})
            stderr = ""
        return R()

    monkeypatch.setattr(PP.gitrepos, "_run_gh", fake_run)
    with _app_tokens({"acme": "tok-acme", "bad": "tok-bad"}):
        resp = PP._default_query([("acme/repo", "main"), ("bad/repo", "main")])

    # acme (global idx 0) succeeded; bad (global idx 1) failed → only r0 present.
    assert "r0" in resp["data"]
    assert "r1" not in resp["data"]
