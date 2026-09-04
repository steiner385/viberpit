"""GET /api/config exposes adoptionEnabled, off by default."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_config_adoption_disabled_by_default():
    client = _client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_ENABLE_ADOPTION", None)
        r = client.get("/api/config", headers=H)
    assert r.status_code == 200
    assert r.json()["adoptionEnabled"] is False


def test_config_adoption_enabled_when_flag_set():
    client = _client()
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_ADOPTION": "1"}):
        r = client.get("/api/config", headers=H)
    assert r.json()["adoptionEnabled"] is True


def test_config_stt_disabled_by_default():
    client = _client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_ENABLE_STT", None)
        r = client.get("/api/config", headers=H)
    assert r.json()["sttEnabled"] is False


def test_config_stt_enabled_requires_flag_and_installed_engine():
    client = _client()
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_STT": "1"}), \
            patch("server.api.config.stt_available", return_value=True):
        assert client.get("/api/config", headers=H).json()["sttEnabled"] is True
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_STT": "1"}), \
            patch("server.api.config.stt_available", return_value=False):
        assert client.get("/api/config", headers=H).json()["sttEnabled"] is False


def test_stt_settings_defaults_and_env(monkeypatch):
    from server.config import get_settings
    for k in ("CCSWITCH_ENABLE_STT", "CCSWITCH_STT_MODEL", "CCSWITCH_STT_CPU_THREADS",
              "CCSWITCH_STT_TICK_S", "CCSWITCH_STT_MAX_CONCURRENT", "CCSWITCH_STT_MAX_SESSION_S"):
        monkeypatch.delenv(k, raising=False)
    s = get_settings()
    assert (s.enable_stt, s.stt_model, s.stt_cpu_threads) == (False, "base.en", 4)
    assert (s.stt_tick_s, s.stt_max_concurrent, s.stt_max_session_s) == (1.0, 3, 600.0)
    monkeypatch.setenv("CCSWITCH_ENABLE_STT", "true")
    monkeypatch.setenv("CCSWITCH_STT_MODEL", "small.en")
    monkeypatch.setenv("CCSWITCH_STT_TICK_S", "0.5")
    s = get_settings()
    assert (s.enable_stt, s.stt_model, s.stt_tick_s) == (True, "small.en", 0.5)
