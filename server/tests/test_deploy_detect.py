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


@pytest.mark.asyncio
async def test_detect_loop_tick_updates_state():
    g = FakeGit("a", "b", [{"sha": "1", "subject": "s", "author": "t", "iso": "i"}])
    loop = detect.DetectLoop("/r", interval_s=0,
                             compute=lambda repo: detect.compute_pending(repo, git_mod=g, ci_fn=lambda s: "green"))
    await loop.tick()
    assert loop.state.behind is True and loop.state.to_dict()["commits"][0]["sha"] == "1"
