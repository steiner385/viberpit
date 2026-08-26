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


# --- SHA abbreviation skew across hosts (2026-08-25 #774 rollback) ------------------
#
# ``git rev-parse --short`` picks the shortest prefix that is UNIQUE within a given
# repository's object database, so the SAME commit abbreviates to different lengths on
# different hosts: dobby rendered f26c56ee475… as "f26c56ee" (8 chars, a colliding
# object forced the extra nibble) while rocky rendered it "f26c56e" (7). The edge's
# expected sha is abbreviated on dobby and the daemon's sha on rocky, so an exact
# ``!=`` wrongly failed the gate and rolled back a deploy where BOTH hosts were on the
# correct commit. The gate must treat one short-sha as matching the other when either
# is a prefix of the other.

def test_daemon_sha_shorter_abbreviation_of_same_commit_passes():
    # dobby expected/edge = 8 chars; rocky daemon = 7 chars; same commit.
    body = _body(sha="f26c56ee", daemon="ok", daemonSha="f26c56e")
    assert steps.wait_healthy("f26c56ee", url="u", attempts=1,
                              fetch=lambda u: body, sleep=lambda s: None) is True


def test_daemon_sha_longer_abbreviation_of_same_commit_passes():
    # The reverse: daemon abbreviates longer than the edge's expected sha.
    body = _body(sha="f26c56e", daemon="ok", daemonSha="f26c56ee")
    assert steps.wait_healthy("f26c56e", url="u", attempts=1,
                              fetch=lambda u: body, sleep=lambda s: None) is True


def test_daemon_ok_prefix_match_unit():
    import json
    ok_short = json.dumps({"daemon": "ok", "daemonSha": "f26c56e"})
    ok_long = json.dumps({"daemon": "ok", "daemonSha": "f26c56ee"})
    diff = json.dumps({"daemon": "ok", "daemonSha": "deadbee"})
    assert steps._daemon_ok(ok_short, "f26c56ee") is True
    assert steps._daemon_ok(ok_long, "f26c56e") is True
    # A genuinely different commit (the #719 skew class) is still rejected.
    assert steps._daemon_ok(diff, "f26c56ee") is False
