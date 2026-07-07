import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_sqlite_engine_uses_wal(tmp_path):
    from server.db import _make_engine
    url = f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}"
    engine = _make_engine(url)
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    finally:
        await engine.dispose()
    assert str(mode).lower() == "wal"
    # 30s: a contended writer waits instead of raising "database is locked"
    # under load (matches PRAGMA busy_timeout + sqlite3 connect timeout).
    assert int(busy) == 30000
