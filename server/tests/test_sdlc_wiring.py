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


def _sqlite_env(tmp: str | None = None) -> dict[str, str]:
    """The one DB-URL override every wiring test patches into the environment:
    a file DB under *tmp*, or in-memory when *tmp* is None."""
    url = f"sqlite+aiosqlite:///{tmp}/wiring.db" if tmp else "sqlite+aiosqlite:///:memory:"
    return {"CCSWITCH_DB_URL": url}


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
        os.environ, _sqlite_env()
    ):
        orch = _make_orch()
        assert orch._sdlc_intake_loop is not None
        assert orch._sdlc_synthesizer_loop is not None


def test_synthesizer_loop_receives_the_arm_auto_merge_seam():
    """An unwired seam is fail-closed, so the wiring itself is the contract."""
    with temp_env(), patch.dict(
        os.environ, _sqlite_env()
    ):
        orch = _make_orch()
        assert orch._sdlc_synthesizer_loop._arm_auto_merge == orch._sdlc_arm_auto_merge


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
        os.environ, _sqlite_env()
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
        os.environ, _sqlite_env(tmp)
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
        os.environ, _sqlite_env(tmp)
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:  # lifespan runs init_db; same loop for the pool (see above)
            rows = client.portal.call(_seed_and_list, orch)
    assert [r["slug"] for r in rows] == ["ui-audit-01"]


def test_sdlc_synth_first_tick_rewrites_a_legacy_stored_mode():
    """A row still carrying `auto` from before the fold is rewritten to `on` once,
    so the API's mode select stops showing a value outside `modes`. An explicit
    `off` is left alone."""
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Automation as AutomationRow
    from sqlalchemy import select

    async def _seed_fold_read(orch):
        async with db_session() as db:
            row = (await db.execute(select(AutomationRow).where(
                AutomationRow.id == "sdlc-synthesizer"))).scalar_one_or_none()
            if row is None:
                db.add(AutomationRow(id="sdlc-synthesizer", enabled=True,
                                     mode="auto", config={}))
            else:
                row.enabled, row.mode = True, "auto"
            await db.commit()
        # The real primary-only sdlc-synthesizer-loop already ticked once at
        # lifespan startup (before this row carried "auto") and consumed the
        # one-shot guard — a known TestClient/startup-tick race, not something
        # this test is exercising. Reset it so the calls below test the fold
        # method's own logic against the state just seeded.
        orch._sdlc_synth_mode_folded = False
        await orch._sdlc_synth_fold_stored_mode()
        await orch._sdlc_synth_fold_stored_mode()          # idempotent
        mode, _ = await orch._sdlc_synth_read_config()
        async with db_session() as db:
            stored = (await db.execute(select(AutomationRow.mode).where(
                AutomationRow.id == "sdlc-synthesizer"))).scalar_one()
        return mode, stored

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, _sqlite_env(tmp)
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            mode, stored = client.portal.call(_seed_fold_read, orch)
    assert (mode, stored) == ("on", "on")


def test_sdlc_list_initiatives_sorts_by_priority_then_age_and_carries_progress():
    """Spec 2026-09-14 D7: highest priority first, unscored last, oldest first
    on ties; each row carries project_slug, priority and epic counts."""
    from datetime import datetime, timedelta, timezone
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Initiative, Project, WorkItem

    async def _seed_and_list(orch):
        t0 = datetime.now(timezone.utc) - timedelta(days=3)
        async with db_session() as db:
            p = Project(slug="viberpit", name="ViberPit")
            db.add(p)
            await db.flush()
            epic = WorkItem(repo="o/r", number=1, kind="issue", state="open", is_epic=True)
            db.add(epic)
            await db.flush()
            db.add(WorkItem(repo="o/r", number=2, kind="issue", state="open",
                            parent_work_item_id=epic.id, reach=10, impact=5, effort=2, confidence=8))
            db.add(WorkItem(repo="o/r", number=3, kind="issue", state="closed",
                            parent_work_item_id=epic.id))
            db.add_all([
                Initiative(slug="unscored-old", repo="o/r", title="u", stage="design",
                           status="active", project_id=p.id, created_at=t0),
                Initiative(slug="scored", repo="o/r", title="s", stage="build",
                           status="active", project_id=p.id, epic_repo="o/r", epic_number=1,
                           created_at=t0 + timedelta(days=1)),
                Initiative(slug="unscored-new", repo="o/r", title="n", stage="design",
                           status="active", project_id=p.id, created_at=t0 + timedelta(days=2)),
            ])
        return await orch._sdlc_list_initiatives()

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/wiring.db"}
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            rows = client.portal.call(_seed_and_list, orch)
    assert [r["slug"] for r in rows] == ["scored", "unscored-old", "unscored-new"]
    scored = rows[0]
    assert (scored["project_slug"], scored["priority"], scored["epic_total"], scored["epic_done"]) == \
        ("viberpit", 200.0, 2, 1)
    assert rows[1]["priority"] is None and rows[1]["project_slug"] == "viberpit"


