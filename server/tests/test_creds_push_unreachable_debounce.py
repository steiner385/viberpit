"""A refused creds_sync push is INFO until the runner's lease is actually at risk (#2757)."""
import asyncio
import logging

import pytest

from server.ipc.client import DaemonTimeout, DaemonUnavailable
from server.sessions import fleet_orchestrator as fo
from server.sessions.fleet_orchestrator import FleetOrchestrator
from viberpit import credential_envelope as ce


class _Router:
    def __init__(self, hosts):
        self._h = hosts

    def get(self, name):
        return self._h[name]


class _Runner:
    """Answers each push with the next scripted outcome: an exception to raise, or ack."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    async def creds_sync(self, envelopes, lease_s):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"acks": {e["account"]: e["generation"] for e in envelopes}}


def _refused():
    return DaemonUnavailable("All connection attempts failed")


@pytest.fixture
def push(monkeypatch):
    env = ce.Envelope("a", "setup_token", "tok", None, 2)
    monkeypatch.setattr("server.sessions.claude_authority.mint_envelopes", lambda names: [env])
    monkeypatch.setattr("viberpit.accounts._anthropic_account_names", lambda: ["a"])

    async def _no_restart(name):
        return False
    monkeypatch.setattr(fo.host_upgrade, "restart_expected", _no_restart)

    def _make(runner):
        fleet = FleetOrchestrator(_Router({"dobby": _Runner(), "rocky": runner}), primary="dobby")

        async def _names():
            return ["dobby", "rocky"]
        fleet._fanout_names = _names
        return lambda: asyncio.run(fleet.push_credentials())
    return _make


def _levels(caplog):
    return [r.levelno for r in caplog.records if "fleet.creds_sync" in r.getMessage()]


def test_threshold_is_derived_from_lease_and_cadence():
    assert fo.CREDS_UNREACHABLE_ESCALATE_AFTER == 3


def test_refused_pushes_are_info_until_the_lease_is_at_risk(push, caplog):
    tick = push(_Runner(_refused(), DaemonTimeout("slow"), _refused(), _refused()))
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        for _ in range(4):
            tick()
    assert _levels(caplog) == [logging.INFO, logging.INFO, logging.WARNING, logging.WARNING]


def test_a_successful_push_resets_the_run(push, caplog):
    tick = push(_Runner(_refused(), _refused(), None, _refused(), _refused()))
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        for _ in range(5):
            tick()
    assert _levels(caplog) == [logging.INFO] * 4


def test_a_non_transport_error_is_a_warning_at_once_and_ends_the_run(push, caplog):
    tick = push(_Runner(_refused(), _refused(), RuntimeError("boom"), _refused()))
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        for _ in range(4):
            tick()
    assert _levels(caplog) == [logging.INFO, logging.INFO, logging.WARNING, logging.INFO]


def test_an_expected_restart_keeps_the_run_quiet(push, monkeypatch, caplog):
    async def _restarting(name):
        return True
    monkeypatch.setattr(fo.host_upgrade, "restart_expected", _restarting)
    tick = push(_Runner(*[_refused() for _ in range(4)]))
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        for _ in range(4):
            tick()
    assert _levels(caplog) == [logging.INFO] * 4
