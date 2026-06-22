# server/tests/test_orchestrator_attachments.py
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_send_prompt_binds_and_forwards(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    await init_db()
    _paths.attachments_dir().mkdir(parents=True, exist_ok=True)
    async with db_session() as db:
        row = Attachment(media_type="image/png", filename="a.png", byte_size=3,
                         sha256="x", uploaded_by="u")
        db.add(row); await db.flush(); rid = row.id; await db.commit()
    (_paths.attachments_dir() / f"{rid}.png").write_bytes(b"\x89PNG\r\n\x1a\nZ")

    from server.sessions.orchestrator import SessionOrchestrator

    class _Spy:
        def __init__(self): self.calls = []
        async def send_prompt(self, text, attachments=None): self.calls.append((text, attachments))

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    spy = _Spy()
    orch._drivers["s1"] = spy  # inject
    await orch.send_prompt("s1", "look", attachment_ids=[rid])
    assert spy.calls[0][0] == "look"
    assert spy.calls[0][1][0]["id"] == rid
    # row is now bound to the session
    async with db_session() as db:
        from sqlalchemy import select
        r = (await db.execute(select(Attachment).where(Attachment.id == rid))).scalars().first()
        assert r.session_id == "s1"
