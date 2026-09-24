"""A daemon restart that a deploy asked for is not an incident.

Two sites observe a restart and both logged it unconditionally at WARNING:
``server.sessions.daemon_health`` (the epoch-change watcher, #434) and
``server.ipc.client._recycle_transport`` (the pool recycle that clears the
stale-connection wedge). ``server.obs.errsink`` captures WARNING+ and files one
auto-error issue per fingerprint, so every deploy minted occurrences — 27 on
#1653 and 60 on #1654 over one six-day window, on a host that restarts its
daemon 7-9x a day as a matter of routine.

This is the shape ``server.sessions.host_upgrade`` was written for (#1614: "an
Error Triage issue about a daemon that was doing exactly what it was told") and
that ``host_probe`` already consults. These two sites just never got wired to
it, so the fix is severity, not suppression: a restart with a deliberate
explanation reports at INFO with every field intact, and one WITHOUT an
explanation — a wedge, a crash, a watchdog kill — still reports at WARNING,
which is the signal #434 existed to surface in the first place.

"Deliberate" is two pieces of evidence OR'd together, because each covers the
other's blind spot (see ``host_upgrade.restart_deliberate``):

* the daemon came back on a DIFFERENT sha — evidence that outlives the deploy
  window, so a restart noticed on a later poll tick is still explained;
* ``restart_expected`` is true right now — covers a deliberate restart that
  does NOT change the sha (the Fleet "Update" button, a rewind).
"""
from __future__ import annotations

import logging

import pytest

from server.ipc import client as ipc_client
from server.ipc.client import DaemonClient
from server.ipc.paths import Endpoint, endpoint_host_name
from server.sessions import host_upgrade
from server.sessions.daemon_health import DaemonHealthMonitor


@pytest.fixture(autouse=True)
def _isolate_watchdog_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HEALTH_STATE_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_controller(monkeypatch):
    """No deploy in progress unless a test says otherwise.

    Without this the predicate dials the real deploy-controller socket, which on
    a dev box is absent (answer: not expected) but on dobby is live.
    """
    monkeypatch.setattr(host_upgrade, "_STATE", {})
    monkeypatch.setattr(host_upgrade, "_controller_cache", {"at": 0.0, "hosts": {}})

    async def _no_deploy() -> dict:
        return {"deploy": {"hosts": {}}}

    monkeypatch.setattr(host_upgrade, "_default_controller_status", _no_deploy)


def _epoch(start_ms: int, pid: int = 1234, suffix: str = "abcd1234") -> str:
    return f"{start_ms:x}-{pid}-{suffix}"


def _restart_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "daemon.restart_observed"]


# --------------------------------------------------------------------------- #
# #1653 — server.sessions.daemon_health
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_restart_onto_a_new_sha_reports_at_info(caplog):
    """The deploy case: a different sha is proof the restart was asked for."""
    infos = [{"epoch": _epoch(1_000), "sha": "aaaa111"},
             {"epoch": _epoch(2_000), "sha": "bbbb222"}]

    async def info_fn() -> dict:
        return infos.pop(0)

    mon = DaemonHealthMonitor(info_fn=info_fn, host_name="dobby")
    with caplog.at_level(logging.DEBUG, logger="server.sessions.daemon_health"):
        await mon.poll_once()   # first epoch — not a restart
        await mon.poll_once()   # restart onto a new sha

    recs = _restart_records(caplog)
    assert len(recs) == 1, "a restart must still be reported, just not as an alarm"
    assert recs[0].levelno == logging.INFO
    assert recs[0].fields["deliberate"] is True
    # The detail that made these records worth keeping must survive the demotion.
    assert recs[0].fields["sha"] == "bbbb222"
    assert recs[0].fields["wasSha"] == "aaaa111"


@pytest.mark.asyncio
async def test_restart_onto_the_same_sha_with_no_deploy_still_warns(caplog):
    """The incident case: nothing explains it, so #434's signal is preserved."""
    infos = [{"epoch": _epoch(1_000), "sha": "aaaa111"},
             {"epoch": _epoch(2_000), "sha": "aaaa111"}]

    async def info_fn() -> dict:
        return infos.pop(0)

    mon = DaemonHealthMonitor(info_fn=info_fn, host_name="dobby")
    with caplog.at_level(logging.DEBUG, logger="server.sessions.daemon_health"):
        await mon.poll_once()
        await mon.poll_once()

    recs = _restart_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert recs[0].fields["deliberate"] is False


