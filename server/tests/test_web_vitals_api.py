"""POST /api/web-vitals and GET /api/web-vitals/summary (#1411).

The privacy assertions here are the point of the feature as much as the p75s
are: the issue forbids collecting prompts, tokens or account names, and an
ingest that trusts the browser is not a promise, it is a hope.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.web_vitals import router
from server.auth.middleware import current_identity

CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setenv("VIBERPIT_DEV", "1")
    from server.api.ratelimit import _web_vitals_limiter
    _web_vitals_limiter._buckets.clear()
    monkeypatch.setattr(_web_vitals_limiter, "_now", lambda: 0.0)
    # The retention sweep is once-per-process global state; reset it so a test
    # that asserts on pruning is not silenced by an earlier test's sweep.
    import server.api.web_vitals as mod
    monkeypatch.setattr(mod, "_last_prune_at", 0.0)

    app = FastAPI()
    app.dependency_overrides[current_identity] = lambda: "tester@example.com"
    app.include_router(router)
    return TestClient(app)


def _batch(samples, **page):
    return {"samples": samples, "page": {"navType": "navigate", "appVersion": "abc1234",
                                         "clientId": "c-1", **page}}


def _post(client, samples, **page):
    return client.post("/api/web-vitals", json=_batch(samples, **page),
                       headers={"User-Agent": CHROME_UA})


class TestIngest:
    def test_accepts_and_stores_a_batch(self, client):
        r = _post(client, [
            {"metric": "LCP", "value": 1800.0, "route": "/sessions/:id", "device": "phone"},
            {"metric": "CLS", "value": 0.03, "route": "/sessions/:id", "device": "phone"},
        ])
        assert r.status_code == 202
        assert r.json()["accepted"] == 2

        s = client.get("/api/web-vitals/summary").json()
        assert s["sampleCount"] == 2
        assert s["overall"]["LCP"]["p75"] == 1800.0
        assert s["byDevice"]["phone"]["CLS"]["count"] == 1

    def test_derives_the_browser_family_from_the_request_header(self, client):
        _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "desktop"}])
        assert set(client.get("/api/web-vitals/summary").json()["byBrowser"]) == {"chrome"}

    def test_drops_an_unknown_metric_without_failing_the_batch(self, client):
        r = _post(client, [
            {"metric": "FID", "value": 10.0, "route": "/", "device": "desktop"},
            {"metric": "TTFB", "value": 120.0, "route": "/", "device": "desktop"},
        ])
        assert r.json()["accepted"] == 1

    @pytest.mark.parametrize("value", [-1.0, 10_000_000.0])
    def test_drops_an_impossible_value(self, client, value):
        assert _post(client, [
            {"metric": "LCP", "value": value, "route": "/", "device": "desktop"}]
        ).json()["accepted"] == 0

    def test_unknown_device_and_nav_type_collapse_to_a_fixed_vocabulary(self, client):
        _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "wristwatch"}],
              navType="teleport")
        s = client.get("/api/web-vitals/summary").json()
        assert set(s["byDevice"]) == {"unknown"}

    def test_batch_size_cap(self, client):
        many = [{"metric": "LCP", "value": 1.0, "route": "/", "device": "desktop"}] * 500
        assert _post(client, many).status_code == 413

    def test_requires_auth(self, isolated_db):
        app = FastAPI()
        app.include_router(router)  # no override → real auth
        assert TestClient(app).post("/api/web-vitals", json=_batch([])).status_code in (401, 403)

    def test_rate_limited_after_a_burst(self, client):
        last = None
        for _ in range(40):
            last = _post(client, [{"metric": "LCP", "value": 1.0, "route": "/",
                                   "device": "desktop"}])
        assert last is not None and last.status_code == 429
        assert int(last.headers["Retry-After"]) >= 1


class TestRouteScrubbing:
    """The stored `route` must be a shape. Nothing that reaches this column may
    identify a session, an account or a user's input."""

    def _stored_routes(self, client):
        return set(client.get("/api/web-vitals/summary").json()["byRoute"])

    def test_a_smuggled_uuid_is_redacted_server_side(self, client):
        _post(client, [{"metric": "LCP", "value": 900.0, "device": "desktop",
                        "route": "/sessions/8f14e45f-ceea-467a-9b2e-000000000000"}])
        assert self._stored_routes(client) == {"/sessions/:id"}

    def test_a_query_string_never_survives(self, client):
        _post(client, [{"metric": "LCP", "value": 900.0, "device": "desktop",
                        "route": "/sessions?prompt=my-secret-prompt&token=sk-ant-123"}])
        assert self._stored_routes(client) == {"/sessions"}

    def test_an_unparseable_route_collapses_rather_than_being_stored(self, client):
        _post(client, [{"metric": "LCP", "value": 900.0, "device": "desktop",
                        "route": "/x/" + "A" * 200}])
        assert self._stored_routes(client) == {"/other"}

    def test_depth_is_bounded(self, client):
        _post(client, [{"metric": "LCP", "value": 900.0, "device": "desktop",
                        "route": "/" + "/".join(f"seg{c}" for c in "abcdefghij")}])
        stored = next(iter(self._stored_routes(client)))
        assert stored.count("/") <= 6


