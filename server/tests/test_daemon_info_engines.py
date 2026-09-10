"""`/info` advertises what this daemon can run, and reports its real session count.

Two things are pinned here. The first is the plan's `/info.engines` contract. The
second is that `/info` is wired to a live orchestrator at all: the handler has
always read ``app.state.orchestrator``, but nothing ever set it, so every daemon
reported ``activeSessions: 0`` and the ranker's headroom term was dead weight.
Publishing engines through the same handle makes that wiring load-bearing, and
this test keeps it that way.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.ipc.daemon import create_daemon_app


class _Codex:
    def __init__(self, availability):
        self.availability = availability


class _Orch:
    """Stands in for the live orchestrator at the /info seam.

    ``owned`` lets a test separate the two numbers that used to be one: what the
    daemon HOLDS (``_records``) and what it may report as occupied capacity. /info
    must publish the second — see test_info_publishes_the_owned_count_not_len_records.
    """

    def __init__(self, codex=None, records=None, owned=None):
        self._codex = codex
        self._records = records or {}
        self._owned = owned

    async def owned_active_session_count(self) -> int:
        return len(self._records) if self._owned is None else self._owned

    def _active_session_count(self) -> int:
        return len(self._records)


def _info(orchestrator=None) -> dict:
    app = create_daemon_app()
    if orchestrator is not None:
        app.state.orchestrator = orchestrator
    with TestClient(app) as client:
        return client.get("/info").json()


def test_info_advertises_claude_even_with_no_orchestrator_attached():
    """A daemon must never look engine-less; Claude is what a daemon IS."""
    body = _info()
    assert body["engines"]["claude_code"]["available"] is True


def test_info_advertises_a_ready_codex_with_its_version_and_contract():
    body = _info(_Orch(_Codex(CodexAvailability.ready(
        binary_path="/usr/bin/codex",
        cli_version="codex-cli 0.153.2",
        contract_sha256="deadbeef",
    ))))
    codex = body["engines"]["codex"]
    assert codex["available"] is True
    assert codex["cliVersion"] == "codex-cli 0.153.2"
    assert codex["contractSha256"] == "deadbeef"
    assert codex["externalAuth"] is True


def test_info_reports_an_unavailable_codex_without_disturbing_claude():
    body = _info(_Orch(_Codex(CodexAvailability.unavailable(
        CodexUnavailableReason.BINARY_NOT_FOUND, "'codex' is not installed"))))
    assert body["engines"]["codex"]["available"] is False
    assert body["engines"]["codex"]["reason"] == "binary_not_found"
    assert body["engines"]["claude_code"]["available"] is True


def test_a_daemon_that_could_not_build_a_codex_manager_still_answers():
    """`daemon_lifespan` sets `_codex = None` when the build raises. Not a 500."""
    body = _info(_Orch(codex=None))
    assert body["engines"]["codex"]["available"] is False
    assert body["engines"]["claude_code"]["available"] is True


def test_info_carries_no_free_text_from_the_runner():
    """No path/version/OS-error text reaches a payload polled every 30s per host."""
    body = _info(_Orch(_Codex(CodexAvailability.unavailable(
        CodexUnavailableReason.VERSION_PROBE_FAILED,
        "/home/agent/.local/bin/codex exploded"))))
    assert "detail" not in body["engines"]["codex"]
    assert "/home/agent" not in repr(body)


@pytest.mark.parametrize("records,expected", [({}, 0), ({"a": 1, "b": 2}, 2)])
def test_info_reports_the_live_session_count(records, expected):
    """Regression: this was hard-wired to 0 because app.state.orchestrator was unset."""
    assert _info(_Orch(records=records))["activeSessions"] == expected


def test_info_publishes_the_owned_count_not_len_records():
    """/info reports occupied CAPACITY, which is not "every record I hold".

    The handler used to publish ``len(orch._records)``. On the primary that is every
    host's rows (#852) in every status, so finished sessions and other hosts' work
    counted against this host's cap — and `_dispatchable` drops a host on
    ``active >= max``, silently and permanently (2026-09-09: 11 published against a
    cap of 6, with one live driver).
    """
    body = _info(_Orch(records={"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}, owned=1))
    assert body["activeSessions"] == 1


def test_info_degrades_to_a_number_rather_than_failing():
    """An orchestrator that cannot answer must not take /info — and the host — down.

    Degrading DOWNWARD is deliberate: under-reporting costs at most one
    over-scheduled session, which the local admission gate still catches, while
    over-reporting removes the host from the fleet.
    """
    class _Mute:
        _codex = None

    body = _info(_Mute())
    assert body["activeSessions"] == 0


def test_build_daemon_attaches_the_orchestrator_to_app_state():
    """The wiring itself, at the seam that had been missing it."""
    from server.ipc.serve import build_daemon

    app, orchestrator = build_daemon()
    assert app.state.orchestrator is orchestrator