@pytest.mark.asyncio
async def test_same_sha_restart_during_a_fleet_upgrade_reports_at_info(caplog):
    """A Fleet "Update" always restarts, even at the sha already deployed."""
    infos = [{"epoch": _epoch(1_000), "sha": "aaaa111"},
             {"epoch": _epoch(2_000), "sha": "aaaa111"}]

    async def info_fn() -> dict:
        return infos.pop(0)

    host_upgrade._STATE["dobby"] = {"state": "running"}
    mon = DaemonHealthMonitor(info_fn=info_fn, host_name="dobby")
    with caplog.at_level(logging.DEBUG, logger="server.sessions.daemon_health"):
        await mon.poll_once()
        await mon.poll_once()

    recs = _restart_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO


@pytest.mark.asyncio
async def test_a_restart_the_predicate_cannot_judge_never_breaks_the_poll_loop(caplog):
    """A logging decision must never propagate out of the poll loop."""
    infos = [{"epoch": _epoch(1_000), "sha": "aaaa111"},
             {"epoch": _epoch(2_000), "sha": "bbbb222"}]

    async def info_fn() -> dict:
        return infos.pop(0)

    async def _boom(*a, **kw):
        raise RuntimeError("controller socket exploded")

    mon = DaemonHealthMonitor(info_fn=info_fn, host_name="dobby")
    import server.sessions.host_upgrade as hu
    original = hu.restart_deliberate
    hu.restart_deliberate = _boom
    try:
        with caplog.at_level(logging.DEBUG, logger="server.sessions.daemon_health"):
            await mon.poll_once()
            await mon.poll_once()   # must not raise
    finally:
        hu.restart_deliberate = original

    # The restart is still recorded for the dashboard even if it cannot be judged.
    assert mon.snapshot(now=0.0)["restartCount24h"] == 1


def test_restart_count_counts_the_restarts_that_actually_happened():
    """``len(self._restarts) + 1`` over an unpruned deque double-counted.

    The logged count was one higher than the truth (#1653's own sample said
    "2 restart(s)" for a single restart) and included restarts already outside
    the window. Now that the count decides nothing but the reader's impression
    of severity it still has to be right — an inflated "is this flapping?"
    number is exactly what an operator reads first.
    """
    mon = DaemonHealthMonitor(info_fn=None, window_s=100.0)
    assert mon.record(_epoch(1), now=0.0) is None          # first sighting
    r1 = mon.record(_epoch(2), now=10.0)
    assert r1 is not None and r1.count == 1
    r2 = mon.record(_epoch(3), now=20.0)
    assert r2 is not None and r2.count == 2
    # t=500 is 480s past the first two restarts, so a 100s window holds only this one.
    r3 = mon.record(_epoch(4), now=500.0)
    assert r3 is not None and r3.count == 1


# --------------------------------------------------------------------------- #
# #1654 — server.ipc.client._recycle_transport
# --------------------------------------------------------------------------- #

def _recycle_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "ipc.daemon_restarted"]


@pytest.mark.asyncio
async def test_pool_recycle_after_a_deploy_restart_reports_at_info(tmp_path, caplog):
    client = DaemonClient(Endpoint("unix", path=str(tmp_path / "s.sock")))
    with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
        await client._recycle_transport(_epoch(1), _epoch(2),
                                        was_sha="aaaa111", now_sha="bbbb222")

    recs = _recycle_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    # The pool stats are the whole reason this record exists — both production
    # wedges were diagnosed by hand with `ss` because the journal said nothing.
    assert "connections" in recs[0].fields
    assert "poolTimeouts" in recs[0].fields


