import logging

import pytest
from server.deploy import detect


@pytest.fixture(autouse=True)
def _fresh_verdict_cache():
    """The module-level CI-verdict cache is process-global; isolate every test."""
    detect._verdict_cache.clear()
    yield
    detect._verdict_cache.clear()


class _Flaky:
    """A ``compute`` callable whose behavior the test flips between ticks: assign a
    ``PendingState`` to succeed, or an ``Exception`` instance to raise it."""

    def __init__(self):
        self.mode = None  # PendingState => return it; Exception => raise it

    def __call__(self, repo):
        if isinstance(self.mode, Exception):
            raise self.mode
        return self.mode


class FakeGit:
    def __init__(self, live, remote, commits, resolvable=None):
        self._live, self._remote, self._commits = live, remote, commits
        # SHAs `short_sha` resolves; None => everything resolves (single-repo reality:
        # any sha a process of this repo ever ran exists in the checkout's history).
        self._resolvable = resolvable
        self.fetched = False
        self.log_ranges: list[tuple[str, str]] = []
    def fetch(self, repo, ref="master", run=None): self.fetched = True
    def current_sha(self, repo, run=None): return self._live
    def remote_sha(self, repo, ref="origin/master", run=None): return self._remote
    def short_sha(self, repo, sha, run=None):
        if self._resolvable is not None and sha not in self._resolvable:
            raise RuntimeError(f"unknown revision {sha}")
        return sha[:7]
    def log_range(self, repo, a, b, run=None):
        self.log_ranges.append((a, b))
        return self._commits


def test_up_to_date_is_not_behind():
    g = FakeGit("abc", "abc", [])
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green")
    assert st.behind is False and st.commits == [] and g.fetched is True


def test_behind_lists_commits_and_ci():
    commits = [{"sha": "d1", "subject": "feat x", "author": "t", "iso": "2026-06-24T00:00:00Z"}]
    g = FakeGit("abc", "def", commits)
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "pending")
    assert st.behind is True and st.target_sha == "def"
    assert st.commits[0]["subject"] == "feat x" and st.ci == "pending"


def test_diverged_checkout_is_not_behind():
    # SHAs differ but origin/master carries nothing we lack (log live..target is empty):
    # the deployed checkout is ahead of or diverged from origin/master. There is nothing
    # to deploy, so behind must be False — otherwise the banner shows "Deploy now" with
    # "0 changes pending deploy". CI is never consulted (no commit to gate).
    ci_calls: list[str] = []

    def ci_fn(sha: str) -> str:
        ci_calls.append(sha)
        return "green"

    g = FakeGit("ahead", "behind", [])
    st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
    assert st.behind is False and st.commits == []
    assert st.live_sha == "ahead" and st.target_sha == "behind"
    assert ci_calls == []


# --- liveSha comes from the RUNNING edge, not the deploy checkout (#859) ---------------
#
# `compute_pending` used to derive liveSha from the checkout's HEAD. A deploy that
# fast-forwards the checkout but never restarts the units (2026-08-30: checkout f4977c3,
# edge/daemon still 5516d05) then reported "behind: false" while the deployment was two
# commits stale — the Upgrade banner vanished with no way back short of rewinding the
# checkout. "Live" must mean the sha the running edge reports on /healthz, with the
# checkout HEAD exposed separately as checkoutSha.


def test_live_sha_prefers_edge_report_over_checkout():
    # Checkout already fast-forwarded to B (== origin), but the RUNNING edge still
    # reports A: the deployment IS behind, and the pending range is A..B.
    commits = [{"sha": "bbbbbbb2", "subject": "s", "author": "t", "iso": "i"}]
    g = FakeGit("bbbbbbb2", "bbbbbbb2", commits)
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green",
                                live_sha_fn=lambda: "aaaaaaa1")
    assert st.behind is True
    assert st.live_sha == "aaaaaaa1"
    assert st.checkout_sha == "bbbbbbb2"
    assert g.log_ranges == [("aaaaaaa1", "bbbbbbb2")]
    d = st.to_dict()
    assert d["liveSha"] == "aaaaaaa1" and d["checkoutSha"] == "bbbbbbb2"


