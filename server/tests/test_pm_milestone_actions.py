"""The milestone write rules (``server/pm/milestone_actions.py``, spec 2026-11-05 M4,
M8–M10, §7): manual ticks with evidence, the status line, decisions/asks, and advance
with the owner-only override. Ladders are seeded with the Phase 1 API tests' helper."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import AuditEvent, Milestone, MilestoneLadder, MilestoneMeasurement
from server.pm.milestone_actions import (
    Actor,
    MilestoneActionError,
    add_note,
    advance,
    set_status_note,
    tick_condition,
    update_note,
)
from server.tests.test_api_pm_milestones import _m, _seed
from server.time_iso import aware_utc

OWNER = Actor("owner@example.com", "api")
OTHER = Actor("other@example.com", "api")
SESSION = Actor("owner@example.com", "session")
ASSISTANT = Actor("owner@example.com", "assistant")
AGENT = Actor("session:abc", "api")
SYSTEM = Actor("automation:x", "api")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _met(scope: str, key: str, value: dict | None = None) -> dict:
    return _m(scope, key, "met", value if value is not None else {"met": True})


def _household_met() -> list[dict]:
    return [_met("household", k, {"n": 0}) for k in ("rubric", "lane-fixes", "nightly", "consent")]


async def _ladder(db) -> MilestoneLadder:
    return (await db.execute(select(MilestoneLadder))).scalar_one()


async def _rows(db) -> dict[str, Milestone]:
    return {m.key: m for m in (await db.execute(select(Milestone))).scalars().all()}


async def _mark_done(*keys: str) -> None:
    """Rungs before the current one are ``done`` on a real ladder (``_seed`` leaves
    every non-current rung ``upcoming``)."""
    async with db_session() as db:
        rows = await _rows(db)
        for k in keys:
            rows[k].state = "done"
        await db.commit()


async def _err(coro) -> MilestoneActionError:
    with pytest.raises(MilestoneActionError) as exc:
        await coro
    return exc.value


# ---------------------------------------------------------------- ticks


@pytest.mark.asyncio
async def test_tick_manual_condition_sets_met_with_evidence(isolated_db):
    await _seed()
    async with db_session() as db:
        row = await tick_condition(db, await _ladder(db), "friendly", "setup", actor=OWNER,
                                   met=True, evidence="  all five families set up  ",
                                   url="https://example.com/e")
        await db.commit()
    async with db_session() as db:
        stored = (await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.id == row.id))).scalar_one()
    assert (stored.scope, stored.key, stored.status) == ("friendly", "setup", "met")
    assert stored.value == {"met": True}
    assert stored.manual_by == "owner@example.com"
    assert stored.manual_evidence == "all five families set up"
    assert stored.manual_url == "https://example.com/e"
    measured = aware_utc(stored.measured_at)
    assert measured is not None and abs((_now() - measured).total_seconds()) < 60
    assert aware_utc(stored.fetched_at) == measured


@pytest.mark.asyncio
async def test_tick_rejects_computed_condition(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        e = await _err(tick_condition(db, lad, "friendly", "p0", actor=OWNER, met=True,
                                      evidence="x", url=None))
        assert (e.status, e.code) == (400, "not_manual")
        e = await _err(tick_condition(db, lad, "friendly", "nope", actor=OWNER, met=True,
                                      evidence="x", url=None))
        assert (e.status, e.code) == (404, "unknown_condition")
        e = await _err(tick_condition(db, lad, "friendly", "setup", actor=OWNER, met=True,
                                      value=3, evidence="x", url=None))
        assert (e.status, e.code) == (400, "bad_tick")
        e = await _err(tick_condition(db, lad, "friendly", "setup", actor=OWNER, value=3,
                                      evidence="x", url=None))
        assert (e.status, e.code) == (400, "bad_tick")
        e = await _err(tick_condition(db, lad, "friendly", "setup", actor=AGENT, met=True,
                                      evidence="x", url=None))
        assert (e.status, e.code) == (403, "person_required")


@pytest.mark.asyncio
async def test_tick_requires_evidence(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(tick_condition(db, await _ladder(db), "friendly", "setup", actor=OWNER,
                                      met=True, evidence="   ", url=None))
    assert (e.status, e.code) == (400, "evidence_required")


@pytest.mark.asyncio
async def test_tick_numbers_value(isolated_db):
    await _seed()
    async with db_session() as db:
        row = await tick_condition(db, await _ladder(db), "numbers", "outside-families",
                                   actor=SESSION, value=4, evidence="count from billing", url=None)
        assert (row.scope, row.key, row.value, row.status) == ("numbers", "outside-families",
                                                                {"n": 4}, "info")
        e = await _err(tick_condition(db, await _ladder(db), "numbers", "outside-families",
                                      actor=OWNER, value=float("nan"), evidence="x", url=None))
        assert (e.status, e.code) == (400, "bad_tick")


@pytest.mark.asyncio
async def test_retick_of_anothers_tick_needs_owner(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        await tick_condition(db, lad, "friendly", "setup", actor=OTHER, met=True,
                             evidence="done", url=None)
        # the same person may change their own tick from a session
        await tick_condition(db, lad, "friendly", "week-two", actor=SESSION, met=True,
                             evidence="ok", url=None)
        await tick_condition(db, lad, "friendly", "week-two", actor=SESSION, met=False,
                             evidence="not yet", url=None)
        # an owner (direct person request) may change another's
        row = await tick_condition(db, lad, "friendly", "setup", actor=OWNER, met=False,
                                   evidence="one family stuck", url=None)
        assert row.manual_by == "owner@example.com" and row.status == "unmet"
        count = len((await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.scope == "friendly"))).scalars().all())
        assert count == 2  # upserted, never duplicated


@pytest.mark.asyncio
async def test_session_actor_cannot_retick_anothers(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        await tick_condition(db, lad, "friendly", "setup", actor=OTHER, met=True,
                             evidence="done", url=None)
        e = await _err(tick_condition(db, lad, "friendly", "setup", actor=SESSION, met=False,
                                      evidence="no", url=None))
    assert (e.status, e.code) == (403, "owner_required")


# ---------------------------------------------------------------- status line


@pytest.mark.asyncio
async def test_status_note_set_and_clear(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        m = await set_status_note(db, lad, "household", actor=SESSION, text="  waiting on DPAs ")
        assert m.status_note == "waiting on DPAs"
        e = await _err(set_status_note(db, lad, "household", actor=OWNER, text="x" * 201))
        assert (e.status, e.code) == (400, "too_long")
        e = await _err(set_status_note(db, lad, "household", actor=SYSTEM, text="hi"))
        assert (e.status, e.code) == (403, "person_or_session_required")
        e = await _err(set_status_note(db, lad, "nope", actor=OWNER, text="hi"))
        assert (e.status, e.code) == (404, "unknown_milestone")
        m = await set_status_note(db, lad, "household", actor=OWNER, text="")
        assert m.status_note is None
        m = await set_status_note(db, lad, "household", actor=OWNER, text="x" * 200)
        await set_status_note(db, lad, "household", actor=OWNER, text=None)
        assert m.status_note is None


# ---------------------------------------------------------------- notes


@pytest.mark.asyncio
async def test_add_decision_defaults_locked(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=SESSION, kind="decision", text=" Founder pricing ",
                           ref="D4", milestone_key="community")
        assert (n.kind, n.status, n.text, n.ref, n.milestone_key) == (
            "decision", "locked", "Founder pricing", "D4", "community")
        assert n.created_by == "owner@example.com" and n.ladder_id == lad.id
        e = await _err(add_note(db, lad, actor=OWNER, kind="memo", text="x"))
        assert (e.status, e.code) == (400, "bad_kind")
        e = await _err(add_note(db, lad, actor=OWNER, kind="decision", text="  "))
        assert (e.status, e.code) == (400, "text_required")
        e = await _err(add_note(db, lad, actor=OWNER, kind="decision", text="x" * 501))
        assert (e.status, e.code) == (400, "too_long")
        e = await _err(add_note(db, lad, actor=OWNER, kind="decision", text="x", status="open"))
        assert (e.status, e.code) == (400, "bad_status")
        e = await _err(add_note(db, lad, actor=OWNER, kind="decision", text="x",
                                milestone_key="nope"))
        assert (e.status, e.code) == (404, "unknown_milestone")
        e = await _err(add_note(db, lad, actor=SYSTEM, kind="decision", text="x"))
        assert (e.status, e.code) == (403, "person_or_session_required")


@pytest.mark.asyncio
async def test_add_ask_defaults_open(isolated_db):
    await _seed(remove=("friendly",))
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=OWNER, kind="ask", text="Sign the DPA")
        assert (n.status, n.milestone_key, n.resolved_by, n.resolved_at) == ("open", None, None, None)
        e = await _err(add_note(db, lad, actor=OWNER, kind="ask", text="x", milestone_key="friendly"))
        assert (e.status, e.code) == (404, "unknown_milestone")  # removed rung


@pytest.mark.asyncio
async def test_resolve_ask_sets_resolved_fields(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=SESSION, kind="ask", text="Sign the DPA")
        n = await update_note(db, lad, n.id, actor=SESSION, status="done")
        assert n.status == "done" and n.resolved_by == "owner@example.com"
        resolved = aware_utc(n.resolved_at)
        assert resolved is not None and abs((_now() - resolved).total_seconds()) < 60
        e = await _err(update_note(db, lad, n.id, actor=OWNER, status="locked"))
        assert (e.status, e.code) == (400, "bad_status")
        e = await _err(update_note(db, lad, "missing", actor=OWNER, status="done"))
        assert (e.status, e.code) == (404, "unknown_note")


@pytest.mark.asyncio
async def test_reopen_clears_resolved(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=OWNER, kind="ask", text="Sign the DPA")
        await update_note(db, lad, n.id, actor=OWNER, status="dropped")
        n = await update_note(db, lad, n.id, actor=OWNER, status="open", text="Sign both DPAs")
        assert (n.status, n.text, n.resolved_by, n.resolved_at) == ("open", "Sign both DPAs",
                                                                    None, None)


@pytest.mark.asyncio
async def test_resolve_anothers_ask_needs_owner(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=OTHER, kind="ask", text="Sign the DPA")
        e = await _err(update_note(db, lad, n.id, actor=SESSION, status="done"))
        assert (e.status, e.code) == (403, "owner_required")
        e = await _err(update_note(db, lad, n.id, actor=ASSISTANT, text="edited"))
        assert (e.status, e.code) == (403, "owner_required")
        n = await update_note(db, lad, n.id, actor=OWNER, status="done")
        assert n.resolved_by == "owner@example.com"


# ---------------------------------------------------------------- advance


@pytest.mark.asyncio
async def test_advance_when_gate_met(isolated_db):
    await _seed(measurements=_household_met())
    now = _now()
    async with db_session() as db:
        res = await advance(db, await _ladder(db), actor=SESSION, expected_current="household",
                            override_reason=None, now=now)
        await db.commit()
    assert (res.from_key, res.to_key, res.override) == ("household", "friendly", False)
    async with db_session() as db:
        rows = await _rows(db)
    assert rows["household"].state == "done"
    assert aware_utc(rows["household"].exited_at) == now
    assert rows["household"].exit_override is None
    assert rows["friendly"].state == "current"
    assert aware_utc(rows["friendly"].entered_at) == now


@pytest.mark.asyncio
async def test_advance_unmet_without_reason_409_lists_unmet(isolated_db):
    await _seed(measurements=[_met("household", "lane-fixes", {"n": 0}),
                              _met("household", "consent", {"n": 0}),
                              _m("household", "nightly", "unmet", {"n": 1})])  # rubric: no row
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                               override_reason=None, now=_now()))
    assert (e.status, e.code) == (409, "gate_unmet")
    unmet = {u["key"]: u for u in e.detail["unmet"]}
    assert set(unmet) == {"rubric", "nightly"}
    assert unmet["nightly"] == {"key": "nightly", "status": "unmet", "value": {"n": 1}}
    assert unmet["rubric"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_advance_override_by_owner_records_snapshot(isolated_db):
    await _seed(measurements=[_m("household", "nightly", "unmet", {"n": 1})])
    now = _now()
    async with db_session() as db:
        res = await advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                            override_reason="  risk accepted by owner  ", now=now)
        await db.commit()
    assert (res.from_key, res.to_key, res.override) == ("household", "friendly", True)
    async with db_session() as db:
        h = (await _rows(db))["household"]
    assert h.state == "done"
    ov = h.exit_override
    assert ov is not None
    assert (ov["by"], ov["reason"]) == ("owner@example.com", "risk accepted by owner")
    assert aware_utc(datetime.fromisoformat(ov["at"])) == now
    assert {u["key"] for u in ov["unmet"]} == {"rubric", "lane-fixes", "nightly", "consent"}
    nightly = next(u for u in ov["unmet"] if u["key"] == "nightly")
    assert nightly == {"key": "nightly", "status": "unmet", "value": {"n": 1}}


@pytest.mark.asyncio
async def test_override_from_session_403(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=SESSION, expected_current="household",
                               override_reason="session says ship it", now=_now()))
    assert (e.status, e.code) == (403, "override_requires_owner")


@pytest.mark.asyncio
async def test_override_from_assistant_403(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=ASSISTANT,
                               expected_current="household",
                               override_reason="assistant says ship it", now=_now()))
    assert (e.status, e.code) == (403, "override_requires_owner")


@pytest.mark.asyncio
async def test_override_reason_too_short(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                               override_reason="  too short  ", now=_now()))
    assert (e.status, e.code) == (400, "reason_too_short")


@pytest.mark.asyncio
async def test_override_not_needed_when_met(isolated_db):
    await _seed(measurements=_household_met())
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                               override_reason="a perfectly long reason", now=_now()))
    assert (e.status, e.code) == (400, "override_not_needed")


@pytest.mark.asyncio
async def test_stale_expected_current_409(isolated_db):
    """Two people advance at once: the second carries the old current key."""
    await _seed(measurements=_household_met())
    async with db_session() as db:
        await advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                      override_reason=None, now=_now())
        await db.commit()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=OTHER, expected_current="household",
                               override_reason=None, now=_now()))
        assert (e.status, e.code, e.detail) == (409, "stale_current", {"current": "friendly"})
        assert (await _rows(db))["friendly"].state == "current"


@pytest.mark.asyncio
async def test_advance_skips_removed_next(isolated_db):
    await _seed(measurements=_household_met(), remove=("friendly",))
    async with db_session() as db:
        res = await advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                            override_reason=None, now=_now())
        rows = await _rows(db)
    assert res.to_key == "community"
    assert rows["friendly"].state == "upcoming" and rows["community"].state == "current"


@pytest.mark.asyncio
async def test_advance_last_completes_ladder(isolated_db):
    await _seed(current="public", measurements=[
        _met("public", k) for k in ("phase2-readout", "launch-day", "daily-watch")])
    await _mark_done("household", "friendly", "community")
    async with db_session() as db:
        res = await advance(db, await _ladder(db), actor=OWNER, expected_current="public",
                            override_reason=None, now=_now())
        rows = await _rows(db)
    assert (res.from_key, res.to_key, res.override) == ("public", None, False)
    assert rows["public"].state == "done"
    assert not any(m.state == "current" for m in rows.values())


@pytest.mark.asyncio
async def test_advance_after_complete_409(isolated_db):
    await _seed(current="public", measurements=[
        _met("public", k) for k in ("phase2-readout", "launch-day", "daily-watch")])
    await _mark_done("household", "friendly", "community")
    async with db_session() as db:
        await advance(db, await _ladder(db), actor=OWNER, expected_current="public",
                      override_reason=None, now=_now())
        await db.commit()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=OWNER, expected_current=None,
                               override_reason=None, now=_now()))
    assert (e.status, e.code) == (409, "ladder_complete")


@pytest.mark.asyncio
async def test_start_when_none_current(isolated_db):
    await _seed(current="", remove=("household",))
    now = _now()
    async with db_session() as db:
        lad = await _ladder(db)
        e = await _err(advance(db, lad, actor=OWNER, expected_current="household",
                               override_reason=None, now=now))
        assert (e.code, e.detail) == ("stale_current", {"current": None})
        e = await _err(advance(db, lad, actor=AGENT, expected_current=None,
                               override_reason=None, now=now))
        assert (e.status, e.code) == (403, "person_required")
        res = await advance(db, lad, actor=SESSION, expected_current=None,
                            override_reason=None, now=now)
        rows = await _rows(db)
    assert (res.from_key, res.to_key, res.override) == (None, "friendly", False)
    assert rows["friendly"].state == "current"
    assert aware_utc(rows["friendly"].entered_at) == now


@pytest.mark.asyncio
async def test_unknown_condition_counts_as_unmet(isolated_db):
    """A stale ``met`` reads ``unknown`` at advance time, so the gate is not met."""
    rows = _household_met()
    rows[2] = _m("household", "nightly", "met", {"n": 3}, age_s=40 * 3600)  # past 36h max age
    await _seed(measurements=rows)
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=OWNER, expected_current="household",
                               override_reason=None, now=_now()))
    assert (e.status, e.code) == (409, "gate_unmet")
    assert e.detail["unmet"] == [{"key": "nightly", "status": "unknown", "value": {"n": 3}}]


@pytest.mark.asyncio
async def test_every_write_audited(isolated_db):
    await _seed(measurements=_household_met())
    async with db_session() as db:
        lad = await _ladder(db)
        await tick_condition(db, lad, "friendly", "setup", actor=SESSION, met=True,
                             evidence="done", url=None)
        await set_status_note(db, lad, "household", actor=SESSION, text="close")
        n = await add_note(db, lad, actor=SESSION, kind="ask", text="Sign the DPA")
        await update_note(db, lad, n.id, actor=SESSION, status="done")
        await advance(db, lad, actor=OWNER, expected_current="household",
                      override_reason=None, now=_now())
        await db.commit()
    async with db_session() as db:
        events = (await db.execute(select(AuditEvent).order_by(AuditEvent.at))).scalars().all()
    by_type = {e.type: e for e in events}
    assert set(by_type) >= {"pm.milestone.tick", "pm.milestone.status_note",
                            "pm.milestone.note_add", "pm.milestone.note_update",
                            "pm.milestone.advance"}
    assert by_type["pm.milestone.tick"].via == "session"
    assert by_type["pm.milestone.tick"].identity == "owner@example.com"
    adv = by_type["pm.milestone.advance"]
    assert adv.via == "api"
    assert adv.detail["from"] == "household" and adv.detail["to"] == "friendly"
    assert adv.detail["override"] is False and adv.detail["unmet"] == []


async def _post_sync_ordinals(removed_current: str, ordinals: dict[str, int]) -> None:
    """What pm-sync leaves after the current rung leaves the YAML: the removed current
    rung keeps its stale ordinal, the live rungs are renumbered densely."""
    async with db_session() as db:
        for key, m in (await _rows(db)).items():
            m.ordinal = ordinals[key]
            if key == removed_current:
                m.removed_at = _now()
        await db.commit()


@pytest.mark.asyncio
async def test_advance_from_removed_current_goes_to_lowest_live_upcoming(isolated_db):
    # household(0) friendly(1) community(2) public(3); friendly current, then deleted:
    # sync renumbers the live rungs (community=1, public=2), friendly keeps 1.
    await _seed(current="friendly")
    await _post_sync_ordinals("friendly", {"household": 0, "friendly": 1, "community": 1,
                                           "public": 2})
    await _mark_done("household")
    async with db_session() as db:
        res = await advance(db, await _ladder(db), actor=OWNER, expected_current="friendly",
                            override_reason="removed rung, moving on", now=_now())
        rows = await _rows(db)
    assert res.to_key == "community"
    assert rows["community"].state == "current" and rows["public"].state == "upcoming"


@pytest.mark.asyncio
async def test_removed_current_with_highest_stale_ordinal_does_not_complete(isolated_db):
    await _seed(current="friendly")
    await _post_sync_ordinals("friendly", {"household": 0, "friendly": 5, "community": 1,
                                           "public": 2})
    await _mark_done("household")
    async with db_session() as db:
        res = await advance(db, await _ladder(db), actor=OWNER, expected_current="friendly",
                            override_reason="removed rung, moving on", now=_now())
        rows = await _rows(db)
    # ordinal > 5 finds nothing; live upcoming rungs remain, so the ladder never completes
    assert res.to_key == "community"
    assert rows["community"].state == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["mcp-external", "internal", "cli", ""])
async def test_override_from_non_api_via_403(isolated_db, via):
    """Owner is fail-closed: only a direct ``api`` request may override."""
    await _seed()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=Actor("owner@example.com", via),
                               expected_current="household",
                               override_reason="a perfectly long reason", now=_now()))
    assert (e.status, e.code) == (403, "override_requires_owner")


@pytest.mark.asyncio
async def test_complete_ladder_409_whatever_expected_current(isolated_db):
    await _seed(current="public", measurements=[
        _met("public", k) for k in ("phase2-readout", "launch-day", "daily-watch")])
    await _mark_done("household", "friendly", "community")
    async with db_session() as db:
        await advance(db, await _ladder(db), actor=OWNER, expected_current="public",
                      override_reason=None, now=_now())
        await db.commit()
    for expected in ("public", "household", None):
        async with db_session() as db:
            e = await _err(advance(db, await _ladder(db), actor=OWNER,
                                   expected_current=expected, override_reason=None, now=_now()))
        assert (e.status, e.code) == (409, "ladder_complete"), expected


# ---------------------------------------------------------------- link scheme (stored XSS)

BAD_URLS = ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "//evil.example/x",
            "ftp://example.com/f", "https://", "notaurl", "https://e.x/" + "a" * 2000)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_tick_rejects_non_http_url(isolated_db, url):
    await _seed()
    async with db_session() as db:
        e = await _err(tick_condition(db, await _ladder(db), "friendly", "setup", actor=OWNER,
                                      met=True, evidence="x", url=url))
    assert (e.status, e.code) == (400, "bad_url")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_add_note_rejects_non_http_url(isolated_db, url):
    await _seed()
    async with db_session() as db:
        e = await _err(add_note(db, await _ladder(db), actor=OWNER, kind="ask", text="x", url=url))
    assert (e.status, e.code) == (400, "bad_url")


@pytest.mark.asyncio
async def test_http_urls_and_blank_are_accepted(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        row = await tick_condition(db, lad, "friendly", "setup", actor=OWNER, met=True,
                                   evidence="x", url="  HTTP://Example.com/a  ")
        assert row.manual_url == "HTTP://Example.com/a"
        n = await add_note(db, lad, actor=OWNER, kind="ask", text="x", url="   ")
        assert n.url is None
        n = await add_note(db, lad, actor=OWNER, kind="ask", text="x",
                           url="https://e.x/" + "a" * (2000 - len("https://e.x/")))
        assert n.url is not None and len(n.url) == 2000


# ---------------------------------------------------------------- final-review fixes


@pytest.mark.asyncio
async def test_concurrent_first_tick_is_409_concurrent_update(isolated_db):
    """Two first ticks race: ours read no row, the other committed one before our
    flush. The unique constraint refuses our insert; that is a 409, not a 500."""
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        real_execute = db.execute
        raced = False

        async def racing_execute(*args, **kwargs):
            nonlocal raced
            result = await real_execute(*args, **kwargs)
            if not raced:
                raced = True
                async with db_session() as other:
                    await tick_condition(other, await _ladder(other), "friendly", "setup",
                                         actor=OTHER, met=True, evidence="first", url=None)
                    await other.commit()
            return result

        db.execute = racing_execute  # type: ignore[method-assign]
        e = await _err(tick_condition(db, lad, "friendly", "setup", actor=OWNER, met=False,
                                      evidence="second", url=None))
    assert (e.status, e.code) == (409, "concurrent_update")
    async with db_session() as db:
        rows = (await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.scope == "friendly",
            MilestoneMeasurement.key == "setup"))).scalars().all()
    assert [r.manual_evidence for r in rows] == ["first"]


@pytest.mark.asyncio
async def test_tick_evidence_is_capped(isolated_db):
    from server.pm.milestone_actions import NOTE_TEXT_MAX
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        e = await _err(tick_condition(db, lad, "friendly", "setup", actor=OWNER, met=True,
                                      evidence="x" * (NOTE_TEXT_MAX + 1), url=None))
        assert (e.status, e.code, e.detail) == (400, "too_long", {"max": NOTE_TEXT_MAX})
        row = await tick_condition(db, lad, "friendly", "setup", actor=OWNER, met=True,
                                   evidence="x" * NOTE_TEXT_MAX, url=None)
        assert row.manual_evidence is not None and len(row.manual_evidence) == NOTE_TEXT_MAX


@pytest.mark.asyncio
async def test_note_ref_is_capped(isolated_db):
    from server.pm.milestone_actions import NOTE_REF_MAX
    assert NOTE_REF_MAX == 20
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        e = await _err(add_note(db, lad, actor=OWNER, kind="decision", text="x",
                                ref="D" * (NOTE_REF_MAX + 1)))
        assert (e.status, e.code, e.detail) == (400, "too_long", {"max": NOTE_REF_MAX})
        n = await add_note(db, lad, actor=OWNER, kind="decision", text="x",
                           ref="  " + "D" * NOTE_REF_MAX + "  ")
        assert n.ref == "D" * NOTE_REF_MAX


# ---------------------------------------------------------------- session actors (R1)


@pytest.mark.asyncio
async def test_session_actor_can_add_note(isolated_db):
    await _seed()
    async with db_session() as db:
        n = await add_note(db, await _ladder(db), actor=AGENT, kind="ask", text="Sign the DPA")
    assert n.created_by == "session:abc"


@pytest.mark.asyncio
async def test_session_actor_can_set_status_note(isolated_db):
    await _seed()
    async with db_session() as db:
        m = await set_status_note(db, await _ladder(db), "household", actor=AGENT, text="on track")
    assert m.status_note == "on track"


@pytest.mark.asyncio
async def test_session_can_resolve_own_ask(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=AGENT, kind="ask", text="Sign the DPA")
        n = await update_note(db, lad, n.id, actor=AGENT, status="done")
    assert (n.status, n.resolved_by) == ("done", "session:abc")


@pytest.mark.asyncio
async def test_session_cannot_resolve_owners_ask(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=OWNER, kind="ask", text="Sign the DPA")
        e = await _err(update_note(db, lad, n.id, actor=AGENT, status="done"))
    assert (e.status, e.code) == (403, "owner_required")


@pytest.mark.asyncio
async def test_session_actor_cannot_tick(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(tick_condition(db, await _ladder(db), "friendly", "setup", actor=AGENT,
                                      met=True, evidence="done", url=None))
    assert (e.status, e.code) == (403, "person_required")


@pytest.mark.asyncio
async def test_session_actor_cannot_advance(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(advance(db, await _ladder(db), actor=AGENT, expected_current="household",
                               override_reason=None, now=_now()))
    assert (e.status, e.code) == (403, "person_required")


@pytest.mark.asyncio
async def test_system_actor_cannot_add_note(isolated_db):
    await _seed()
    async with db_session() as db:
        e = await _err(add_note(db, await _ladder(db), actor=SYSTEM, kind="ask", text="x"))
    assert (e.status, e.code) == (403, "person_or_session_required")


@pytest.mark.asyncio
async def test_session_cannot_edit_null_creator_note(isolated_db):
    await _seed()
    async with db_session() as db:
        lad = await _ladder(db)
        n = await add_note(db, lad, actor=OWNER, kind="ask", text="Sign the DPA")
        n.created_by = None
        await db.flush()
        e = await _err(update_note(db, lad, n.id, actor=AGENT, status="done"))
        assert (e.status, e.code) == (403, "owner_required")
        n = await update_note(db, lad, n.id, actor=OWNER, status="done")
    assert n.status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["assistant:x", "lan@local"])
async def test_agent_actor_cannot_add_note(isolated_db, identity):
    await _seed()
    async with db_session() as db:
        e = await _err(add_note(db, await _ladder(db), actor=Actor(identity, "api"),
                                kind="ask", text="x"))
    assert (e.status, e.code) == (403, "person_or_session_required")
