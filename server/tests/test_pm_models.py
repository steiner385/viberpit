"""PM product-layer model smoke tests (design: 2026-08-31-pm-product-layer)."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    PROJECT_EDGE_KINDS,
    PROJECT_ONBOARDING_STATES,
    Base,
    Project,
    ProjectEdge,
    ProjectRepo,
)


@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh per-test in-memory SQLite session with the full schema."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pm_test.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_project_defaults(db_session):
    p = Project(slug="kindash", name="KinDash")
    db_session.add(p)
    await db_session.flush()
    assert p.id
    assert p.onboarding_state == "registered"
    assert p.status == "active"
    assert p.created_at is not None


@pytest.mark.asyncio
async def test_project_repo_and_edge(db_session):
    a = Project(slug="kindash", name="KinDash")
    b = Project(slug="infra", name="Infra")
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add(ProjectRepo(project_id=a.id, repo="steiner385/KinDash", is_primary=True))
    db_session.add(ProjectEdge(src_project_id=a.id, dst_project_id=b.id, kind="depends_on"))
    await db_session.flush()
    edge = (await db_session.execute(select(ProjectEdge))).scalar_one()
    assert edge.kind in PROJECT_EDGE_KINDS


def test_state_constants_are_ordered():
    assert PROJECT_ONBOARDING_STATES[0] == "registered"
    assert PROJECT_ONBOARDING_STATES[-1] == "live"
