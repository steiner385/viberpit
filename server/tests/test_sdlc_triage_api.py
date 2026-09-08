"""Control-band triage queue: store + `/api/sdlc/triage` routes (#804).

Covers the round trip the Maintain loop actually depends on — a breach is filed,
survives re-observation without duplicating or reopening, is dispositioned by a
human, and (once Intake adopts it) stops being re-drivable.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                # The REAL orchestrator, not `app.state.sessions`: the app wraps it
                # in an InProcessOrchestratorAdapter that publishes only the
                # cross-process facade, and the control-band seams are private
                # in-process methods the loop calls directly.
                c.orch = orch
                yield c


_BREACH = dict(
    key="pr_cycle_time_hours:we1:high", metric="ccswitch-pr-cycle-time",
    source="pr_cycle_time_hours", repo="steiner385/ccswitch", rule="we1", sigma=3,
    side="high", tier="propose", response="propose", value=50.0,
    baseline_mean=10.0, baseline_stddev=0.5, baseline_n=30,
    observed_at="2026-01-38", summary="cycle time above 3σ",
    evidence=[{"at": "2026-01-38", "value": 50.0}],
    slug="incident-cycle-time",
)


async def _file(**overrides):
    from server.sdlc import triage_store
    return await triage_store.upsert_breach(**{**_BREACH, **overrides})


# --------------------------------------------------------------------------- #
# Store semantics (driven through the API's DB, via the routes)                 #
# --------------------------------------------------------------------------- #

def test_a_filed_breach_appears_in_the_queue_with_its_evidence():
    with _client() as c:
        c.portal.call(_file)
        body = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS).json()
        (item,) = body["items"]
        assert item["key"] == "pr_cycle_time_hours:we1:high"
        assert item["status"] == "open"
        assert item["sigma"] == 3 and item["rule"] == "we1"
        assert item["tier"] == "propose" and item["response"] == "propose"
        assert item["baselineMean"] == 10.0 and item["baselineN"] == 30
        assert item["evidence"] == [{"at": "2026-01-38", "value": 50.0}]
        assert item["initiativeSlug"] == "incident-cycle-time"
        assert item["occurrences"] == 1


def test_re_observing_a_breach_updates_the_row_instead_of_duplicating_it():
    """A control-band breach persists for days; the monitor sees it every tick."""
    with _client() as c:
        first = c.portal.call(_file)
        assert first.created is True and first.status == "open"

        async def again():
            return await _file(value=60.0, summary="worse now")
        second = c.portal.call(again)
        assert second.created is False
        assert second.id == first.id
        # The status returned is the one the row had BEFORE this tick — that is
        # what decides whether the monitor may act on it.
        assert second.status == "open"

        (item,) = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS).json()["items"]
        assert item["value"] == 60.0 and item["summary"] == "worse now"
        assert item["occurrences"] == 2


def test_a_distinct_rule_or_side_files_its_own_incident():
    with _client() as c:
        c.portal.call(_file)

        async def other():
            return await _file(key="pr_cycle_time_hours:we2:low", rule="we2",
                               side="low", sigma=2, tier="diagnose",
                               response="diagnose")
        c.portal.call(other)
        items = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS).json()["items"]
        assert len(items) == 2


def test_status_filter_selects_a_subset_and_rejects_an_unknown_status():
    with _client() as c:
        c.portal.call(_file)
        empty = c.get("/api/sdlc/triage?status_filter=dismissed",
                      headers=IDENTITY_HEADERS).json()
        assert empty["items"] == []
        full = c.get("/api/sdlc/triage?status_filter=open,queued",
                     headers=IDENTITY_HEADERS).json()
        assert len(full["items"]) == 1
        bad = c.get("/api/sdlc/triage?status_filter=nonsense",
                    headers=IDENTITY_HEADERS)
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "unknown_status"


# --------------------------------------------------------------------------- #
# Human dispositions                                                            #
# --------------------------------------------------------------------------- #

def test_triage_now_queues_the_incident_for_intake():
    """The human tap and the automatic 3σ path converge on ONE status."""
    with _client() as c:
        filed = c.portal.call(_file)
        r = c.post(f"/api/sdlc/triage/{filed.id}/action",
                   json={"action": "triage_now"}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"

        from server.sdlc import triage_store
        assert c.portal.call(triage_store.count_queued) == 1


def test_dismiss_requires_a_reason_and_records_it():
    with _client() as c:
        filed = c.portal.call(_file)
        bad = c.post(f"/api/sdlc/triage/{filed.id}/action",
                     json={"action": "dismiss"}, headers=IDENTITY_HEADERS)
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "missing_reason"
        blank = c.post(f"/api/sdlc/triage/{filed.id}/action",
                       json={"action": "dismiss", "reason": "   "},
                       headers=IDENTITY_HEADERS)
        assert blank.status_code == 422

        ok = c.post(f"/api/sdlc/triage/{filed.id}/action",
                    json={"action": "dismiss", "reason": "seasonal, not a regression"},
                    headers=IDENTITY_HEADERS)
        assert ok.status_code == 200
        assert ok.json()["status"] == "dismissed"
        assert ok.json()["dismissedReason"] == "seasonal, not a regression"


def test_schedule_defers_the_incident_and_refuses_a_past_time():
    with _client() as c:
        filed = c.portal.call(_file)
        missing = c.post(f"/api/sdlc/triage/{filed.id}/action",
                         json={"action": "schedule"}, headers=IDENTITY_HEADERS)
        assert missing.status_code == 422
        assert missing.json()["detail"]["error"] == "missing_scheduled_for"

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        stale = c.post(f"/api/sdlc/triage/{filed.id}/action",
                       json={"action": "schedule", "scheduledFor": past},
                       headers=IDENTITY_HEADERS)
        assert stale.status_code == 422
        assert stale.json()["detail"]["error"] == "scheduled_in_the_past"

        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        ok = c.post(f"/api/sdlc/triage/{filed.id}/action",
                    json={"action": "schedule", "scheduledFor": soon.isoformat()},
                    headers=IDENTITY_HEADERS)
        assert ok.status_code == 200
        assert ok.json()["status"] == "scheduled"
        assert ok.json()["scheduledFor"] is not None


def test_release_due_returns_only_the_snoozes_that_expired():
    """Without this, 'schedule for later' is a black hole — the monitor never
    re-drives a dispositioned item."""
    from server.sdlc import triage_store
    with _client() as c:
        filed = c.portal.call(_file)
        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        c.post(f"/api/sdlc/triage/{filed.id}/action",
               json={"action": "schedule", "scheduledFor": soon.isoformat()},
               headers=IDENTITY_HEADERS)

        assert c.portal.call(triage_store.release_due) == 0

        async def release_later():
            return await triage_store.release_due(
                datetime.now(timezone.utc) + timedelta(hours=3))
        assert c.portal.call(release_later) == 1

        (item,) = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS).json()["items"]
        assert item["status"] == "open" and item["scheduledFor"] is None


def test_an_adopted_incident_refuses_every_further_disposition():
    """Once Intake owns it, it is an Initiative — the queue must not pretend
    otherwise."""
    from server.sdlc import triage_store
    with _client() as c:
        filed = c.portal.call(_file)
        c.post(f"/api/sdlc/triage/{filed.id}/action", json={"action": "triage_now"},
               headers=IDENTITY_HEADERS)

        async def adopt():
            await triage_store.mark_intake("incident-cycle-time", "sess-9")
        c.portal.call(adopt)

        (item,) = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS).json()["items"]
        assert item["status"] == "intake" and item["sessionId"] == "sess-9"

        for body in ({"action": "triage_now"},
                     {"action": "dismiss", "reason": "no"},
                     {"action": "schedule",
                      "scheduledFor": (datetime.now(timezone.utc)
                                       + timedelta(days=1)).isoformat()}):
            r = c.post(f"/api/sdlc/triage/{filed.id}/action", json=body,
                       headers=IDENTITY_HEADERS)
            assert r.status_code == 409, body
            assert r.json()["detail"]["error"] == "already_adopted"


def test_mark_intake_only_touches_queued_rows_for_that_slug():
    from server.sdlc import triage_store
    with _client() as c:
        filed = c.portal.call(_file)          # status "open", not queued

        async def adopt():
            await triage_store.mark_intake("incident-cycle-time")
        c.portal.call(adopt)

        (item,) = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS).json()["items"]
        assert item["status"] == "open", "an unqueued incident must not be adopted"
        assert item["id"] == filed.id


def test_unknown_action_and_unknown_item_are_rejected():
    with _client() as c:
        filed = c.portal.call(_file)
        bad = c.post(f"/api/sdlc/triage/{filed.id}/action",
                     json={"action": "delete-everything"}, headers=IDENTITY_HEADERS)
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "unknown_action"

        missing = c.post("/api/sdlc/triage/no-such-id/action",
                         json={"action": "triage_now"}, headers=IDENTITY_HEADERS)
        assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# The intake hand-off — where the loop closes                                   #
# --------------------------------------------------------------------------- #

def test_queued_incidents_become_intake_triggers_carrying_the_breach_evidence():
    with _client() as c:
        filed = c.portal.call(_file)
        c.post(f"/api/sdlc/triage/{filed.id}/action", json={"action": "triage_now"},
               headers=IDENTITY_HEADERS)

        orch = c.orch

        async def read():
            return await orch._sdlc_intake_read_triggers()
        (trigger,) = c.portal.call(read)

        assert trigger.slug == "incident-cycle-time"
        assert trigger.repo == "steiner385/ccswitch"
        assert trigger.origin == "control-band"
        # origin_ref stays None: InitiativeView.issue is built from it, and a
        # breach key is not an issue ref.
        assert trigger.origin_ref is None
        assert "CONTROL-BAND BREACH" in trigger.text
        assert "Rolling baseline" in trigger.text
        assert "evals/regressions/" in trigger.text


def test_an_unqueued_incident_is_not_handed_to_intake():
    with _client() as c:
        c.portal.call(_file)
        orch = c.orch

        async def read():
            return await orch._sdlc_intake_read_triggers()
        assert c.portal.call(read) == []


@pytest.mark.parametrize("source", ["pr_cycle_time_hours", "ci_failure_rate"])
def test_every_declared_metric_source_has_a_reader_that_returns_a_series(source):
    """The bands loader validates `source` against METRIC_SOURCES; this proves the
    other half of that pairing — that each name actually resolves to a reader."""
    from server.sdlc.bands import MetricBand
    with _client() as c:
        orch = c.orch
        band = MetricBand(id="x", source=source, repo="steiner385/ccswitch")

        async def read():
            return await orch._control_band_read_series(band)
        assert c.portal.call(read) == []   # empty DB → no samples, no crash
