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
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url,
                                     "VIBERPIT_DEV": "1", "VIBERPIT_TAILNET_ALLOWLIST": ""}):
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
        # The sessions list's foreground/background split reads this, published
        # rather than re-derived so the rule lives only in `work_class_of`.
        assert pd["workClass"] == "maintenance"
        br = next(a for a in r.json() if a["id"] == "backlog-runner")
        assert br["workClass"] == "development"


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
        assert "1 work in flight" in br["status"]["text"]


def test_list_carries_products_for_the_runner_and_null_for_pr_doctor():
    """``AutomationStatus.products`` is a NEW field on the status (``counts``
    stays int-only) — present as a list for an automation whose status reports
    per-product rows, ``None`` for one that has no concept of products at all.

    It is published exactly ONCE, inside ``status`` (R3-8): a sibling top-level
    key beside ``status`` is a second copy of one fact that can only ever drift
    or be read by the wrong half of the web."""
    from unittest.mock import AsyncMock

    from server.automations.backlog_runner import BacklogRunnerLoop

    with patch.object(BacklogRunnerLoop, "_stash_counts", new=AsyncMock()), _client() as c:
        orch = c.app.state.sessions._orch
        orch._backlog_ctx.backlog_counts = {"in_flight": 1, "eligible": 3, "needs_human": 0}
        orch._backlog_ctx.backlog_products = {
            "kwgt-cairn": {"dispatch": "auto", "eligible": 3, "blocked": 0,
                           "untriaged": 0, "in_flight": 1, "cap": 1, "bounced": 2},
        }

        automations = c.get("/api/automations", headers=H).json()
        br = next(a for a in automations if a["id"] == "backlog-runner")
        assert br["status"]["products"] == [
            {"slug": "kwgt-cairn", "dispatch": "auto", "inFlight": 1,
             "cap": 1, "eligible": 3, "bounced": 2, "held": 0,
             "blocked": 0, "skipped": {}}]
        pd = next(a for a in automations if a["id"] == "pr-doctor")
        assert pd["status"]["products"] is None
        # Published once — no sibling copy beside ``status``.
        assert "products" not in br and "products" not in pd


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


def test_a_pre_flip_runner_mode_reads_as_on_after_the_seed():
    """M4: the global vocabulary is ``off | on`` since policy phase 1, but a row
    written before the flip still STORED ``propose``/``auto``. Every acting read
    folds it, so the runner kept working — what did not fold was the row the
    operator sees here, which went on claiming a mode the UI no longer offers (and
    the mode select, hidden at two modes, gave them no way to correct it). The
    one-shot policy seed normalises the stored value as it consumes the row."""
    with _client() as c:
        async def _pre_flip_row():
            from sqlalchemy import select

            from server.db import db_session
            from server.db.models import Automation
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "backlog-runner"))).scalar_one()
                row.enabled, row.mode = True, "auto"
                row.config = {"enabled_repos": []}
        c.portal.call(_pre_flip_row)

        def _runner():
            return next(a for a in c.get("/api/automations", headers=H).json()
                        if a["id"] == "backlog-runner")
        # No "reads ``auto`` first" assertion: the app's live runner loop shares
        # this event loop and its first tick runs the same seed, so between the
        # row write and a read it may already have normalised the row (it did,
        # under a loaded parallel run). What is pinned is the outcome: after the
        # seed the row the operator sees says ``on`` and carries no retired key.
        orch = c.app.state.sessions._orch
        c.portal.call(orch._backlog_seed_policies_once)
        row = _runner()
        assert row["mode"] == "on"
        assert "enabled_repos" not in (row.get("config") or {})


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


def test_list_disconnected_telemetry_is_unknown_and_count_is_not_zero():
    from unittest.mock import AsyncMock
    with _client() as c:
        with patch.object(c.app.state.sessions, "automation_overview",
                          new=AsyncMock(side_effect=ConnectionError("private detail"))):
            response = c.get("/api/automations", headers=H)
        assert response.status_code == 200
        for automation in response.json():
            assert automation["status"]["level"] == "unknown"
            assert automation["actionableCount"] is None
            assert "private detail" not in automation["status"]["text"]
            assert "enabled" in automation and "mode" in automation


