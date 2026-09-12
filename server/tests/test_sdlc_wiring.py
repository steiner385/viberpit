"""SDLC orchestrator-wiring smoke test (Task 7).

Boots the whole import + glue chain the way ``test_api._make_app_and_client``
does, then asserts the intake/synthesizer loops are constructed, the automations
are registered, and one DB-backed glue seam answers on an empty DB. This catches
import-time regressions in the orchestrator and confirms the seams are bound.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env  # noqa: E402
from server.tests.fakes.fake_factory import fake_sdk_factory  # noqa: E402


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator

    return SessionOrchestrator(
        sdk_factory=fake_sdk_factory(),
        usage_fn=lambda name: None,
        poll_interval=9999.0,
    )


def test_sdlc_loops_are_constructed():
    """Both SDLC loops are built as orchestrator attributes on construction."""
    with temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}
    ):
        orch = _make_orch()
        assert orch._sdlc_intake_loop is not None
        assert orch._sdlc_synthesizer_loop is not None


def test_sdlc_automations_registered():
    """The registry resolves both SDLC automation ids to their classes."""
    from server.automations.registry import registry
    from server.automations.sdlc_intake import SdlcIntake
    from server.automations.sdlc_synthesizer import SdlcSynthesizer

    assert isinstance(registry.get("sdlc-intake"), SdlcIntake)
    assert isinstance(registry.get("sdlc-synthesizer"), SdlcSynthesizer)


def test_sdlc_list_initiatives_empty_on_fresh_db():
    """The list-initiatives glue returns [] on an initialised, empty DB.

    Uses the app lifespan (``with client:``) so init_db has run — the seam reads
    the ``initiative`` table, which must exist.
    """
    from server.app import create_app

    with temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:  # lifespan runs init_db → the initiative table exists
            # On the lifespan's loop, not a fresh asyncio.run(): init_db bound the
            # engine's connection pool to that loop, and a pooled connection used
            # from another loop fails with "bound to a different event loop"
            # (flaked on PR #1549's CI shard).
            result = client.portal.call(orch._sdlc_list_initiatives)
    assert result == []


def test_sdlc_list_initiatives_projects_the_epic_pointer():
    """The projection the synthesizer feeds to the engine carries epic_repo /
    epic_number (#992). Without them ``_to_views`` cannot tell a materialized
    initiative from one still awaiting decomposition."""
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Initiative

    async def _seed_and_list(orch):
        async with db_session() as db:
            db.add(Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                              stage="plan", status="active",
                              epic_repo="o/r", epic_number=42))
        return await orch._sdlc_list_initiatives()

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/wiring.db"}
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:  # lifespan runs init_db; same loop for the pool (see above)
            rows = client.portal.call(_seed_and_list, orch)
    assert [(r["slug"], r["epic_repo"], r["epic_number"]) for r in rows] == \
        [("dark-mode", "o/r", 42)]


def test_sdlc_list_initiatives_excludes_a_program():
    """A program (an initiative with phases) is never driven directly — the
    synthesizer must never propose artifacts for it or materialize it as a
    plan. Only the leaf phase belongs in the candidate pool."""
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Initiative

    async def _seed_and_list(orch):
        async with db_session() as db:
            program = Initiative(slug="ui-audit", repo="o/r", title="UI audit",
                                 stage="plan", status="active",
                                 epic_repo="o/r", epic_number=10)
            db.add(program)
            await db.flush()
            db.add(Initiative(slug="ui-audit-01", repo="o/r", title="01 phase",
                              stage="plan", status="active",
                              epic_repo="o/r", epic_number=11,
                              parent_initiative_id=program.id))
        return await orch._sdlc_list_initiatives()

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/wiring.db"}
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:  # lifespan runs init_db; same loop for the pool (see above)
            rows = client.portal.call(_seed_and_list, orch)
    assert [r["slug"] for r in rows] == ["ui-audit-01"]
