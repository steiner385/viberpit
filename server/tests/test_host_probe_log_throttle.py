"""One unreachable host must not file one auto-error issue per failure text.

``probe_loop`` re-enters the failure path every 30s, so a host that is down for a
day produces ~2880 identical WARNING records. ``server.obs.errsink`` captures
WARNING+ and files one issue per message fingerprint, which is how a single dead
``rocky`` and a single dead ``dobby`` became four auto-error issues carrying no
information past their first record: #1507 and #1669 (rocky, ``/info timed out``
and ``All connection attempts failed``) and #1724 / #1676 (the same two for
dobby).

The fix routes the record through ``server.obs.logdedupe`` — the repo's existing
answer to this shape — keyed per host and per exception class. The tests below
pin the four properties that make that key the right one:

* repeats of the SAME failure are demoted (the flood),
* a DIFFERENT failure class on the same host still reports (the two texts that
  filed two separate issues are two separate conditions),
* another host is never suppressed by the first (the key is per host),
* and an outage that outlives a deploy restart still escalates INFO -> WARNING.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


def _seed(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


@pytest.fixture(autouse=True)
def _fresh_throttles():
    """Module-level throttles outlive a test; start every one from no window."""
    from server.sessions import host_probe

    host_probe._PROBE_FAILURE_LOG.clear()
    host_probe._DEPLOY_STATE_LOG.clear()
    host_probe._CONSECUTIVE_TIMEOUTS.clear()
    yield
    host_probe._PROBE_FAILURE_LOG.clear()
    host_probe._DEPLOY_STATE_LOG.clear()
    host_probe._CONSECUTIVE_TIMEOUTS.clear()


def _probe_records(caplog):
    return [r for r in caplog.records if "probe_failed" in r.getMessage()]


def test_a_hosts_repeated_probe_failure_warns_once_and_demotes_the_repeats(
        tmp_path, caplog):
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def failing_fetch(endpoint):
        raise ConnectionError("connection refused")

    async def run():
        await _seed(tmp_path, "t1.db",
                    Host(name="rocky", endpoint="https://rocky:9443"))()
        for _ in range(5):
            await probe_all(fetch=failing_fetch)

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        asyncio.run(run())

    records = _probe_records(caplog)
    assert len(records) == 5, "every failure is still written somewhere"
    assert [r.levelno for r in records] == [logging.WARNING] + [logging.DEBUG] * 4


def test_a_different_failure_class_on_the_same_host_reports_immediately(
        tmp_path, caplog):
    """The two texts that filed #1507 and #1669 are two exception classes.

    ``DaemonTimeout`` (the daemon answered too slowly) and ``DaemonUnavailable``
    (nothing listening) are different conditions with different fixes, so one
    must not sit inside the other's window. The timeout is a SUSTAINED one here:
    a lone timeout is not a WARNING at all (#2701, test_host_probe_timeout_debounce).
    """
    from server.db.models import Host
    from server.ipc.client import DaemonTimeout, DaemonUnavailable
    from server.sessions.host_probe import TIMEOUT_ESCALATE_AFTER, probe_all

    failures = iter([DaemonTimeout("/info timed out: "),
                     DaemonUnavailable("All connection attempts failed")])
    pending: list[Exception] = [next(failures)]

    async def failing_fetch(endpoint):
        raise pending[0]

    async def run():
        await _seed(tmp_path, "t2.db",
                    Host(name="rocky", endpoint="https://rocky:9443"))()
        for _ in range(TIMEOUT_ESCALATE_AFTER):
            await probe_all(fetch=failing_fetch)
        pending[0] = next(failures)
        await probe_all(fetch=failing_fetch)

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        asyncio.run(run())

    warnings = [r for r in _probe_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "timed out" in warnings[0].fields["error"]
    assert "All connection attempts failed" in warnings[1].fields["error"]


def test_a_second_failing_host_is_not_suppressed_by_the_first(tmp_path, caplog):
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def failing_fetch(endpoint):
        raise ConnectionError("connection refused")

    async def run():
        await _seed(tmp_path, "t3.db",
                    Host(name="rocky", endpoint="https://rocky:9443"),
                    Host(name="dobby", endpoint="https://dobby:9443"))()
        await probe_all(fetch=failing_fetch)

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        asyncio.run(run())

    warnings = [r for r in _probe_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    hosts = [r.fields["host"] for r in warnings]
    assert "rocky" in hosts
    assert "dobby" in hosts


def test_a_still_dead_host_re_reports_once_the_window_elapses(
        tmp_path, caplog, monkeypatch):
    """A window, not a latch: "still down" stays answerable from the journal."""
    from server.db.models import Host
    from server.obs.logdedupe import DEFAULT_WINDOW_S, LogThrottle
    from server.sessions import host_probe

    now = [0.0]
    monkeypatch.setattr(host_probe, "_PROBE_FAILURE_LOG",
                        LogThrottle(clock=lambda: now[0]))

    async def failing_fetch(endpoint):
        raise ConnectionError("connection refused")

    async def run():
        await _seed(tmp_path, "t4.db",
                    Host(name="rocky", endpoint="https://rocky:9443"))()
        await host_probe.probe_all(fetch=failing_fetch)
        now[0] += DEFAULT_WINDOW_S / 2
        await host_probe.probe_all(fetch=failing_fetch)
        now[0] += DEFAULT_WINDOW_S
        await host_probe.probe_all(fetch=failing_fetch)

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        asyncio.run(run())

    assert [r.levelno for r in _probe_records(caplog)] == [
        logging.WARNING, logging.DEBUG, logging.WARNING]


def test_an_outage_that_outlives_the_deploy_restart_escalates_to_warning(
        tmp_path, caplog):
    """#1614 demotes a restart-driven outage to INFO. That demotion must not
    become a six-hour gag on the WARNING that says the restart never finished."""
    from unittest import mock

    from server.db.models import Host
    from server.sessions import host_upgrade
    from server.sessions.host_probe import probe_all

    restarting = [True]

    async def failing_fetch(endpoint):
        raise ConnectionError("connection refused")

    async def expected(name, **_):
        return restarting[0]

    async def run():
        await _seed(tmp_path, "t5.db",
                    Host(name="rocky", endpoint="https://rocky:9443"))()
        with mock.patch.object(host_upgrade, "restart_expected", expected):
            await probe_all(fetch=failing_fetch)
            restarting[0] = False
            await probe_all(fetch=failing_fetch)

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        asyncio.run(run())

    records = _probe_records(caplog)
    assert [r.levelno for r in records] == [logging.INFO, logging.WARNING]
    assert records[0].fields.get("restart") == "in_progress"


def test_an_unreadable_deploy_state_warns_once_per_window(tmp_path, caplog):
    """The sibling flood in the same tick: the skew gate's own fallback WARNING."""
    from server.db.models import Host
    from server.sessions import host_probe

    async def boom():
        raise RuntimeError("deploy state unreadable")

    async def ok_fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1}

    async def run():
        await _seed(tmp_path, "t6.db",
                    Host(name="rocky", endpoint="https://rocky:9443"))()
        for _ in range(4):
            await host_probe.probe_all(fetch=ok_fetch)

    host_probe_state = host_probe.read_deploy_state
    try:
        host_probe.read_deploy_state = boom  # type: ignore[assignment]
        with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
            asyncio.run(run())
    finally:
        host_probe.read_deploy_state = host_probe_state  # type: ignore[assignment]

    records = [r for r in caplog.records if "deploy_state unreadable" in r.getMessage()]
    assert len(records) == 4
    assert [r.levelno for r in records] == [logging.WARNING] + [logging.DEBUG] * 3
