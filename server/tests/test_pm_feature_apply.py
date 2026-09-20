"""`_pm_sync_apply_feature_plan` writes the capability tree and feature links (#993)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def _orch():
    """Bare orchestrator over a real (temp) SQLite db — no TestClient/app, no
    second event loop. The tests never make an HTTP call, and opening a
    TestClient (its own event loop) alongside `asyncio.run(...)` against the
    same aiosqlite pool is a cross-loop race (reproduced on this branch)."""
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    _clear = ["CCSWITCH_SESSIOND_ENDPOINT", "CCSWITCH_SESSIOND_SOCKET", "CCSWITCH_SESSIOND_HOST",
              "CCSWITCH_SESSIOND_PORT", "CCSWITCH_SESSIOND_CLIENT_CERT",
              "CCSWITCH_SESSIOND_CLIENT_KEY", "CCSWITCH_SESSIOND_CA"]
    with tempfile.TemporaryDirectory() as tmp:
        saved = {k: os.environ.pop(k, None) for k in _clear}
        try:
            db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                         "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
                asyncio.run(init_db(db_url))
                yield SessionOrchestrator(sdk_factory=fake_sdk_factory())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def test_apply_writes_parents_and_links_and_rewrites_stale_links():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, FeatureLink, Project
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    with _orch() as orch:
        async def _seed() -> tuple[str, str | None]:
            async with db_session() as db:
                infra = Project(slug="example-infra", name="Example infra")
                prod = Project(slug="example-product", name="Example product")
                db.add_all([infra, prod])
                await db.flush()
                mq = Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live")
                stale_dst = Feature(project_id=prod.id, slug="old", title="Old", state="live")
                db.add_all([mq, stale_dst])
                await db.flush()
                await db.commit()
                return prod.id, prod.slug
        prod_id, _ = asyncio.run(_seed())

        def _up(slug, parent, links=()):
            return FeatureUpsert(slug=slug, title=slug, state="live", summary="", repo_path=f"d/{slug}.md",
                                 parent_slug=parent, links=tuple(links))

        plan1 = FeatureIndexPlan(
            upserts=(_up("sessions", None), _up("dictation", "sessions",
                                                  [FeatureLinkRef("powered_by", "example-infra", "message-queue"),
                                                   FeatureLinkRef("integrates_with", "example-product", "old")])),
            orphan_slugs=(), revive_slugs=(), warnings=("dictation: sample warning",))
        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, plan1))

        async def _read():
            async with db_session() as db:
                feats = {f.slug: f for f in (await db.execute(
                    select(Feature).where(Feature.project_id == prod_id))).scalars().all()}
                links = (await db.execute(select(FeatureLink).where(
                    FeatureLink.src_feature_id == feats["dictation"].id))).scalars().all()
                return feats, links
        feats, links = asyncio.run(_read())
        assert feats["sessions"].parent_feature_id is None
        assert feats["dictation"].parent_feature_id == feats["sessions"].id
        assert sorted(link.kind for link in links) == ["integrates_with", "powered_by"]


def test_second_apply_rewrites_links():
    """The link set after a second apply is exactly the resolvable links of the
    second plan: a link that left the doc is gone, an unresolvable one is skipped."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, FeatureLink, Project
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    with _orch() as orch:
        async def _seed() -> str:
            async with db_session() as db:
                infra = Project(slug="example-infra", name="Example infra")
                prod = Project(slug="example-product", name="Example product")
                db.add_all([infra, prod])
                await db.flush()
                db.add(Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live"))
                db.add(Feature(project_id=prod.id, slug="old", title="Old", state="live"))
                await db.commit()
                return prod.id
        prod_id = asyncio.run(_seed())

        def _plan(links):
            return FeatureIndexPlan(
                upserts=(FeatureUpsert(slug="dictation", title="D", state="live", summary="",
                                       repo_path="d/dictation.md", parent_slug=None, links=tuple(links)),),
                orphan_slugs=(), revive_slugs=(), warnings=())

        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, _plan([
            FeatureLinkRef("powered_by", "example-infra", "message-queue"),
            FeatureLinkRef("integrates_with", "example-product", "old")])))
        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, _plan([
            FeatureLinkRef("powered_by", "example-infra", "message-queue"),
            FeatureLinkRef("powered_by", "nope", "nope")])))

        async def _links():
            async with db_session() as db:
                src = (await db.execute(select(Feature).where(
                    Feature.project_id == prod_id, Feature.slug == "dictation"))).scalar_one()
                dst = (await db.execute(select(Feature).where(Feature.slug == "message-queue"))).scalar_one()
                rows = (await db.execute(select(FeatureLink).where(
                    FeatureLink.src_feature_id == src.id))).scalars().all()
                return [(r.kind, r.dst_feature_id) for r in rows], dst.id
        rows, mq_id = asyncio.run(_links())
        assert rows == [("powered_by", mq_id)]


