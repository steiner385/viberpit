"""Tests for the queue-stall reason surfaced on GET /api/sessions.

The 2026-08-22 deadlock left 15 sessions queued with capacity fully occupied by
finished-but-exempt-from-archive sessions, and the UI said nothing — finding the
cause took a DB query, a source read, and a py-spy dump. This endpoint should say
why the queue isn't draining, using the numbers, whenever it can't.

Mirrors the fixture/client conventions in test_session_queue_api.py (fake facade
+ real orchestrator app + a temp-file DB so AppSettings.max_active_sessions reads
hit real SQLAlchemy machinery, not a stub).
"""
from __future__ import annotations

import os
import tempfile
import types
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.api.sessions import _record_to_dict
from server.app import create_app
from server.db.models import DEFAULT_MAX_ACTIVE_SESSIONS
from server.sessions.background_tasks import EMPTY_BACKGROUND_WORK
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}


def _make_env(db_url: str) -> dict:
    return {
        "VIBERPIT_DB_URL": db_url,
        "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": "",
    }


def _make_record(sid: str, status: str = "idle") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=sid,
        name=f"Session {sid}",
        account="acct1",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None,
        reasoning_effort=None,
        repo=None,
        status=status,
        name_is_auto=True,
        detached=False,
        adopted_in_place=False,
        sub_agents=None,
        created_at=None,
        last_turn_at=None,
        account_pinned=False,
        model=None,
        active_goal=None,
        usage_unverified=False,
        updated_at_iso=None,
        rev_at=None,
        cwd=None,
    )


class _FakeOrch:
    """Minimal facade implementing list_sessions for API tests (no queue RPCs needed)."""

    def __init__(self, records=None, positions=None):
        self._records: dict[str, types.SimpleNamespace] = {r.id: r for r in (records or [])}
        self._positions: dict[str, int] = dict(positions or {})

    async def list_sessions(self):
        return list(self._records.values())

    async def latest_suggestions(self, sid):
        return []

    async def prs_for(self, sid):
        return []

    async def latest_summary(self, sid, *, viewing=False):
        return ""

    async def session_queue_positions(self) -> dict[str, int]:
        return dict(self._positions)

    async def get_session(self, session_id: str):
        return self._records.get(session_id)


def _build_client(fake_orch: _FakeOrch, db_url: str) -> TestClient:
    env = _make_env(db_url)
    with patch.dict(os.environ, env):
        real_orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=lambda n: None
        )
        # The real orchestrator's background drains (rehydrate/status/close) would
        # otherwise race the seeded queue rows: with nothing occupying the cap they
        # try to spawn the entry, fail on its bare params, and delete it before the
        # GET reads it — a load-dependent CI-only red. These tests read the stall
        # reason; they never want a drain.
        async def _no_drain(force: str | None = None,
                            ceiling_out: dict | None = None) -> int:
            return 0

        real_orch._maybe_start_queued = _no_drain  # type: ignore[method-assign]
        app = create_app(orchestrator=real_orch)
    app.state.sessions = fake_orch
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Unit: _record_to_dict
# ---------------------------------------------------------------------------


def test_record_to_dict_includes_queue_stall_reason_when_passed():
    rec = _make_record("q1", status="queued")
    reason = {"code": "capacity_full", "message": "stalled", "capOccupying": 5,
               "cap": 5, "queuedCount": 1}
    d = _record_to_dict(rec, queue_stall_reason=reason)
    assert d["queueStallReason"] == reason


def test_record_to_dict_queue_stall_reason_none_by_default():
    rec = _make_record("s1", status="idle")
    d = _record_to_dict(rec)
    assert d.get("queueStallReason") is None


_SNAPSHOT = {
    "items": [{"toolUseId": "tu1", "kind": "task", "name": "Bash",
               "description": "pytest -q", "startedAt": None}],
    "wakeup": True,
    "wakeupAt": None,
}


def test_record_to_dict_ended_session_serves_empty_background_work():
    rec = _make_record("e1", status="ended")
    rec.background_work = _SNAPSHOT
    assert _record_to_dict(rec)["backgroundWork"] == EMPTY_BACKGROUND_WORK


def test_record_to_dict_waiting_external_keeps_background_work_snapshot():
    rec = _make_record("w1", status="waiting_external")
    rec.background_work = _SNAPSHOT
    assert _record_to_dict(rec)["backgroundWork"] == _SNAPSHOT


# ---------------------------------------------------------------------------
# Integration: GET /api/sessions
# ---------------------------------------------------------------------------