def test_list_uses_one_overview_for_counts_and_missing_entries_are_unknown():
    from unittest.mock import AsyncMock
    with _client() as c:
        overview = AsyncMock(return_value={
            "statuses": {"pr-doctor": {"text": "all PRs green", "level": "idle",
                                       "counts": {"failing": 0}, "products": None}},
            "actionableCounts": {"pr-doctor": 0, "backlog-runner": 4, "error-triage": 7},
            "maintenanceCounts": {"error-triage": 47},
        })
        with patch.object(c.app.state.sessions, "automation_overview", new=overview):
            response = c.get("/api/automations", headers=H)
        assert response.status_code == 200
        overview.assert_awaited_once()
        rows = {a["id"]: a for a in response.json()}
        assert rows["pr-doctor"]["status"]["level"] == "idle"
        assert rows["pr-doctor"]["actionableCount"] == 0
        assert rows["backlog-runner"]["actionableCount"] == 4
        assert rows["pm-sync"]["status"]["level"] == "unknown"
        assert rows["pm-sync"]["actionableCount"] is None
        # Decisions badge; housekeeping rides beside it and is None when the
        # daemon predates the split (2026-10-01 Delivery critique, P0).
        assert rows["error-triage"]["actionableCount"] == 7
        assert rows["error-triage"]["maintenanceCount"] == 47
        assert rows["pr-doctor"]["maintenanceCount"] is None


def test_list_suppresses_obsolete_waits_when_off_without_hiding_human_decisions():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.api.automations import list_automations

    async def check():
        # Disabling new automation work never resolves an existing human decision.
        cases = [
            (False, "auto", "schedule", 0, 0, False),
            (True, "off", "schedule", 1, 0, False),
            (False, "auto", "capacity", 1, 0, False),
            (False, "auto", "human", 0, 0, True),
            (True, "off", "human", 0, 0, True),
            (False, "auto", "human", 1, 0, True),
            (False, "auto", "human", 0, 2, True),
            (True, "auto", "schedule", 0, 0, True),
        ]
        for enabled, mode, reason, live, actionable, expected_wait in cases:
            observed = {"level": "error", "text": "Observed failure",
                        "counts": {"failing": 2}, "products": None,
                        "waitingReason": reason}
            row = SimpleNamespace(enabled=enabled, mode=mode, config={},
                                  schedule=None, last_active_at=None)
            overview = AsyncMock(return_value={"statuses": {"pr-doctor": observed},
                                               "actionableCounts": {"pr-doctor": actionable}})
            request = SimpleNamespace(app=SimpleNamespace(
                state=SimpleNamespace(sessions=SimpleNamespace(automation_overview=overview))))
            db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
                all=lambda: [("pr-doctor", live)])))
            with patch("server.api.automations._row", new=AsyncMock(return_value=row)):
                rows = await list_automations(request, _identity="test", db=db)
            automation = next(a for a in rows if a["id"] == "pr-doctor")
            assert ("waitingReason" in automation["status"]) is expected_wait
            assert observed["waitingReason"] == reason  # shared snapshot is untouched
            assert automation["status"]["counts"] == observed["counts"]
            assert automation["status"]["text"] == observed["text"]
            assert automation["status"]["level"] == "error"
            assert automation["activeSessions"] == live
            assert automation["actionableCount"] == actionable
    asyncio.run(check())


# --- enabled_repos is stored canonical, never as a rename alias -------------
# GitHub forwards a renamed repo forever, so an operator (or a config written
# before the rename) can persist a slug that silently becomes a SECOND identity
# for a repo already in the list. pr-doctor keys its jobs on "owner/repo#N", so
# the duplicate spawned two concurrent sessions on one PR. Canonicalize on the
# write path and the alias can never be stored in the first place.

from server.github import repo_canon  # noqa: E402  (module-level block above is fixtures)


