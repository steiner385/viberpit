"""/ws/stt: identity-gated PCM stream → interim/final transcript events."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.auth.middleware import IdentityMiddleware
from server.stt.ws import CLOSE_BUSY, CLOSE_NOT_ENABLED, build_stt_ws_router

H = {"Tailscale-User-Login": "alice@example.com"}


class FakeEngine:
    def __init__(self):
        self.warmed = 0
        self.fed_total = 0

    def warm(self):
        self.warmed += 1

    def speech_segments(self, pcm: bytes):
        return [(0.0, len(pcm) / 32000)]

    def transcribe(self, pcm: bytes) -> str:
        self.fed_total = max(self.fed_total, len(pcm))
        return f"words:{len(pcm)}"


@pytest.fixture(autouse=True)
def _identity_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TAILNET_ALLOWLIST", "alice@example.com")
    monkeypatch.delenv("CCSWITCH_ALLOW_UNAUTHENTICATED", raising=False)


def _app(engine, **kw) -> TestClient:
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)
    app.include_router(build_stt_ws_router(lambda: engine, tick_s=kw.pop("tick_s", 0.05), **kw))
    return TestClient(app, raise_server_exceptions=False)


def _collect_until(ws, kind: str, limit: int = 50) -> list[dict]:
    got = []
    for _ in range(limit):
        m = ws.receive_json()
        got.append(m)
        if m["type"] == kind:
            return got
    raise AssertionError(f"no {kind!r} in {got}")


def test_disabled_engine_closes_4004():
    client = _app(None)
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == CLOSE_NOT_ENABLED


def test_happy_path_streams_interim_then_final_then_done():
    eng = FakeEngine()
    client = _app(eng)
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00\x00" * 8000)          # 0.5 s
        got = _collect_until(ws, "interim")
        assert got[-1]["text"].startswith("words:")
        ws.send_bytes(b"\x00\x00" * 8000)
        ws.send_json({"type": "stop"})
        got = _collect_until(ws, "done")
        kinds = [m["type"] for m in got]
        assert "final" in kinds and kinds[-1] == "done"
    assert eng.warmed == 1
    assert eng.fed_total == 32000                     # both frames reached the session


def test_bad_sample_rate_is_rejected_with_error():
    client = _app(FakeEngine())
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 44100})
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 1003


def test_missing_start_frame_is_rejected():
    client = _app(FakeEngine())
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_bytes(b"\x00\x00" * 10)
        assert ws.receive_json()["type"] == "error"


def test_over_capacity_closes_4005():
    client = _app(FakeEngine(), max_concurrent=1)
    with client.websocket_connect("/ws/stt", headers=H) as first:
        first.send_json({"type": "start", "sampleRate": 16000})
        assert first.receive_json() == {"type": "ready"}
        with client.websocket_connect("/ws/stt", headers=H) as second:
            with pytest.raises(WebSocketDisconnect) as ei:
                second.receive_text()
        assert ei.value.code == CLOSE_BUSY
        first.send_json({"type": "stop"})
        _collect_until(first, "done")


def test_engine_failure_reports_error_and_closes_1011():
    class Boom(FakeEngine):
        def warm(self):
            raise RuntimeError("model download failed")
    client = _app(Boom())
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 16000})
        m = ws.receive_json()
        assert m["type"] == "error" and "model download failed" in m["message"]
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 1011


def test_session_deadline_flushes_and_closes():
    eng = FakeEngine()
    client = _app(eng, max_session_s=0.2)
    with client.websocket_connect("/ws/stt", headers=H) as ws:
        ws.send_json({"type": "start", "sampleRate": 16000})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00\x00" * 1600)
        t0 = time.monotonic()
        got = _collect_until(ws, "done")
        assert time.monotonic() - t0 < 5
        assert got[-1] == {"type": "done"}


def test_unauthenticated_upgrade_is_rejected():
    client = _app(FakeEngine())
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/stt") as ws:
            ws.receive_text()