def test_live_sha_falls_back_to_checkout_when_edge_unreachable(caplog):
    # Edge down => probe returns "" => fall back to checkout HEAD (the old behavior),
    # loudly: operators must be able to see the signal degraded to the proxy.
    g = FakeGit("abc", "abc", [])
    with caplog.at_level(logging.WARNING):
        st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green",
                                    live_sha_fn=lambda: "")
    assert st.behind is False and st.live_sha == "abc" and st.checkout_sha == "abc"
    assert any("checkout HEAD" in r.getMessage() for r in caplog.records)


def test_short_edge_sha_prefix_matches_full_target():
    # /healthz reports `git rev-parse --short`; git rev-parse gives the full sha. The
    # same commit must compare equal despite the length difference (#788 prefix match),
    # or every tick would claim a freshly-deployed edge is behind.
    full = "f4977c3" + "0" * 33
    g = FakeGit(full, full, [{"sha": "x"}])
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green",
                                live_sha_fn=lambda: "f4977c3")
    assert st.behind is False and st.commits == []
    assert g.log_ranges == [], "prefix-equal shas must short-circuit before git log"


def test_unresolvable_edge_sha_falls_back_to_checkout(caplog):
    # An edge sha that doesn't resolve in the checkout (garbage body, GC'd commit)
    # must not wedge the poll loop into persistent failure — fall back, loudly.
    g = FakeGit("abc", "abc", [], resolvable={"abc"})
    with caplog.at_level(logging.WARNING):
        st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green",
                                    live_sha_fn=lambda: "deadbee")
    assert st.behind is False and st.live_sha == "abc"
    assert any("does not resolve" in r.getMessage() for r in caplog.records)


def test_default_compute_pending_keeps_checkout_head():
    # Without a live_sha_fn (CLI callers, existing tests) behavior is unchanged:
    # liveSha == checkoutSha == checkout HEAD.
    g = FakeGit("abc", "def", [{"sha": "d1", "subject": "s", "author": "t", "iso": "i"}])
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green")
    assert st.live_sha == "abc" and st.checkout_sha == "abc"


def test_edge_live_sha_parses_healthz_body():
    ok = '{"status":"ok","sha":"5516d05","daemonSha":"5516d05"}'
    assert detect.edge_live_sha("http://h", fetch=lambda url: ok) == "5516d05"


@pytest.mark.parametrize("body", [
    "",                       # edge unreachable (fetch error path returns "")
    "not json",               # proxy error page
    "[1,2]",                  # parseable but not an object
    '{"status":"ok"}',        # sha key absent (edge too old to report it)
    '{"sha":"unknown"}',      # source tarball / no .git — not a usable sha
])
def test_edge_live_sha_unusable_bodies_yield_empty(body):
    assert detect.edge_live_sha("http://h", fetch=lambda url: body) == ""


# --- green frontier: deploy the newest GREEN commit when the tip is red ----------------
#
# `ci` keeps meaning "what the UI's Deploy button can act on"; `tip_ci` is the tip's own
# verdict. When the tip is not green, the pending commits are walked newest→oldest for the
# first green one (`deployable_sha`, covering `deployable_count` of the pending commits).


def _commits(*shas):
    return [{"sha": s, "subject": f"s {s}", "author": "t", "iso": "i"} for s in shas]


def _counting_ci(verdicts, default="red"):
    calls: list[str] = []

    def ci_fn(sha: str) -> str:
        calls.append(sha)
        return verdicts.get(sha, default)
    return ci_fn, calls


def test_red_tip_with_an_older_green_is_deployable():
    # Pending (newest first): c3(red tip), c2(green), c1 — the frontier is c2 and it
    # covers 2 of the 3 pending commits (c2 and c1).
    g = FakeGit("d0", "c3", _commits("c3", "c2", "c1"))
    ci_fn, calls = _counting_ci({"c3": "red", "c2": "green"})
    st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
    assert st.behind is True
    assert st.tip_ci == "red"
    assert st.ci == "green", "a deployable frontier exists — the UI can act"
    assert st.deployable_sha == "c2"
    assert st.deployable_count == 2
    assert calls == ["c3", "c2"], "the walk stops at the first green"
    d = st.to_dict()
    assert d["tipCi"] == "red" and d["deployableSha"] == "c2" and d["deployableCount"] == 2


def test_all_red_pending_has_no_deployable():
    g = FakeGit("d0", "c3", _commits("c3", "c2", "c1"))
    ci_fn, calls = _counting_ci({})
    st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
    assert st.ci == "red" and st.tip_ci == "red"
    assert st.deployable_sha == "" and st.deployable_count == 0
    assert calls == ["c3", "c2", "c1"]