def _fake_gh(mapping):
    """A ``gh`` stub answering the aliased canonical-name query it was given."""
    import json as _json
    import re as _re

    field = _re.compile(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')

    def run(args, **kw):
        data = {}
        for alias, owner, name in field.findall(kw.get("input") or ""):
            canonical = mapping.get(f"{owner}/{name}")
            data[alias] = {"nameWithOwner": canonical} if canonical else None

        class P:
            returncode = 0
            stdout = _json.dumps({"data": data})
            stderr = ""

        return P()

    return run


def test_patching_enabled_repos_stores_the_canonical_name_not_the_alias():
    gh = _fake_gh({"acme/old-name": "acme/new-name",
                   "acme/new-name": "acme/new-name",
                   "cairnea/KinDash": "cairnea/KinDash"})
    with _client() as c, patch.object(repo_canon.gitrepos, "_run_gh", gh):
        r = c.patch("/api/automations/pr-doctor", headers=H, json={"config": {
            "enabled_repos": ["acme/old-name", "cairnea/KinDash",
                              "acme/new-name"]}})
        assert r.status_code == 200
        assert r.json()["config"]["enabled_repos"] == [
            "acme/new-name", "cairnea/KinDash"]


def test_an_unreachable_github_still_persists_the_operators_repo_list():
    """Fail OPEN: canonicalization is a correction, not a gate. A config write
    that 500s (or silently drops repos) because GitHub blipped is far worse than
    an alias that gets corrected on the next write."""
    def boom(args, **kw):
        raise OSError("network down")

    repos = ["acme/old-name", "cairnea/KinDash"]
    with _client() as c, patch.object(repo_canon.gitrepos, "_run_gh", boom):
        r = c.patch("/api/automations/pr-doctor", headers=H,
                    json={"config": {"enabled_repos": list(repos)}})
        assert r.status_code == 200
        assert r.json()["config"]["enabled_repos"] == repos


def test_config_schema_publishes_the_editing_surface():
    """spec 2026-09-20 D7: a field owned by Delivery → Capacity says so on the wire,
    so the generic Config tab can render it read-only instead of as a second editor."""
    with _client() as c:
        rows = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        runner = {f["key"]: f for f in rows["backlog-runner"]["configSchema"]}
        assert runner["max_concurrent"]["surface"] == "capacity"
        assert runner["max_concurrent"]["label"] == "Runner ceiling"
        assert runner["max_spawns_per_day"]["surface"] == "capacity"
        assert runner["max_spawns_per_day"]["label"] == "Daily budget"
        assert runner["max_issues_per_pr"]["surface"] == "config"
        governor = {f["key"]: f for f in rows["capacity-governor"]["configSchema"]}
        assert {governor["push_level"]["surface"], governor["sessions_per_account"]["surface"]} == {"capacity"}


def test_patch_partial_config_keeps_the_other_stored_keys():
    """A partial config PATCH is a merge, not a reset: the push-level radio sends
    {push_level} alone and must not put sessions_per_account back to its default."""
    with _client() as c:
        r = c.patch("/api/automations/capacity-governor", headers=H,
                    json={"config": {"push_level": "steady", "sessions_per_account": 3}})
        assert r.status_code == 200, r.text
        r = c.patch("/api/automations/capacity-governor", headers=H, json={"config": {"push_level": "max"}})
        assert r.status_code == 200, r.text
        assert r.json()["config"] == {"push_level": "max", "sessions_per_account": 3,
                                       "hysteresis_ticks": 2}


def test_patch_runner_ceiling_above_the_global_cap_is_422():
    """spec 2026-09-20 §6: runner ceiling <= effective global cap. A fresh DB has no
    hosts and no attention ceiling, so the effective cap is the default 5."""
    with _client() as c:
        ok = c.patch("/api/automations/backlog-runner", headers=H, json={"config": {"max_concurrent": 5}})
        assert ok.status_code == 200, ok.text
        r = c.patch("/api/automations/backlog-runner", headers=H, json={"config": {"max_concurrent": 6}})
        assert r.status_code == 422, r.text
        assert "exceeds the effective global cap (5)" in r.json()["detail"]
        # Nothing was written by the refused request.
        rows = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        assert rows["backlog-runner"]["config"]["max_concurrent"] == 5


def test_patch_with_mode_and_an_over_cap_ceiling_leaves_the_mode_unchanged():
    """M10: a PATCH body can carry both `mode` and `config` in one request. When
    the config half is refused (over-cap ceiling) the whole PATCH must 422 —
    nothing partially applies, so the mode stays whatever it already was."""
    with _client() as c:
        ok = c.patch("/api/automations/backlog-runner", headers=H,
                     json={"mode": "on", "config": {"max_concurrent": 5}})
        assert ok.status_code == 200, ok.text

        r = c.patch("/api/automations/backlog-runner", headers=H,
                    json={"mode": "off", "config": {"max_concurrent": 6}})
        assert r.status_code == 422, r.text

        rows = {a["id"]: a for a in c.get("/api/automations", headers=H).json()}
        assert rows["backlog-runner"]["mode"] == "on"
        assert rows["backlog-runner"]["config"]["max_concurrent"] == 5


def test_patch_merge_ignores_an_unknown_key_stranded_on_the_stored_row():
    """Fix wave M4: an old row can carry a key its (narrowed) schema no longer
    knows — e.g. a field a later migration dropped without adding it to
    `retired_config_keys`. Merging that stale key back in for validation would
    422 every partial PATCH forever, since nothing ever clears it. The merge
    must drop unknown stored keys before validating.

    Startup's `ensure_rows()` already inserted this row (enabled=False, no
    config) before the request path runs, so the seed UPDATES the existing row
    rather than inserting a second one with the same primary key."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Automation as AutomationRow

    async def _seed():
        async with db_session() as db:
            row = (await db.execute(select(AutomationRow).where(
                AutomationRow.id == "capacity-governor"))).scalar_one()
            row.enabled = True
            row.config = {"push_level": "steady", "zombie": 1}

    with _client() as c:
        assert c.portal is not None
        c.portal.call(_seed)

        r = c.patch("/api/automations/capacity-governor", headers=H,
                    json={"config": {"push_level": "max"}})
        assert r.status_code == 200, r.text
        assert "zombie" not in r.json()["config"]