class TestSummary:
    def _seed(self, client, metric, values, **kw):
        # Chunked to stay inside MAX_SAMPLES and the rate limiter's bucket.
        _post(client, [{"metric": metric, "value": v, "route": kw.get("route", "/sessions"),
                        "device": kw.get("device", "desktop")} for v in values])

    def test_p75_and_verdict_over_enough_samples(self, client):
        self._seed(client, "LCP", [1000.0] * 12)
        out = client.get("/api/web-vitals/summary").json()["overall"]["LCP"]
        assert out["p75"] == 1000.0 and out["meetsTarget"] is True

    def test_thin_data_carries_no_verdict(self, client):
        self._seed(client, "LCP", [1000.0] * 3)
        assert client.get("/api/web-vitals/summary").json()["overall"]["LCP"]["meetsTarget"] is None

    def test_route_filter_narrows_every_figure(self, client):
        self._seed(client, "LCP", [1000.0] * 12, route="/sessions")
        self._seed(client, "LCP", [6000.0] * 12, route="/roadmap")
        slow = client.get("/api/web-vitals/summary", params={"route": "/roadmap"}).json()
        assert slow["overall"]["LCP"]["p75"] == 6000.0
        assert slow["overall"]["LCP"]["meetsTarget"] is False
        assert set(slow["byRoute"]) == {"/roadmap"}

    def test_window_excludes_older_samples(self, client):
        self._seed(client, "LCP", [1000.0] * 3)
        import asyncio
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import update as sa_update

        from server.db import db_session
        from server.db.models import WebVitalSample

        async def age_them():
            async with db_session() as db:
                await db.execute(sa_update(WebVitalSample).values(
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(hours=48)))
                await db.commit()

        asyncio.run(age_them())
        assert client.get("/api/web-vitals/summary",
                          params={"hours": 24}).json()["sampleCount"] == 0
        assert client.get("/api/web-vitals/summary",
                          params={"hours": 72}).json()["sampleCount"] == 3

    def test_echoes_the_thresholds_the_verdicts_came_from(self, client):
        body = client.get("/api/web-vitals/summary").json()
        assert body["thresholds"]["LCP"] == {"good": 2500.0, "poor": 4000.0}
        assert body["coreMetrics"] == ["LCP", "INP", "CLS"]

    def test_rejects_an_out_of_range_window(self, client):
        assert client.get("/api/web-vitals/summary", params={"hours": 0}).status_code == 422


class TestKillSwitch:
    def test_disabled_ingest_stores_nothing(self, client, monkeypatch):
        monkeypatch.setenv("VIBERPIT_WEB_VITALS_ENABLED", "false")
        r = _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "desktop"}])
        assert r.status_code == 202
        assert r.json() == {"accepted": 0, "enabled": False}
        assert client.get("/api/web-vitals/summary").json()["sampleCount"] == 0