def test_green_tip_behaviour_unchanged_and_covers_everything():
    g = FakeGit("d0", "c3", _commits("c3", "c2", "c1"))
    ci_fn, calls = _counting_ci({}, default="green")
    st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
    assert st.behind is True and st.ci == "green" and st.tip_ci == "green"
    assert st.deployable_sha == "c3" and st.deployable_count == 3
    assert calls == ["c3"], "a green tip never walks the backlog"


def test_green_walk_is_capped():
    # A deep all-red backlog must not burn one GitHub round-trip per commit: the walk
    # is capped at 10 lookups beyond the tip.
    shas = [f"c{i:02d}" for i in range(20, 0, -1)]     # c20 (tip) .. c01
    g = FakeGit("d0", "c20", _commits(*shas))
    ci_fn, calls = _counting_ci({})
    st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
    assert st.deployable_sha == ""
    assert len(calls) == 11, "tip + at most 10 walked lookups"


def test_completed_verdicts_are_cached_across_ticks():
    # green/red are immutable for a given sha: the second tick must serve both from
    # the cache — ci_fn is called once per sha across ticks, not once per tick.
    ci_fn, calls = _counting_ci({"c2": "green"})       # c3 red, c2 green
    for _ in range(2):
        g = FakeGit("d0", "c3", _commits("c3", "c2", "c1"))
        st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
        assert st.deployable_sha == "c2" and st.tip_ci == "red"
    assert calls == ["c3", "c2"]


def test_pending_verdicts_are_requeried_every_tick():
    ci_fn, calls = _counting_ci({}, default="pending")
    for _ in range(2):
        g = FakeGit("d0", "c1", _commits("c1"))
        st = detect.compute_pending("/r", git_mod=g, ci_fn=ci_fn)
        assert st.ci == "pending" and st.tip_ci == "pending"
    assert calls == ["c1", "c1"], "a non-terminal verdict must be re-checked"


def test_verdict_cache_is_bounded():
    for i in range(detect._VERDICT_CACHE_MAX + 8):
        detect._cached_ci(f"s{i}", lambda s: "green")
    assert len(detect._verdict_cache) == detect._VERDICT_CACHE_MAX


@pytest.mark.asyncio
async def test_detect_loop_tick_updates_state():
    g = FakeGit("a", "b", [{"sha": "1", "subject": "s", "author": "t", "iso": "i"}])
    loop = detect.DetectLoop("/r", interval_s=0,
                             compute=lambda repo: detect.compute_pending(repo, git_mod=g, ci_fn=lambda s: "green"))
    await loop.tick()
    assert loop.state.behind is True and loop.state.to_dict()["commits"][0]["sha"] == "1"


def test_detect_loop_default_interval_is_30s():
    # Snappy banner: the notify-only poll defaults to 30s (was 120s).
    assert detect.DetectLoop("/r").interval_s == 30.0


class HistoryGit:
    """A git fake backed by a chronological history list, so ``log_range(live, target)``
    returns the *true* ``live..target`` range (newest-first, exclusive of ``live``) as the
    remote tip advances — unlike ``FakeGit`` which returns a static commit list."""

    def __init__(self, live, remote, history, isos):
        self.live, self.remote, self._history, self._isos = live, remote, history, isos

    def fetch(self, repo, ref="master", run=None): pass
    def current_sha(self, repo, run=None): return self.live
    def remote_sha(self, repo, ref="origin/master", run=None): return self.remote

    def log_range(self, repo, a, b, run=None):
        ai, bi = self._history.index(a), self._history.index(b)
        return [{"sha": s, "subject": f"work {s}", "author": "t", "iso": self._isos[s]}
                for s in reversed(self._history[ai + 1:bi + 1])]


