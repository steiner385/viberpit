from __future__ import annotations

import pytest


def test_session_tools_flags_default(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_SESSION_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSION_FEEDBACK_CAP", raising=False)
    s = Settings()
    assert s.session_tools_enabled is True
    assert s.session_feedback_cap == 3


def test_session_tools_flags_from_env(monkeypatch):
    from server.config import Settings
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    monkeypatch.setenv("CCSWITCH_SESSION_FEEDBACK_CAP", "7")
    s = Settings()
    assert s.session_tools_enabled is False
    assert s.session_feedback_cap == 7


def test_flags_are_listed_on_about():
    from server.api.settings import _MANAGED
    keys = {row[0] for row in _MANAGED}
    assert {"session_tools_enabled", "session_feedback_cap"} <= keys


@pytest.mark.asyncio
async def test_session_feedback_table_round_trips(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import SessionFeedback
    async with db_session() as db:
        db.add(SessionFeedback(session_id="s1", fingerprint="abc", category="dispatch",
                               impact="degraded", title="t", body="b", status="pending",
                               context={"repo": "o/n"}))
        await db.commit()
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.id and row.reported_at is not None and row.issue_number is None
        assert row.context == {"repo": "o/n"}


def test_migration_0029_chains_off_the_current_head():
    import importlib
    mod = importlib.import_module("server.db.migrations.versions.0029_session_feedback")
    assert mod.revision == "0029_session_feedback"
    assert mod.down_revision == "0028_assistant_tokens"


def test_fingerprint_normalizes_title():
    from server.sessions.feedback import fingerprint
    a = fingerprint("dispatch", "Dispatch prompt names a branch that does not exist")
    b = fingerprint("dispatch", "  dispatch PROMPT names a branch — that does not exist!! ")
    assert a == b and len(a) == 16
    assert fingerprint("prompt", "Dispatch prompt names a branch that does not exist") != a


def test_validate_item_rejects_bad_args():
    from server.mcp_spec import ToolArgError
    from server.sessions.feedback import validate_item
    good = {"category": "dispatch", "impact": "degraded", "title": "Branch missing",
            "body": "The prompt named feat/x; it does not exist."}
    item = validate_item(good)
    assert item.category == "dispatch" and item.evidence is None
    for bad in ({**good, "category": "bug"}, {**good, "impact": "huge"},
                {**good, "title": "short"}, {**good, "body": ""}):
        with pytest.raises(ToolArgError):
            validate_item(bad)
    long = validate_item({**good, "body": "x" * 5000, "evidence": "e" * 2000})
    assert len(long.body) == 4000 and len(long.evidence) == 1000


@pytest.mark.asyncio
async def test_record_feedback_writes_row_audit_metric(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, SessionFeedback
    from server.obs.metrics import session_feedback_total
    from server.sessions.feedback import record_feedback, validate_item
    before = session_feedback_total.value("dispatch")
    item = validate_item({"category": "dispatch", "impact": "degraded",
                          "title": "Branch missing", "body": "The prompt named feat/x."})
    res = await record_feedback(session_id="s1", identity="u@x", item=item,
                                context={"repo": "o/n"}, cap=3)
    assert res.status == "pending" and res.recorded
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.session_id == "s1" and row.status == "pending" and row.context == {"repo": "o/n"}
        audit = (await db.execute(select(AuditEvent).where(AuditEvent.type == "session.feedback"))).scalar_one()
        assert audit.identity == "u@x" and audit.via == "session"
        assert audit.detail["fingerprint"] == row.fingerprint and audit.detail["status"] == "pending"
    assert session_feedback_total.value("dispatch") == before + 1


@pytest.mark.asyncio
async def test_record_feedback_duplicate_and_cap(isolated_db):
    from server.sessions.feedback import record_feedback, validate_item
    def mk(t: str):
        return validate_item({"category": "prompt", "impact": "minor", "title": t, "body": "b" * 10})
    assert (await record_feedback(session_id="s1", identity="u", item=mk("Title one"), context={}, cap=2)).status == "pending"
    assert (await record_feedback(session_id="s1", identity="u", item=mk("title ONE"), context={}, cap=2)).status == "duplicate"
    assert (await record_feedback(session_id="s1", identity="u", item=mk("Title two"), context={}, cap=2)).status == "pending"
    capped = await record_feedback(session_id="s1", identity="u", item=mk("Title three"), context={}, cap=2)
    assert capped.status == "capped" and capped.recorded
    # another session is not affected by s1's cap
    assert (await record_feedback(session_id="s2", identity="u", item=mk("Title three"), context={}, cap=2)).status == "pending"
