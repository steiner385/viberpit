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
    def __init__(self, codex=None, records=None):
        self._codex = codex
        self._records = records or {}


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


def test_build_daemon_attaches_the_orchestrator_to_app_state():
    """The wiring itself, at the seam that had been missing it."""
    from server.ipc.serve import build_daemon

    app, orchestrator = build_daemon()
    assert app.state.orchestrator is orchestrator
