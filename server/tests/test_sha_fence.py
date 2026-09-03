"""The reconciler's origin/master fence (spec 003 §6 'rocky rule')."""
import asyncio

from server.github import sha_fence


def test_fence_matches_prefix(monkeypatch):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote", lambda: "abc1234deadbeef")
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
