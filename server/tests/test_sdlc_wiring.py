"""SDLC orchestrator-wiring smoke test (Task 7).

Boots the whole import + glue chain the way ``test_api._make_app_and_client``
does, then asserts the intake/synthesizer loops are constructed, the automations
are registered, and one DB-backed glue seam answers on an empty DB. This catches
import-time regressions in the orchestrator and confirms the seams are bound.
"""
from __future__ import annotations

import os
import sys
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
            import asyncio

            result = asyncio.run(orch._sdlc_list_initiatives())
    assert result == []
