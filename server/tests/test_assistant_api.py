"""POST /api/assistant/session · GET /api/assistant/status (spec §4.1, sub-spec B §7)."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from ccswitch.selector import Picked
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "u@example.com"}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": "", "VIBERPIT_SESSION_SECRET": "x" * 32,
        "VIBERPIT_ASSISTANT_ENABLED": "true"}


def _seed_account(name: str) -> None:
    """Mirrors test_assistant_orchestrator.py's helper: a real cred slot so
    ``select_account`` (unpatched paths in ``_isolated_dir_for``) has something
    to isolate."""
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


@contextmanager
def _client(tmp_path, env=None):
    # The env patch must stay live for the whole request lifecycle, not just app
    # construction: assistant_enabled()/resolve_model_policy() re-read
    # get_settings() (env) on every request, and the TestClient lifespan
    # (init_db) itself runs lazily on `__enter__`, after this function would
    # otherwise have already returned and unwound the patch.
    shared = tmp_path / "dot-claude"
    shared.mkdir(exist_ok=True)
    (shared / "settings.json").write_text("{}")
    (shared / "projects").mkdir(exist_ok=True)
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".claude.json").write_text("{}")
    full_env = {**_ENV, "VIBERPIT_HOME": str(tmp_path / "store"),
                "CLAUDE_CONFIG_DIR": str(shared), **(env or {})}
    with patch.dict(os.environ, full_env):
        _seed_account("alpha")
        # create_session's account/compose/isolation plumbing is exercised for
        # real (see test_assistant_orchestrator.py); only account SELECTION
        # (which would otherwise hit this host's real, possibly-exhausted
        # account ledger) and cred-store composition are faked out.
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir.trust_project"), \
             patch("server.sessions.orchestrator.select_account",
                   return_value=Picked("alpha")):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            with TestClient(app) as c:
                yield c


def test_disabled_is_404(tmp_path):
    with _client(tmp_path, {"VIBERPIT_ASSISTANT_ENABLED": "false"}) as c:
        assert c.get("/api/assistant/status", headers=_H).json()["enabled"] is False
        r = c.post("/api/assistant/session", json={}, headers=_H)
        assert r.status_code == 404 and r.json()["error"]["code"] == "ASSISTANT_DISABLED"


def test_status_reports_operator_tools_off_by_default(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/assistant/status", headers=_H).json()["operatorTools"] is False


def test_status_reports_operator_tools_on_via_env(tmp_path):
    with _client(tmp_path, {"VIBERPIT_ASSISTANT_OPERATOR_TOOLS": "true"}) as c:
        assert c.get("/api/assistant/status", headers=_H).json()["operatorTools"] is True


def test_status_reports_the_engine_the_drawer_runs_on(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/assistant/status", headers=_H).json()["engine"] == "claude_code"


def test_every_create_path_reports_an_engine(tmp_path):
    # The drawer's boundary IS its overlay, so "which engine answered?" must never
    # be something a client has to assume — including on the reuse path, where the
    # running thread's engine wins over the deployment default.
    with _client(tmp_path) as c:
        created = c.post("/api/assistant/session", json={}, headers=_H).json()
        existing = c.post("/api/assistant/session", json={}, headers=_H).json()
    assert created["engine"] == "claude_code"
    assert existing["engine"] == "claude_code"


def test_a_codex_drawer_is_refused_rather_than_downgraded(tmp_path):
    # A host with no Codex runner must answer 409, never fall back to Claude: the
    # two engines have different overlays, so substituting one silently would
    # answer a question the operator did not ask.
    with _client(tmp_path, {"VIBERPIT_ASSISTANT_ENGINE": "codex"}) as c:
        r = c.post("/api/assistant/session", json={}, headers=_H)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ASSISTANT_ENGINE_UNAVAILABLE"


def test_create_is_idempotent_per_identity_and_hidden(tmp_path):
    with _client(tmp_path) as c:
        r1 = c.post("/api/assistant/session", json={}, headers=_H)
        assert r1.status_code == 201 and r1.json()["state"] == "created"
        r2 = c.post("/api/assistant/session", json={}, headers=_H)
        assert r2.json()["id"] == r1.json()["id"] and r2.json()["state"] == "existing"
        other = c.post("/api/assistant/session", json={},
                       headers={"Tailscale-User-Login": "v@example.com"})
        assert other.json()["id"] != r1.json()["id"]
        ids = {s["id"] for s in c.get("/api/sessions", headers=_H).json()}
        assert r1.json()["id"] not in ids
        assert c.get(f"/api/sessions/{r1.json()['id']}", headers=_H).json()["kind"] == "assistant"


def test_fresh_archives_and_recreates(tmp_path):
    with _client(tmp_path) as c:
        first = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        second = c.post("/api/assistant/session", json={"fresh": True}, headers=_H).json()
        assert second["state"] == "created" and second["id"] != first
        assert c.get("/api/assistant/status", headers=_H).json()["sessionId"] == second["id"]


async def _end_row(session_id: str) -> None:
    """Park the row exactly as the idle reap does (spec §4.7: status=ended, driver
    closed, CLI transcript kept)."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                         .values(status="ended"))
        await db.commit()