def test_duplicate_link_in_one_upsert_applies_without_error():
    """F1 regression: a doc listing the same (kind, project, feature) link twice
    must not insert two identical FeatureLink rows — that trips uq_feature_link
    and rolls back the whole apply."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, FeatureLink, Project
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    with _orch() as orch:
        async def _seed() -> str:
            async with db_session() as db:
                infra = Project(slug="example-infra", name="Example infra")
                prod = Project(slug="example-product", name="Example product")
                db.add_all([infra, prod])
                await db.flush()
                db.add(Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live"))
                await db.commit()
                return prod.id
        prod_id = asyncio.run(_seed())

        plan = FeatureIndexPlan(
            upserts=(FeatureUpsert(
                slug="dictation", title="D", state="live", summary="",
                repo_path="d/dictation.md", parent_slug=None,
                links=(FeatureLinkRef("powered_by", "example-infra", "message-queue"),
                       FeatureLinkRef("powered_by", "example-infra", "message-queue"))),),
            orphan_slugs=(), revive_slugs=(), warnings=())
        asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, plan))

        async def _links():
            async with db_session() as db:
                src = (await db.execute(select(Feature).where(
                    Feature.project_id == prod_id, Feature.slug == "dictation"))).scalar_one()
                return (await db.execute(select(FeatureLink).where(
                    FeatureLink.src_feature_id == src.id))).scalars().all()
        rows = asyncio.run(_links())
        assert len(rows) == 1
        assert rows[0].kind == "powered_by"


def test_unresolved_link_warns_once_not_once_per_tick(caplog):
    """A link naming a project/feature that never resolves repeats on every
    pm-sync tick for as long as the doc stands (#1531, re-filed as #1875 once
    #1531 was closed without the doc being fixed — same fingerprint, same
    first-seen). Repeats within the throttle window must not re-report at
    WARNING."""
    from server.sessions import orchestrator as orch_mod
    from server.db.models import Project
    from server.db import db_session
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    orch_mod._PM_SYNC_LINK_LOG.clear()

    with _orch() as orch:
        async def _seed() -> str:
            async with db_session() as db:
                prod = Project(slug="example-product", name="Example product")
                db.add(prod)
                await db.commit()
                return prod.id
        prod_id = asyncio.run(_seed())

        # example-infra is deliberately NOT seeded above, so this link never
        # resolves — which is the condition that used to re-warn every tick.
        plan = FeatureIndexPlan(
            upserts=(FeatureUpsert(
                slug="dictation", title="D", state="live", summary="",
                repo_path="d/dictation.md", parent_slug=None,
                links=(FeatureLinkRef("powered_by", "example-infra", "account-reauthentication"),)),),
            orphan_slugs=(), revive_slugs=(), warnings=())

        with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
            for _ in range(5):
                asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, plan))

        warnings = [r for r in caplog.records
                    if r.levelno >= logging.WARNING and "feature_link_unresolved" in r.getMessage()]
        assert len(warnings) == 1, f"{len(warnings)} records for one unresolved link"


def test_two_unresolved_links_from_one_doc_each_warn_independently(caplog):
    """#1531/#1875 (account-reauthentication) and #1532/#1876 (config-dir-isolation)
    are two distinct broken links out of the SAME doc (credential-store-encryption).
    The throttle key must include the link's own target, not just its source, or
    fixing/silencing one would silently swallow the other. Each identity gets its
    own one-report window.

    The record is structured (#1877), so the link a report is *about* lives in
    ``record.fields`` rather than in the message text. That is the point of the
    structured form: the edge stays fully recoverable from the record while no
    longer forking the error sink's fingerprint, which is what filed these as
    separate issues in the first place."""
    from server.sessions import orchestrator as orch_mod
    from server.db.models import Project
    from server.db import db_session
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    orch_mod._PM_SYNC_LINK_LOG.clear()

    with _orch() as orch:
        async def _seed() -> str:
            async with db_session() as db:
                prod = Project(slug="example-product", name="Example product")
                db.add(prod)
                await db.commit()
                return prod.id
        prod_id = asyncio.run(_seed())

        # example-infra is deliberately NOT seeded, so both links never resolve.
        plan = FeatureIndexPlan(
            upserts=(FeatureUpsert(
                slug="credential-store-encryption", title="C", state="live", summary="",
                repo_path="d/credential-store-encryption.md", parent_slug=None,
                links=(FeatureLinkRef("powered_by", "example-infra", "config-dir-isolation"),
                       FeatureLinkRef("integrates_with", "example-infra", "account-reauthentication"))),),
            orphan_slugs=(), revive_slugs=(), warnings=())

        with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
            for _ in range(5):
                asyncio.run(orch._pm_sync_apply_feature_plan(prod_id, plan))

        warnings = [r for r in caplog.records
                    if r.levelno >= logging.WARNING and "feature_link_unresolved" in r.getMessage()]
        assert len(warnings) == 2, f"expected one report per distinct link, got {len(warnings)}"
        targets = {r.fields["dst_feature"] for r in warnings}
        assert targets == {"config-dir-isolation", "account-reauthentication"}


def test_unresolved_link_edges_share_one_fingerprint(caplog):
    """Two different unresolved edges are one condition, not two (#1877).

    #2095 stopped this call site repeating every tick, but the records still
    carried their slugs in the message text — and the error sink fingerprints a
    plain record on its *normalized* message, where normalization masks uuids,
    ints and quoted strings but never a slug. So one call site filed #1531 (and
    its re-file #1875) for ``src=credential-store-encryption`` and #1533 (and
    #1877) for ``src=config-dir-isolation``: same bug, same line, two issue
    lineages, each re-opening itself as a "regression" when closed.

    Structured, the family shares one fingerprint. The throttle key stays
    per-edge, so a newly broken edge still reports at WARNING immediately —
    which is what the test above pins.
    """
    from server.sessions import orchestrator as orch_mod
    from server.db.models import Project
    from server.db import db_session
    from server.obs.error_signature import signature_of
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    from server.pm.featuredoc import FeatureLinkRef

    orch_mod._PM_SYNC_LINK_LOG.clear()

    def _plan(src: str, dst_feature: str) -> "FeatureIndexPlan":
        # example-infra is never seeded, so every link below stays unresolved.
        return FeatureIndexPlan(
            upserts=(FeatureUpsert(
                slug=src, title="T", state="live", summary="",
                repo_path=f"d/{src}.md", parent_slug=None,
                links=(FeatureLinkRef("powered_by", "example-infra", dst_feature),)),),
            orphan_slugs=(), revive_slugs=(), warnings=())

    with _orch() as orch:
        async def _seed() -> str:
            async with db_session() as db:
                prod = Project(slug="example-product", name="Example product")
                db.add(prod)
                await db.commit()
                return prod.id
        prod_id = asyncio.run(_seed())

        with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
            asyncio.run(orch._pm_sync_apply_feature_plan(
                prod_id, _plan("dictation", "account-reauthentication")))
            asyncio.run(orch._pm_sync_apply_feature_plan(
                prod_id, _plan("worktrees", "credential-store")))

    records = [r for r in caplog.records
               if r.levelno >= logging.WARNING and "feature_link_unresolved" in r.getMessage()]
    assert len(records) == 2, (
        f"{len(records)} records for two distinct unresolved edges — a new edge is news")

    sigs = [signature_of(r, source="daemon", host="h") for r in records]
    assert all(s is not None for s in sigs)
    assert len({s.fingerprint for s in sigs}) == 1, (
        "distinct edges must share one fingerprint, or the sink files an issue per edge")
    # The edge is still recoverable from the record — just not from its identity.
    assert "dictation" in sigs[0].sample_message
    assert "worktrees" in sigs[1].sample_message
