"""snooze_session / unsnooze_session (#3438): the Sessions page's own snooze and wake,
one route each, with a card that names the session and the wake time in the
operator's zone."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from server.assistant.policy import operator_timezone, route_allowed
from server.assistant.tools import (MCP_PREFIX, SESSION_TARGET_KEYS, TOOL_SPECS, ToolClass,
                                    batch_items, build_tools, card_items)
from server.db import db_session
from server.db.models import AppSettings
from server.tests.test_assistant_tools import _api, _tool

SID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NY = ZoneInfo("America/New_York")


def _record(seen: list):
    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"id": SID, "snooze": None})
    return handler


def test_both_are_session_mutations_on_the_snooze_route():
    for name in ("snooze_session", "unsnooze_session"):
        assert TOOL_SPECS[name].tool_class is ToolClass.MUTATION
        assert SESSION_TARGET_KEYS[name] == ("id",)   # the side-chat guard sees the target
    assert route_allowed("POST", f"/api/sessions/{SID}/snooze")
    assert route_allowed("DELETE", f"/api/sessions/{SID}/snooze")
    assert not route_allowed("POST", "/api/sessions/batch/snooze")


@pytest.mark.asyncio
async def test_snooze_posts_until_and_reason_through_the_ui_route():
    seen: list = []
    tool = _tool(build_tools(_api(_record(seen))), "snooze_session")
    await tool.handler({"id": SID.upper(), "title": "KinDash #20202", "until": " 24h ",
                        "reason": "let other backlog work run"})
    assert seen == [("POST", f"/api/sessions/{SID}/snooze",
                     {"until": "24h", "reason": "let other backlog work run"})]


@pytest.mark.asyncio
async def test_snooze_without_until_is_refused_before_any_call():
    seen: list = []
    tool = _tool(build_tools(_api(_record(seen))), "snooze_session")
    out = await tool.handler({"id": SID, "title": "T", "until": "  "})
    assert seen == [] and "until is required" in json.dumps(out)


@pytest.mark.asyncio
async def test_unsnooze_deletes_the_snooze():
    seen: list = []
    tool = _tool(build_tools(_api(_record(seen))), "unsnooze_session")
    await tool.handler({"id": SID, "title": "T"})
    assert seen == [("DELETE", f"/api/sessions/{SID}/snooze", None)]


def test_card_shows_title_wake_time_in_the_operators_zone_and_reason():
    from server.assistant.tools import _snooze_items
    items = _snooze_items(
        {"id": SID, "title": "KinDash #20202", "until": "2026-11-24T05:00:00Z",
         "reason": "  wait for the\nnightly  "}, tz=NY)
    assert items == ["session: KinDash #20202",
                     "until: Tue Nov 24, 12:00 AM EST (05:00 UTC)",
                     "reason: wait for the nightly"]


def test_card_resolves_a_duration_and_marks_a_bad_time():
    from server.assistant.tools import _snooze_items
    row = _snooze_items({"id": SID, "until": "24h"}, tz=timezone.utc)[1]
    want = datetime.now(timezone.utc) + timedelta(hours=24)
    assert row.startswith(f"until: {want:%a %b} {want.day}, ") and "UTC" in row
    assert "(" not in row                       # a UTC operator gets no duplicate clock
    assert _snooze_items({"id": SID, "until": "soon"})[1] == (
        "until: 'soon' (not a valid time — the call will be refused)")
    # No title: the card still names the target by id.
    assert _snooze_items({"id": SID, "until": "1h"})[0] == f"session: {SID}"


def test_unsnooze_card_names_the_session():
    assert batch_items(MCP_PREFIX + "unsnooze_session", {"id": SID, "title": "T"}) == [
        "session: T"]


@pytest.mark.asyncio
async def test_card_items_reads_the_operator_zone_from_quiet_hours(isolated_db):
    async with db_session() as db:
        assert await operator_timezone(db) is None          # unset: the host's zone
        db.add(AppSettings(id="singleton", quiet_hours_timezone="America/New_York"))
    async with db_session() as db:
        assert await operator_timezone(db) == NY
    items = await card_items(MCP_PREFIX + "snooze_session",
                             {"id": SID, "title": "T", "until": "2026-07-01T16:00:00Z"})
    assert items[1] == "until: Wed Jul 1, 12:00 PM EDT (16:00 UTC)"
    # Every other tool's card is batch_items' own, untouched.
    assert await card_items(MCP_PREFIX + "archive_sessions",
                            {"ids": [SID], "titles": ["A"]}) == ["A"]


@pytest.mark.asyncio
async def test_an_unknown_zone_name_falls_back_to_the_host_zone(isolated_db):
    async with db_session() as db:
        db.add(AppSettings(id="singleton", quiet_hours_timezone="Mars/Olympus"))
    async with db_session() as db:
        assert await operator_timezone(db) is None


# --- Visibility: the assistant can find snoozed sessions (list + get) -------------
# Snooze is an overlay on the `/api/sessions` row (`snooze: {at, until, reason, by}`),
# not a status — the Sessions page's SNOOZED section partitions the same list by it
# (`isSnoozed` in web/src/api/types.ts). Before this, `_trim_session` dropped the
# overlay and status="snoozed" matched nothing, so the assistant saw no snoozed rows.

_SNOOZED_ROWS = [
    {"id": "live", "name": "Live", "status": "in_progress", "repo": "o/r", "snooze": None},
    {"id": "op-forever", "name": "Parked", "status": "ready_review", "repo": "o/r",
     "snooze": {"at": "2026-11-20T10:00:00+00:00", "until": None, "reason": None, "by": "user"}},
    {"id": "self-soon", "name": "Waiting on CI", "status": "ready_review", "repo": "o/k",
     "cwd": "/secret", "snooze": {"at": "2026-11-24T10:00:00+00:00",
                                   "until": "2026-11-26T09:00:00+00:00",
                                   "reason": "wait for the deploy to soak", "by": "session"}},
    {"id": "op-later", "name": "Later", "status": "waiting_input", "repo": "o/r",
     "snooze": {"at": "2026-11-23T10:00:00+00:00", "until": "2026-11-30T09:00:00+00:00",
                "reason": "after the holiday", "by": "user"}},
]


def _list_api():
    return _api(lambda req: httpx.Response(200, json=_SNOOZED_ROWS))


def _data(out) -> list:
    return json.loads(out["content"][0]["text"])["data"]


@pytest.mark.asyncio
async def test_list_sessions_status_snoozed_returns_the_snoozed_overlay_soonest_wake_first():
    out = await _tool(build_tools(_list_api()), "list_sessions").handler({"status": "snoozed"})
    rows = _data(out)
    # The UI's order: soonest wake first, an indefinite snooze last.
    assert [r["id"] for r in rows] == ["self-soon", "op-later", "op-forever"]
    soon = rows[0]
    assert soon["snooze"] == {"at": "2026-11-24T10:00:00+00:00",
                              "until": "2026-11-26T09:00:00+00:00",
                              "reason": "wait for the deploy to soak", "by": "session"}
    assert soon["name"] == "Waiting on CI" and soon["repo"] == "o/k"
    assert "cwd" not in soon
    assert rows[2]["snooze"]["until"] is None


@pytest.mark.asyncio
async def test_list_sessions_real_status_filter_leaves_snoozed_rows_out_like_the_live_list():
    tool = _tool(build_tools(_list_api()), "list_sessions")
    assert [r["id"] for r in _data(await tool.handler({"status": "waiting_input"}))] == []
    assert [r["id"] for r in _data(await tool.handler({"status": "in_progress"}))] == ["live"]


@pytest.mark.asyncio
async def test_list_sessions_unfiltered_keeps_every_row_and_carries_the_overlay():
    rows = _data(await _tool(build_tools(_list_api()), "list_sessions").handler({}))
    assert [r["id"] for r in rows] == ["live", "op-forever", "self-soon", "op-later"]
    assert rows[0]["snooze"] is None and rows[1]["snooze"]["by"] == "user"


@pytest.mark.asyncio
async def test_get_session_exposes_the_snooze_overlay():
    rec = _SNOOZED_ROWS[2]
    tool = _tool(build_tools(_api(lambda req: httpx.Response(200, json=rec))), "get_session")
    assert _data_one(await tool.handler({"id": "self-soon"}))["snooze"] == rec["snooze"]


def _data_one(out) -> dict:
    return json.loads(out["content"][0]["text"])["data"]


def test_tool_descriptions_and_prompt_say_how_to_find_snoozed_sessions():
    from server.assistant.prompt import ASSISTANT_SYSTEM_PROMPT
    tools = {t.name: t for t in build_tools(_list_api())}
    assert "snoozed" in tools["list_sessions"].description
    assert "snooze" in tools["get_session"].description
    assert 'list_sessions with status "snoozed"' in ASSISTANT_SYSTEM_PROMPT
