"""#2701/#2749: one unreachable probe is load or a restart, not a fault worth an issue.

``DaemonTimeout`` means the daemon is up and its event loop was too starved to answer
inside the deadline. On 2026-10-06 dobby sat at load 28 with multi-second loop stalls,
each stalled probe tick wrote a WARNING, and ``server.obs.errsink`` filed it. The probe
now reports a lone timeout at INFO and escalates only a run of them.

``healthy=False`` is NOT debounced: dispatch must avoid a starved host from the first
timeout. Only the log level — the auto-error filing decision — waits for the run.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

ENDPOINT = "https://dobby:9443"


@pytest.fixture(autouse=True)
def _fresh_probe_state():
    from server.sessions import host_probe

    def clear():
        host_probe._PROBE_FAILURE_LOG.clear()
        host_probe._CONSECUTIVE_UNREACHABLE.clear()
    clear()
    yield
    clear()


def _run_probes(tmp_path, outcomes):
    """Probe dobby once per outcome (an exception to raise, or an /info dict to return).

    Returns the persisted ``healthy`` after each probe.
    """
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.host_probe import probe_host

    queue = list(outcomes)

    async def fetch(endpoint):
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint=ENDPOINT))
        return [(await probe_host("dobby", ENDPOINT, fetch=fetch))["healthy"]
                for _ in outcomes]

    return asyncio.run(go())


def _levels(caplog):
    return [r.levelno for r in caplog.records if "probe_failed" in r.getMessage()]


def _timeout():
    from server.ipc.client import DaemonTimeout
    return DaemonTimeout("/info timed out: ")


def test_a_lone_timeout_is_info_but_still_marks_the_host_unhealthy(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        healthy = _run_probes(tmp_path, [_timeout()])

    assert _levels(caplog) == [logging.INFO]
    assert healthy == [False], "dispatch must avoid the host from the first timeout"


def test_a_run_of_timeouts_escalates_to_warning_on_the_nth(tmp_path, caplog):
    from server.sessions.host_probe import UNREACHABLE_TIMEOUT_ESCALATE_AFTER as N

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        _run_probes(tmp_path, [_timeout() for _ in range(N)])

    levels = _levels(caplog)
    assert levels[0] == logging.INFO
    assert levels[-1] == logging.WARNING, "a sustained timeout is a real condition"
    assert logging.WARNING not in levels[:-1]
    last = [r for r in caplog.records if "probe_failed" in r.getMessage()][-1]
    assert last.fields["host"] == "dobby"


def test_timeouts_stay_info_past_the_refusal_threshold(tmp_path, caplog):
    """#2899: a load-starved daemon (timeouts) outlasts 3 probes without being an outage."""
    from server.sessions.host_probe import (UNREACHABLE_ESCALATE_AFTER,
                                            UNREACHABLE_TIMEOUT_ESCALATE_AFTER as N)

    assert N > UNREACHABLE_ESCALATE_AFTER
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        _run_probes(tmp_path, [_timeout() for _ in range(N - 1)])

    assert logging.WARNING not in _levels(caplog)


def test_a_success_ends_the_run(tmp_path, caplog):
    """N timeouts that are not CONSECUTIVE never escalate — the load-spike shape."""
    from server.sessions.host_probe import UNREACHABLE_TIMEOUT_ESCALATE_AFTER as N

    outcomes: list = []
    for _ in range(N):
        outcomes += [_timeout(), _timeout(), {"sha": "abc"}]
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        healthy = _run_probes(tmp_path, outcomes)

    assert logging.WARNING not in _levels(caplog)
    assert healthy[2] is True, "the answered probe restores health"


def _refused():
    from server.ipc.client import DaemonUnavailable
    return DaemonUnavailable("All connection attempts failed")


def test_a_lone_refused_connection_is_info_but_still_marks_the_host_unhealthy(
        tmp_path, caplog):
    """#2749: a watchdog restart of a wedged daemon refuses connections for ~10-20s.

    One ``DaemonUnavailable`` is that restart window, not an outage; only a run is.
    """
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        healthy = _run_probes(tmp_path, [_refused()])

    assert _levels(caplog) == [logging.INFO]
    assert healthy == [False], "dispatch must avoid the host from the first refusal"


def test_a_run_of_refused_connections_escalates_to_warning_on_the_nth(tmp_path, caplog):
    from server.sessions.host_probe import UNREACHABLE_ESCALATE_AFTER

    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        _run_probes(tmp_path, [_refused() for _ in range(UNREACHABLE_ESCALATE_AFTER)])

    levels = _levels(caplog)
    assert levels[-1] == logging.WARNING, "a daemon that stays down is a real outage"
    assert logging.WARNING not in levels[:-1]


def test_timeouts_and_refusals_count_as_one_run(tmp_path, caplog):
    """A wedge (timeouts) followed by its restart (refusals) is ONE unreachable spell.

    The refusal is judged against the refusal threshold, so it escalates the run.
    """
    from server.sessions.host_probe import UNREACHABLE_ESCALATE_AFTER

    before = [_timeout() for _ in range(UNREACHABLE_ESCALATE_AFTER - 1)]
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        _run_probes(tmp_path, [*before, _refused()])

    warnings = [r for r in caplog.records
                if "probe_failed" in r.getMessage() and r.levelno == logging.WARNING]
    assert [r.fields["error"] for r in warnings] == ["All connection attempts failed"]


def test_a_success_after_a_restart_window_ends_the_run(tmp_path, caplog):
    """The #2749 shape: refused during the restart, answered once it is back up."""
    from server.sessions.host_probe import UNREACHABLE_ESCALATE_AFTER

    outcomes: list = []
    for _ in range(UNREACHABLE_ESCALATE_AFTER):
        outcomes += [_timeout(), _refused(), {"sha": "abc"}]
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        _run_probes(tmp_path, outcomes)

    assert logging.WARNING not in _levels(caplog)


def test_a_non_daemon_failure_ends_the_run_and_warns_at_once(tmp_path, caplog):
    """Anything outside the daemon-unreachable family is not debounced."""
    from server.sessions.host_probe import UNREACHABLE_ESCALATE_AFTER

    before = [_timeout() for _ in range(UNREACHABLE_ESCALATE_AFTER - 1)]
    with caplog.at_level(logging.DEBUG, logger="server.sessions.host_probe"):
        _run_probes(tmp_path, [*before, RuntimeError("bad /info payload"), _timeout()])

    warnings = [r for r in caplog.records
                if "probe_failed" in r.getMessage() and r.levelno == logging.WARNING]
    assert [r.fields["error"] for r in warnings] == ["bad /info payload"]
