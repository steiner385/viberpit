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

    async def deploy_config_list(self):
        return {}

    async def deploy_status(self):
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


class _FakeImpactResolver:
    """Stands in for WorkflowImpactResolver in _workflow_impact_ctx tests."""

    def __init__(self, impacts: dict):
        self.impacts = impacts          # {(repo, head_sha): impact dict}
        self.pruned: set | None = None
        self.calls: list = []

    async def impact_for_async(self, repo, head_sha):
        self.calls.append((repo, head_sha))
        return self.impacts.get((repo, head_sha))

    def prune(self, live_keys):
        self.pruned = set(live_keys)


def _pipeline_pr(number: int, **kw):
    from server.github.pr_status import PipelinePr, PullRequestInfo
    base: dict = dict(number=number, url=f"https://github.com/o/r/pull/{number}",
                      state="open", checks="pending", title="t", stage="ci", repo="o/r")
    base.update(kw)
    return PipelinePr(pr=PullRequestInfo(**base))


@pytest.mark.asyncio
async def test_workflow_impact_ctx_resolves_touching_open_prs_and_prunes():
    import server.api.pipeline as _pipeline_mod
    impact = {"summary": ["+ lint joins the merge_group gate"]}
    fake = _FakeImpactResolver({("o/r", "sha7"): impact})
    prs_by_repo = {"o/r": [
        _pipeline_pr(7, touches_workflows=True, head_sha="sha7"),
        _pipeline_pr(8, head_sha="sha8"),                          # not touching
        _pipeline_pr(9, touches_workflows=True),                   # no head sha
        _pipeline_pr(10, touches_workflows=True, head_sha="sha10",
                     state="merged", merged=True),                 # merged → skip
        _pipeline_pr(11, touches_workflows=True, head_sha="sha11"),  # no-change → None
    ]}
    with patch.object(_pipeline_mod, "_impact_resolver", fake):
        ctx = await _pipeline_mod._workflow_impact_ctx(prs_by_repo)
    assert ctx == {"o/r": {7: impact}}          # None impacts omitted
    assert set(fake.calls) == {("o/r", "sha7"), ("o/r", "sha11")}
    assert fake.pruned == {("o/r", "sha7"), ("o/r", "sha11")}


@pytest.mark.asyncio
async def test_workflow_impact_ctx_no_candidates_is_hermetic():
    """No touching PRs → {} without constructing the resolver (no gh, no sqlite)."""
    import server.api.pipeline as _pipeline_mod
    prs_by_repo = {"o/r": [_pipeline_pr(8, head_sha="sha8")]}
    with patch.object(_pipeline_mod, "_impact_resolver", None):
        ctx = await _pipeline_mod._workflow_impact_ctx(prs_by_repo)
        assert ctx == {}
        assert _pipeline_mod._impact_resolver is None   # never built


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
async def test_events_emits_notification_on_new_ci_failure():
    """Second frame introduces a ci-failed PR → expect an event: notification line.

    We patch _build_state at the module level so we control both frames precisely:
    frame 1 is a baseline (empty repos), frame 2 has a ci-failed PR. The notifier
    in the SSE generator should emit an event: notification line after frame 2.
    """
    import server.api.pipeline as _pipeline_mod  # noqa: PLC0415

    _build_state_calls = 0
    _FAILING_STATE = {
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "staleSince": None,
        "repos": [{
            "repo": "o/r", "hasDeploy": False, "queue": None,
            "prs": [{
                "repo": "o/r", "number": 42, "title": "break things", "url": "u",
                "stage": {"stage": "parked", "substate": "ci-failed",
                          "percent": None, "etaSeconds": None,
                          "etaRangeSeconds": None, "overdue": False},
            }],
        }],
    }
    _EMPTY_STATE = {
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "staleSince": None,
        "repos": [],
    }

    original_build_state = _pipeline_mod._build_state

    async def _patched_build_state(orch):
        nonlocal _build_state_calls
        _build_state_calls += 1
        if _build_state_calls == 1:
            return _EMPTY_STATE           # baseline — no notifications
        return _FAILING_STATE             # triggers ci-failed notification

    app = _make_app()
    scope = {
        "type": "http", "method": "GET", "path": "/api/pipeline/events",
        "raw_path": b"/api/pipeline/events", "query_string": b"",
        "headers": [(b"tailscale-user-login", b"testuser@example.com")],
        "client": ("1.2.3.4", 9999), "scheme": "http", "server": ("t", 80),
    }
    sent_request = False
    disconnect = asyncio.Event()
    notification_found = asyncio.Event()
    chunks: list[bytes] = []
    start: dict = {}

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
            combined = b"".join(chunks).decode()
            if "event: notification" in combined:
                notification_found.set()

    with patch.object(_pipeline_mod, "_build_state", _patched_build_state), \
         patch.object(_pipeline_mod, "_EVENTS_INTERVAL_S", 0.0):
        task = asyncio.create_task(app(scope, receive, send))
        try:
            with patch.dict(os.environ, _ENV):
                await asyncio.wait_for(notification_found.wait(), timeout=10)
        finally:
            disconnect.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    combined = b"".join(chunks).decode()
    assert "event: notification" in combined
    # Find the data line after event: notification
    lines = combined.splitlines()
    notif_idx = next(i for i, ln in enumerate(lines) if ln == "event: notification")
    data_line = lines[notif_idx + 1]
    assert data_line.startswith("data: ")
    ev = json.loads(data_line[len("data: "):])
    assert ev["type"] == "ci-failed"
    assert ev["rendered"]["title"] == "o/r#42 CI failed"
    assert ev["rendered"]["body"] == "break things"