def test_queue_stalled_on_capacity_reports_reason_with_numbers():
    """Queue non-empty AND capacity full (== DEFAULT_MAX_ACTIVE_SESSIONS) → a
    queued session's payload names the cause and carries the numbers."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        cap = DEFAULT_MAX_ACTIVE_SESSIONS
        occupying = [_make_record(f"a{i}", status="in_progress") for i in range(cap)]
        queued = [_make_record("q1", status="queued")]
        fake = _FakeOrch(records=occupying + queued, positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = {s["id"]: s for s in resp.json()}
        reason = sessions["q1"]["queueStallReason"]
        assert reason is not None
        assert reason["capOccupying"] == cap
        assert reason["cap"] == cap
        assert reason["queuedCount"] == 1
        assert str(cap) in reason["message"]
        # Non-queued sessions never carry a stall reason.
        assert sessions["a0"]["queueStallReason"] is None
    finally:
        os.unlink(tf.name)


def test_queue_nonempty_with_capacity_available_reports_no_reason():
    """Queue non-empty but capacity is NOT full → nothing is wrong, no reason."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        cap = DEFAULT_MAX_ACTIVE_SESSIONS
        occupying = [_make_record(f"a{i}", status="in_progress") for i in range(cap - 1)]
        queued = [_make_record("q1", status="queued")]
        fake = _FakeOrch(records=occupying + queued, positions={"q1": 1})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = {s["id"]: s for s in resp.json()}
        assert sessions["q1"]["queueStallReason"] is None
    finally:
        os.unlink(tf.name)


def test_empty_queue_reports_no_reason():
    """No queued sessions at all → no stall reason, even if capacity is full."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        cap = DEFAULT_MAX_ACTIVE_SESSIONS
        occupying = [_make_record(f"a{i}", status="in_progress") for i in range(cap)]
        fake = _FakeOrch(records=occupying, positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        sessions = resp.json()
        assert all(s["queueStallReason"] is None for s in sessions)
    finally:
        os.unlink(tf.name)


def test_response_is_backward_compatible():
    """Every field the endpoint returned before this change is still present
    with the same type — the field addition is purely additive."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        rec = _make_record("s1", status="idle")
        fake = _FakeOrch(records=[rec], positions={})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        s = resp.json()[0]
        expected_types = {
            "id": str, "name": str, "accountName": str, "status": str,
            "nameIsAuto": bool, "detached": bool, "adoptedInPlace": bool,
            "subAgents": list, "backgroundWork": dict, "accountPinned": bool, "latestSuggestions": list,
            "prs": list, "usageUnverified": bool, "interruptedByRestart": bool,
            "summary": str,
        }
        for field, typ in expected_types.items():
            assert field in s, f"missing pre-existing field {field!r}"
            assert isinstance(s[field], typ), (
                f"{field!r} changed type: expected {typ}, got {type(s[field])}")
        # Nullable pre-existing fields must still be present (value may be None).
        for field in (
            "repo", "host", "origin", "originRef", "assistedBy", "createdAt",
            "updatedAt", "revAt", "model", "activeGoal", "inferredGoal",
            "effectiveGoal", "goalSource", "autopilotPaused",
            "autopilotCooldownUntil", "autoArchiveOptOut", "queuePosition",
        ):
            assert field in s, f"missing pre-existing field {field!r}"
        # And the new field is additive, not a replacement.
        assert "queueStallReason" in s
    finally:
        os.unlink(tf.name)


# ---------------------------------------------------------------------------
# Per-session reasons: a queued session that has WAITED with free fleet capacity
# always says why (incident 2026-10-06: ten sessions queued 14 h behind a product
# cap, hosts and accounts free, queueStallReason null on every one).
# ---------------------------------------------------------------------------


