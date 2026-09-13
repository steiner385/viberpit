# server/tests/test_host_restart_expected.py
"""``host_upgrade.restart_expected``: is this host down ON PURPOSE right now?

#1614: a runner restarts on every deploy and is unreachable for 30–90 s each time.
The fleet's failure paths logged every failed poll in that window at WARNING, and
Error Triage filed the aggregate as "rocky daemon unreachable" — 19 restarts in
48 h produced ~100 warnings about a host that was doing exactly what it was told.

There are two ways a host gets restarted deliberately, and the predicate must see
both: the Fleet "Update" button (in-process ``host_upgrade`` state) and the deploy
controller's fan-out (a separate process, read through its status socket).
"""
from __future__ import annotations

import asyncio

import pytest

from server.deploy.client import DeployControllerUnavailable
from server.sessions import host_upgrade


@pytest.fixture(autouse=True)
def _fresh_state():
    host_upgrade._reset_state_for_tests()
    yield
    host_upgrade._reset_state_for_tests()


def _status(**hosts):
    async def fetch():
        return {"hosts": {n: {"state": s} for n, s in hosts.items()}}
    return fetch


def test_true_while_a_fleet_ui_upgrade_is_running():
    host_upgrade._STATE["rocky"] = {"state": "running"}
    assert asyncio.run(host_upgrade.restart_expected("rocky", controller_status=_status()))


def test_true_while_the_deploy_controller_is_on_that_runner():
    ok = asyncio.run(host_upgrade.restart_expected(
        "rocky", controller_status=_status(rocky="running")))
    assert ok is True


def test_false_when_nothing_is_touching_the_host():
    assert asyncio.run(host_upgrade.restart_expected(
        "rocky", controller_status=_status(rocky="done", dobby="running"))) is False
    assert asyncio.run(host_upgrade.restart_expected(
        "rocky", controller_status=_status())) is False


def test_false_when_the_controller_is_unreachable():
    """No controller ⇒ no evidence of a deploy ⇒ the failure is genuinely unexpected.
    Never raise from a logging decision."""
    async def down():
        raise DeployControllerUnavailable("socket missing")
    assert asyncio.run(host_upgrade.restart_expected("rocky", controller_status=down)) is False


def test_controller_status_is_cached_across_a_burst_of_failures():
    """A restart window produces a burst of failed polls (two RPCs every 5 s plus the
    probe). One status read per burst, not one per failure."""
    calls = 0

    async def counting():
        nonlocal calls
        calls += 1
        return {"hosts": {"rocky": {"state": "running"}}}

    async def go():
        return [await host_upgrade.restart_expected("rocky", controller_status=counting)
                for _ in range(6)]

    assert asyncio.run(go()) == [True] * 6
    assert calls == 1
