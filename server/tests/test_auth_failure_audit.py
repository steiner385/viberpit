# server/tests/test_auth_failure_audit.py
import pytest
from ccswitch.accounts import AuthFailure
from server.usage.auth_failure_audit import build_needs_reauth_detail


def test_build_detail_maps_fields():
    f = AuthFailure(account="primary", status=400, body='{"error":"invalid_grant"}',
                    path="poller-idle", token_expires_at=123, attempts=1)
    d = build_needs_reauth_detail(f)
    assert d == {"account": "primary", "status": 400, "body": '{"error":"invalid_grant"}',
                 "path": "poller-idle", "token_expires_at": 123, "attempts": 1}


@pytest.mark.asyncio
async def test_drainer_processes_enqueued_item_and_stops_cleanly():
    import asyncio
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    from server.usage.auth_failure_audit import AuthFailureAuditor
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    auditor = AuthFailureAuditor(identity="system", session_factory=db_session)
    auditor.start()
    try:
        auditor._enqueue(AuthFailure(account="primary", status=400, body="{}",
                                     path="poller-idle", token_expires_at=1, attempts=1))
        # poll up to ~3s for the drained row
        row = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            async with db_session() as db:
                row = (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "account.needs_reauth"))).scalars().first()
            if row is not None:
                break
        assert row is not None and row.detail["account"] == "primary"
    finally:
        auditor.stop()
        await asyncio.sleep(0)  # let cancellation propagate


@pytest.mark.asyncio
async def test_drainer_writes_redacted_event():
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    from server.usage.auth_failure_audit import AuthFailureAuditor

    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    auditor = AuthFailureAuditor(identity="system", session_factory=db_session)
    f = AuthFailure(account="primary", status=400,
                    body='{"refreshToken":"sk-ant-LEAKMEsk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"}',
                    path="poller-idle", token_expires_at=1, attempts=1)
    await auditor._write(f)
    async with db_session() as db:
        rows = (await db.execute(
            select(AuditEvent).where(AuditEvent.type == "account.needs_reauth")
        )).scalars().all()
    assert len(rows) == 1
    detail_str = str(rows[0].detail)
    assert "sk-ant-" not in detail_str  # redacted at the server boundary
    assert rows[0].detail["account"] == "primary"
