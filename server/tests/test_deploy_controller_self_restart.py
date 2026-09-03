"""A deploy that changes deploy code must restart the deploy controller.

A deploy restarts ccswitch.service (steps.restart_edge) and every fleet daemon
(server.deploy.runners) — never ccswitch-deploy-controller.service. So the process
that RUNS deploys keeps executing whatever code it started with.

Observed 2026-08-21: #719 taught the (then) restart_units to update a remote daemon's
checkout, that landed on disk at 18:18, and the controller kept running its
16:56 code. The very next deploy therefore did NOT update rocky, which stayed on
a stale commit — the failure #719 exists to prevent, reintroduced by the deployer
being unable to update itself.

The restart must be DEFERRED: the controller is the process performing the
deploy, so restarting it inline would kill the deploy that requested it.
"""
from __future__ import annotations

import pytest

from server.deploy import steps


def _recorder(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()
    return run


@pytest.mark.parametrize("path,expected", [
    ("server/deploy/steps.py", True),
    ("server/deploy/controller.py", True),
    ("server/deploy/__init__.py", True),
    ("server/api/sessions.py", False),
    ("server/ipc/daemon.py", False),
    ("web/src/App.tsx", False),
])
def test_deploy_code_detection(path, expected):
    assert steps.deploy_code_changed([path]) is expected


def test_empty_change_list_is_not_deploy_code():
    """all([]) is True — an empty list must not be read as 'everything changed'.

    classify_change has exactly this trap: `all(_EDGE_ONLY.match(f) ...)` returns
    True for [], silently classifying an empty diff as edge-only.
    """
    assert steps.deploy_code_changed([]) is False
    assert steps.deploy_code_changed(None) is False


def test_restart_is_deferred_not_inline():
    """Restarting inline would kill the running deploy, so it must be scheduled."""
    calls: list[list[str]] = []
    assert steps.schedule_controller_restart(run=_recorder(calls)) is True
    flat = " ".join(" ".join(c) for c in calls)
    assert "ccswitch-deploy-controller" in flat
    # Deferred via systemd-run --on-active, never a bare `systemctl restart`.
    assert "systemd-run" in flat and "--on-active" in flat
    assert ["sudo", "systemctl", "restart", "ccswitch-deploy-controller.service"] not in calls


def test_scheduling_failure_is_reported_not_raised():
    """A deploy that already succeeded must not be failed by this bookkeeping —
    but it must not silently claim the controller was refreshed either."""
    def failing_run(cmd, **kw):
        raise OSError("systemd-run missing")

    assert steps.schedule_controller_restart(run=failing_run) is False
