"""Unit tests for the canonical image-attachments helpers (server/sessions/attachments.py).

Covers sniffing, content-addressed storage, dedup, resolve_and_bind (cap at 5),
sweep_orphans, and dedup-aware delete_for_session.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from server.db import db_session, init_db
from server.db.models import Attachment
from server.sessions import attachments as A

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_sniff_png():
    assert A.sniff_media_type(PNG) == "image/png"


def test_sniff_jpeg():
    assert A.sniff_media_type(b"\xff\xd8\xff\xe0junk") == "image/jpeg"


def test_sniff_gif():
    assert A.sniff_media_type(b"GIF89a....") == "image/gif"


def test_sniff_webp():
    assert A.sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_sniff_rejects_non_image():
    assert A.sniff_media_type(b"not an image at all") is None


def test_storage_path_is_content_addressed():
    import hashlib
    with patch.dict(os.environ, {"CCSWITCH_HOME": "/tmp/ccx"}):
        sha = hashlib.sha256(PNG).hexdigest()
        p = A.storage_path(sha, "image/png")
        assert p.name == f"{sha}.png"


# --------------------------------------------------------------------------- #
# DB lifecycle helpers
# --------------------------------------------------------------------------- #


def _new_row(sha: str, *, session_id=None, media_type="image/png",
             created_at=None) -> Attachment:
    return Attachment(
        session_id=session_id, turn_id=None, media_type=media_type,
        filename="x.png", byte_size=len(PNG), sha256=sha,
        uploaded_by="me", created_at=created_at or datetime.now(timezone.utc),
    )


def test_resolve_and_bind_binds_and_caps_at_five(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            ids = []
            async with db_session() as db:
                for i in range(6):  # 6 distinct attachments
                    r = _new_row(f"sha{i}")
                    db.add(r)
                    await db.flush()
                    ids.append(r.id)
            async with db_session() as db:
                out = await A.resolve_and_bind(db, "sessA", "turn1", ids)
            # Cap at MAX_PER_MESSAGE (5)
            assert len(out) == A.MAX_PER_MESSAGE == 5
            # Each descriptor carries the content-addressed path + serve url.
            d0 = out[0]
            assert d0["url"] == f"/api/attachments/{d0['id']}"
            assert d0["path"].endswith(".png")
            # Bound rows now carry session_id + turn_id.
            from sqlalchemy import select
            async with db_session() as db:
                bound = (await db.execute(
                    select(Attachment).where(Attachment.session_id == "sessA")
                )).scalars().all()
            return len(bound)

    n_bound = asyncio.run(run())
    assert n_bound == 5


def test_resolve_and_bind_skips_unknown_ids(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            async with db_session() as db:
                out = await A.resolve_and_bind(db, "sessA", "t", ["ghost1", "ghost2"])
            return out

    assert asyncio.run(run()) == []


def test_sweep_orphans_removes_old_unbound(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            old = datetime.now(timezone.utc) - timedelta(days=3)
            recent = datetime.now(timezone.utc)
            async with db_session() as db:
                # Old, unbound → swept. Place its file on disk.
                r_old = _new_row("shaold", created_at=old)
                db.add(r_old)
                # Recent, unbound → kept.
                db.add(_new_row("sharecent", created_at=recent))
                # Old but BOUND → kept (only unbound orphans are swept).
                db.add(_new_row("shabound", session_id="sessB", created_at=old))
            A.storage_path("shaold", "image/png").write_bytes(PNG)
            async with db_session() as db:
                n = await A.sweep_orphans(db, older_than_seconds=86400)
            from sqlalchemy import select
            async with db_session() as db:
                remaining = (await db.execute(select(Attachment.sha256))).scalars().all()
            file_gone = not A.storage_path("shaold", "image/png").exists()
            return n, sorted(remaining), file_gone

    n, remaining, file_gone = asyncio.run(run())
    assert n == 1
    assert remaining == ["shabound", "sharecent"]
    assert file_gone  # unbound orphan's on-disk file removed


def test_delete_for_session_dedup_aware_unlink(tmp_path):
    """Deleting a session must NOT unlink a sha file referenced by another session."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            # Two rows, SAME bytes/sha, bound to DIFFERENT sessions (dedup).
            async with db_session() as db:
                db.add(_new_row("shadup", session_id="sessA"))
                db.add(_new_row("shadup", session_id="sessB"))
                # A second, unique sha bound only to sessA.
                db.add(_new_row("shasolo", session_id="sessA"))
            shared = A.storage_path("shadup", "image/png")
            solo = A.storage_path("shasolo", "image/png")
            shared.write_bytes(PNG)
            solo.write_bytes(PNG)

            async with db_session() as db:
                n = await A.delete_for_session(db, "sessA")
            return n, shared.exists(), solo.exists()

    n, shared_exists, solo_exists = asyncio.run(run())
    assert n == 2  # both sessA rows deleted (shadup + shasolo)
    # The shared sha file survives — sessB still references it.
    assert shared_exists
    # The solo sha file is gone — no other row references it.
    assert not solo_exists
