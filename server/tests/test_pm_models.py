"""PM product-layer model smoke tests (design: 2026-08-31-pm-product-layer)."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    FEATURE_LIFECYCLE_STATES,
    PROJECT_EDGE_KINDS,
    PROJECT_ONBOARDING_STATES,
    TRIAGE_PROPOSAL_STATUSES,
    WORK_ITEM_TRIAGE_STATES,
    Base,
    Feature,
    FeatureLink,
    Project,
    ProjectEdge,
    ProjectRepo,
    TriageProposal,
    WorkItem,
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


@pytest.mark.asyncio
async def test_feature_is_an_index_row(db_session):
    p = Project(slug="ccswitch", name="ccswitch")
    db_session.add(p)
    await db_session.flush()
    f = Feature(project_id=p.id, slug="sessions", title="Session manager",
                state="live", repo_path="docs/product/features/sessions.md")
    db_session.add(f)
    await db_session.flush()
    assert f.commit_sha is None          # not merged yet — pointer semantics
    assert f.orphaned is False or f.orphaned is None
    assert f.state in FEATURE_LIFECYCLE_STATES


@pytest.mark.asyncio
async def test_work_item_overlay_and_proposal(db_session):
    w = WorkItem(repo="steiner385/ccswitch", number=42, kind="issue",
                 title="Fix the thing", state="open")
    db_session.add(w)
    await db_session.flush()
    assert w.triage_state == "unclassified"
    assert w.triage_state in WORK_ITEM_TRIAGE_STATES
    assert w.feature_id is None
    t = TriageProposal(work_item_id=w.id, rationale="looks like sessions work")
    db_session.add(t)
    await db_session.flush()
    assert t.status == "pending"
    assert t.status in TRIAGE_PROPOSAL_STATUSES


@pytest.mark.asyncio
async def test_feature_link(db_session):
    p = Project(slug="p1", name="P1")
    db_session.add(p)
    await db_session.flush()
    f1 = Feature(project_id=p.id, slug="a", title="A", state="live")
    f2 = Feature(project_id=p.id, slug="b", title="B", state="live")
    db_session.add_all([f1, f2])
    await db_session.flush()
    db_session.add(FeatureLink(src_feature_id=f1.id, dst_feature_id=f2.id, kind="powered_by"))
    await db_session.flush()
