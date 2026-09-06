"""`GET /api/config` tells the browser which agents it may offer, and why not.

This is the single place the launch UI learns that Codex exists. Two properties
matter more than the shape: Claude must stay offerable on a fleet that has never
been probed (or the app cannot launch anything on first boot), and a registry
failure must degrade rather than blank the response that boots the whole app.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.api.config import _engine_offers
from server.db import db_session, init_db
from server.db.models import Host

H = {"Tailscale-User-Login": "testuser@example.com"}

_CODEX_UP = {
    "claude_code": {"available": True},
    "codex": {"available": True, "cliVersion": "codex-cli 0.153.2"},
}
_CODEX_DOWN = {
    "claude_code": {"available": True},
    "codex": {"available": False, "reason": "external_auth_disabled"},
}


def _offers(tmp_path, dbname: str, *hosts: Host, authority: bool = True) -> dict[str, dict]:
    """Aggregate the offers over a freshly seeded registry.

    ``authority`` defaults to True because these tests are about per-host
    capability; the edge-side authority precondition has its own tests below.
    """
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for host in hosts:
                db.add(host)
        return await _engine_offers()

    with patch("server.codex.auth_endpoint.authority_configured", return_value=authority):
        return {entry["engine"]: entry for entry in asyncio.run(run())}


def test_claude_is_offered_on_a_fleet_that_has_never_been_probed(tmp_path):
    """First boot: no host row has an advertisement yet, and Claude must still work."""
    offers = _offers(tmp_path, "c1.db", Host(name="dobby", endpoint="unix:///a"))
    assert offers["claude_code"]["available"] is True
    assert offers["claude_code"]["hosts"] == ["dobby"]


def test_claude_stays_offerable_even_with_no_hosts_at_all(tmp_path):
    """A single-host deployment mid-first-probe must still be able to launch."""
    offers = _offers(tmp_path, "c2.db")
    assert offers["claude_code"]["available"] is True
    assert offers["codex"]["available"] is False


def test_codex_is_not_offered_until_some_runner_advertises_it(tmp_path):
    offers = _offers(tmp_path, "c3.db", Host(name="dobby", endpoint="unix:///a"))
    assert offers["codex"]["available"] is False
    assert offers["codex"]["hosts"] == []
    assert offers["codex"]["unavailableReasons"] == {"dobby": "engine_not_advertised"}


def test_one_capable_runner_is_enough_to_offer_codex(tmp_path):
    """Dispatch ranks and fails over, so the question is 'any', not 'all'."""
    offers = _offers(
        tmp_path, "c4.db",
        Host(name="dobby", endpoint="unix:///a", enabled=True, engines=_CODEX_UP),
        Host(name="rocky", endpoint="unix:///b", enabled=True, engines=_CODEX_DOWN),
    )
    assert offers["codex"]["available"] is True
    assert offers["codex"]["hosts"] == ["dobby"]
    # The incapable runner is still explained, not silently dropped.
    assert offers["codex"]["unavailableReasons"] == {"rocky": "external_auth_disabled"}


def test_a_disabled_host_does_not_make_codex_offerable(tmp_path):
    offers = _offers(
        tmp_path, "c5.db",
        Host(name="dobby", endpoint="unix:///a", enabled=False, engines=_CODEX_UP),
    )
    assert offers["codex"]["available"] is False
    assert offers["codex"]["hosts"] == []


def test_capabilities_come_from_the_drivers_own_declaration(tmp_path):
    """A second copy here is how a UI ends up offering a control nothing implements."""
    from server.codex.capabilities import CODEX_CAPABILITIES
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.engine import capabilities_to_wire

    offers = _offers(tmp_path, "c6.db")
    assert offers["codex"]["capabilities"] == capabilities_to_wire(CODEX_CAPABILITIES)
    assert offers["claude_code"]["capabilities"] == capabilities_to_wire(
        ClaudeAgentDriver.capabilities)


def test_the_two_engines_really_do_differ_in_capability(tmp_path):
    """Guards against a serializer that silently reports one engine twice."""
    offers = _offers(tmp_path, "c7.db")
    codex, claude = offers["codex"]["capabilities"], offers["claude_code"]["capabilities"]
    assert codex["reasoningEffort"] is True and claude["reasoningEffort"] is False
    assert codex["subagents"] is False and claude["subagents"] is True
    # `questions` is deliberate parity (#1271), not a case of one dict aliasing
    # the other — reasoningEffort/subagents above already prove they differ.
    assert codex["questions"] is True and claude["questions"] is True


def test_codex_is_not_offered_without_a_credential_authority(tmp_path):
    """Runners authenticate ONLY with authority-minted envelopes.

    With no authority configured, every capable runner in the fleet would still
    produce a session that cannot log in — so the whole engine is off for an
    edge-side reason, and no runner is at fault.
    """
    offers = _offers(
        tmp_path, "c9.db",
        Host(name="dobby", endpoint="unix:///a", enabled=True, engines=_CODEX_UP),
        authority=False,
    )
    assert offers["codex"]["available"] is False
    assert offers["codex"]["blockedReason"] == "authority_unconfigured"
    assert offers["codex"]["unavailableReasons"] == {}
    # ...and Claude is untouched by it.
    assert offers["claude_code"]["available"] is True
    assert offers["claude_code"]["blockedReason"] is None


def test_an_unreadable_registry_degrades_to_claude_rather_than_raising(tmp_path):
    """/api/config boots the whole browser app; a fleet hiccup must not blank it."""
    with patch("server.api.config.db_session", side_effect=RuntimeError("db down")):
        offers = _offers(tmp_path, "c8.db")
    assert offers["claude_code"]["available"] is True
    assert offers["codex"]["available"] is False


def test_the_route_actually_carries_the_offers():
    """The aggregation above is only useful if the endpoint serves it."""
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        body = TestClient(app, raise_server_exceptions=False).get(
            "/api/config", headers=H).json()
    engines = {entry["engine"]: entry for entry in body["engines"]}
    assert set(engines) == {"claude_code", "codex"}
    assert engines["claude_code"]["available"] is True
    assert "capabilities" in engines["codex"]
