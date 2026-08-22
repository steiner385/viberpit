"""The deploy gate probes functional endpoints, not just liveness.

/healthz answers "the process is up and the DB schema matches". It does not read
the ledger, so a corrupt ledger left /api/accounts 500ing while /healthz stayed
green — the 2026-08-22 outage where every account showed "Unknown".

Probes treat 5xx and unreachable as deploy failures. 4xx is NOT a failure: on a
deployment where the probe is unauthenticated, 401/403 means "reached the app and
it answered", which is what this checks. Failing on 4xx would make the safety
check itself the outage.
"""
from __future__ import annotations

import pytest

from server.deploy import steps
from server.deploy.types import DeployError


def test_all_endpoints_ok_returns_no_failures():
    ok = lambda u: 200  # noqa: E731
    assert steps.probe_endpoints("http://x", ["/api/sessions", "/api/accounts"],
                                 fetch_status=ok, sleep=lambda s: None) == []


def test_5xx_is_reported_as_a_failure():
    """The corrupt-ledger case: /healthz green, /api/accounts 500."""
    def st(u):
        return 500 if "accounts" in u else 200
    bad = steps.probe_endpoints("http://x", ["/api/sessions", "/api/accounts"],
                                attempts=2, fetch_status=st, sleep=lambda s: None)
    assert bad == ["/api/accounts"]


def test_unreachable_is_a_failure():
    def st(u):
        raise OSError("connection refused")
    bad = steps.probe_endpoints("http://x", ["/api/sessions"], attempts=2,
                                fetch_status=st, sleep=lambda s: None)
    assert bad == ["/api/sessions"]


@pytest.mark.parametrize("code", [401, 403, 404])
def test_4xx_is_not_a_deploy_failure(code):
    """Auth or routing config is not what this gate is for; failing here would
    make the check itself an outage on any deployment that authenticates."""
    assert steps.probe_endpoints("http://x", ["/api/sessions"],
                                 fetch_status=lambda u: code,
                                 sleep=lambda s: None) == []


def test_transient_5xx_recovers_within_attempts():
    """A still-warming app must not fail the deploy on its first response."""
    seq = iter([503, 200])
    assert steps.probe_endpoints("http://x", ["/api/sessions"], attempts=3,
                                 fetch_status=lambda u: next(seq, 200),
                                 sleep=lambda s: None) == []


def test_controller_helper_raises_on_failure():
    """The deploy must fail loudly, not append a warning nobody reads."""
    with pytest.raises(DeployError):
        steps.require_endpoints_healthy(
            "http://x", ["/api/accounts"], attempts=1,
            fetch_status=lambda u: 500, sleep=lambda s: None)
