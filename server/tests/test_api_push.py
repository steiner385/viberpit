from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client(monkeypatch):
    import server.config as config
    monkeypatch.setattr(config, "VAPID_PUBLIC_KEY", "PUBKEY")
    monkeypatch.setattr(config, "VAPID_PRIVATE_KEY", "PRIV")
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(poll_interval=9999.0)
        with TestClient(create_app(orchestrator=orch), raise_server_exceptions=False) as c:
            yield c


def test_config_returns_public_key(monkeypatch):
    with _client(monkeypatch) as c:
        r = c.get("/api/push/config", headers=IDENTITY)
        assert r.status_code == 200
        assert r.json() == {"vapidPublicKey": "PUBKEY", "enabled": True}


def test_subscribe_then_unsubscribe(monkeypatch):
    with _client(monkeypatch) as c:
        sub = {"endpoint": "https://push/1", "keys": {"p256dh": "k", "auth": "a"}}
        assert c.post("/api/push/subscribe", json=sub, headers=IDENTITY).status_code == 200
        # idempotent re-subscribe
        assert c.post("/api/push/subscribe", json=sub, headers=IDENTITY).status_code == 200
        r = c.delete("/api/push/subscribe?endpoint=https://push/1", headers=IDENTITY)
        assert r.status_code == 200
        # unauthenticated is rejected
        assert c.get("/api/push/config").status_code == 401
