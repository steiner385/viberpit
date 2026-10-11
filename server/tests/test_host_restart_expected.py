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
from fastapi.testclient import TestClient

from server.deploy import app as deploy_app
from server.deploy.client import DeployControllerUnavailable
from server.deploy.detect import DetectLoop, PendingState
from server.deploy.types import DeployStatus
from server.sessions import host_upgrade


@pytest.fixture(autouse=True)
def _fresh_state():
    host_upgrade._reset_state_for_tests()
    yield
    host_upgrade._reset_state_for_tests()


class _Controller:
    """Minimal `server.deploy.app._ControllerLike`: a status to serve, nothing to run."""

    def __init__(self, hosts: dict[str, dict]):
        self.status = DeployStatus(target_sha="abc", running=bool(hosts), hosts=hosts)

    async def deploy_once(self, target_sha: str | None = None, *,
                          force: bool = False) -> DeployStatus:
        # Part of the protocol, never part of this test: /status only reads `.status`.
        raise AssertionError("the /status route must not start a deploy")


def _controller_payload(**hosts) -> dict:
    """What the deploy controller's real ``/status`` route serves for these host states.

    Built through the route rather than written by hand: the hand-written fake this
    replaces put ``hosts`` at the top level, the real payload nests it under
    ``deploy``, and every test here passed while production never once saw a
    controller-driven restart (#2181)."""
    loop = DetectLoop("/r", compute=lambda r: PendingState())
    app, _, _ = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_python="/v", daemon_enabled=False,
        detect_loop=loop, controller=_Controller({n: {"state": s} for n, s in hosts.items()}))
    return TestClient(app).get("/status").json()


def _status(**hosts):
    payload = _controller_payload(**hosts)

    async def fetch():
        return payload
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
    payload = _controller_payload(rocky="running")

    async def counting():
        nonlocal calls
        calls += 1
        return payload

    async def go():
        return [await host_upgrade.restart_expected("rocky", controller_status=counting)
                for _ in range(6)]

    assert asyncio.run(go()) == [True] * 6
    assert calls == 1


def test_stale_cache_window_is_bounded_by_the_shrunk_ttl(monkeypatch):
    """#2112: a controller-driven restart flips a host to "running" the instant its
    deploy step begins, but the cache only refetches on its own timer — so a failure
    landing inside that TTL window still sees the pre-transition snapshot and gets
    misclassified as a genuine outage (WARNING) instead of a deliberate restart
    (INFO). The fix cannot eliminate the gap (it is inherent to a pulled cache), but
    it must be bounded by ``_CONTROLLER_STATUS_TTL_S``, not by the 5 s value #2112
    was filed against.
    """
    now = 1_000.0
    monkeypatch.setattr(host_upgrade.time, "monotonic", lambda: now)

    calls = 0
    running_since = 100.0  # a deploy step marks this host "running" at this instant

    async def fetch():
        nonlocal calls
        calls += 1
        state = "running" if now >= running_since else "done"
        payload = _controller_payload(rocky=state)
        return payload

    async def check() -> bool:
        return await host_upgrade.restart_expected("rocky", controller_status=fetch)

    # Populate the cache just BEFORE the transition: a stale snapshot showing "not
    # running" is now cached for one TTL.
    now = running_since - 0.01
    assert asyncio.run(check()) is False
    assert calls == 1

    # The transition happens, but the cache is still within its TTL: same stale
    # snapshot, still misclassified — this is the unavoidable, bounded blind spot.
    now = running_since + (host_upgrade._CONTROLLER_STATUS_TTL_S / 2)
    assert asyncio.run(check()) is False
    assert calls == 1

    # Once the TTL elapses, the next failure gets a fresh read and classifies
    # correctly. With the old 5 s TTL this would still be stale here.
    now = running_since + host_upgrade._CONTROLLER_STATUS_TTL_S + 0.01
    assert asyncio.run(check()) is True
    assert calls == 2
