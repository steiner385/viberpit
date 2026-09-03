"""Boot-time reconcile (spec §3): a runner brings itself to deploy_state.deployed_sha
before its daemon starts; every failure is loud but never blocks the start (decision 3a)."""
from __future__ import annotations
import subprocess
from server.deploy import runner_sync as RS

TARGET = "b" * 40


class Git:
    """Scripted git: HEAD, cleanliness, ancestry; records argv.

    Real argv is ``["git", "-C", repo, <subcommand>, ...]``; the fake strips the
    ``-C repo`` pair before matching. Ancestry is DIRECTIONAL: with ``ff=True`` only
    "HEAD is an ancestor of the target" is true (the runner is behind); the reverse
    question ("target is an ancestor of HEAD" = the runner is ahead) is false unless
    a subclass says otherwise.
    """
    def __init__(self, head, *, clean=True, ff=True, fetch_ok=True, pip_ok=True, deps_changed=False,
                 checkout_ok=True, branch="master", master_is_ancestor=True):
        self.head, self.clean, self.ff, self.fetch_ok, self.pip_ok = head, clean, ff, fetch_ok, pip_ok
        self.deps_changed = deps_changed
        self.checkout_ok = checkout_ok
        # Which branch HEAD is on, and whether the LOCAL master is already contained in the
        # deployed sha. Only a detached/other-branch HEAD makes the second question matter:
        # `checkout master` moves HEAD onto local master, which ff-only can then only move
        # forward — so local commits master carries and the deployed sha does not would be
        # a silent BACKWARDS move.
        self.branch, self.master_is_ancestor = branch, master_is_ancestor
        self.calls: list[list[str]] = []

    def is_ancestor(self, anc: str, desc: str) -> bool:
        if anc == "master":
            return self.master_is_ancestor
        return self.ff and anc == self.head and desc != self.head

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = list(argv)
        if a[:2] == ["git", "-C"]:
            a = ["git"] + a[3:]
        def done(rc=0, out=""):
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="err")
        if a[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return done(0, self.branch + "\n")
        if a[:3] == ["git", "rev-parse", "HEAD"]:
            return done(0, self.head + "\n")
        if a[:3] == ["git", "status", "--porcelain"]:
            return done(0, "" if self.clean else " M server/x.py\n")
        if a[:2] == ["git", "fetch"]:
            return done(0 if self.fetch_ok else 1)
        if a[:3] == ["git", "merge-base", "--is-ancestor"]:
            return done(0 if self.is_ancestor(a[-2], a[-1]) else 1)
        if a[:2] == ["git", "checkout"]:
            return done(0 if self.checkout_ok else 1)
        if a[:2] == ["git", "merge"]:
            return done(0 if self.ff else 1)
        if a[:2] == ["git", "diff"]:
            return done(0, "uv.lock\n" if self.deps_changed else "")
        if "pip" in argv:
            return done(0 if self.pip_ok else 1)
        if argv == ["sync"]:
            return done(0)
        raise AssertionError(f"unexpected argv {argv}")


def test_no_state_is_a_noop():
    g = Git("a" * 40)
    assert RS.sync("/repo", deployed_sha=None, run=g) == "no-state"
    assert g.calls == []


def test_in_sync_touches_nothing():
    g = Git(TARGET)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "in-sync"
    assert len(g.calls) == 1 and "rev-parse" in g.calls[0]


def test_fast_forwards_a_behind_runner_and_syncs():
    g = Git("a" * 40)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "updated"
    joined = [" ".join(c) for c in g.calls]
    assert "git -C /repo fetch --quiet origin" in joined
    assert "git -C /repo checkout -q master" in joined
    assert f"git -C /repo merge --ff-only {TARGET}" in joined
    assert joined[-1] == "sync"
    assert not any("pip" in j for j in joined), "lockfile unchanged → no pip"
    assert not any("reset" in j for j in joined)


def test_runs_pip_when_lockfile_changed():
    g = Git("a" * 40, deps_changed=True)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "updated"
    pip = next(c for c in g.calls if "pip" in c)
    assert pip == ["/repo/.venv/bin/python", "-m", "pip", "install", "-e", "/repo[server]", "--quiet"]


def test_ahead_of_deployed_is_left_alone():
    """A deploy in progress fast-forwarded the checkout BEFORE deploy_state moved: HEAD is
    a descendant of deployed_sha. Reconcile only ever moves forward."""
    class Ahead(Git):
        def is_ancestor(self, anc, desc):
            return anc == TARGET and desc == "c" * 40   # deployed IS an ancestor of HEAD
    g = Ahead("c" * 40)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "ahead"
    assert not any(c[:2] == ["git", "merge"] for c in g.calls)


def test_dirty_tree_refuses():
    g = Git("a" * 40, clean=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "dirty"
    assert not any(c[:2] == ["git", "fetch"] for c in g.calls)


def test_diverged_refuses_without_reset():
    g = Git("a" * 40, ff=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "not-fast-forward"
    assert not any("reset" in " ".join(c) for c in g.calls)


def test_diverged_local_master_is_not_checked_out():
    """HEAD is fast-forwardable to the deployed sha, but it is on a BRANCH — and local
    master carries commits the deployed sha does not. `git checkout master` would move HEAD
    backwards onto them before the ff-only merge could object, quietly starting the daemon
    on somebody's unmerged work. Refuse before touching HEAD."""
    g = Git("a" * 40, branch="hotfix", master_is_ancestor=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "not-fast-forward"
    assert not any(c[:2] == ["git", "checkout"] for c in g.calls), "HEAD must not have moved"
    assert not any("reset" in " ".join(c) for c in g.calls)


def test_on_a_branch_with_clean_master_fast_forwards():
    """The benign half: a branch whose local master is already contained in the deployed
    sha is safe to check out, so the reconcile proceeds exactly as from master."""
    g = Git("a" * 40, branch="hotfix", master_is_ancestor=True)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "updated"
    joined = [" ".join(c) for c in g.calls]
    assert "git -C /repo checkout -q master" in joined
    assert f"git -C /repo merge --ff-only {TARGET}" in joined
    assert joined.index("git -C /repo checkout -q master") < \
        joined.index(f"git -C /repo merge --ff-only {TARGET}")


def test_fetch_failure_is_reported():
    g = Git("a" * 40, fetch_ok=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "fetch-failed"


def test_checkout_failure_is_reported():
    g = Git("a" * 40, checkout_ok=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "checkout-failed"
    assert not any(c[:2] == ["git", "merge"] for c in g.calls)


def test_unknown_sha_when_head_unreadable():
    class NoHead(Git):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            a = list(argv)
            if a[:2] == ["git", "-C"]:
                a = ["git"] + a[3:]
            if a[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected argv {argv}")
    g = NoHead("a" * 40)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "unknown-sha"


def test_pip_failure_is_reported_after_the_checkout_moved():
    g = Git("a" * 40, deps_changed=True, pip_ok=False)
    assert RS.sync("/repo", deployed_sha=TARGET, run=g) == "pip-failed"


def test_read_deployed_sha_sqlite_and_missing_table(tmp_path):
    import asyncio
    from server.db import init_db
    from server.deploy.state import record_deploy
    url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"
    async def seed():
        await init_db(db_url=url)
        await record_deploy(sha=TARGET, short="bbbbbbb", daemon_changed=True)
    asyncio.run(seed())
    assert RS.read_deployed_sha(url) == TARGET
    assert RS.read_deployed_sha(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}") is None


def test_main_always_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///" + str(tmp_path / "none.db"))
    assert RS.main(["--repo", str(tmp_path)]) == 0
    monkeypatch.setenv("CCSWITCH_DB_URL", "postgresql+asyncpg://nobody@127.0.0.1:1/none")
    assert RS.main(["--repo", str(tmp_path)]) == 0
    monkeypatch.delenv("CCSWITCH_DB_URL", raising=False)
    assert RS.main(["--repo", str(tmp_path)]) == 0
