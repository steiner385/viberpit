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
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is True


def test_fence_fails_closed(monkeypatch):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote",
                        lambda: (_ for _ in ()).throw(RuntimeError("net")))
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False
    monkeypatch.setattr(sha_fence, "_remote", lambda: "fff0000")
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False


def test_short_local_sha_fails_closed(monkeypatch):
    """A 3-char sha is a prefix of far too many commits to mean 'this code is the tip'."""
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc")
    monkeypatch.setattr(sha_fence, "_remote", lambda: FULL)
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False


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


# --------------------------------------------------------------------------- #
# Edge-only commits do not fence the daemon (#1526)                            #
# --------------------------------------------------------------------------- #

TIP = "fff1234000000000000000000000000000000000"


def _compare(status="ahead", behind_by=0, files=("docs/x.md",), total_commits=1):
    return {"status": status, "ahead_by": total_commits, "behind_by": behind_by,
            "total_commits": total_commits, "files": [{"filename": f} for f in files]}


def _arm(monkeypatch, compare):
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote", lambda: TIP)
    monkeypatch.setattr(sha_fence, "_compare", compare)
    sha_fence._cache.clear()


def test_docs_only_commits_ahead_of_the_daemon_keep_the_fence_open(monkeypatch):
    """The deploy controller restarts only the edge for docs/tests/workflow changes
    (steps._EDGE_ONLY), so master legitimately runs ahead of the daemon by those
    commits — the daemon's code IS the tip's daemon code."""
    seen = {}

    def compare(local, remote):
        seen["args"] = (local, remote)
        return _compare(files=("docs/superpowers/specs/x.md", "specs/003/plan.md",
                               "server/tests/test_x.py", ".github/workflows/ci.yml"))

    _arm(monkeypatch, compare)
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is True
    assert seen["args"] == ("abc1234", TIP)


def test_a_daemon_code_change_ahead_of_the_daemon_closes_the_fence(monkeypatch):
    _arm(monkeypatch, lambda local, remote: _compare(
        files=("docs/x.md", "server/automations/gh_reconcile.py"), total_commits=2))
    verdict = asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0))
    assert verdict.open is False and not verdict
    # The reason names what closed the fence — the daemon-changing path, not the
    # edge-only one beside it — so a starved board is diagnosable from the status card.
    assert "server/automations/gh_reconcile.py" in verdict.reason
    assert "docs/x.md" not in verdict.reason
    assert "2 commit" in verdict.reason


def test_agent_tooling_only_commits_ahead_keep_the_fence_open(monkeypatch):
    """The starvation this fixes: every rebrand / instructions merge touched only repo-root
    tooling config (``.agents/``, ``.claude/``, ``.codex/``, ``.specify/``,
    ``.brand-ratchet.toml``), none of which the daemon loads, yet each one closed the
    fence until the next daemon deploy — so new issues reached the board only in the
    minutes after a deploy."""
    _arm(monkeypatch, lambda local, remote: _compare(
        files=(".agents/instructions/core.md", ".brand-ratchet.toml",
               ".claude/agents/viberpit-reviewer.md", ".codex/agents/viberpit-reviewer.toml",
               ".specify/memory/constitution.md", "AGENTS.md", "tests/test_ledger.py"),
        total_commits=3))
    verdict = asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0))
    assert verdict.open is True and verdict.reason == ""


def test_open_verdict_is_truthy_and_closed_reasons_are_specific(monkeypatch):
    assert sha_fence.FenceVerdict.opened() and sha_fence.FenceVerdict.opened().reason == ""
    # A failed remote read says so, rather than pretending the sha mismatched.
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "abc1234")
    monkeypatch.setattr(sha_fence, "_remote",
                        lambda: (_ for _ in ()).throw(RuntimeError("ls-remote: net down")))
    sha_fence._cache.clear()
    verdict = asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0))
    assert not verdict and "net down" in verdict.reason
    # An unusable local sha is named as such.
    monkeypatch.setattr(sha_fence, "running_git_sha", lambda: "unknown")
    monkeypatch.setattr(sha_fence, "_remote", lambda: FULL)
    sha_fence._cache.clear()
    verdict = asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0))
    assert not verdict and "unknown" in verdict.reason
    # Divergence and truncation each carry their own reason.
    _arm(monkeypatch, lambda local, remote: _compare(status="diverged", behind_by=2))
    assert "diverged" in asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).reason
    _arm(monkeypatch, lambda local, remote: _compare(
        files=tuple(f"docs/{i}.md" for i in range(sha_fence._COMPARE_FILE_CAP))))
    assert "truncated" in asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).reason


def test_diverged_or_behind_local_closes_the_fence(monkeypatch):
    for status, behind in (("diverged", 2), ("behind", 1)):
        _arm(monkeypatch, lambda local, remote, s=status, b=behind: _compare(status=s, behind_by=b))
        assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False


def test_compare_failure_or_truncated_file_list_fails_closed(monkeypatch):
    _arm(monkeypatch, lambda local, remote: (_ for _ in ()).throw(RuntimeError("net")))
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False
    # GitHub caps compare payloads at 300 files: a full page may hide a daemon change.
    _arm(monkeypatch, lambda local, remote: _compare(
        files=tuple(f"docs/{i}.md" for i in range(sha_fence._COMPARE_FILE_CAP))))
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False
    # No files at all with commits ahead is not evidence of edge-only either.
    _arm(monkeypatch, lambda local, remote: _compare(files=()))
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is False


def test_exact_tip_never_calls_compare(monkeypatch):
    _arm(monkeypatch, lambda local, remote: (_ for _ in ()).throw(AssertionError("compare called")))
    monkeypatch.setattr(sha_fence, "_remote", lambda: "abc1234" + "0" * 33)
    sha_fence._cache.clear()
    assert asyncio.run(sha_fence.daemon_matches_origin_master(ttl_s=0)).open is True


def test_compare_runs_gh_against_the_origin_repo(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.setdefault("cmds", []).append(cmd)
        if cmd[:3] == ["git", "config", "--get"]:
            return _completed("git@github.com:steiner385/viberpit.git\n")
        return _completed('{"status": "ahead", "behind_by": 0, "total_commits": 1, '
                          '"files": [{"filename": "docs/a.md"}]}')

    monkeypatch.setattr(sha_fence, "_run", fake_run)
    out = sha_fence._compare("abc1234", TIP)
    assert out["files"][0]["filename"] == "docs/a.md"
    assert seen["cmds"][1][:3] == ["gh", "api", f"repos/steiner385/viberpit/compare/abc1234...{TIP}"]


def test_origin_repo_parses_ssh_and_https_urls(monkeypatch):
    for url in ("git@github.com:steiner385/viberpit.git", "https://github.com/steiner385/viberpit",
                "https://github.com/steiner385/viberpit.git", "ssh://git@github.com/steiner385/viberpit.git"):
        monkeypatch.setattr(sha_fence, "_run", lambda cmd, **kw: _completed(url + "\n"))
        assert sha_fence._origin_repo() == "steiner385/viberpit", url
    monkeypatch.setattr(sha_fence, "_run", lambda cmd, **kw: _completed("", returncode=1))
    with pytest.raises(RuntimeError):
        sha_fence._origin_repo()
