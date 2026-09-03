"""The reconciler's origin/master fence (spec 003 §6 'rocky rule')."""
import asyncio
import subprocess

import pytest

from server.github import sha_fence

FULL = "abc1234deadbeef0000000000000000000000000"


def test_fence_matches_prefix(monkeypatch):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote", lambda: FULL)
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is True


def test_fence_fails_closed(monkeypatch):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote",
                        lambda: (_ for _ in ()).throw(RuntimeError("net")))
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is False
    monkeypatch.setattr(sha_fence, "_remote", lambda: "fff0000")
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is False


def test_short_local_sha_fails_closed(monkeypatch):
    """A 3-char sha is a prefix of far too many commits to mean 'this code is the tip'."""
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc")
    monkeypatch.setattr(sha_fence, "_remote", lambda: FULL)
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)) is False


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["git"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_remote_parses_ls_remote_output(monkeypatch):
    """The fence must read the TRUE remote tip: ls-remote, not the local tracking ref
    (rocky's deploy is disabled, so its origin/master is frozen at the stale daemon's sha)."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _completed(f"{FULL}\trefs/heads/master\n")

    monkeypatch.setattr(sha_fence, "_run", fake_run)
    assert sha_fence._remote() == FULL
    assert seen["cmd"] == ["git", "ls-remote", "--quiet", "origin", "refs/heads/master"]
    assert seen["kw"]["timeout"] == sha_fence._LS_REMOTE_TIMEOUT_S


def test_remote_raises_on_empty_output(monkeypatch):
    monkeypatch.setattr(sha_fence, "_run", lambda cmd, **kw: _completed(""))
    with pytest.raises(RuntimeError):
        sha_fence._remote()


def test_remote_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(sha_fence, "_run",
                        lambda cmd, **kw: _completed("", returncode=128, stderr="no remote"))
    with pytest.raises(RuntimeError):
        sha_fence._remote()