def test_reaped_thread_is_reused_after_a_daemon_restart(tmp_path):
    """#1122: reap (status=ended) + restart used to lose the thread — `rehydrate`
    skips ended rows, so the drawer created a new assistant and orphaned the old
    transcript. The endpoint must hand back the SAME session, resumed."""
    with _client(tmp_path) as c:
        first = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        # app.state.sessions is the facade; the records live on the real orchestrator.
        orch = c.app.state.sessions._orch
        c.portal.call(_end_row, first)
        orch._records.clear()          # daemon restart: nothing in memory…
        orch._drivers.clear()
        c.portal.call(orch.rehydrate)  # …and rehydrate does not bring an ended row back
        assert first not in orch._records

        again = c.post("/api/assistant/session", json={}, headers=_H).json()
        assert again["id"] == first and again["state"] == "resumed"
        assert c.get("/api/assistant/status", headers=_H).json()["sessionId"] == first


def test_resume_threads_the_callers_identity(tmp_path):
    """The resume path must build the driver under the REAL identity.

    ensure_driver defaults to SYSTEM_IDENTITY, and a driver built under "system"
    registers its questions/permissions as owned by "system" — every answer the
    real user then sends is rejected ("question no longer pending", #1080). The WS
    attach re-threads identity moments later, so the window is narrow, but anything
    the resumed driver asks before the drawer attaches is unanswerable."""
    from unittest.mock import AsyncMock

    with _client(tmp_path) as c:
        sid = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        orch = c.app.state.sessions._orch
        orch._records[sid].status = "ended"
        with patch.object(SessionOrchestrator, "ensure_driver", new=AsyncMock()) as ed:
            r = c.post("/api/assistant/session", json={}, headers=_H)
        assert r.json()["state"] == "resumed"
        assert ed.await_args.kwargs.get("identity") == "u@example.com"


def test_gated_model_is_400(tmp_path):
    with _client(tmp_path, {"VIBERPIT_MODEL_APPROVAL_DENYLIST": "fable"}) as c:
        r = c.post("/api/assistant/session", json={"model": "fable"}, headers=_H)
        assert r.status_code == 400 and r.json()["error"]["code"] == "MODEL_GATED"


def test_switching_the_assistant_to_a_gated_model_is_400_not_a_parked_hold(tmp_path):
    """The drawer's model picker posts to the ordinary session model route. For the
    assistant kind a gated tier must be REFUSED, not parked: the approval card that
    clears a hold renders inside the drawer that the hold just silenced."""
    with _client(tmp_path, {"VIBERPIT_MODEL_APPROVAL_DENYLIST": "fable"}) as c:
        sid = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        r = c.post(f"/api/sessions/{sid}/model", json={"model": "fable"}, headers=_H)
        assert r.status_code == 400
        # This route wraps ActionError as {"detail": {...}} (_action_error_response),
        # whereas POST /api/assistant/session uses {"error": {...}}. The web decoder
        # (apiErrorCode in @/api/client) reads both spellings for exactly this reason.
        assert r.json()["detail"]["code"] == "MODEL_GATED"
        # And the refusal left no hold behind for a human to clear.
        assert c.app.state.sessions._orch._records[sid].pending_model is None


