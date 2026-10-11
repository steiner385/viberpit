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


def test_is_ignored_claude_agent_sdk_reader_noise():
    # #1886: the vendored SDK's own "Fatal error in message reader" ERROR is a
    # duplicate of the one record claude_driver already logs for the same dead
    # cursor (#1885) — ignored here so triage never files it as a second issue.
    from server.automations.error_triage import is_ignored
    assert is_ignored("claude_agent_sdk._internal.query", CFG)
    assert is_ignored("claude_agent_sdk._internal.query", {**CFG, "ignore_loggers": ""})


def test_a_persisted_pre_1886_ignore_list_still_ignores_claude_agent_sdk(tmp_path):
    # #2626: #2457 fixed #1886 by changing the ignore_loggers DEFAULT, but the live
    # row already persisted "uvicorn.error,websockets" — validate_config only fills
    # ABSENT keys, so the new default never reached is_ignored() and the vendor line
    # re-filed as a [regression]. Go through the real reader with that saved row.
    import asyncio

    from sqlalchemy import update as sa_update

    from server.automations.config_store import read_automation_config
    from server.automations.error_triage import ErrorTriage, is_ignored
    from server.automations.registry import registry
    from server.db import db_session, init_db
    from server.db.models import Automation

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'cfg.db'}")
        await registry.ensure_rows()
        async with db_session() as db:
            await db.execute(sa_update(Automation).where(Automation.id == "error-triage").values(
                enabled=True, mode="file", config={"ignore_loggers": "uvicorn.error,websockets"}))
        return await read_automation_config(ErrorTriage, modes=("file",))

    mode, config = asyncio.run(run())
    assert mode == "file" and config["ignore_loggers"] == "uvicorn.error,websockets"
    assert is_ignored("claude_agent_sdk._internal.query", config)
    assert is_ignored("uvicorn.error", config)
    assert not is_ignored("server.sessions.claude_driver", config)


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
    text = recurrence_comment([_row(count_since_filed=5)], since=datetime(2026, 9, 6, 8, 0))
    assert "5 more occurrence(s) since 2026-09-06 08:00 UTC" in text
    assert "last at 2026-09-07 13:40 UTC" in text
    assert "Fingerprint `a1b2c3d4e5f60718`." in text
    # Collapsed siblings on one issue: one comment, summed, each named (#2683).
    both = recurrence_comment([_row(count_since_filed=5),
                               _row(fingerprint="b2", count_since_filed=2,
                                    last_seen_at=datetime(2026, 9, 7, 14, 0))],
                              since=datetime(2026, 9, 6, 8, 0))
    assert "7 more occurrence(s)" in both and "last at 2026-09-07 14:00 UTC" in both
    assert "`a1b2c3d4e5f60718` (5)" in both and "`b2` (2)" in both


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


# -- duplicate collapse (#2683) -------------------------------------------------

def test_collapse_key_is_the_issue_title_minus_its_prefix():
    """One function renders both, so the dedupe key cannot drift from what a
    human reads: #2776/#2777/#2784/#2787 shared a title and were four issues."""
    from server.automations.error_triage import collapse_key, issue_title
    for title in ("ERROR server: unhandled exception: [errno <n>] connection refused",
                  "x" * 500):
        r = _row(title=title)
        assert issue_title(r, regression=False) == ("[auto-error] " + collapse_key(r))[:120]
        assert issue_title(r, regression=True) == \
            ("[auto-error][regression] " + collapse_key(r))[:120]
    assert collapse_key(_row(title=None, fingerprint="ffff")) == "ffff"
    # Differing frame/source (hence fingerprint) but one rendered title → one key.
    a = _row(fingerprint="a" * 16, source="edge", title="ERROR server: boom")
    b = _row(fingerprint="b" * 16, source="daemon", title="ERROR server: boom")
    assert collapse_key(a) == collapse_key(b)


def test_client_burst_window_and_client_id():
    from datetime import timedelta
    from server.automations.error_triage import CLIENT_BURST_WINDOW, collapses_into
    t0 = datetime(2026, 9, 7, 9, 0)
    lead = _row(source="client", title="ERROR client: failed to fetch", first_seen_at=t0,
                sample_context={"client_id": "tab-1"})
    near = _row(source="client", title="ERROR client: api error <n>",
                first_seen_at=t0 + CLIENT_BURST_WINDOW, sample_context={"client_id": "tab-1"})
    far = _row(source="client", title="ERROR client: api error <n>",
               first_seen_at=t0 + CLIENT_BURST_WINDOW + timedelta(seconds=1),
               sample_context={"client_id": "tab-1"})
    other_tab = _row(source="client", title="ERROR client: api error <n>",
                     first_seen_at=t0, sample_context={"client_id": "tab-2"})
    no_id = _row(source="client", title="ERROR client: api error <n>",
                 first_seen_at=t0, sample_context={})
    server = _row(source="edge", title="ERROR server: other", first_seen_at=t0)
    assert collapses_into(near, lead)
    assert not collapses_into(far, lead)
    assert not collapses_into(other_tab, lead)
    assert collapses_into(no_id, lead)          # no tab recorded → window alone
    assert not collapses_into(server, lead)     # the burst rule is client-only
    assert collapses_into(_row(title=lead.title, source="edge", first_seen_at=None), lead)


def test_issue_body_lists_collapsed_siblings():
    from server.automations.error_triage import issue_body
    sib = _row(fingerprint="feedfacecafebeef", title="ERROR client: failed to fetch <!-- x -->")
    body = issue_body(_row(), regression_of=None, repo="o/r", collapsed=[sib])
    assert "`feedfacecafebeef`" in body and "failed to fetch" in body
    assert "<!--" not in body
    assert body.rstrip().endswith("Fingerprint: `a1b2c3d4e5f60718`")
    assert "Collapsed" not in issue_body(_row(), regression_of=None, repo="o/r")


def test_collapse_comment_names_the_sibling_fingerprint():
    from server.automations.error_triage import collapse_comment
    text = collapse_comment(_row(count_since_filed=3))
    assert "`a1b2c3d4e5f60718`" in text and "collapsed" in text.lower()
    assert "3 occurrence(s)" in text
