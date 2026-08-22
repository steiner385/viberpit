"""The health gate must outlast a remote daemon's restart.

#726 made wait_healthy fail on daemon:"unavailable". Correct — but the poll
window was 15 x 2s = 30s, sized for an edge-only restart. A REMOTE daemon is
restarted over ssh, and `systemctl restart` returns when the process starts, not
when uvicorn is serving: rocky needs ~10-30s more to bind its port.

So the first real deploy after #726 failed at "restart: health gate failed" and
rolled back, even though nothing was wrong — the daemon was simply still coming
up. Worse, the rollback reverted the EDGE while the remote daemon kept the new
commit, leaving exactly the skew the gate exists to detect (in the safe
direction, but still).

The window is only spent when something is actually wrong: wait_healthy returns
on the first healthy poll, so a longer ceiling costs nothing on success.
"""
from __future__ import annotations

import json

from server.deploy import steps

SHA = "abc1234"


def _body(**over) -> str:
    d = {"status": "ok", "sha": SHA, "busy": False, "activeSessions": 0,
         "vncAttached": False}
    d.update(over)
    return json.dumps(d, separators=(",", ":"))


def test_daemon_window_is_longer_than_edge_only():
    assert steps.HEALTH_ATTEMPTS_WITH_DAEMON > steps.HEALTH_ATTEMPTS_EDGE_ONLY


def test_daemon_window_covers_a_realistic_remote_restart():
    """Rocky took ~30s+ to bind after systemctl returned; 30s was not enough."""
    window = steps.HEALTH_ATTEMPTS_WITH_DAEMON * steps.HEALTH_POLL_INTERVAL_S
    assert window >= 120, f"window {window}s is too tight for a remote daemon restart"


def test_gate_waits_out_a_slow_daemon_and_then_passes():
    """The real production sequence: unavailable for a while, then healthy."""
    slow = [_body(daemon="unavailable")] * 20 + [_body(daemon="ok", daemonSha=SHA)]
    it = iter(slow)
    assert steps.wait_healthy(SHA, url="u",
                              attempts=steps.HEALTH_ATTEMPTS_WITH_DAEMON,
                              fetch=lambda u: next(it, slow[-1]),
                              sleep=lambda s: None) is True


def test_a_daemon_that_never_comes_up_still_fails():
    """The window is longer, not infinite — a genuinely dead daemon must fail."""
    assert steps.wait_healthy(SHA, url="u",
                              attempts=steps.HEALTH_ATTEMPTS_WITH_DAEMON,
                              fetch=lambda u: _body(daemon="unavailable"),
                              sleep=lambda s: None) is False


def test_edge_only_deploy_keeps_the_short_window():
    """No daemon restart means nothing slow to wait for; fail fast as before."""
    assert steps.HEALTH_ATTEMPTS_EDGE_ONLY == 15