def test_switching_the_assistant_to_an_allowed_model_still_works(tmp_path):
    with _client(tmp_path, {"VIBERPIT_MODEL_APPROVAL_DENYLIST": "fable"}) as c:
        sid = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        r = c.post(f"/api/sessions/{sid}/model", json={"model": "opus"}, headers=_H)
        assert r.status_code == 200, r.text


# ── History + resume (drawer "History" list) ──────────────────────────────────


def test_history_is_scoped_to_owner_identity(tmp_path):
    other_h = {"Tailscale-User-Login": "v@example.com"}
    with _client(tmp_path) as c:
        mine = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        c.post("/api/assistant/session", json={"fresh": True}, headers=_H)  # archives `mine`
        theirs = c.post("/api/assistant/session", json={}, headers=other_h).json()["id"]
        c.post("/api/assistant/session", json={"fresh": True}, headers=other_h)  # archives `theirs`

        mine_history = c.get("/api/assistant/history", headers=_H).json()
        assert [h["id"] for h in mine_history] == [mine]

        their_history = c.get("/api/assistant/history", headers=other_h).json()
        assert [h["id"] for h in their_history] == [theirs]


def test_history_reports_snippet_and_archived_at(tmp_path):
    with _client(tmp_path) as c:
        sid = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        c.post("/api/assistant/session", json={"fresh": True}, headers=_H)
        row = c.get("/api/assistant/history", headers=_H).json()[0]
        assert row["id"] == sid
        assert row["archivedAt"]
        # `initial_prompt` is unset for a freshly created assistant thread — the
        # field is None (not KeyError), the same "no snippet yet" shape the web
        # side renders as a placeholder.
        assert "snippet" in row


def test_history_caps_at_20_most_recent(tmp_path):
    with _client(tmp_path) as c:
        ids = []
        for _ in range(22):
            sid = c.post("/api/assistant/session", json={"fresh": True}, headers=_H).json()["id"]
            ids.append(sid)
        # Each `fresh` create archives the PREVIOUS live thread, so 21 of the 22
        # created threads end up archived (the 22nd is still live) — enough to
        # prove the 20-item cap.
        history = c.get("/api/assistant/history", headers=_H).json()
        assert len(history) == 20
        history_ids = [h["id"] for h in history]
        assert ids[0] not in history_ids            # oldest archived, dropped by the cap
        assert history_ids[0] == ids[-2]             # most-recently-archived first


def test_resume_restores_the_target_and_archives_the_current_live_thread(tmp_path):
    with _client(tmp_path) as c:
        first = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        second = c.post("/api/assistant/session", json={"fresh": True}, headers=_H).json()["id"]
        assert c.get("/api/assistant/status", headers=_H).json()["sessionId"] == second

        r = c.post(f"/api/assistant/session/{first}/resume", headers=_H)
        assert r.status_code == 200, r.text
        assert r.json() == {"id": first, "model": r.json()["model"],
                            "engine": r.json()["engine"], "state": "resumed"}

        status = c.get("/api/assistant/status", headers=_H).json()
        assert status["sessionId"] == first

        # `second` is now the archived one.
        history_ids = [h["id"] for h in c.get("/api/assistant/history", headers=_H).json()]
        assert history_ids == [second]


def test_resume_is_a_noop_for_the_already_live_thread(tmp_path):
    with _client(tmp_path) as c:
        sid = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        r = c.post(f"/api/assistant/session/{sid}/resume", headers=_H)
        assert r.status_code == 200
        assert r.json() == {"id": sid, "model": r.json()["model"],
                            "engine": r.json()["engine"], "state": "existing"}
        # No archive happened — history is still empty.
        assert c.get("/api/assistant/history", headers=_H).json() == []


