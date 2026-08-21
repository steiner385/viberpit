import pytest

from server.deploy import steps
from server.deploy.types import DeployError


def _recorder(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        class R:  # minimal stand-in for CompletedProcess
            returncode = 0
            stdout = b""
            stderr = b""
        return R()
    return run


def test_remote_daemon_restarts_over_ssh(monkeypatch):
    """With a remote daemon host configured, the daemon restart must go over SSH."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=False,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith("ssh tony@rocky") for c in joined), (
        f"expected an ssh daemon restart, got {joined}")
    assert any("ccswitch-daemon.service" in c for c in joined)
    assert "ccswitch-daemon.service" in units
    # the edge still restarts locally, and still AFTER the daemon (order is load-bearing, #190)
    assert joined[-1] == "sudo systemctl restart ccswitch.service"


def test_local_behavior_unchanged_without_the_env_var(monkeypatch):
    """Single-host deployments must behave exactly as before."""
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_HOST", raising=False)
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=True,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert joined == ["sudo systemctl restart ccswitch-daemon.service",
                      "sudo systemctl restart ccswitch.service"]
    assert units == ["ccswitch-daemon.service", "ccswitch.service"]


def test_no_stray_local_daemon_restart_in_remote_mode(monkeypatch):
    """In remote mode, only the ssh form of the daemon restart may run — never a bare local one."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=True, daemon_enabled=False, run=_recorder(calls))
    assert ["sudo", "systemctl", "restart", "ccswitch-daemon.service"] not in calls


def test_malicious_ssh_host_raises_before_any_command_runs(monkeypatch):
    """A value that ssh would parse as a flag must be rejected, not smuggled through."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "-oProxyCommand=touch /tmp/pwned")
    calls: list[list[str]] = []
    with pytest.raises(DeployError):
        steps.restart_units(daemon_changed=True, daemon_enabled=False, run=_recorder(calls))
    # Nothing executed at all — not even the local edge restart — proving the deploy stopped
    # rather than silently falling back.
    assert calls == []


@pytest.mark.parametrize("host", ["tony@rocky", "rocky", "rocky.tail0dd570.ts.net"])
def test_legitimate_ssh_hosts_are_accepted(monkeypatch, host):
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", host)
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=False,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith(f"ssh {host} ") for c in joined), (
        f"expected an ssh restart for {host!r}, got {joined}")
    assert "ccswitch-daemon.service" in units


# ---------------------------------------------------------------------------
# The remote daemon's CODE must be updated, not just its unit restarted.
#
# restart_units used to only `ssh host systemctl restart ccswitch-daemon` — it
# never fetched or checked out on that host, so the remote daemon restarted the
# SAME stale commit indefinitely. Its own docstring asserted "both hosts run the
# same deployed commit", an invariant nothing established.
#
# Real consequence (2026-08-21): deploying #715 gave dobby's edge the
# `cloned_repos` RPC caller while rocky's daemon stayed on f310aa6 without the
# handler. Every repo listing returned `409 {"detail":"cloned_repos"}` and the
# New Session repo dropdown showed "— could not load repos —".
# ---------------------------------------------------------------------------

TARGET = "bd67eba803a2a2b847d2efe031c1d39d65bb43de"


def test_remote_checkout_is_updated_before_the_restart(monkeypatch):
    """Fetch + fast-forward must both run, and both must precede the restart."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=True, daemon_enabled=False,
                        target_sha=TARGET, run=_recorder(calls))
    joined = [" ".join(c) for c in calls]

    fetch_at = next(i for i, c in enumerate(joined) if "fetch" in c)
    ff_at = next(i for i, c in enumerate(joined) if "merge --ff-only" in c)
    restart_at = next(i for i, c in enumerate(joined)
                      if "systemctl restart ccswitch-daemon.service" in c)

    assert fetch_at < ff_at < restart_at, (
        f"must fetch, fast-forward, THEN restart; got {joined}")
    assert TARGET in joined[ff_at], "must fast-forward to the exact deployed sha"
    assert "-C /home/tony/GitHub/ccswitch" in joined[ff_at]


def test_fast_forward_only_never_resets(monkeypatch):
    """--ff-only refuses on divergence; a reset --hard would destroy remote work."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=True, daemon_enabled=False,
                        target_sha=TARGET, run=_recorder(calls))
    flat = " ".join(" ".join(c) for c in calls)
    assert "--ff-only" in flat
    assert "reset --hard" not in flat, "never hard-reset an operator's checkout"


def test_missing_remote_repo_path_fails_before_any_command(monkeypatch):
    """Refuse to deploy rather than restart a host whose code we cannot update.

    Silently restarting is what produced the stale-daemon outage; a deploy that
    cannot update the remote MUST fail loudly.
    """
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_REPO", raising=False)
    calls: list[list[str]] = []
    with pytest.raises(DeployError):
        steps.restart_units(daemon_changed=True, daemon_enabled=False,
                            target_sha=TARGET, run=_recorder(calls))
    assert calls == [], "nothing may run, including the edge restart"


@pytest.mark.parametrize("bad", [
    "relative/path",              # must be absolute
    "/home/tony/x; rm -rf /",     # command separator
    "/home/tony/$(whoami)",       # substitution
    "/home/tony/x`id`",           # backticks
    "-oProxyCommand=touch /tmp/x",  # option smuggling
])
def test_malicious_remote_repo_path_rejected(monkeypatch, bad):
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", bad)
    calls: list[list[str]] = []
    with pytest.raises(DeployError):
        steps.restart_units(daemon_changed=True, daemon_enabled=False,
                            target_sha=TARGET, run=_recorder(calls))
    assert calls == []


def test_malicious_target_sha_rejected(monkeypatch):
    """target_sha lands in an argv position; only hex may pass."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    with pytest.raises(DeployError):
        steps.restart_units(daemon_changed=True, daemon_enabled=False,
                            target_sha="HEAD; rm -rf /", run=_recorder(calls))
    assert calls == []


def test_unchanged_daemon_does_not_touch_the_remote(monkeypatch):
    """No daemon-relevant changes → no remote churn, same as before."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=False, daemon_enabled=False,
                        target_sha=TARGET, run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert not any(c.startswith("ssh") for c in joined), joined
    assert joined == ["sudo systemctl restart ccswitch.service"]


def test_local_daemon_never_uses_ssh(monkeypatch):
    """A same-host daemon shares the edge's checkout; nothing to sync."""
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_HOST", raising=False)
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_REPO", raising=False)
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=True, daemon_enabled=True,
                        target_sha=TARGET, run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert not any("ssh" in c for c in joined)
    assert joined == ["sudo systemctl restart ccswitch-daemon.service",
                      "sudo systemctl restart ccswitch.service"]


def test_target_sha_optional_for_backward_compatibility(monkeypatch):
    """Callers that pass no sha still work; the remote fast-forwards to origin."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")
    calls: list[list[str]] = []
    steps.restart_units(daemon_changed=True, daemon_enabled=False,
                        run=_recorder(calls))
    flat = " ".join(" ".join(c) for c in calls)
    assert "merge --ff-only" in flat