async def _get_json(app, path: str, headers: dict[str, str]):
    """Drive a normal (non-streaming) GET through the ASGI app via httpx."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path, headers=headers)


@pytest.mark.asyncio
async def test_snapshot_returns_cached_frame_without_rebuilding():
    """A warm poller serves its cached frame; the request triggers no new build."""
    from server.pipeline.state_poller import PipelineStatePoller

    app = _make_app()
    builds = 0

    async def builder():
        nonlocal builds
        builds += 1
        return {"generatedAt": "x", "staleSince": None, "repos": [], "cached": True}

    poller = PipelineStatePoller(builder)
    await poller.poll_once()          # warm the cache (builds == 1)
    app.state.pipeline_state = poller

    with patch.dict(os.environ, _ENV):
        resp = await _get_json(
            app, "/api/pipeline/snapshot",
            {"tailscale-user-login": "testuser@example.com"},
        )
    assert resp.status_code == 200
    assert resp.json()["cached"] is True
    assert builds == 1                # served from cache — no extra build


@pytest.mark.asyncio
async def test_snapshot_cold_cache_builds_once_inline():
    """No frame yet → the endpoint builds a single frame inline and returns it."""
    from server.pipeline.state_poller import PipelineStatePoller

    app = _make_app()
    builds = 0

    async def builder():
        nonlocal builds
        builds += 1
        return {"generatedAt": "x", "staleSince": None, "repos": [], "n": builds}

    app.state.pipeline_state = PipelineStatePoller(builder)  # never polled → cold

    with patch.dict(os.environ, _ENV):
        resp = await _get_json(
            app, "/api/pipeline/snapshot",
            {"tailscale-user-login": "testuser@example.com"},
        )
    assert resp.status_code == 200
    assert resp.json()["n"] == 1
    assert builds == 1
    assert app.state.pipeline_state.generation == 1  # published for the next reader


@pytest.mark.asyncio
async def test_events_serves_cached_frame_without_per_connection_build():
    """The SSE feed reads the shared cache: a connection does not rebuild state."""
    from server.pipeline.state_poller import PipelineStatePoller

    app = _make_app()
    builds = 0

    async def builder():
        nonlocal builds
        builds += 1
        return {"generatedAt": "x", "staleSince": None, "repos": [], "b": builds}

    poller = PipelineStatePoller(builder)
    await poller.poll_once()          # warm frame (builds == 1)
    app.state.pipeline_state = poller

    with patch.dict(os.environ, _ENV):
        status, hdrs, body = await _drive_sse(
            app, [(b"tailscale-user-login", b"testuser@example.com")]
        )
    assert status == 200
    frame = json.loads(body.decode().splitlines()[0][len("data: "):])
    assert frame["b"] == 1            # the cached frame, served immediately
    assert builds == 1               # the connection did NOT trigger another build


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