def test_resume_rejects_a_thread_owned_by_another_identity(tmp_path):
    other_h = {"Tailscale-User-Login": "v@example.com"}
    with _client(tmp_path) as c:
        theirs = c.post("/api/assistant/session", json={}, headers=other_h).json()["id"]
        c.post("/api/assistant/session", json={"fresh": True}, headers=other_h)  # archives `theirs`

        r = c.post(f"/api/assistant/session/{theirs}/resume", headers=_H)
        assert r.status_code == 404


def test_resume_rejects_a_non_assistant_session(tmp_path):
    with _client(tmp_path) as c:
        orch = c.app.state.sessions._orch

        async def _make_and_archive_plain_session() -> str:
            from server.assistant.api import _scratch_cwd
            rec = await orch.create_session(
                name="Plain", cwd=_scratch_cwd("u@example.com"), identity="u@example.com")
            await orch.archive_session(rec.id, archived_by="user")
            return rec.id

        sid = c.portal.call(_make_and_archive_plain_session)
        r = c.post(f"/api/assistant/session/{sid}/resume", headers=_H)
        assert r.status_code == 404


def test_resume_rejects_an_unknown_id(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/assistant/session/does-not-exist/resume", headers=_H)
        assert r.status_code == 404


def test_resume_threads_the_callers_identity_onto_the_resumed_driver(tmp_path):
    """Same #1080 hazard as the reap-resume path: a driver resumed under the wrong
    identity rejects every answer the real caller sends."""
    from unittest.mock import AsyncMock

    with _client(tmp_path) as c:
        first = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        c.post("/api/assistant/session", json={"fresh": True}, headers=_H)  # archives `first`
        with patch.object(SessionOrchestrator, "ensure_driver", new=AsyncMock()) as ed:
            r = c.post(f"/api/assistant/session/{first}/resume", headers=_H)
        assert r.status_code == 200, r.text
        assert ed.await_args.kwargs.get("identity") == "u@example.com"


# ---------------------------------------------------------------------------
# #2184: fleet dispatch refusals must be typed, never an unhandled 500
# ---------------------------------------------------------------------------


class _RefusingCreate:
    """A ``create_session`` that always raises *exc* — the fleet conditions the
    drawer's create genuinely hits (a restart window, every host at capacity, a
    stale host preference), with no second host needed to produce them."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def __call__(self, *a, **k):
        raise self.exc


def _refusal(tmp_path, exc: Exception):
    with _client(tmp_path) as c, \
            patch.object(SessionOrchestrator, "create_session", _RefusingCreate(exc)):
        return c.post("/api/assistant/session", json={}, headers=_H)


def test_no_host_available_is_a_typed_409_not_a_500(tmp_path):
    """The bug: on 2026-09-21 every daemon was mid-restart, ``FleetRouter.pick``
    raised ``NoHostAvailable``, and nothing in ``create`` caught it — a routine,
    fully-expected fleet condition escaped as an unhandled 500 (#2184), so the
    drawer said "internal server error" instead of why."""
    from server.sessions.fleet_router import NoHostAvailable

    r = _refusal(tmp_path, NoHostAvailable("no enabled, healthy host with capacity"))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "no_host_available"


def test_no_engine_host_available_keeps_its_engine_and_host_reasons(tmp_path):
    """Ordered before ``NoHostAvailable``, which it subclasses: "no host available"
    sends an operator to look at host health when the real answer names the runner
    to fix. Same body the public create path returns."""
    # Via the server re-export, not the legacy package path: the brand ratchet's
    # `python-package` entry sits AT its ceiling, so one more import of it here
    # goes red. Every other server-side fleet test reads AgentEngine this way too.
    from server.sessions.engine import AgentEngine
    from server.sessions.fleet_router import NoEngineHostAvailable

    r = _refusal(tmp_path, NoEngineHostAvailable(
        AgentEngine.CODEX, {"dobby": "binary_not_found"}))
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "no_engine_host_available"
    assert err["engine"] == "codex"
    assert err["hostReasons"] == {"dobby": "binary_not_found"}


def test_unknown_host_is_a_typed_400(tmp_path):
    from server.sessions.fleet_router import UnknownHost

    r = _refusal(tmp_path, UnknownHost("no orchestrator for host 'bogus'"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "unknown_host"