class TestRetention:
    def test_sweep_deletes_rows_past_the_retention_window(self, client, monkeypatch):
        import asyncio
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import update as sa_update

        from server.db import db_session
        from server.db.models import WebVitalSample

        _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "desktop"}])

        async def age_them():
            async with db_session() as db:
                await db.execute(sa_update(WebVitalSample).values(
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(days=99)))
                await db.commit()

        asyncio.run(age_them())
        # The next ingest sweeps: the guard is time-based, so reset it explicitly.
        import server.api.web_vitals as mod
        monkeypatch.setattr(mod, "_last_prune_at", 0.0)
        _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "desktop"}])

        body = client.get("/api/web-vitals/summary", params={"hours": 24 * 60}).json()
        assert body["sampleCount"] == 1  # the aged row is gone, the fresh one stays


class TestNavTypeVocabulary:
    """A bfcache restore is the fastest load a browser can produce; pooling it
    with a cold navigate flatters one number and punishes the other."""

    @pytest.mark.parametrize("nav", ["navigate", "reload", "back-forward",
                                     "back-forward-cache", "prerender", "restore"])
    def test_every_web_vitals_navigation_type_survives_ingest(self, client, nav):
        import asyncio

        from sqlalchemy import select as sa_select

        from server.db import db_session
        from server.db.models import WebVitalSample

        assert _post(client, [{"metric": "LCP", "value": 900.0, "route": "/",
                               "device": "desktop"}], navType=nav).json()["accepted"] == 1

        async def stored():
            async with db_session() as db:
                return (await db.execute(sa_select(WebVitalSample.nav_type))).scalars().all()

        assert asyncio.run(stored()) == [nav]


class TestSummaryBounding:
    def test_reports_whether_the_window_was_truncated(self, client, monkeypatch):
        """A p75 over a silently-truncated window looks authoritative and is
        about a different period than the one asked for."""
        import server.api.web_vitals as mod
        monkeypatch.setattr(mod, "MAX_SUMMARY_ROWS", 2)

        _post(client, [{"metric": "LCP", "value": float(v), "route": "/", "device": "desktop"}
                       for v in (100, 200, 300, 400)])
        body = client.get("/api/web-vitals/summary").json()
        assert body["sampleCount"] == 2
        assert body["truncated"] is True

    def test_an_untruncated_window_says_so(self, client):
        _post(client, [{"metric": "LCP", "value": 100.0, "route": "/", "device": "desktop"}])
        assert client.get("/api/web-vitals/summary").json()["truncated"] is False


class TestProvenanceFields:
    """`appVersion` and `clientId` are grouping keys, not free text. The endpoint
    is reachable by anything on the tailnet, so a value that cannot be a build
    sha or a uuid is dropped whole rather than stored."""

    def _stored(self, column):
        import asyncio

        from sqlalchemy import select as sa_select

        from server.db import db_session

        async def go():
            async with db_session() as db:
                return (await db.execute(sa_select(column))).scalars().all()

        return asyncio.run(go())

    def test_a_real_build_sha_and_client_id_are_kept(self, client):
        from server.db.models import WebVitalSample
        _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "desktop"}],
              appVersion="e4bdb25d", clientId="8f14e45f-ceea-467a-9b2e-000000000000")
        assert self._stored(WebVitalSample.app_version) == ["e4bdb25d"]
        assert self._stored(WebVitalSample.client_id) == ["8f14e45f-ceea-467a-9b2e-000000000000"]

    @pytest.mark.parametrize("junk", [
        "<script>alert(1)</script>",
        "sk-ant-api03-SECRET/leak",
        "a" * 200,
        "has spaces",
    ])
    def test_anything_else_is_dropped_rather_than_stored(self, client, junk):
        from server.db.models import WebVitalSample
        _post(client, [{"metric": "LCP", "value": 900.0, "route": "/", "device": "desktop"}],
              appVersion=junk, clientId=junk)
        assert self._stored(WebVitalSample.app_version) == [None]
        assert self._stored(WebVitalSample.client_id) == [None]
