from __future__ import annotations
import sys
from datetime import datetime, timedelta
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest


@pytest.mark.asyncio
async def test_sweep_deletes_old_orphans(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    from server.sessions import attachments as A
    reload(A)
    await init_db()
    _paths.attachments_dir().mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        old = Attachment(media_type="image/png", filename="o.png", byte_size=1, sha256="a",
                         uploaded_by="u", created_at=datetime.now() - timedelta(days=2))
        fresh = Attachment(media_type="image/png", filename="f.png", byte_size=1, sha256="b",
                           uploaded_by="u")
        bound = Attachment(session_id="s1", media_type="image/png", filename="b.png",
                           byte_size=1, sha256="c", uploaded_by="u",
                           created_at=datetime.now() - timedelta(days=2))
        db.add_all([old, fresh, bound]); await db.flush()
        oid = old.id; await db.commit()
    (_paths.attachments_dir() / f"{oid}.png").write_bytes(b"x")
    async with db_session() as db:
        n = await A.sweep_orphans(db); await db.commit()
    assert n == 1
    assert not (_paths.attachments_dir() / f"{oid}.png").exists()
