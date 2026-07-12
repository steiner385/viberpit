"""SSE feed: /api/pipeline/events streams DashboardState frames.

Auth approach mirrors test_merge_trains_route.py: create_app() with CCSWITCH_DEV=1
and an empty allowlist so any identity passes, then swap app.state.sessions for a
lightweight fake facade. The Tailscale-User-Login header supplies the identity.

Transport note: httpx's ASGITransport buffers the full response body before
returning it, so it cannot read a single frame off an infinite SSE stream (it
hangs). We drive the ASGI app directly instead — send one request, capture the
first `http.response.body` frame, then send http.disconnect and cancel — which
is the real integration path (status line, headers, and a live DashboardState
frame all flow through the auth middleware).
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import pytest

from server.app import create_app
from server.github.merge_queue import QueueEntry
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    """Minimal sessions facade for the pipeline-events SSE route tests."""

    async def all_prs(self):
        return []

    async def all_merge_trains(self):
        return {}

    async def latest_queue_entries(self):
        return {
            "cairnea/KinDash": [
                QueueEntry(
                    pr_number=10824, position=1, state="QUEUED", enqueued_at=None,
                    batch_head_oid=None, group_checks=None, checks_total=None,
                    checks_done=None, eta_seconds=None,
                )
            ]
        }

    async def all_repos_stage_models(self):
        return {}


def _make_app():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )
        app = create_app(orchestrator=orch)
    app.state.sessions = _FakeSessions()
    return app


async def _drive_sse(app, headers: list[tuple[bytes, bytes]]):
    """Send one GET to /api/pipeline/events at the ASGI level, return
    (status, header_dict, first_body_bytes). Then disconnect + cancel."""
    scope = {
        "type": "http", "method": "GET", "path": "/api/pipeline/events",
        "raw_path": b"/api/pipeline/events", "query_string": b"",
        "headers": headers, "client": ("1.2.3.4", 9999), "scheme": "http",
        "server": ("t", 80),
    }
    sent_request = False
    disconnect = asyncio.Event()
    first_body = asyncio.Event()
    start: dict = {}
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            start.update(msg)
        elif msg["type"] == "http.response.body" and msg.get("body"):
            chunks.append(msg["body"])
            first_body.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout=10)
    finally:
        disconnect.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    hdrs = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return start.get("status"), hdrs, chunks[0]


@pytest.mark.asyncio
async def test_events_stream_emits_dashboard_state_frame():
    app = _make_app()
    with patch.dict(os.environ, _ENV):
        status, hdrs, body = await _drive_sse(
            app, [(b"tailscale-user-login", b"testuser@example.com")]
        )
    assert status == 200
    assert hdrs["content-type"].startswith("text/event-stream")
    line = body.decode().splitlines()[0]
    assert line.startswith("data: ")
    frame = json.loads(line[len("data: "):])
    assert "generatedAt" in frame and isinstance(frame["repos"], list)


@pytest.mark.asyncio
async def test_events_requires_identity():
    """No identity header -> 401 from the auth middleware (never opens the stream)."""
    app = _make_app()
    scope = {
        "type": "http", "method": "GET", "path": "/api/pipeline/events",
        "raw_path": b"/api/pipeline/events", "query_string": b"",
        "headers": [], "client": ("1.2.3.4", 9999), "scheme": "http",
        "server": ("t", 80),
    }
    start: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            start.update(msg)

    with patch.dict(os.environ, _ENV):
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
    assert start.get("status") == 401
