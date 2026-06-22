# VERIFY-NOTE (Principle III): inline-base64 transcript storage is ASSUMED, not verified against
# the real claude binary in this env. Covered by the final manual reload smoke-test. If the CLI
# externalizes images, hash-match finds nothing and replay degrades to text-only (no crash); an
# ownership-join fallback would be the follow-up.
from __future__ import annotations
import sys, json, base64, hashlib
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from tests.helpers import temp_env


@pytest.mark.asyncio
async def test_replay_reconstructs_attachment_ref(monkeypatch, tmp_path):
    """Inline base64 image block whose sha256 matches an Attachment row → ref emitted."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db, db_session
    from server.db.models import Attachment
    await init_db()

    raw = b"\x89PNG\r\n\x1a\nIMG"
    sha = hashlib.sha256(raw).hexdigest()
    async with db_session() as db:
        row = Attachment(session_id="s1", media_type="image/png", filename="a.png",
                         byte_size=len(raw), sha256=sha, uploaded_by="u")
        db.add(row)
        await db.flush()
        rid = row.id
        await db.commit()

    # Minimal transcript with a user turn containing a base64 image block.
    tpath = tmp_path / "t.jsonl"
    b64 = base64.b64encode(raw).decode()
    line = {"type": "user", "uuid": "U1", "message": {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
    ]}}
    tpath.write_text(json.dumps(line) + "\n")

    from server.sessions.replay import replay_events
    events = await replay_events(str(tpath), session_id="s1")
    users = [e for e in events if e.__class__.__name__ == "UserMessage"]
    assert users, "expected a UserMessage event"
    assert users[0].attachments and users[0].attachments[0]["id"] == rid
    assert users[0].attachments[0]["mediaType"] == "image/png"
    assert users[0].attachments[0]["filename"] == "a.png"
    assert users[0].text == "look"


@pytest.mark.asyncio
async def test_replay_no_match_degrades_gracefully(monkeypatch, tmp_path):
    """Image block whose bytes match NO Attachment row → text-only UserMessage, no crash."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    monkeypatch.setenv("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    from importlib import reload
    from ccswitch import paths as _paths
    reload(_paths)
    from server.db import init_db
    await init_db()

    # Transcript with a base64 image block that has no matching Attachment in the DB.
    tpath = tmp_path / "t.jsonl"
    raw = b"\x89PNG\r\n\x1a\nUNKNOWN"
    b64 = base64.b64encode(raw).decode()
    line = {"type": "user", "uuid": "U2", "message": {"role": "user", "content": [
        {"type": "text", "text": "see this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
    ]}}
    tpath.write_text(json.dumps(line) + "\n")

    from server.sessions.replay import replay_events
    events = await replay_events(str(tpath), session_id="s-unknown")
    users = [e for e in events if e.__class__.__name__ == "UserMessage"]
    assert users, "expected a UserMessage event even when no attachment row matches"
    assert users[0].text == "see this"
    # attachments should be absent/empty — no crash
    assert not users[0].attachments


@pytest.mark.asyncio
async def test_replay_no_session_id_skips_attachment_lookup(tmp_path):
    """Without session_id, replay emits text-only and does not attempt DB access."""
    tpath = tmp_path / "t.jsonl"
    raw = b"\x89PNG\r\n\x1a\nIMG"
    b64 = base64.b64encode(raw).decode()
    line = {"type": "user", "uuid": "U3", "message": {"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
    ]}}
    tpath.write_text(json.dumps(line) + "\n")

    from server.sessions.replay import replay_events
    # No session_id → should not hit DB, should not crash
    events = await replay_events(str(tpath))
    users = [e for e in events if e.__class__.__name__ == "UserMessage"]
    assert users
    assert users[0].text == "hi"
    assert not users[0].attachments
