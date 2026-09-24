"""Pure-function tests for the field Core Web Vitals summary (#1411).

No DB, no app: these pin the arithmetic and the thresholds the API route and
the docs both quote, so a threshold can only move in one place.
"""
from __future__ import annotations

import pytest

from server.obs.web_vitals import (
    CORE_WEB_VITALS,
    MIN_SAMPLES_FOR_VERDICT,
    WEB_VITAL_THRESHOLDS,
    Sample,
    browser_family,
    p75,
    rate,
    summarize,
    summarize_by,
)


class TestThresholds:
    def test_core_web_vitals_carry_the_targets_issue_1411_states(self):
        assert CORE_WEB_VITALS == ("LCP", "INP", "CLS")
        assert WEB_VITAL_THRESHOLDS["LCP"][0] == 2500.0
        assert WEB_VITAL_THRESHOLDS["INP"][0] == 200.0
        assert WEB_VITAL_THRESHOLDS["CLS"][0] == 0.1

    def test_every_metric_has_an_ordered_good_and_poor_bound(self):
        for metric, (good, poor) in WEB_VITAL_THRESHOLDS.items():
            assert good < poor, metric


class TestRate:
    @pytest.mark.parametrize(("value", "expected"), [
        (2499.0, "good"),
        (2500.0, "good"),          # the boundary is inclusive, as CrUX defines it
        (2500.1, "needs-improvement"),
        (4000.0, "needs-improvement"),
        (4000.1, "poor"),
    ])
    def test_lcp_bands(self, value, expected):
        assert rate("LCP", value) == expected

    def test_unknown_metric_is_unrated_rather_than_guessed(self):
        assert rate("FID", 10.0) is None


class TestP75:
    def test_nearest_rank_matches_crux(self):
        # 75% of 4 samples is the 3rd smallest.
        assert p75([100.0, 200.0, 300.0, 400.0]) == 300.0

    def test_single_sample(self):
        assert p75([42.0]) == 42.0

    def test_empty_is_none_not_zero(self):
        # A zero LCP would read as a perfect score; absence must stay absence.
        assert p75([]) is None

    def test_order_does_not_matter(self):
        assert p75([400.0, 100.0, 300.0, 200.0]) == 300.0


def _samples(metric: str, values: list[float], **kw) -> list[Sample]:
    return [Sample(metric=metric, value=v, route=kw.get("route", "/sessions"),
                   device=kw.get("device", "desktop"),
                   browser=kw.get("browser", "chrome")) for v in values]


class TestSummarize:
    def test_reports_p75_count_and_rating_per_metric(self):
        rows = _samples("LCP", [1000.0] * 12) + _samples("CLS", [0.02] * 12)
        out = summarize(rows)
        assert out["LCP"]["p75"] == 1000.0
        assert out["LCP"]["count"] == 12
        assert out["LCP"]["rating"] == "good"
        assert out["LCP"]["meetsTarget"] is True
        assert out["CLS"]["rating"] == "good"

    def test_a_failing_p75_is_reported_as_failing(self):
        out = summarize(_samples("LCP", [9000.0] * 12))
        assert out["LCP"]["rating"] == "poor"
        assert out["LCP"]["meetsTarget"] is False

    def test_thin_data_withholds_a_verdict_rather_than_asserting_one(self):
        out = summarize(_samples("LCP", [1000.0] * (MIN_SAMPLES_FOR_VERDICT - 1)))
        assert out["LCP"]["p75"] == 1000.0
        assert out["LCP"]["meetsTarget"] is None

    def test_metrics_with_no_samples_are_present_and_empty(self):
        out = summarize(_samples("LCP", [1000.0]))
        assert out["INP"]["count"] == 0
        assert out["INP"]["p75"] is None
        assert out["INP"]["meetsTarget"] is None

    def test_ignores_a_metric_it_does_not_know(self):
        out = summarize([Sample(metric="FID", value=1.0, route="/", device="phone",
                                browser="chrome")])
        assert "FID" not in out
        assert all(v["count"] == 0 for v in out.values())


class TestSummarizeBy:
    def test_segments_by_route(self):
        rows = (_samples("LCP", [1000.0] * 12, route="/sessions")
                + _samples("LCP", [5000.0] * 12, route="/roadmap"))
        out = summarize_by(rows, "route")
        assert out["/sessions"]["LCP"]["p75"] == 1000.0
        assert out["/roadmap"]["LCP"]["p75"] == 5000.0
        assert out["/roadmap"]["LCP"]["meetsTarget"] is False

    def test_segments_by_device_and_browser(self):
        rows = (_samples("INP", [100.0] * 12, device="phone")
                + _samples("INP", [400.0] * 12, device="desktop", browser="safari"))
        by_device = summarize_by(rows, "device")
        assert by_device["phone"]["INP"]["p75"] == 100.0
        assert by_device["desktop"]["INP"]["p75"] == 400.0
        by_browser = summarize_by(rows, "browser")
        assert set(by_browser) == {"chrome", "safari"}

    def test_rejects_a_dimension_that_is_not_a_segment(self):
        with pytest.raises(ValueError):
            summarize_by([], "value")


class TestBrowserFamily:
    @pytest.mark.parametrize(("ua", "expected"), [
        ("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15", "safari"),
        ("Mozilla/5.0 (X11; Linux) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "chrome"),
        ("Mozilla/5.0 (Windows) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 Edg/131.0", "edge"),
        ("Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/133.0", "firefox"),
        ("curl/8.5.0", "other"),
        ("", "unknown"),
        (None, "unknown"),
    ])
    def test_families(self, ua, expected):
        assert browser_family(ua) == expected

    def test_never_returns_the_raw_user_agent(self):
        # The UA string is a fingerprint; only the family is ever stored.
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Version/18.0 Mobile Safari/604.1"
        assert browser_family(ua) == "safari"
