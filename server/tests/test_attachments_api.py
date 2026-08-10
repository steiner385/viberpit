"""API tests for /api/attachments (upload + serve), the single canonical surface."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from server.tests.fakes.fake_factory import fake_sdk_factory

IDENT = {"Tailscale-User-Login": "testuser@example.com"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
GIF = b"GIF89a" + b"\x00" * 20
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
TEXT = b"hello, world\n"


def _client(tmp_path, monkeypatch):
    """TestClient on a file-backed SQLite DB so out-of-request db_session() checks
    (audit row, dedup) see the same persisted state the request wrote.

    CCSWITCH_HOME stays set for the whole test (not just construction) because
    attachments_dir() reads it lazily on every request that writes a file.
    """
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    monkeypatch.setenv("CCSWITCH_DB_URL", db_url)
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_upload_then_serve(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("shot.png", PNG, "image/png")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["mediaType"] == "image/png"
        assert body["byteSize"] == len(PNG)
        assert body["url"] == f"/api/attachments/{body['id']}"
        g = client.get(body["url"], headers=IDENT)
        assert g.status_code == 200 and g.content == PNG
        assert g.headers["content-type"].startswith("image/png")


def test_upload_oversize_413(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    big = PNG + b"\x00" * (10 * 1024 * 1024 + 1)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("big.png", big, "image/png")})
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "too_large"


def test_upload_pdf_201(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("doc.pdf", PDF, "application/pdf")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["mediaType"] == "application/pdf"
        assert body["byteSize"] == len(PDF)
        g = client.get(body["url"], headers=IDENT)
        assert g.status_code == 200 and g.content == PDF
        assert g.headers["content-type"].startswith("application/pdf")


def test_upload_text_plain_201(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("notes.txt", TEXT, "text/plain")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["mediaType"] == "text/plain"
        g = client.get(body["url"], headers=IDENT)
        assert g.status_code == 200 and g.content == TEXT


def test_upload_bad_type_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("x.png", b"not an image", "image/png")})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "bad_type"


def test_upload_declared_not_equal_sniffed_rejected(tmp_path, monkeypatch):
    """Declared image/png but the bytes sniff as GIF → 400 type_mismatch."""
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("x.png", GIF, "image/png")})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "type_mismatch"


def test_upload_dedup_same_bytes_reuses_row_and_file(tmp_path, monkeypatch):
    """Uploading identical bytes twice yields ONE on-disk file and ONE DB row."""
    from ccswitch import paths
    client = _client(tmp_path, monkeypatch)
    with client:
        r1 = client.post("/api/attachments", headers=IDENT,
                         files={"file": ("a.png", PNG, "image/png")})
        r2 = client.post("/api/attachments", headers=IDENT,
                         files={"file": ("b.png", PNG, "image/png")})
        assert r1.status_code == 201 and r2.status_code == 201
        # Same id returned — the row is reused.
        assert r1.json()["id"] == r2.json()["id"]
        # Exactly one file on disk under attachments_dir.
        files = list(paths.attachments_dir().glob("*.png"))
        assert len(files) == 1


def test_serve_unknown_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        g = client.get("/api/attachments/does-not-exist", headers=IDENT)
        assert g.status_code == 404


def test_upload_writes_audit_row(tmp_path, monkeypatch):
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent
    client = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/attachments", headers=IDENT,
                        files={"file": ("shot.png", PNG, "image/png")})
        assert r.status_code == 201

        import asyncio

        async def _check():
            async with db_session() as db:
                rows = (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "attachment.upload")
                )).scalars().all()
            return rows

        rows = asyncio.run(_check())
    assert len(rows) == 1
    assert rows[0].detail["type"] == "image/png"
    # Audit detail must never carry the raw bytes.
    assert "data" not in rows[0].detail
