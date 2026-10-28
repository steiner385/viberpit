"""Fleet capacity alarms (#1517): a host over its cap, and a fleet with no
dispatchable host, must surface as a WARNING before they surface as a launch
failure — once per spell, not once per 30 s probe tick."""
from __future__ import annotations

import asyncio
import logging

import pytest

from server.sessions.fleet import HostCandidate
from server.sessions.fleet_capacity_alarm import (
    NO_HOST_ESCALATE_AFTER, OVER_CAP_ESCALATE_AFTER, CapacityAlarm)

LOGGER = "server.sessions.fleet_capacity_alarm"


def _host(name="dobby", *, active=0, cap=6, healthy=True, enabled=True):
    return HostCandidate(name=name, enabled=enabled, healthy=healthy, weight=1,
                         active_sessions=active, max_sessions=cap, load_avg=None,
                         mem_available_mb=None)


def _warnings(caplog):
    return [r for r in caplog.records
            if r.name == LOGGER and r.levelno >= logging.WARNING]


def _tick(alarm, n, *hosts):
    for _ in range(n):
        alarm.observe(hosts)


def test_over_cap_warns_once_after_the_run_and_clears(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    _tick(alarm, OVER_CAP_ESCALATE_AFTER - 1, _host(active=11))
    assert _warnings(caplog) == []
    _tick(alarm, 50, _host(active=11))
    [w] = _warnings(caplog)
    assert "dobby" in w.getMessage() and "11" in w.getMessage()
    caplog.clear()
    alarm.observe([_host(active=2)])
    assert any("back within its cap" in r.getMessage() for r in caplog.records)
    # A fresh spell warns again.
    caplog.clear()
    _tick(alarm, OVER_CAP_ESCALATE_AFTER, _host(active=7))
    assert len(_warnings(caplog)) == 1


def test_an_over_cap_blip_shorter_than_the_run_never_warns(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    for _ in range(5):
        _tick(alarm, OVER_CAP_ESCALATE_AFTER - 1, _host(active=7))
        alarm.observe([_host(active=6)])
    assert _warnings(caplog) == []


def test_at_cap_is_info_on_entry_and_exit_only(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    _tick(alarm, 40, _host(active=6))
    assert _warnings(caplog) == []
    entered = [r for r in caplog.records if "is at its cap" in r.getMessage()]
    assert len(entered) == 1
    alarm.observe([_host(active=5)])
    assert any("headroom again" in r.getMessage() for r in caplog.records)


def test_uncapped_host_never_alarms(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    _tick(alarm, 40, _host(active=99, cap=None))
    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_over_cap_counts_per_host(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    _tick(alarm, OVER_CAP_ESCALATE_AFTER, _host("dobby", active=11),
          _host("rocky", active=7))
    names = sorted(w.getMessage().split()[2] for w in _warnings(caplog))
    assert names == ["dobby", "rocky"]


def test_no_dispatchable_host_warns_once_after_the_run(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    down = (_host("dobby", healthy=False), _host("rocky", healthy=None))
    _tick(alarm, NO_HOST_ESCALATE_AFTER - 1, *down)
    assert _warnings(caplog) == []
    _tick(alarm, 20, *down)
    [w] = _warnings(caplog)
    assert "no dispatchable host" in w.getMessage()
    caplog.clear()
    alarm.observe([_host("dobby", healthy=True), _host("rocky", healthy=None)])
    assert any("dispatchable host is back" in r.getMessage() for r in caplog.records)


def test_a_full_but_healthy_fleet_is_not_no_dispatchable_host(caplog):
    """Full is the queue's job, and each full host already has its own signal."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    _tick(alarm, 20, _host("dobby", active=6), _host("rocky", active=4, cap=4))
    assert _warnings(caplog) == []


def test_disabled_hosts_are_ignored(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    # Nothing enabled is an operator's choice, not an outage; a disabled host
    # over its cap is draining by design.
    _tick(alarm, 20, _host("dobby", enabled=False, healthy=False, active=11))
    assert _warnings(caplog) == []


@pytest.fixture
def _fresh_alarm():
    from server.sessions import host_probe

    host_probe.CAPACITY_ALARM.clear()
    host_probe._CONSECUTIVE_UNREACHABLE.clear()
    yield
    host_probe.CAPACITY_ALARM.clear()
    host_probe._CONSECUTIVE_UNREACHABLE.clear()


def test_probe_all_feeds_the_alarm_from_the_registry(tmp_path, caplog, _fresh_alarm):
    """The 2026-09-09 shape end to end: /info publishes 11 against a cap of 6."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'alarm.db'}")
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint="https://dobby:8737", enabled=True,
                        max_sessions=6))

        async def fetch(_endpoint):
            return {"sha": "abc1234", "protocol": 1, "activeSessions": 11}

        for _ in range(OVER_CAP_ESCALATE_AFTER):
            await probe_all(fetch=fetch)

    caplog.set_level(logging.INFO, logger=LOGGER)
    asyncio.run(go())
    [w] = _warnings(caplog)
    assert "dobby" in w.getMessage() and "11" in w.getMessage()


# --- #3173: a deploy that restarts / skew-fences every host is not an outage ---

_DOWN = (_host("dobby", healthy=False), _host("rocky", healthy=False))


def test_no_host_spell_a_deploy_explains_is_info_not_warning(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    for _ in range(NO_HOST_ESCALATE_AFTER * 5):
        alarm.observe(_DOWN, deploy_excused={"dobby", "rocky"})
    assert _warnings(caplog) == []
    noted = [r for r in caplog.records if "while a deploy" in r.getMessage()]
    assert len(noted) == 1 and noted[0].levelno == logging.INFO
    # The deploy's spell still gets its recovery line, so the journal answers
    # "when was the fleet undispatchable".
    alarm.observe([_host("dobby"), _host("rocky")])
    assert any("dispatchable host is back" in r.getMessage() for r in caplog.records)


def test_no_host_spell_that_outlives_the_deploy_warns_after_a_full_run(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    _tick(alarm, NO_HOST_ESCALATE_AFTER - 1, *_DOWN)  # run started before the deploy
    for _ in range(10):
        alarm.observe(_DOWN, deploy_excused={"dobby", "rocky"})
    # Deploy over, fleet still down: the run restarts from zero, then escalates.
    _tick(alarm, NO_HOST_ESCALATE_AFTER - 1, *_DOWN)
    assert _warnings(caplog) == []
    _tick(alarm, 5, *_DOWN)
    [w] = _warnings(caplog)
    assert "no dispatchable host" in w.getMessage()


def test_no_host_spell_only_partly_explained_by_a_deploy_still_warns(caplog):
    """rocky restarting for the deploy does not explain dobby being down."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    alarm = CapacityAlarm()
    for _ in range(NO_HOST_ESCALATE_AFTER):
        alarm.observe(_DOWN, deploy_excused={"rocky"})
    assert len(_warnings(caplog)) == 1


@pytest.fixture
def _no_upgrades():
    from server.sessions import host_upgrade

    host_upgrade._reset_state_for_tests()
    yield host_upgrade
    host_upgrade._reset_state_for_tests()


def _row(name, *, healthy=False, last_error=None, enabled=True):
    from types import SimpleNamespace
    return SimpleNamespace(name=name, enabled=enabled, healthy=healthy,
                           last_error=last_error)


def test_deploy_excused_covers_restarting_and_skew_fenced_hosts(monkeypatch, _no_upgrades):
    from server.sessions import host_probe

    host_upgrade = _no_upgrades

    async def status():
        return {"deploy": {"running": True,
                           "hosts": {"dobby": {"state": "running"},
                                     "rocky": {"state": "done"}}}}

    monkeypatch.setattr(host_upgrade, "_default_controller_status", status)
    rows = [_row("dobby", last_error="DaemonUnavailable: All connection attempts failed"),
            _row("rocky", last_error="version skew: daemon on abc1234, deployed def5678"),
            _row("spare", last_error="DaemonTimeout: /info timed out"),
            _row("up", healthy=True)]
    assert asyncio.run(host_probe._deploy_excused(rows)) == {"dobby", "rocky"}


def test_deploy_excused_is_empty_with_no_deploy_running(monkeypatch, _no_upgrades):
    """A skew fence outside a deploy is a real fault — it is what the alarm is for."""
    from server.sessions import host_probe

    host_upgrade = _no_upgrades

    async def status():
        return {"deploy": {"running": False, "hosts": {"rocky": {"state": "done"}}}}

    monkeypatch.setattr(host_upgrade, "_default_controller_status", status)
    rows = [_row("rocky", last_error="version skew: daemon on abc1234, deployed def5678")]
    assert asyncio.run(host_probe._deploy_excused(rows)) == set()


def test_deploy_running_reads_a_fleet_ui_upgrade(_no_upgrades):
    host_upgrade = _no_upgrades

    async def no_controller():
        raise OSError("no controller socket")

    assert asyncio.run(host_upgrade.deploy_running(controller_status=no_controller)) is False
    host_upgrade._STATE["rocky"] = {"state": "running"}
    assert asyncio.run(host_upgrade.deploy_running(controller_status=no_controller)) is True