def _list_with_queue(queued_records, *, entries, seed=None):
    """GET /api/sessions with real ``session_queue`` rows for *entries*
    (``{sid: (age_seconds, params)}``) and an optional async *seed(db)*."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from server.db import db_session
    from server.db.models import Session as DbSession, SessionQueue

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        fake = _FakeOrch(records=queued_records,
                         positions={r.id: i + 1 for i, r in enumerate(queued_records)})
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            async def _seed():
                now = datetime.now(timezone.utc)
                async with db_session() as db:
                    for sid, (age, params) in entries.items():
                        db.add(DbSession(id=sid, name=sid, account_name="a", cwd="/tmp",
                                         status="queued"))
                        db.add(SessionQueue(session_id=sid, priority=1, params=params,
                                            enqueued_at=now - timedelta(seconds=age)))
                    if seed is not None:
                        await seed(db)
            client.portal.call(_seed) if getattr(client, "portal", None) else asyncio.run(_seed())
            resp = client.get("/api/sessions", headers=_H)
        assert resp.status_code == 200, resp.text
        return {s["id"]: s["queueStallReason"] for s in resp.json()}
    finally:
        os.unlink(tf.name)


async def _seed_full_product(db):
    from server.db.models import Automation, AutomationTargetState, Project, ProjectRepo
    if await db.get(Automation, "backlog-runner") is None:
        db.add(Automation(id="backlog-runner"))
    db.add(Project(id="proj-k", slug="kindash",
                   automation_policy={"dispatch": "auto", "max_in_flight": 1}))
    db.add(ProjectRepo(project_id="proj-k", repo="cairnea/KinDash"))
    db.add(AutomationTargetState(automation_id="backlog-runner", target="cairnea/KinDash#1",
                                 state={"product": "kindash", "claimed": True}))


def test_a_session_queued_behind_a_full_product_names_the_product_and_numbers():
    params = {"queue_reason": "product_capacity", "repo": "cairnea/KinDash"}
    reasons = _list_with_queue([_make_record("q1", status="queued")],
                               entries={"q1": (600, params)}, seed=_seed_full_product)
    reason = reasons["q1"]
    assert reason["code"] == "product_capacity_full"
    assert (reason["product"], reason["inFlight"], reason["cap"]) == ("kindash", 1, 1)
    assert "kindash" in reason["message"] and "1/1" in reason["message"]
    assert reason["waitedSeconds"] >= 600


def test_a_session_that_waited_with_everything_free_still_carries_a_reason():
    params = {"queue_reason": "product_capacity", "repo": "org/unregistered"}
    reasons = _list_with_queue([_make_record("q1", status="queued")],
                               entries={"q1": (600, params)})
    assert reasons["q1"]["code"] == "waiting_for_drain"


def test_a_freshly_queued_session_is_not_yet_a_stall():
    params = {"queue_reason": "product_capacity", "repo": "cairnea/KinDash"}
    reasons = _list_with_queue([_make_record("q1", status="queued")],
                               entries={"q1": (5, params)}, seed=_seed_full_product)
    assert reasons["q1"] is None


def test_a_queued_session_with_no_queue_entry_says_it_is_orphaned():
    """2026-10-06: ec4c174f read ``queued`` with ``queuePosition: null`` for 14 h —
    a row nothing would ever start, and no field said so."""
    from datetime import datetime, timedelta

    rec = _make_record("ghost", status="queued")
    rec.created_at = datetime.utcnow() - timedelta(hours=2)
    reasons = _list_with_queue([rec], entries={})
    assert reasons["ghost"]["code"] == "no_queue_entry"


# ---------------------------------------------------------------------------
# account_limited: every account that can run the model is at its ceiling.
# ---------------------------------------------------------------------------


def test_an_account_limited_session_names_the_ceiling_and_reset():
    from datetime import datetime

    from server.sessions.selection import AllAccountsBusy
    from server.time_iso import iso_utc

    params = {"queue_reason": "account_limited"}
    busy = AllAccountsBusy(datetime(2030, 1, 1, 10), "at_ceiling")
    with patch("server.api.sessions.select_account", side_effect=busy):
        reasons = _list_with_queue([_make_record("q1", status="queued")],
                                   entries={"q1": (120, params)})
    reason = reasons["q1"]
    assert reason["code"] == "account_limited"
    assert reason["earliestReset"] == iso_utc(datetime(2030, 1, 1, 10))
    assert "usage ceiling" in reason["message"]
    assert reason["waitedSeconds"] >= 120


def test_an_account_limited_session_with_headroom_is_waiting_for_drain():
    params = {"queue_reason": "account_limited"}
    with patch("server.api.sessions.select_account", return_value=object()):
        reasons = _list_with_queue([_make_record("q1", status="queued")],
                                   entries={"q1": (120, params)})
    assert reasons["q1"]["code"] == "waiting_for_drain"


def test_the_account_probe_runs_once_for_many_account_limited_sessions():
    from datetime import datetime

    from server.sessions.selection import AllAccountsBusy

    params = {"queue_reason": "account_limited"}
    busy = AllAccountsBusy(datetime(2030, 1, 1, 10), "at_ceiling")
    with patch("server.api.sessions.select_account", side_effect=busy) as probe:
        reasons = _list_with_queue(
            [_make_record(s, status="queued") for s in ("q1", "q2", "q3")],
            entries={s: (120, params) for s in ("q1", "q2", "q3")})
    assert {r["code"] for r in reasons.values()} == {"account_limited"}
    assert probe.call_count == 1


def test_grace_zero_reports_a_session_queued_a_second_ago():
    import asyncio
    from datetime import datetime, timedelta, timezone

    from server.api.sessions import _queue_stall_reasons
    from server.db import db_session
    from server.db.models import Session as DbSession, SessionQueue

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        client = _build_client(_FakeOrch(), db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            async def _run():
                async with db_session() as db:
                    db.add(DbSession(id="q1", name="q1", account_name="a", cwd="/tmp",
                                     status="queued"))
                    db.add(SessionQueue(
                        session_id="q1", priority=1, params={},
                        enqueued_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                rec = _make_record("q1", status="queued")
                return (await _queue_stall_reasons([rec], grace_s=0),
                        await _queue_stall_reasons([rec]))
            fast, default = (client.portal.call(_run) if getattr(client, "portal", None)
                             else asyncio.run(_run()))
        assert fast["q1"]["code"] == "queued"
        assert "q1" not in default
    finally:
        os.unlink(tf.name)


def test_just_queued_reason_never_claims_capacity_is_free():
    """grace 0 = "just queued": it was queued because something is full."""
    cases = {"global_cap": "slot", "host_capacity": "host"}
    for kind, word in cases.items():
        reasons = _list_with_queue_grace0(kind)
        assert reasons["code"] == "queued"
        assert "Capacity is free" not in reasons["message"]
        assert word in reasons["message"]


def test_just_queued_account_limited_whose_probe_races_to_headroom():
    """An account-limited create whose edge probe already sees headroom again is
    still described as account-limited — never with the global-cap copy (which
    offers a Start now that cannot override an account limit)."""
    from unittest.mock import MagicMock

    with patch("server.api.sessions.select_account", return_value=MagicMock()):
        reasons = _list_with_queue_grace0("account_limited")
    assert reasons["code"] == "queued"
    assert reasons["message"] == (
        "Every account that can run this model is at its usage ceiling; this "
        "session starts automatically when quota frees.")


def _list_with_queue_grace0(kind):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from server.api.sessions import _queue_stall_reasons
    from server.db import db_session
    from server.db.models import Session as DbSession, SessionQueue

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_url = f"sqlite+aiosqlite:///{tf.name}"
    try:
        client = _build_client(_FakeOrch(), db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            async def _run():
                async with db_session() as db:
                    db.add(DbSession(id="q1", name="q1", account_name="a", cwd="/tmp",
                                     status="queued"))
                    db.add(SessionQueue(
                        session_id="q1", priority=1, params={"queue_reason": kind},
                        enqueued_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                return await _queue_stall_reasons(
                    [_make_record("q1", status="queued")], grace_s=0)
            out = (client.portal.call(_run) if getattr(client, "portal", None)
                   else asyncio.run(_run()))
        return out["q1"]
    finally:
        os.unlink(tf.name)


def test_no_usable_accounts_says_reauth_not_usage_ceiling():
    from datetime import datetime

    from server.sessions.selection import AllAccountsBusy

    busy = AllAccountsBusy(datetime(2030, 1, 1, 10), "no_usable_accounts")
    with patch("server.api.sessions.select_account", side_effect=busy):
        reasons = _list_with_queue([_make_record("q1", status="queued")],
                                   entries={"q1": (120, {"queue_reason": "account_limited"})})
    assert reasons["q1"]["code"] == "account_limited"
    assert "re-authentication" in reasons["q1"]["message"]
    assert "usage ceiling" not in reasons["q1"]["message"]


def test_a_failing_account_probe_degrades_only_that_entry():
    al = {"queue_reason": "account_limited"}
    pc = {"queue_reason": "product_capacity", "repo": "org/unregistered"}
    with patch("server.api.sessions.select_account", side_effect=RuntimeError("boom")):
        reasons = _list_with_queue(
            [_make_record("q1", status="queued"), _make_record("q2", status="queued")],
            entries={"q1": (600, al), "q2": (600, pc)})
    assert reasons["q1"]["code"] == "waiting_for_drain"
    assert reasons["q2"]["code"] == "waiting_for_drain"