def test_sdlc_advance_stage_is_a_compare_and_set_that_records_the_gate():
    """The synthesizer's Advance write (spec 2026-09-14 §6/§8/§15): one UPDATE
    moves the stage, activates the row and stores `last_auto_gate`, and it
    refuses — touching nothing — when the stage a human left behind is not the
    one the engine decided from, or the row is blocked."""
    from server.app import create_app
    from server.db import db_session
    from server.db.models import AuditEvent, Initiative
    from server.sdlc.gates import auto_gate_record
    from datetime import datetime, timezone
    from sqlalchemy import select

    async def _run(orch):
        async with db_session() as db:
            db.add(Initiative(id="i-adv", slug="adv", repo="o/r", stage="design",
                              status="awaiting_human"))
            db.add(Initiative(id="i-blk", slug="blk", repo="o/r", stage="design",
                              status="blocked"))
            await db.commit()
        gate = auto_gate_record("stage.advance", "balanced", datetime.now(timezone.utc),
                                detail={"from": "design", "to": "plan"})
        first = await orch._sdlc_advance_stage("i-adv", "design", "plan", gate)
        stale = await orch._sdlc_advance_stage("i-adv", "design", "plan", gate)   # already at plan
        blocked = await orch._sdlc_advance_stage("i-blk", "design", "plan", gate)
        async with db_session() as db:
            row = (await db.execute(select(Initiative).where(Initiative.id == "i-adv"))).scalar_one()
            blk = (await db.execute(select(Initiative).where(Initiative.id == "i-blk"))).scalar_one()
            audits = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "sdlc.gate.auto_resolve"))).scalars().all()
        return first, stale, blocked, row, blk, audits

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, _sqlite_env(tmp)
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            first, stale, blocked, row, blk, audits = client.portal.call(_run, orch)
    assert (first, stale, blocked) == (True, False, False)
    assert (row.stage, row.status) == ("plan", "active")
    assert row.last_auto_gate["gate"] == "stage.advance"
    assert row.last_auto_gate["detail"] == {"from": "design", "to": "plan"}
    assert (blk.stage, blk.status, blk.last_auto_gate) == ("design", "blocked", None)
    assert len(audits) == 1 and audits[0].identity == "sdlc-synthesizer"
    assert audits[0].detail["to"] == "plan"


def test_sdlc_list_artifacts_carries_auto_merge_armed_at():
    """The synthesizer's idempotence key for auto-merge arming (roadmap
    auto-execution phase 2b, task 2): ``_sdlc_list_artifacts`` must project
    ``auto_merge_armed_at`` from the row, not just the six original keys —
    otherwise the loop's `art.get("auto_merge_armed_at")` check always reads
    None and re-arms on every tick."""
    from datetime import datetime, timezone
    from server.app import create_app
    from server.db import db_session
    from server.db.models import Initiative, InitiativeArtifact

    async def _seed_and_list(orch):
        async with db_session() as db:
            init = Initiative(slug="armed", repo="o/r", title="Armed",
                              stage="build", status="active")
            db.add(init)
            await db.flush()
            db.add(InitiativeArtifact(initiative_id=init.id, kind="plan",
                                      pr_number=7, pr_url="https://x/7",
                                      auto_merge_armed_at=None))
            db.add(InitiativeArtifact(initiative_id=init.id, kind="spec",
                                      pr_number=8, pr_url="https://x/8",
                                      auto_merge_armed_at=datetime(
                                          2026, 9, 16, tzinfo=timezone.utc)))
            await db.commit()
            initiative_id = init.id
        return await orch._sdlc_list_artifacts(initiative_id)

    with tempfile.TemporaryDirectory() as tmp, temp_env(), patch.dict(
        os.environ, _sqlite_env(tmp)
    ):
        orch = _make_orch()
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            rows = client.portal.call(_seed_and_list, orch)
    by_pr = {r["pr_number"]: r for r in rows}
    assert "auto_merge_armed_at" in by_pr[7]
    assert by_pr[7]["auto_merge_armed_at"] is None
    # SQLite stores naive UTC (per the DB clock rule), so the round trip drops
    # tzinfo even though the column is declared DateTime(timezone=True).
    assert by_pr[8]["auto_merge_armed_at"].replace(tzinfo=timezone.utc) == \
        datetime(2026, 9, 16, tzinfo=timezone.utc)
