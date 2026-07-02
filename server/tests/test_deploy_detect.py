import pytest
from server.deploy import detect


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
