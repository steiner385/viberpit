# server/tests/test_attachment_lifecycle.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from tests.helpers import temp_env


@pytest.mark.asyncio
async def test_resolve_bind_and_delete(tmp_path, monkeypatch):
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
        row = Attachment(media_type="image/png", filename="a.png", byte_size=3,
                         sha256="x", uploaded_by="u")
        db.add(row)
        await db.flush()
        rid = row.id
        await db.commit()
    (_paths.attachments_dir() / f"{rid}.png").write_bytes(b"abc")

    async with db_session() as db:
        resolved = await A.resolve_and_bind(db, "sess-1", "turn-1", [rid, "missing"])
        await db.commit()
    assert [r["id"] for r in resolved] == [rid]
    assert resolved[0]["media_type"] == "image/png"
    assert Path(resolved[0]["path"]).read_bytes() == b"abc"

    async with db_session() as db:
        n = await A.delete_for_session(db, "sess-1")
        await db.commit()
    assert n == 1
    assert not (_paths.attachments_dir() / f"{rid}.png").exists()
