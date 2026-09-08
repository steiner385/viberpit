from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

CFG = {"target_repo": "steiner385/viberpit", "min_count_error": 1, "min_count_warning": 3,
       "warning_window_h": 24, "daily_cap": 10, "comment_every_h": 24,
       "ignore_loggers": "uvicorn.error,websockets", "retention_days": 90}


def _row(**kw):
    base = dict(fingerprint="a1b2c3d4e5f60718", source="daemon", level="ERROR",
                logger="server.sessions.orchestrator", event="completion.question_hold",
                title="ERROR completion.question_hold: sid=<hex>",
                sample_message="completion.question_hold {\"sid\": \"abc\"}",
                sample_exc="Traceback…\nValueError: x <!-- sneaky -->",
                sample_context={"host": "dobby", "app_version": "4d2096a5",
                                "recent_sessions": ["s1", "s2"], "proc_epoch": "1-2"},
                count=14, count_since_filed=14, status="observed", issue_number=None,
                first_seen_at=datetime(2026, 9, 7, 9, 12), last_seen_at=datetime(2026, 9, 7, 13, 40),
                filed_at=None, last_commented_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_automation_contract():
    from server.automations.base import Automation, validate_config
    from server.automations.engine_policy import EnginePolicy
    from server.automations.error_triage import ErrorTriage
    a = ErrorTriage()
    assert isinstance(a, Automation)
    assert a.id == "error-triage" and a.modes == ("off", "file")
    assert a.engine_policy == EnginePolicy.CLAUDE_ONLY
    assert validate_config(a.config_schema, {}) == CFG
    assert a.status(None).level == "idle"
    ctx = SimpleNamespace(error_triage_counts={"candidates": 2, "filed_today": 1,
                                               "daily_cap": 10, "muted": 0, "filed": 3})
    st = a.status(ctx)
    assert st.level == "active" and "2 waiting" in st.text
    ctx.error_triage_counts.update(filed_today=10, candidates=4)
    assert a.status(ctx).level == "warn"


@pytest.mark.parametrize("level, n, want", [
    ("ERROR", 0, False), ("ERROR", 1, True), ("CRITICAL", 1, True),
    ("WARNING", 2, False), ("WARNING", 3, True),
])
def test_should_file(level, n, want):
    from server.automations.error_triage import should_file
    assert should_file(level=level, count_since_filed=n, config=CFG) is want


def test_is_ignored_prefix_match():
    from server.automations.error_triage import is_ignored
    assert is_ignored("uvicorn.error", CFG) and is_ignored("websockets.server", CFG)
    assert not is_ignored("server.sessions", CFG) and not is_ignored(None, CFG)
    assert not is_ignored("uvicorn.error", {**CFG, "ignore_loggers": ""})


def test_issue_title_and_labels():
    from server.automations.error_triage import LABEL, issue_title, source_label
    assert issue_title(_row(), regression=False) == \
        "[auto-error] ERROR completion.question_hold: sid=<hex>"
    assert issue_title(_row(), regression=True).startswith("[auto-error][regression] ")
    assert len(issue_title(_row(title="x" * 500), regression=False)) <= 120
    assert LABEL == "auto-error"
    assert source_label("client") == "source:client"
    assert source_label("edge") == source_label("daemon") == "source:server"


def test_issue_body_contents_and_safety():
    from server.automations.error_triage import issue_body
    from server.github.ci_issues import BODY_MAX_CHARS
    body = issue_body(_row(), regression_of=None, repo="steiner385/viberpit")
    assert body.startswith("**Filed by ViberPit Error Triage**")
    assert "| Level / source | ERROR / daemon |" in body
    assert "| Logger / event | server.sessions.orchestrator / completion.question_hold |" in body
    assert "14 since 2026-09-07 09:12 UTC" in body and "last 2026-09-07 13:40 UTC" in body
    assert "s1" in body and "s2" in body and "4d2096a5" in body
    assert "Fingerprint: `a1b2c3d4e5f60718`" in body
    assert "<!--" not in body and "sneaky" in body
    assert "Regression of" not in body
    reg = issue_body(_row(), regression_of=1234, repo="steiner385/viberpit")
    assert "| Regression of | #1234 |" in reg
    huge = issue_body(_row(sample_exc="y" * 20000), regression_of=None, repo="o/r")
    assert len(huge) <= BODY_MAX_CHARS


def test_recurrence_comment():
    from server.automations.error_triage import recurrence_comment
    text = recurrence_comment(_row(count_since_filed=5), since=datetime(2026, 9, 6, 8, 0))
    assert "5 more occurrence(s) since 2026-09-06 08:00 UTC" in text
    assert "last at 2026-09-07 13:40 UTC" in text
    assert "`a1b2c3d4e5f60718`" in text


def test_issue_body_worst_case_sample_stays_under_cap():
    from server.automations.error_triage import issue_body
    from server.github.ci_issues import BODY_MAX_CHARS

    # Extreme case 1: very long sample message
    body1 = issue_body(_row(sample_message="\n" * 4000, sample_exc="y" * 20000),
                       regression_of=None, repo="o/r")
    assert len(body1) <= BODY_MAX_CHARS
    assert body1.rstrip().endswith("Fingerprint: `a1b2c3d4e5f60718`")

    # Extreme case 2: long message with repeated lines
    body2 = issue_body(_row(sample_message="line\n" * 1000, sample_exc="z" * 15000),
                       regression_of=None, repo="o/r")
    assert len(body2) <= BODY_MAX_CHARS
    assert body2.rstrip().endswith("Fingerprint: `a1b2c3d4e5f60718`")
