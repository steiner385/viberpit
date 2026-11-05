"""GET /api/pm/projects/{slug}/milestones and POST …/milestones/refresh (spec
2026-11-05 §9–§10), plus the pure view-model builder (``server/pm/milestone_view.py``).

Rows are seeded directly, BEFORE the TestClient opens its own event loop (see
test_pm_map_api.py: seeding inside the open client races the app's loop).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import (
    Milestone,
    MilestoneLadder,
    MilestoneMeasurement,
    Project,
    ProjectRepo,
)
from server.pm.milestone_def import parse_ladder, to_json
from server.pm.milestone_view import build_milestones_view, leaf_state
from server.time_iso import aware_utc

FIXTURES = Path(__file__).parent / "fixtures" / "milestones"
PRIMARY = "cairnea/KinDash"
IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}
URL = "/api/pm/projects/kd/milestones"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _client(db_url: str, *, auto_refresh: bool | None = None):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url,
                                                    "VIBERPIT_DEV": "1",
                                                    "VIBERPIT_TAILNET_ALLOWLIST": ""}))
        if auto_refresh is not None:
            async def _enabled() -> bool:
                return auto_refresh
            stack.enter_context(patch("server.api.pm_milestones.automatic_refresh_enabled", _enabled))
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        yield stack.enter_context(TestClient(app, raise_server_exceptions=False))


def _leaf(n: int, state: str, prs: list[dict] | None = None) -> dict:
    return {"repo": PRIMARY, "number": n, "title": f"leaf {n}",
            "url": f"https://github.com/{PRIMARY}/issues/{n}", "state": state, "prs": prs or []}


LEAVES = [
    _leaf(1, "CLOSED"),
    _leaf(2, "MERGED"),
    _leaf(3, "OPEN", [{"number": 90, "url": f"https://github.com/{PRIMARY}/pull/90"}]),
    _leaf(4, "OPEN"),
    _leaf(5, "OPEN"),
]


async def _seed(*, ladder_text: str | None = None, parse_error: str | None = None,
                measurements: list[dict] | None = None, current: str = "household",
                remove: tuple[str, ...] = (), with_ladder: bool = True,
                refresh_requested_at: datetime | None = None,
                exit_overrides: dict[str, object] | None = None) -> str:
    text = ladder_text or (FIXTURES / "ladder.yaml").read_text()
    ladder = parse_ladder(text, primary_repo=PRIMARY)
    definition = to_json(ladder)
    async with db_session() as db:
        p = Project(slug="kd", name="kd")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=PRIMARY, is_primary=True))
        if not with_ladder:
            return p.id
        lad = MilestoneLadder(
            project_id=p.id, definition=definition, title=ladder.title, spec_path=ladder.spec,
            source_path="docs/product/milestones.yaml", source_sha="abc123",
            synced_at=_now(), parse_error=parse_error,
            parse_error_sha="def456" if parse_error else None,
            refresh_requested_at=refresh_requested_at)
        db.add(lad)
        await db.flush()
        for i, m in enumerate(definition["milestones"]):
            db.add(Milestone(
                ladder_id=lad.id, key=m["key"], ordinal=i, name=m["name"], label=m["label"],
                audience=m["audience"], platforms=m["platforms"], epics=m["epics"],
                refs=m["refs"], gate=m["gate"],
                state="current" if m["key"] == current else "upcoming",
                entered_at=_now() if m["key"] == current else None,
                removed_at=_now() if m["key"] in remove else None,
                exit_override=(exit_overrides or {}).get(m["key"])))
        for row in measurements or []:
            db.add(MilestoneMeasurement(ladder_id=lad.id, **row))
        await db.flush()
        return p.id


def _m(scope: str, key: str, status: str, value: dict | None, *, age_s: int = 60,
       error: str | None = None, fetched_age_s: int | None = None) -> dict:
    now = _now()
    return {"scope": scope, "key": key, "status": status, "value": value,
            "measured_at": now - timedelta(seconds=age_s),
            "fetched_at": now - timedelta(seconds=age_s if fetched_age_s is None else fetched_age_s),
            "error": error}


def _household_rows() -> list[dict]:
    """Built per test: the times are relative to now, so never at import."""
    return [
        _m("household", "lane-fixes", "met", {"done": 9, "total": 9}),
        _m("household", "nightly", "unmet", {"n": 1}),
        _m("household", "consent", "unmet", {"done": 1, "total": 3}),
        # rubric has no row -> unknown
        _m("leaves:household", "leaves", "info", {"done": 2, "total": 5, "items": LEAVES}),
        _m("numbers", "leaves", "info", {"done": 2, "total": 5}),
        _m("household", "gone", "met", {"n": 0}),  # no such condition -> ignored
    ]


def _get(isolated_db, **seed) -> dict:
    asyncio.run(_seed(**seed))
    with _client(isolated_db, auto_refresh=True) as c:
        r = c.get(URL, headers=IDENTITY_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- GET


def test_get_returns_null_ladder_for_product_without_one(isolated_db):
    assert _get(isolated_db, with_ladder=False) == {"ladder": None}


def test_404_unknown_product(isolated_db):
    with _client(isolated_db) as c:
        assert c.get("/api/pm/projects/nope/milestones", headers=IDENTITY_HEADERS).status_code == 404
        assert c.post("/api/pm/projects/nope/milestones/refresh",
                      headers=IDENTITY_HEADERS).status_code == 404


def test_get_shapes_kindash_fixture(isolated_db):
    body = _get(isolated_db, measurements=_household_rows())
    lad = body["ladder"]
    assert lad["title"] == "Launch readiness"
    assert lad["currentKey"] == "household"
    assert lad["currentRemoved"] is False
    assert lad["autoRefresh"] is True
    assert lad["specPath"].endswith("spec.md")
    assert lad["specUrl"] == f"https://github.com/{PRIMARY}/blob/HEAD/{lad['specPath']}"
    assert lad["sourceSha"] == "abc123" and lad["parseError"] is None
    assert lad["updatedAt"] is not None and lad["updatedAt"].endswith("+00:00")

    ms = body["milestones"]
    assert [m["key"] for m in ms] == ["household", "friendly", "community", "public"]
    h = ms[0]
    assert h["state"] == "current" and h["label"] == "Phase 0" and h["name"] == "Household gate"
    assert h["epics"] == [{"ref": f"{PRIMARY}#20191",
                           "url": f"https://github.com/{PRIMARY}/issues/20191"}]
    assert h["gateSummary"] == {"met": 1, "total": 4, "unknown": 1, "ready": False}
    gate = {g["key"]: g for g in h["gate"]}
    assert set(gate) == {"rubric", "lane-fixes", "nightly", "consent"}
    assert gate["rubric"]["status"] == "unknown" and gate["rubric"]["measuredAt"] is None
    assert gate["rubric"]["kind"] == "ci_metric"
    assert gate["rubric"]["op"] == "eq" and gate["rubric"]["target"] == 0
    assert gate["lane-fixes"]["status"] == "met"
    assert gate["lane-fixes"]["value"] == {"done": 9, "total": 9}
    assert 60 <= gate["lane-fixes"]["ageSeconds"] < 600  # wide: xdist load

    leaves = h["leaves"]
    assert (leaves["done"], leaves["inFlight"], leaves["notStarted"], leaves["total"]) == (2, 1, 2, 5)
    assert leaves["measuredAt"] is not None
    states = {i["ref"]: i["state"] for i in leaves["items"]}
    assert states[f"{PRIMARY}#3"] == "in_flight" and states[f"{PRIMARY}#1"] == "done"
    in_flight = next(i for i in leaves["items"] if i["state"] == "in_flight")
    assert in_flight["prs"] == [{"number": 90, "url": f"https://github.com/{PRIMARY}/pull/90"}]

    # a milestone with no leaves row: zero counts, no time
    assert ms[1]["leaves"] == {"done": 0, "inFlight": 0, "notStarted": 0, "total": 0,
                               "measuredAt": None, "items": []}
    plat = {p["name"]: p for p in ms[1]["platforms"]}
    assert plat["Kiosk"] == {"name": "Kiosk", "off": True, "note": "not yet"}
    assert [n["key"] for n in body["numbers"]] == ["rubric-gap", "leaves", "outside-families",
                                                     "defects-by-ci"]
    assert body["numbers"][1]["value"] == {"done": 2, "total": 5}


def test_stale_measurement_reported_unknown_with_age(isolated_db):
    rows = [_m("friendly", "p0", "met", {"n": 0}, age_s=2 * 3600, fetched_age_s=30)]
    body = _get(isolated_db, measurements=rows)
    friendly = next(m for m in body["milestones"] if m["key"] == "friendly")
    p0 = next(g for g in friendly["gate"] if g["key"] == "p0")
    assert p0["status"] == "unknown"
    assert 7200 <= p0["ageSeconds"] < 7800
    assert p0["measuredAt"] is not None
    assert friendly["gateSummary"]["met"] == 0


def test_parse_error_surfaces(isolated_db):
    body = _get(isolated_db, parse_error="milestones[0]: unknown key 'foo'")
    assert body["ladder"]["parseError"] == "milestones[0]: unknown key 'foo'"
    assert body["ladder"]["parseErrorSha"] == "def456"
    assert len(body["milestones"]) == 4  # the last good definition still renders


def test_removed_milestones_hidden_unless_current(isolated_db):
    body = _get(isolated_db, remove=("household", "public"))
    assert [m["key"] for m in body["milestones"]] == ["household", "friendly", "community"]
    assert body["ladder"]["currentRemoved"] is True


def test_exit_override_shape(isolated_db):
    """``exitOverride`` is ``{by, reason, at, unmet: [{key, status, value}]}`` or null —
    a malformed stored value reads null, never a raw blob."""
    stored = {"by": "owner@example.com", "reason": "accepted the risk", "at": "2026-10-20T00:00:00Z",
              "unmet": [{"key": "nightly", "status": "unmet", "value": {"n": 1}, "extra": 1}],
              "stray": True}
    body = _get(isolated_db, exit_overrides={"household": stored, "friendly": "just a string",
                                             "community": {"by": "x"}})
    by_key = {m["key"]: m for m in body["milestones"]}
    assert by_key["household"]["exitOverride"] == {
        "by": "owner@example.com", "reason": "accepted the risk", "at": "2026-10-20T00:00:00Z",
        "unmet": [{"key": "nightly", "status": "unmet", "value": {"n": 1}}]}
    assert by_key["friendly"]["exitOverride"] is None
    assert by_key["community"]["exitOverride"] is None
    assert by_key["public"]["exitOverride"] is None


# ---------------------------------------------------------------- refresh


async def _requested_at(db_url: str) -> datetime | None:
    from server.db import init_db

    await init_db(db_url)  # the app's shutdown disposed the engine
    async with db_session() as db:
        return (await db.execute(select(MilestoneLadder.refresh_requested_at))).scalar_one()


def test_refresh_sets_request_flag_and_rate_limits(isolated_db):
    asyncio.run(_seed())
    with _client(isolated_db, auto_refresh=True) as c:
        r = c.post(f"{URL}/refresh", headers=IDENTITY_HEADERS)
        assert r.status_code == 202, r.text
        assert r.json() == {"refreshed": True}
        again = c.post(f"{URL}/refresh", headers=IDENTITY_HEADERS)
        assert again.status_code == 429
        assert again.json()["refreshed"] is False
        assert 1 <= again.json()["retryAfter"] <= 60
    stamped = aware_utc(asyncio.run(_requested_at(isolated_db)))
    assert stamped is not None and abs((_now() - stamped).total_seconds()) < 60


def test_refresh_rate_limited_by_recent_fetch(isolated_db):
    asyncio.run(_seed(measurements=[_m("household", "nightly", "unmet", {"n": 1}, age_s=5)]))
    with _client(isolated_db, auto_refresh=True) as c:
        r = c.post(f"{URL}/refresh", headers=IDENTITY_HEADERS)
    assert r.status_code == 429
    assert 1 <= r.json()["retryAfter"] <= 55
    assert asyncio.run(_requested_at(isolated_db)) is None


def test_refresh_allowed_after_window(isolated_db):
    asyncio.run(_seed(measurements=[_m("household", "nightly", "unmet", {"n": 1}, age_s=600)],
                      refresh_requested_at=_now() - timedelta(seconds=900)))
    with _client(isolated_db, auto_refresh=True) as c:
        r = c.post(f"{URL}/refresh", headers=IDENTITY_HEADERS)
    assert r.status_code == 202, r.text


def test_refresh_refused_when_automatic_refresh_off(isolated_db):
    asyncio.run(_seed())
    # No automation row: the Milestones Refresh automation is off by default.
    with _client(isolated_db) as c:
        r = c.post(f"{URL}/refresh", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json() == {"refreshed": False, "reason": "automatic_refresh_off"}
        assert c.get(URL, headers=IDENTITY_HEADERS).json()["ladder"]["autoRefresh"] is False
    assert asyncio.run(_requested_at(isolated_db)) is None


# ---------------------------------------------------------------- pure builder


@pytest.mark.parametrize(("state", "prs", "expected"), [
    ("CLOSED", [], "done"),
    ("MERGED", [], "done"),
    ("CLOSED", [{"number": 1, "url": "u"}], "done"),
    ("OPEN", [{"number": 1, "url": "u"}], "in_flight"),
    ("OPEN", [], "not_started"),
    (None, [], "not_started"),
])
def test_leaf_state(state, prs, expected):
    assert leaf_state({"state": state, "prs": prs}) == expected


def test_builder_ignores_orphan_rows_and_null_definition():
    now = _now()
    ladder = SimpleNamespace(
        title="t", spec_path=None, source_path=None, source_sha=None, synced_at=None,
        parse_error="bad", parse_error_sha="s", definition=None)
    view = build_milestones_view(
        ladder, [], [SimpleNamespace(scope="x", key="y", status="met", value=None, measured_at=now,
                                     fetched_at=now, error=None, source_url=None, manual_by=None,
                                     manual_evidence=None)],
        now=now, primary_repo=PRIMARY, auto_refresh=False)
    assert view["milestones"] == [] and view["numbers"] == []
    assert view["ladder"]["specUrl"] is None and view["ladder"]["currentKey"] is None
    # updatedAt is the newest fetched_at across the ladder, orphan rows included
    assert view["ladder"]["updatedAt"] is not None
