"""/api/automations: list, events, sessions, PATCH validation, auth."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    # File-backed DB (not ``:memory:``): an in-memory SQLite gives each pooled
    # connection its OWN empty database, so a seed written on one connection is
    # invisible to the request read on another — an order-dependent flake. A real
    # file makes every connection share one DB. Same fix as the batch tests (#408).
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_list_includes_pr_doctor():
    with _client() as c:
        r = c.get("/api/automations", headers=H)
        assert r.status_code == 200
        ids = {a["id"] for a in r.json()}
        assert "pr-doctor" in ids
        pd = next(a for a in r.json() if a["id"] == "pr-doctor")
        assert pd["modes"][0] == "off"
        assert "configSchema" in pd and "status" in pd


def test_list_status_reflects_live_ctx_counts():
    """The badge shows real status threaded from the orchestrator's live ctx —
    not a hardcoded 'idle'. Regression: list endpoint passed ``ctx=None``, so a
    busily-working runner always rendered idle."""
    from unittest.mock import AsyncMock

    from server.automations.backlog_runner import BacklogRunnerLoop

    # The backlog loop ticks immediately at startup and ends each tick with
    # ``_stash_counts``, which overwrites ``backlog_counts`` with real (zero)
    # counts from the empty test DB — racing the fake counts injected below on
    # slow runners (CI flake). Neuter the stash so the injected counts stand;
    # the pr-doctor sweep needs no patch (its poller sleeps 75s before cycle 1).
    with patch.object(BacklogRunnerLoop, "_stash_counts", new=AsyncMock()), _client() as c:
        # The HTTP layer talks to the async adapter; the live ctx lives on the
        # wrapped orchestrator (``._orch``) where the loops stash counts each tick.
        orch = c.app.state.sessions._orch
        orch._pr_doctor_ctx.pr_doctor_counts = {"failing": 2}
        orch._backlog_ctx.backlog_counts = {"in_flight": 1, "eligible": 3, "needs_human": 0}

        automations = c.get("/api/automations", headers=H).json()
        pd = next(a for a in automations if a["id"] == "pr-doctor")
        assert pd["status"]["level"] == "active"
        assert "2" in pd["status"]["text"]
        br = next(a for a in automations if a["id"] == "backlog-runner")
        assert br["status"]["level"] == "active"
        assert "1 implementing" in br["status"]["text"]


def test_requires_auth():
    with _client() as c:
        assert c.get("/api/automations").status_code == 401


def test_patch_valid_mode_persists():
    with _client() as c:
        r = c.patch("/api/automations/pr-doctor", json={"enabled": True, "mode": "auto"}, headers=H)
        assert r.status_code == 200
        assert c.get("/api/automations", headers=H).json()[0]  # smoke
        pd = next(a for a in c.get("/api/automations", headers=H).json() if a["id"] == "pr-doctor")
        assert pd["enabled"] is True and pd["mode"] == "auto"


def test_patch_bad_mode_422():
    with _client() as c:
        assert c.patch("/api/automations/pr-doctor", json={"mode": "nope"}, headers=H).status_code == 422


def test_patch_bad_config_422():
    with _client() as c:
        r = c.patch("/api/automations/pr-doctor", json={"config": {"unknown": 1}}, headers=H)
        assert r.status_code == 422


def test_patch_unknown_automation_404():
    with _client() as c:
        assert c.patch("/api/automations/nope", json={"enabled": True}, headers=H).status_code == 404


def test_events_endpoint_empty_ok():
    with _client() as c:
        r = c.get("/api/automations/pr-doctor/events", headers=H)
        assert r.status_code == 200 and r.json() == []


def test_sessions_endpoint_returns_only_origin_matched():
    """GET /api/automations/{id}/sessions returns only sessions with matching origin."""
    from server.db import db_session
    from server.db.models import Session as DbSession

    async def _seed():
        async with db_session() as db:
            # Session spawned by pr-doctor automation
            db.add(DbSession(
                id="s-origin-match",
                name="pr-fix-session",
                account_name="acct1",
                cwd="/tmp",
                origin="pr-doctor",
                origin_ref="owner/repo#42",
            ))
            # Session with no origin (human-created) — must NOT appear
            db.add(DbSession(
                id="s-no-origin",
                name="manual-session",
                account_name="acct2",
                cwd="/tmp",
                origin=None,
            ))

    with _client() as c:
        # Seed on the app's OWN loop now that init_db has run. A bare
        # `asyncio.run` would touch the module-level engine's pool from a second
        # loop; that pool's asyncio.Queue binds to whichever loop first *awaits*
        # a checkout, so it only fails once startup saturates the pool.
        assert c.portal is not None
        c.portal.call(_seed)

        r = c.get("/api/automations/pr-doctor/sessions", headers=H)
        assert r.status_code == 200
        sessions = r.json()
        ids = [s["id"] for s in sessions]
        assert "s-origin-match" in ids
        assert "s-no-origin" not in ids
        matched = next(s for s in sessions if s["id"] == "s-origin-match")
        assert matched["originRef"] == "owner/repo#42"

        # 401 without auth header
        assert c.get("/api/automations/pr-doctor/sessions").status_code == 401


def test_list_reports_active_spawned_session_count():
    """GET /api/automations reports activeSessions = spawned sessions not ended/archived."""
    from server.db import db_session
    from server.db.models import Session as DbSession

    async def _seed():
        async with db_session() as db:
            # Two live spawned sessions (count), plus one ended and one archived (don't).
            db.add(DbSession(id="a-live-1", name="s1", account_name="x", cwd="/tmp",
                             status="in_progress", origin="pr-doctor"))
            db.add(DbSession(id="a-live-2", name="s2", account_name="x", cwd="/tmp",
                             status="idle", origin="pr-doctor"))
            db.add(DbSession(id="a-ended", name="s3", account_name="x", cwd="/tmp",
                             status="ended", origin="pr-doctor"))
            db.add(DbSession(id="a-archived", name="s4", account_name="x", cwd="/tmp",
                             status="archived", origin="pr-doctor"))
            # A live session spawned by a DIFFERENT automation must not be counted.
            db.add(DbSession(id="a-other", name="s5", account_name="x", cwd="/tmp",
                             status="idle", origin="backlog-runner"))

    with _client() as c:
        assert c.portal is not None
        c.portal.call(_seed)  # the app's own loop — see the note above
        automations = c.get("/api/automations", headers=H).json()
        pd = next(a for a in automations if a["id"] == "pr-doctor")
        assert pd["activeSessions"] == 2
        # An automation with no spawned sessions reports 0 (never null/absent).
        others = [a for a in automations if a["id"] != "pr-doctor"]
        assert all(isinstance(a["activeSessions"], int) for a in automations)
        assert all(a["activeSessions"] == 0 for a in others if a["id"] != "backlog-runner")
        br = next(a for a in automations if a["id"] == "backlog-runner")
        assert br["activeSessions"] == 1


# --------------------------------------------------------------------------- #
# Operator-triggered origins (#1268, #1269): registered for their engine        #
# ceiling and their config row, but with no control loop behind them.          #
# --------------------------------------------------------------------------- #

def test_the_pm_origins_offer_an_engine_selector_and_no_enable_toggle():
    """`pm-onboard` / `pm-classify` spawn sessions but are not ticked. They are
    registered so their audited ceiling is declared and an operator has somewhere
    to opt in — which means an `engine` field in the schema, and `operatorTriggered`
    so the card renders config without a switch that switches nothing."""
    with _client() as c:
        automations = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        for origin in ("pm-onboard", "pm-classify"):
            a = automations[origin]
            assert a["operatorTriggered"] is True
            assert a["modes"] == []
            assert a["enginePolicy"] == "codex_allowed"
            engine_field = next(f for f in a["configSchema"] if f["key"] == "engine")
            assert engine_field["choices"] == ["claude_code", "codex"]
            assert engine_field["default"] == "claude_code"


def test_a_looped_automation_is_not_marked_operator_triggered():
    with _client() as c:
        automations = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        assert automations["pr-doctor"]["operatorTriggered"] is False
        assert automations["backlog-runner"]["operatorTriggered"] is False


def test_patching_enabled_on_an_operator_triggered_origin_is_422():
    """There is no loop to enable, so persisting `enabled` would record a state
    no reader consults — refused rather than silently accepted."""
    with _client() as c:
        r = c.patch("/api/automations/pm-classify", json={"enabled": True}, headers=H)
        assert r.status_code == 422
        assert "no control loop" in r.json()["detail"]
        assert c.patch("/api/automations/pm-classify",
                       json={"schedule": {"kind": "every", "minutes": 30}},
                       headers=H).status_code == 422


def test_patching_the_engine_on_an_operator_triggered_origin_persists():
    """The half that DOES apply: the ceiling's own config field."""
    with _client() as c:
        r = c.patch("/api/automations/pm-onboard",
                    json={"config": {"engine": "codex"}}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["config"]["engine"] == "codex"
        assert c.patch("/api/automations/pm-onboard",
                       json={"config": {"engine": "gemini"}},
                       headers=H).status_code == 422
