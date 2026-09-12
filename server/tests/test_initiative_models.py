"""SDLC artifact-chain spine: Initiative + InitiativeArtifact (issue #796).

These are new tables, so init_db()/create_all builds them fully-formed on dev
SQLite and the reconcile + drift-assert pass must stay clean (every non-key
column is nullable). The state-machine counters are NOT a new table — they
reuse AutomationTargetState keyed by the initiative slug.
"""
from __future__ import annotations

import asyncio


def test_initiative_row_roundtrips_with_defaults(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Initiative

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sdlc.db'}")
        async with db_session() as db:
            db.add(Initiative(slug="ai-native-sdlc", repo="steiner385/viberpit",
                              title="AI-native SDLC", origin="backlog-runner"))
        async with db_session() as db:
            return (await db.execute(
                sa_select(Initiative).where(Initiative.slug == "ai-native-sdlc")
            )).scalars().first()

    row = asyncio.run(run())
    assert row.id, "id is a generated UUID PK"
    assert row.stage == "intake", "stage defaults to the first lifecycle stage"
    assert row.status == "proposed", "status defaults to proposed"
    assert row.origin_ref is None, "origin_ref is nullable and unset by default"
    assert row.created_at is not None and row.updated_at is not None


def test_initiative_artifact_links_to_its_initiative(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Initiative, InitiativeArtifact

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sdlc.db'}")
        async with db_session() as db:
            init = Initiative(slug="ai-native-sdlc", title="AI-native SDLC")
            db.add(init)
            await db.flush()
            db.add(InitiativeArtifact(
                initiative_id=init.id, kind="spec",
                repo_path="docs/superpowers/specs/2026-08-29-ai-native-sdlc-design.md",
                pr_number=795, model="claude-opus-4-8"))
            init_id = init.id
        async with db_session() as db:
            return init_id, (await db.execute(
                sa_select(InitiativeArtifact).where(
                    InitiativeArtifact.initiative_id == init_id)
            )).scalars().first()

    init_id, art = asyncio.run(run())
    assert art.initiative_id == init_id
    assert art.kind == "spec"
    assert art.pr_number == 795
    assert art.commit_sha is None, "commit_sha is nullable until the artifact is committed"
    assert art.created_at is not None


def test_initiative_parent_link_roundtrips(tmp_path):
    """A phase initiative points at its program by id; a root has None."""
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Initiative

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sdlc.db'}")
        async with db_session() as db:
            program = Initiative(slug="ui-audit-2026-09", title="UI audit 2026-09")
            db.add(program)
            await db.flush()
            db.add(Initiative(slug="ui-audit-2026-09-01", title="01 Restore core actions",
                              parent_initiative_id=program.id))
            program_id = program.id
        async with db_session() as db:
            phase = (await db.execute(sa_select(Initiative).where(
                Initiative.slug == "ui-audit-2026-09-01"))).scalars().one()
            root = (await db.execute(sa_select(Initiative).where(
                Initiative.slug == "ui-audit-2026-09"))).scalars().one()
            return program_id, phase.parent_initiative_id, root.parent_initiative_id

    program_id, phase_parent, root_parent = asyncio.run(run())
    assert phase_parent == program_id
    assert root_parent is None


def test_feature_route_labels_roundtrip_as_a_list(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Feature, Project

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'pm.db'}")
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash")
            db.add(p)
            await db.flush()
            db.add(Feature(project_id=p.id, slug="flaky-test-triage", title="Flaky test triage",
                           state="live", route_labels=["flaky"]))
            db.add(Feature(project_id=p.id, slug="task-views", title="Task views", state="live"))
        async with db_session() as db:
            rows = (await db.execute(sa_select(Feature).order_by(Feature.slug))).scalars().all()
            return [(r.slug, r.route_labels) for r in rows]

    assert asyncio.run(run()) == [("flaky-test-triage", ["flaky"]), ("task-views", None)]
