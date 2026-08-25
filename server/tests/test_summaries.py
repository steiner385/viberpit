# server/tests/test_summaries.py
import asyncio

from server.sessions.summaries import (
    clamp_summary,
    should_regenerate,
    SummaryService,
)
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def test_clamp_summary_collapses_whitespace_and_strips_wrappers():
    assert clamp_summary('  "Refactoring   the\n auth"  ') == "Refactoring the auth"
    assert clamp_summary("```Running tests```") == "Running tests"


def test_clamp_summary_truncates_with_ellipsis():
    out = clamp_summary("x" * 250, limit=100)
    assert len(out) == 100
    assert out.endswith("…")


def test_should_regenerate_requires_viewer():
    assert should_regenerate(False, None, 10.0, 1000.0, "in_progress") is False


def test_should_regenerate_skips_terminal_status():
    assert should_regenerate(True, None, 10.0, 1000.0, "ended") is False


def test_should_regenerate_skips_limited_status():
    assert should_regenerate(True, None, 10.0, 1000.0, "limited") is False


def test_should_regenerate_first_time_when_viewed_and_active():
    assert should_regenerate(True, None, 10.0, 1000.0, "in_progress") is True


def test_should_regenerate_false_when_signature_unchanged():
    cached = (10.0, 990.0, "old")
    assert should_regenerate(True, cached, 10.0, 1000.0, "in_progress") is False


def test_should_regenerate_throttled_within_60s():
    cached = (10.0, 990.0, "old")  # changed signature below, but only 10s elapsed
    assert should_regenerate(True, cached, 11.0, 1000.0, "in_progress") is False


def test_should_regenerate_after_60s_and_change():
    cached = (10.0, 900.0, "old")  # 100s elapsed, signature changed
    assert should_regenerate(True, cached, 11.0, 1000.0, "in_progress") is True


def test_summary_service_empty_context_returns_empty():
    svc = SummaryService(sdk_factory=lambda cwd, cfg, account=None: None)
    assert asyncio.run(svc.generate("/x", "/c", "   ")) == ""


def test_summary_service_failure_returns_empty():
    def boom(cwd, cfg, account=None):
        raise RuntimeError("no sdk")
    svc = SummaryService(sdk_factory=boom)
    assert asyncio.run(svc.generate("/x", "/c", "User: hi\n\nAssistant: hey")) == ""


def test_generate_parses_streamed_text():
    msgs = [f.stream_start(uuid="t1"),
            f.stream_text("Refactoring the auth middleware", uuid="t1"),
            f.result(uuid="t1")]
    svc = SummaryService(lambda cwd, cfg, account=None: FakeSDKClient(msgs))
    out = asyncio.run(svc.generate("/tmp/r", "/tmp/cfg", "User: hi\n\nAssistant: done"))
    assert out == "Refactoring the auth middleware"
