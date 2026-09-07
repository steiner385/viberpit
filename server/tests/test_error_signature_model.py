from __future__ import annotations

import importlib
from datetime import datetime

import pytest


def test_naive_utc_now_is_naive():
    from server.db.clock import naive_utc_now
    now = naive_utc_now()
    assert isinstance(now, datetime) and now.tzinfo is None


@pytest.mark.asyncio
async def test_error_signature_round_trips(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature
    async with db_session() as db:
        db.add(ErrorSignature(fingerprint="abc", source="edge", level="ERROR",
                              logger="server.x", title="ERROR server.x: boom",
                              sample_message="boom", count=1, count_since_filed=1,
                              sample_context={"host": "dobby", "recent_sessions": ["s1"]}))
        await db.commit()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
        assert row.status == "observed"
        assert row.issue_number is None and row.muted_by is None
        assert row.sample_context["recent_sessions"] == ["s1"]
        assert row.first_seen_at is not None and row.first_seen_at.tzinfo is None


@pytest.mark.asyncio
async def test_fingerprint_is_unique(isolated_db):
    from sqlalchemy.exc import IntegrityError
    from server.db import db_session
    from server.db.models import ErrorSignature
    async with db_session() as db:
        db.add(ErrorSignature(fingerprint="dup"))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            db.add(ErrorSignature(fingerprint="dup"))
            await db.commit()


def test_migration_0034_chains_off_the_current_head():
    mod = importlib.import_module("server.db.migrations.versions.0035_error_signatures")
    assert mod.revision == "0035_error_signatures"
    assert mod.down_revision == "0034_pr_resolution_source_tool"


def test_error_sink_flag_default_and_env(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_ERROR_SINK_ENABLED", raising=False)
    assert Settings().error_sink_enabled is True
    monkeypatch.setenv("CCSWITCH_ERROR_SINK_ENABLED", "false")
    assert Settings().error_sink_enabled is False


def test_error_sink_flag_listed_on_about():
    from server.api.settings import _MANAGED
    assert "error_sink_enabled" in {row[0] for row in _MANAGED}


def test_dropped_counter_exists():
    from server.obs import metrics
    before = metrics.error_sink_dropped_total.value()
    metrics.error_sink_dropped_total.inc()
    assert metrics.error_sink_dropped_total.value() == before + 1
