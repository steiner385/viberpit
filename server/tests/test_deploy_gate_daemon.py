"""The deploy health gate must actually see the daemon.

wait_healthy greps /healthz for '"status":"ok"' and a matching sha. In remote mode
the edge stays healthy when the daemon is dead — /healthz explicitly degrades
(active=0, daemon="unavailable") and still returns status ok with the right sha.
The daemon's own sha was never checked at all.

All three production failures on 2026-08-21/22 would pass that gate:
  * rocky running a stale commit -> 409 on every repo listing (skew, sha unchecked)
  * rocky's daemon hung in startup, never bound -> daemon:"unavailable", ignored
  * corrupt ledger -> /api/accounts returned 0 accounts (never probed)
"""
from __future__ import annotations

from server.deploy import steps

SHA = "abc1234"


def _body(**over) -> str:
    import json
    d = {"status": "ok", "sha": SHA, "busy": False, "activeSessions": 0,
         "vncAttached": False}
    d.update(over)
    return json.dumps(d, separators=(",", ":"))


def test_healthy_edge_and_daemon_passes():
    body = _body(daemon="ok", daemonSha=SHA)
    assert steps.wait_healthy(SHA, url="u", attempts=1, fetch=lambda u: body,
                              sleep=lambda s: None) is True


def test_unavailable_daemon_fails_the_gate():
    """The flag is already in the response; the gate used to ignore it."""
    body = _body(daemon="unavailable")
    assert steps.wait_healthy(SHA, url="u", attempts=2, fetch=lambda u: body,
                              sleep=lambda s: None) is False


def test_daemon_running_a_different_sha_fails_the_gate():
    """Version skew: the edge is on the new commit, the daemon is not.

    This is the #719 class — deploying a commit that adds an RPC while the daemon
    still lacks the handler, which 409s every call.
    """
    body = _body(daemon="ok", daemonSha="0000000")
    assert steps.wait_healthy(SHA, url="u", attempts=2, fetch=lambda u: body,
                              sleep=lambda s: None) is False


def test_single_host_deploy_without_a_daemon_field_still_passes():
    """In-process (non-remote) mode reports no daemon key at all — that is not a
    failure, it is a deployment without a separate daemon."""
    assert steps.wait_healthy(SHA, url="u", attempts=1, fetch=lambda u: _body(),
                              sleep=lambda s: None) is True


def test_daemon_ok_but_sha_absent_still_passes():
    """A daemon too old to report its sha must not brick deploys — degrade to the
    previous behaviour rather than failing closed on a missing field."""
    body = _body(daemon="ok")
    assert steps.wait_healthy(SHA, url="u", attempts=1, fetch=lambda u: body,
                              sleep=lambda s: None) is True


def test_edge_sha_mismatch_still_fails():
    """Existing contract unchanged."""
    assert steps.wait_healthy(SHA, url="u", attempts=2,
                              fetch=lambda u: _body(sha="9999999", daemon="ok"),
                              sleep=lambda s: None) is False


def test_recovers_when_the_daemon_comes_up_mid_poll():
    """The gate polls; a daemon still starting must not fail the deploy outright."""
    seq = [_body(daemon="unavailable"), _body(daemon="ok", daemonSha=SHA)]
    it = iter(seq)
    assert steps.wait_healthy(SHA, url="u", attempts=5,
                              fetch=lambda u: next(it, seq[-1]),
                              sleep=lambda s: None) is True
