import logging

import pytest
from server.deploy import detect


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
    def __init__(self, live, remote, commits):
        self._live, self._remote, self._commits = live, remote, commits
        self.fetched = False
    def fetch(self, repo, ref="master", run=None): self.fetched = True
    def current_sha(self, repo, run=None): return self._live
    def remote_sha(self, repo, ref="origin/master", run=None): return self._remote
    def log_range(self, repo, a, b, run=None): return self._commits


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
