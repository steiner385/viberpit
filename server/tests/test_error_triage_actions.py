from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import update

from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import ErrorSignature as ES
from server.tests.test_error_triage_loop import NOW, FakeGh, _loop, _row, _seed


def _run(coro_fn, tmp_path):
    async def go():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await coro_fn()
    asyncio.run(go())


def test_actionable_lists_candidates_filed_and_muted(tmp_path):
    async def body():
        await _seed(fingerprint="cand", level="WARNING", count_since_filed=3)
        await _seed(fingerprint="below", level="WARNING", count_since_filed=1)
        await _seed(fingerprint="filed", status="filed", issue_number=42, count_since_filed=0)
        await _seed(fingerprint="muted", status="muted", muted_by="operator", count_since_filed=0)
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        items = {it.id: it for it in await loop.actionable_items()}
        assert set(items) == {"cand", "filed", "muted"}
        assert {a.kind for a in items["cand"].actions} == {"file_now", "mute"}
        assert {a.kind for a in items["filed"].actions} == {"mute"}
        assert items["filed"].url == "https://github.com/o/r/issues/42"
        assert {a.kind for a in items["muted"].actions} == {"unmute"}
    _run(body, tmp_path)


def test_file_now_bypasses_threshold_and_cap(tmp_path):
    async def body():
        await _seed(fingerprint="w1", level="WARNING", count_since_filed=1)
        gh = FakeGh()
        loop = _loop(AutomationContext("error-triage"), gh, config={"daily_cap": 0})
        assert await loop.action("w1", "file_now") == "filed"
        row = await _row("w1")
        assert row.status == "filed" and row.issue_number == 42 and row.filed_at == NOW
        assert await loop.action("nope", "file_now") == "not_found"
    _run(body, tmp_path)


def test_file_now_reports_failure_and_stays_retryable(tmp_path):
    async def body():
        await _seed()
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(AutomationContext("error-triage"), gh)
        assert await loop.action("fp1", "file_now") == "failed"
        row = await _row("fp1")
        assert row.status == "filed" and row.issue_number is None
    _run(body, tmp_path)


def test_action_result_returns_the_filed_number(tmp_path):
    async def body():
        await _seed()
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        assert await loop.action_result("fp1", "file_now") == ("filed", 42)
        assert await loop.action_result("fp1", "mute") == ("muted", None)
    _run(body, tmp_path)


def test_mute_and_unmute(tmp_path):
    async def body():
        await _seed(fingerprint="a")
        await _seed(fingerprint="b", status="filed", issue_number=7, count_since_filed=0)
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        assert await loop.action("a", "mute") == "muted"
        row = await _row("a")
        assert row.status == "muted" and row.muted_by == "operator" and row.muted_at == NOW
        assert await loop.action("a", "unmute") == "unmuted"
        assert (await _row("a")).status == "observed"
        assert await loop.action("b", "mute") == "muted"
        assert await loop.action("b", "unmute") == "unmuted"
        assert (await _row("b")).status == "filed"  # still has an issue → back to filed
        assert await loop.action("a", "bogus") == "failed"
    _run(body, tmp_path)


def test_unmute_drops_the_mute_window_backlog(tmp_path):
    async def body():
        await _seed(fingerprint="a", status="filed", issue_number=7,
                    count_since_filed=0)
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        assert await loop.action("a", "mute") == "muted"
        # Activity while muted must not replay in full the moment it is unmuted
        # (#2104, sibling of #1878): the sink keeps incrementing count_since_filed
        # for muted rows even though no one is watching for it to file/comment.
        async with db_session() as db:
            await db.execute(update(ES).where(ES.fingerprint == "a")
                             .values(count_since_filed=50))
        assert await loop.action("a", "unmute") == "unmuted"
        row = await _row("a")
        assert row.status == "filed" and row.count_since_filed == 0
    _run(body, tmp_path)


def test_unmute_after_not_planned_close_goes_to_resolved_not_filed(tmp_path):
    async def body():
        # A github:not_planned mute leaves issue_number set on a CLOSED issue
        # (see _sync_issue_states). Unmuting must not land on "filed" — that
        # would post a recurrence comment onto an issue a human deliberately
        # closed as not planned. It must land on "resolved" so new occurrences
        # file a fresh regression instead (#2104, sibling of #1878).
        await _seed(fingerprint="np", status="muted", issue_number=42,
                    muted_by="github:not_planned", count_since_filed=99)
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        assert await loop.action("np", "unmute") == "unmuted"
        row = await _row("np")
        assert row.status == "resolved" and row.count_since_filed == 0
        assert row.muted_by is None and row.muted_at is None
    _run(body, tmp_path)


def test_file_now_when_off(tmp_path):
    async def body():
        await _seed()
        loop = _loop(AutomationContext("error-triage"), FakeGh(), mode="off")
        assert await loop.action("fp1", "file_now") == "off"
    _run(body, tmp_path)


def test_action_provider_maps_statuses(tmp_path):
    async def body():
        from server.automations.error_triage import ErrorTriageActionProvider
        await _seed()
        loop = _loop(AutomationContext("error-triage"), FakeGh())
        provider = ErrorTriageActionProvider(SimpleNamespace(_error_triage_loop=loop))
        assert provider.valid_kinds == {"file_now", "mute", "unmute"}
        items = await provider.actionable()
        assert items and items[0].id == "fp1"
        res = await provider.trigger("fp1", "file_now")
        assert res.status == "filed" and res.level == "ok" and "#42" in res.message
        res = await provider.trigger("zzz", "mute")
        assert res.status == "not_found" and res.level == "warn"
    _run(body, tmp_path)
