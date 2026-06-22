# server/tests/test_attachments_api.py
from __future__ import annotations
import os
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest
from fastapi.testclient import TestClient
from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}
PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64


def _client(env):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_upload_then_serve_roundtrip():
    with temp_env():
        os.environ["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(None)
        with c:
            r = c.post("/api/attachments",
                       files={"file": ("shot.png", PNG, "image/png")},
                       headers=IDENTITY)
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["mediaType"] == "image/png"
            assert body["filename"] == "shot.png"
            assert body["url"] == f"/api/attachments/{body['id']}"
            g = c.get(body["url"], headers=IDENTITY)
            assert g.status_code == 200
            assert g.content == PNG
            assert g.headers["content-type"].startswith("image/png")


def test_upload_rejects_non_image():
    with temp_env():
        os.environ["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(None)
        with c:
            r = c.post("/api/attachments",
                       files={"file": ("x.png", b"%PDF-1.7 not an image", "image/png")},
                       headers=IDENTITY)
            assert r.status_code == 400


def test_upload_rejects_oversize():
    with temp_env():
        os.environ["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(None)
        with c:
            big = PNG + b"\x00" * (10 * 1024 * 1024 + 1)
            r = c.post("/api/attachments",
                       files={"file": ("big.png", big, "image/png")},
                       headers=IDENTITY)
            assert r.status_code == 413


def test_serve_unknown_404():
    with temp_env():
        os.environ["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(None)
        with c:
            assert c.get("/api/attachments/nope", headers=IDENTITY).status_code == 404


def test_upload_requires_identity():
    with temp_env():
        os.environ["CCSWITCH_DB_URL"] = "sqlite+aiosqlite:///:memory:"
        c = _client(None)
        with c:
            r = c.post("/api/attachments", files={"file": ("a.png", PNG, "image/png")})
            assert r.status_code == 401