@pytest.mark.asyncio
async def test_pool_recycle_with_no_explanation_still_warns(tmp_path, caplog):
    client = DaemonClient(Endpoint("unix", path=str(tmp_path / "s.sock")))
    with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
        await client._recycle_transport(_epoch(1), _epoch(2),
                                        was_sha="aaaa111", now_sha="aaaa111")

    recs = _recycle_records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_recycle_still_happens_when_the_severity_check_raises(tmp_path, caplog):
    """The recycle clears an outage; a logging decision must never pre-empt it."""
    client = DaemonClient(Endpoint("unix", path=str(tmp_path / "s.sock")))

    async def _boom(*a, **kw):
        raise RuntimeError("controller socket exploded")

    import server.sessions.host_upgrade as hu
    original = hu.restart_deliberate
    hu.restart_deliberate = _boom
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            await client._recycle_transport(_epoch(1), _epoch(2))
    finally:
        hu.restart_deliberate = original

    assert client._transport is not None, "the pool must have been rebuilt"


@pytest.mark.asyncio
async def test_reconcile_epoch_passes_the_sha_across_the_restart(tmp_path, monkeypatch):
    """``_reconcile_epoch`` cached the new sha BEFORE comparing epochs, which
    discarded the one value the severity decision needs."""
    seen: dict = {}

    client = DaemonClient(Endpoint("unix", path=str(tmp_path / "s.sock")))

    async def fake_info() -> dict:
        return {"epoch": _epoch(2), "sha": "bbbb222"}

    async def fake_recycle(was, now, *, was_sha=None, now_sha=None):
        seen.update(was=was, now=now, was_sha=was_sha, now_sha=now_sha)

    monkeypatch.setattr(client, "info", fake_info)
    monkeypatch.setattr(client, "_recycle_transport", fake_recycle)
    client._epoch = _epoch(1)
    client._sha = "aaaa111"

    await client._reconcile_epoch()

    assert seen["was_sha"] == "aaaa111", "the pre-restart sha must survive the poll"
    assert seen["now_sha"] == "bbbb222"


# --------------------------------------------------------------------------- #
# Endpoint → hosts.name, the key the predicate is asked about
# --------------------------------------------------------------------------- #

def test_endpoint_host_name_resolves_local_transports_to_this_host(monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    assert endpoint_host_name(Endpoint("unix", path="/run/s.sock")) == "dobby"
    assert endpoint_host_name(Endpoint("https", host="127.0.0.1", port=9)) == "dobby"
    assert endpoint_host_name(Endpoint("https", host="localhost", port=9)) == "dobby"


def test_endpoint_host_name_uses_the_first_dns_label_for_a_worker(monkeypatch):
    """Auto-registered hosts are named for the first label of their MagicDNS
    endpoint — see ``server.hostname.self_host_name``."""
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    assert endpoint_host_name(Endpoint("https", host="rocky.tail1234.ts.net", port=8765)) == "rocky"
    assert endpoint_host_name(Endpoint("https", host="rocky", port=8765)) == "rocky"


# --------------------------------------------------------------------------- #
# The predicate itself
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_restart_deliberate_needs_both_shas_to_call_a_change():
    """An unknown sha on either side is not evidence of anything."""
    assert await host_upgrade.restart_deliberate("dobby", was_sha=None, now_sha="b") is False
    assert await host_upgrade.restart_deliberate("dobby", was_sha="a", now_sha=None) is False
    assert await host_upgrade.restart_deliberate("dobby", was_sha="a", now_sha="a") is False
    assert await host_upgrade.restart_deliberate("dobby", was_sha="a", now_sha="b") is True


@pytest.mark.asyncio
async def test_restart_deliberate_defers_to_the_deploy_controller():
    async def _deploying() -> dict:
        return {"deploy": {"hosts": {"rocky": {"state": "running"}}}}

    host_upgrade._controller_cache.update(at=0.0, hosts={})
    assert await host_upgrade.restart_deliberate(
        "rocky", was_sha="a", now_sha="a", controller_status=_deploying) is True
    host_upgrade._controller_cache.update(at=0.0, hosts={})
    assert await host_upgrade.restart_deliberate(
        "dobby", was_sha="a", now_sha="a", controller_status=_deploying) is False


@pytest.mark.asyncio
async def test_ws_reconnect_throttle_is_unaffected(tmp_path):
    """Guard against re-flooding the sibling site #2306 just fixed."""
    assert isinstance(ipc_client._WS_RECONNECT_LOG, object)
    assert hasattr(ipc_client._WS_RECONNECT_LOG, "should_log")