def test_oldest_undeployed_commit_anchors_pending_and_never_resets():
    """Regression guard for #163 — auto-deploy starvation via per-tip defer reset.

    The retired ``scripts/auto-deploy.sh`` persisted a ``<sha> <first-deferred-epoch>``
    defer file and rewrote the epoch every time the pending tip changed, so *every new
    master commit reset the 30-minute force-cap* — on a busy repo with a steady commit
    stream a merged, CI-green fix could defer forever and never ship (observed 2026-06-23,
    #148's fix stuck for hours).

    The user-controlled controller keeps no such persisted timer: ``compute_pending()``
    re-derives the whole undeployed range from ``git log live..target`` on every tick, so
    the OLDEST undeployed commit — ``commits[-1]``, the "pending since" anchor any staleness
    measure must key off — is stable as newer commits land instead of resetting to the tip.
    This locks the acceptance criterion ("within MAX_DEFER of the *first* undeployed commit,
    regardless of later commits") against the per-tip reset ever creeping back in.
    """
    history = ["d0", "c1", "c2", "c3"]  # chronological; d0 is the deployed checkout
    isos = {"c1": "2026-06-23T00:00:00Z",
            "c2": "2026-06-23T00:20:00Z",
            "c3": "2026-06-23T00:40:00Z"}

    # Three ticks: c1, then c2, then c3 land on origin/master while d0 stays deployed
    # (server continuously "busy", nothing deployed yet).
    states = [detect.compute_pending("/r", git_mod=HistoryGit("d0", tip, history, isos),
                                     ci_fn=lambda s: "green")
              for tip in ("c1", "c2", "c3")]

    # The pending-since anchor (oldest undeployed commit) stays c1/T1 across every tick —
    # it must NOT reset to the newest tip as later commits arrive. The old script reset here.
    for st in states:
        assert st.behind is True
        assert st.commits[-1]["sha"] == "c1"
        assert st.commits[-1]["iso"] == isos["c1"]

    # The range genuinely grows (later commits are included) — only the anchor is stable.
    assert [c["sha"] for c in states[-1].commits] == ["c3", "c2", "c1"]


@pytest.mark.asyncio
async def test_tick_failure_logs_warning_and_retains_last_good_state(caplog):
    """A failing tick must not silently swallow: it logs at WARNING (with the traceback)
    and keeps serving the last good state, so a broken poll is visible to operators."""
    good = detect.PendingState(behind=True, live_sha="a", target_sha="b",
                               commits=[{"sha": "1"}], ci="green")
    flaky = _Flaky()
    loop = detect.DetectLoop("/r", interval_s=0, compute=flaky)

    flaky.mode = good
    await loop.tick()
    assert loop.state is good

    flaky.mode = RuntimeError("git fetch exploded")
    with caplog.at_level(logging.WARNING):
        await loop.tick()

    assert loop.state is good, "last good state must be retained on failure"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a failing tick must emit a WARNING (was a silent swallow)"
    assert warnings[0].exc_info is not None, "the WARNING must carry the traceback (exc_info)"


@pytest.mark.asyncio
async def test_persistent_failure_escalates_to_error(caplog):
    """Consecutive failures mean the poll is stuck, not a transient blip: after the
    escalation threshold the log level rises to ERROR so operators get a hard signal."""
    flaky = _Flaky()
    flaky.mode = RuntimeError("stuck")
    loop = detect.DetectLoop("/r", interval_s=0, compute=flaky, escalate_after_consecutive=3)

    with caplog.at_level(logging.WARNING):
        await loop.tick()  # 1st failure -> WARNING
        await loop.tick()  # 2nd failure -> WARNING
        await loop.tick()  # 3rd consecutive -> ERROR

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warns) == 2, "first (threshold-1) failures stay at WARNING"
    assert len(errors) == 1, "the threshold'th consecutive failure escalates to ERROR"


@pytest.mark.asyncio
async def test_success_resets_failure_escalation(caplog):
    """A successful tick clears the consecutive-failure streak, so a later single
    failure logs WARNING again — escalation tracks *consecutive* failures only."""
    good = detect.PendingState(behind=False, live_sha="x", target_sha="x", commits=[], ci="green")
    flaky = _Flaky()
    loop = detect.DetectLoop("/r", interval_s=0, compute=flaky, escalate_after_consecutive=3)

    with caplog.at_level(logging.WARNING):
        flaky.mode = RuntimeError("t1")
        await loop.tick()  # 1st failure -> WARNING
        await loop.tick()  # 2nd failure -> WARNING
        flaky.mode = good
        await loop.tick()  # success resets the streak
        flaky.mode = RuntimeError("t2")
        await loop.tick()  # streak restarts at 1 -> WARNING, not ERROR

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [], "a success between failures must prevent escalation"
