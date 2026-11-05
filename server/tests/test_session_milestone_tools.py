"""The four ``roadmap_milestone*`` session tools (release milestones Phase 3, Task 2):
a READ view, two carded MUTATIONs acting as the session's human owner with
``via="session"``, and the SIGNAL note tool that acts as the session itself (R1–R3).
Every rule stays in ``server/pm/milestone_actions.py``; these pin the wiring."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import AuditEvent, Milestone, MilestoneMeasurement, MilestoneNote
from server.mcp_spec import MCP_PREFIX, MCP_TOOL_PREFIXES
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.tools import build_session_tools
from server.tests.test_api_pm_milestones import _m, _seed

OWNER = "owner@example.com"
SID = "ms-session"


def context(identity: str = OWNER, mode: str = "default") -> SessionToolContext:
    return SessionToolContext(session_id=SID, identity=identity, kind=None, engine="claude",
                              signalling_tools=True,
                              record=lambda: SimpleNamespace(permission_mode=mode))


async def call(name: str, args: dict, ctx: SessionToolContext | None = None) -> dict:
    tool = next(t for t in build_session_tools(ctx or context()) if t.name == name)
    return await tool.handler(args)


def ok(out: dict) -> tuple[str, dict]:
    """(summary text, envelope data) — the summary is the FIRST content block."""
    assert not out.get("is_error"), out
    blocks = out["content"]
    return blocks[0]["text"], json.loads(blocks[1]["text"])["data"]


def err(out: dict) -> str:
    assert out.get("is_error"), out
    return out["content"][0]["text"]


def _household_met() -> list[dict]:
    return [_m("household", k, "met", {"n": 0}) for k in ("rubric", "lane-fixes", "nightly", "consent")]


async def _audits(type_: str) -> list[AuditEvent]:
    async with db_session() as db:
        return list((await db.execute(select(AuditEvent).where(
            AuditEvent.type == type_))).scalars().all())


async def _state() -> dict[str, str | None]:
    async with db_session() as db:
        return {m.key: m.state for m in (await db.execute(select(Milestone))).scalars().all()}


# ---------------------------------------------------------------- read


@pytest.mark.asyncio
async def test_read_returns_summary_and_view(isolated_db):
    await _seed()
    text, data = ok(await call("roadmap_milestones", {"product": "kd"}))
    assert text.startswith("Launch readiness — current: household (Phase 0 · Household gate) — 0/4 met")
    assert "Upcoming: Phase 1 · Friendly families" in text
    assert data["ladder"]["currentKey"] == "household"
    assert [m["key"] for m in data["milestones"]][:2] == ["household", "friendly"]


@pytest.mark.asyncio
async def test_read_no_ladder_text(isolated_db):
    await _seed(with_ladder=False)
    text, data = ok(await call("roadmap_milestones", {"product": "kd"}))
    assert text == "kd: no milestone ladder (add docs/product/milestones.yaml)"
    assert data is None


# ---------------------------------------------------------------- tick


@pytest.mark.asyncio
async def test_tick_calls_rules_with_owner_identity_and_session_via(isolated_db):
    await _seed()
    text, data = ok(await call("roadmap_milestone_tick", {
        "product": "kd", "milestone": "friendly", "condition": "setup", "met": True,
        "evidence": "every family set up without a support call"}))
    assert "Launch readiness" in text
    friendly = next(m for m in data["milestones"] if m["key"] == "friendly")
    setup = next(c for c in friendly["gate"] if c["key"] == "setup")
    assert setup["status"] == "met" and setup["manualBy"] == OWNER
    async with db_session() as db:
        row = (await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.key == "setup"))).scalar_one()
        assert row.manual_by == OWNER
    tick = await _audits("pm.milestone.tick")
    assert len(tick) == 1 and tick[0].identity == OWNER and tick[0].via == "session"
    delegate = await _audits("pm.session.delegate")
    assert len(delegate) == 1 and delegate[0].via == "session"
    assert delegate[0].detail["tool"] == "roadmap_milestone_tick"
    assert delegate[0].detail["sessionId"] == SID


@pytest.mark.asyncio
async def test_tick_computed_condition_is_tool_error_not_manual(isolated_db):
    await _seed()
    msg = err(await call("roadmap_milestone_tick", {
        "product": "kd", "milestone": "household", "condition": "nightly", "met": True,
        "evidence": "trust me"}))
    assert "not_manual" in msg and "measured automatically" in msg
    assert await _audits("pm.session.delegate") == []   # rolled back with the refusal


# ---------------------------------------------------------------- advance


@pytest.mark.asyncio
async def test_advance_when_ready(isolated_db):
    await _seed(measurements=_household_met())
    text, data = ok(await call("roadmap_milestone_advance",
                               {"product": "kd", "expected_current": "household"}))
    assert data["ladder"]["currentKey"] == "friendly"
    assert "current: friendly (Phase 1 · Friendly families)" in text
    adv = await _audits("pm.milestone.advance")
    assert len(adv) == 1 and adv[0].identity == OWNER and adv[0].via == "session"


@pytest.mark.asyncio
async def test_advance_gate_unmet_is_tool_error(isolated_db):
    await _seed()
    msg = err(await call("roadmap_milestone_advance",
                         {"product": "kd", "expected_current": "household"}))
    assert "gate_unmet" in msg
    assert (await _state())["household"] == "current"


@pytest.mark.asyncio
async def test_advance_refused_in_bypass_permissions(isolated_db):
    await _seed(measurements=_household_met())
    msg = err(await call("roadmap_milestone_advance",
                         {"product": "kd", "expected_current": "household"},
                         context(mode="bypassPermissions")))
    assert "bypassPermissions" in msg
    assert (await _state())["household"] == "current"


@pytest.mark.asyncio
async def test_advance_has_no_override_parameter(isolated_db):
    await _seed()
    msg = err(await call("roadmap_milestone_advance", {
        "product": "kd", "expected_current": "household",
        "override_reason": "ship it anyway, the owner said so"}))
    assert "override_reason" in msg
    assert (await _state())["household"] == "current"


# ---------------------------------------------------------------- note (SIGNAL)


@pytest.mark.asyncio
async def test_note_add_records_session_actor(isolated_db):
    await _seed()
    text, data = ok(await call("roadmap_milestone_note", {
        "product": "kd", "action": "add", "kind": "ask", "milestone": "friendly",
        "text": "Fill phase1_households"}))
    assert "(by you) [friendly] Fill phase1_households" in text
    ask = data["notes"]["asks"][0]
    assert ask["createdBy"] == f"session:{SID}"
    delegate = await _audits("pm.session.delegate")
    assert len(delegate) == 1
    assert delegate[0].identity == f"session:{SID}" and delegate[0].via == "session"
    assert delegate[0].detail["tool"] == "roadmap_milestone_note"
    assert delegate[0].detail["arguments"]["action"] == "add"
    added = await _audits("pm.milestone.note_add")
    assert added and added[0].identity == f"session:{SID}"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["", "automation:runner", "lan@local"])
async def test_note_works_without_human_owner(isolated_db, identity):
    await _seed()
    unattended = context(identity=identity, mode="bypassPermissions")
    text, data = ok(await call("roadmap_milestone_note", {
        "product": "kd", "action": "add", "kind": "decision", "ref": "D9",
        "text": "Use the household fixture"}, unattended))
    assert data["notes"]["decisions"][0]["createdBy"] == f"session:{SID}"


@pytest.mark.asyncio
async def test_note_cannot_resolve_owner_ask(isolated_db):
    await _seed()
    async with db_session() as db:
        from server.db.models import MilestoneLadder
        ladder = (await db.execute(select(MilestoneLadder))).scalar_one()
        note = MilestoneNote(ladder_id=ladder.id, kind="ask", text="Owner's ask",
                             status="open", created_by=OWNER)
        db.add(note)
        await db.flush()
        note_id = note.id
    msg = err(await call("roadmap_milestone_note", {
        "product": "kd", "action": "set_status", "note_id": note_id, "status": "done"}))
    assert "owner_required" in msg and "Only the owner" in msg
    async with db_session() as db:
        assert (await db.get(MilestoneNote, note_id)).status == "open"


@pytest.mark.asyncio
async def test_note_status_line_and_refresh(isolated_db):
    await _seed()
    text, data = ok(await call("roadmap_milestone_note", {
        "product": "kd", "action": "status_line", "milestone": "household",
        "text": "waiting on the nightly streak"}))
    household = next(m for m in data["milestones"] if m["key"] == "household")
    assert household["statusNote"] == "waiting on the nightly streak"
    assert "waiting on the nightly streak" in text
    msg = err(await call("roadmap_milestone_note", {"product": "kd", "action": "status_line",
                                                    "text": "no milestone named"}))
    assert "milestone" in msg

    async def on() -> bool:
        return True

    async def off() -> bool:
        return False

    with patch("server.api.pm_milestones.automatic_refresh_enabled", on):
        text, data = ok(await call("roadmap_milestone_note", {"product": "kd", "action": "refresh"}))
        assert data["ladder"]["currentKey"] == "household"
        msg = err(await call("roadmap_milestone_note", {"product": "kd", "action": "refresh"}))
        assert "429" in msg
    with patch("server.api.pm_milestones.automatic_refresh_enabled", off):
        msg = err(await call("roadmap_milestone_note", {"product": "kd", "action": "refresh"}))
        assert "409" in msg and "automatic_refresh_off" in msg
    refresh = await _audits("pm.milestone.refresh")
    assert len(refresh) == 1 and refresh[0].identity == f"session:{SID}"
    # The refused refreshes rolled their delegation rows back with them.
    delegated = [a.detail["arguments"]["action"] for a in await _audits("pm.session.delegate")]
    assert delegated == ["status_line", "refresh"]


# ---------------------------------------------------------------- permissions


@pytest.mark.parametrize("name", ["roadmap_milestone_advance", "roadmap_milestone_tick"])
def test_advance_and_tick_are_never_ruled(name):
    from server.permission_rules.patterns import NEVER_RULED_TOOLS, matches_any, parse
    for prefix in MCP_TOOL_PREFIXES:
        tool = prefix + name
        assert tool in NEVER_RULED_TOOLS
        assert matches_any([parse(tool)], tool, {}, "/tmp") is None


def test_tick_is_never_ruled():
    from server.permission_rules.patterns import NEVER_RULED_TOOLS
    assert MCP_PREFIX + "roadmap_milestone_tick" in NEVER_RULED_TOOLS
    assert MCP_PREFIX + "roadmap_milestones" not in NEVER_RULED_TOOLS


def test_one_refusal_formatter():
    from fastapi.responses import JSONResponse
    from server.pm.milestone_tool_result import milestone_refusal
    from server.sessions.tools.milestone_note import _refused_refresh
    assert str(milestone_refusal(409, "stale_current", {"current": "m1"})).startswith(
        "409 stale_current: Someone else advanced")
    assert str(milestone_refusal(409, "stale_current", {"current": "m1"})).endswith('{"current": "m1"}')
    assert str(milestone_refusal(400, "mystery")) == "400 mystery: refused"
    resp = JSONResponse({"retryAfter": 7}, status_code=429)
    assert str(_refused_refresh(resp)).startswith("429 refresh_too_soon: Measurements")
    assert "retryAfter" in str(_refused_refresh(resp))
    resp = JSONResponse({"reason": "automatic_refresh_off"}, status_code=409)
    assert str(_refused_refresh(resp)) == str(milestone_refusal(409, "automatic_refresh_off"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "full-auto"])
async def test_note_is_signal_no_card(mode):
    from server.sessions.permissions import PermissionRegistry
    registry = PermissionRegistry()
    pid, future, _ = await registry.request(
        SID, "turn-1", MCP_PREFIX + "roadmap_milestone_note", {}, mode=mode,
        denylist=(), identity=OWNER)
    assert pid is None and future.result() is True
    pid, future, _ = await registry.request(
        SID, "turn-1", MCP_PREFIX + "roadmap_milestones", {}, mode=mode,
        denylist=(), identity=OWNER)
    assert pid is None and future.result() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "full-auto"])
@pytest.mark.parametrize("name", ["roadmap_milestone_tick", "roadmap_milestone_advance"])
async def test_tick_and_advance_card(mode, name):
    from server.sessions.permissions import PermissionRegistry
    registry = PermissionRegistry()
    pid, future, _ = await registry.request(
        SID, "turn-1", MCP_PREFIX + name, {}, mode=mode, denylist=(), identity=OWNER)
    assert pid is not None and not future.done()
    registry.resolve(pid, "deny", OWNER)
    assert await future is False


# ---------------------------------------------------------------- summary


def _fixture_view() -> dict:
    """The KinDash ladder (fixtures/milestones/ladder.yaml) as the view builder shapes
    it, with the readings the design's sample summary shows."""
    from server.pm.milestone_def import parse_ladder, to_json
    from server.pm.milestone_view import build_milestones_view
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    ladder_def = parse_ladder((Path(__file__).parent / "fixtures" / "milestones" /
                               "ladder.yaml").read_text(), primary_repo="cairnea/KinDash")
    definition = to_json(ladder_def)
    ladder = SimpleNamespace(definition=definition, title=ladder_def.title, spec_path=None,
                             source_path="docs/product/milestones.yaml", source_sha="abc",
                             synced_at=now, parse_error=None, parse_error_sha=None)
    milestones = [SimpleNamespace(
        key=m["key"], ordinal=i, label=m["label"], name=m["name"], audience=m["audience"],
        platforms=m["platforms"], epics=m["epics"], gate=m["gate"],
        state="current" if m["key"] == "household" else "upcoming",
        entered_at=now, exited_at=None, removed_at=None, exit_override=None,
        status_note="the long phase by design" if m["key"] == "household" else None)
        for i, m in enumerate(definition["milestones"])]

    def row(scope, key, status, value, age_s, error=None):
        at = now - timedelta(seconds=age_s)
        return SimpleNamespace(scope=scope, key=key, status=status, value=value,
                               measured_at=at, fetched_at=at, error=error, source_url=None,
                               manual_by=None, manual_evidence=None, manual_url=None)
    leaves = [{"repo": "cairnea/KinDash", "number": 20210 + i, "title": "t", "url": "u",
               "state": "CLOSED", "prs": []} for i in range(17)]
    leaves.append({"repo": "cairnea/KinDash", "number": 20300, "title": "t", "url": "u",
                   "state": "OPEN", "prs": []})
    measurements = [
        row("household", "consent", "met", {"done": 3, "total": 3}, 11 * 60),
        row("household", "lane-fixes", "unmet", {"done": 8, "total": 9}, 11 * 60),
        row("household", "nightly", "unmet", {"n": 0}, 13 * 3600),
        row("household", "rubric", "unknown", None, 11 * 60,
            error="no viberpit-metrics artifact on the latest run"),
        row("leaves:household", "leaves", "info", {"done": 17, "total": 18, "items": leaves}, 600),
    ]

    def note(i, kind, text, *, ref=None, status, key=None):
        return SimpleNamespace(id=f"n{i}", kind=kind, ref=ref, text=text, url=None,
                               status=status, milestone_key=key, created_by=OWNER,
                               created_at=now - timedelta(minutes=100 - i),
                               resolved_by=None, resolved_at=None)
    notes = [note(1, "ask", "Post sign-off on #20271", status="open", key="community"),
             note(2, "ask", "Fill phase1_households", status="open", key="friendly")]
    notes += [note(10 + d, "decision", f"decision {d}", ref=f"D{d}", status="locked")
              for d in range(1, 9)]
    return build_milestones_view(ladder, milestones, measurements, notes=notes, now=now,
                                 primary_repo="cairnea/KinDash", auto_refresh=True)


SAMPLE = """\
Launch readiness — current: household (Phase 0 · Household gate) — 1/4 met, 1 unknown, ready: no
  status: the long phase by design
  ? rubric: Evidence rubric satisfied for every live capability (unknown: no viberpit-metrics artifact on the latest run)
  ✖ lane-fixes: Nine lane-integrity fixes landed (8 of 9 closed, 11m ago)
  ✖ nightly: Nightly full suite green three runs running (0 of ≥3, 13h ago)
  ✔ consent: Card-consent charge captured; processor DPAs on file (3 of 3 closed, 11m ago)
  leaves: 17 done · 0 in flight · 1 not started
Open asks (2): #n1 [community] Post sign-off on #20271 · #n2 [friendly] Fill phase1_households
Decisions (8): D1 locked, D2 locked, D3 locked, D4 locked, D5 locked, D6 locked +2 more
Upcoming: Phase 1 · Friendly families, Phase 2 · Community cohort, Phase 3 · Public self-serve"""


def test_summarize_milestones_format():
    from server.pm.milestone_view import summarize_milestones
    text = summarize_milestones(_fixture_view())
    assert text == SAMPLE
    assert len(text) < 1500


def test_summarize_names_the_keys_an_agent_needs():
    from server.pm.milestone_view import summarize_milestones
    view = _fixture_view()
    household = view["milestones"][0]
    household["gate"][0] = dict(household["gate"][0], kind="manual")
    manual_key = household["gate"][0]["key"]
    text = summarize_milestones(view)
    assert "current: household (Phase 0 · Household gate)" in text
    assert f"{manual_key} [manual]:" in text
    assert text.count("[manual]") == 1
    assert "Open asks (2): #n1 [community] Post sign-off" in text and "#n2 [friendly]" in text
    assert "(by you)" not in text


def test_summarize_marks_the_viewers_own_asks():
    from server.pm.milestone_view import summarize_milestones
    view = _fixture_view()
    view["notes"]["asks"][1]["createdBy"] = "session:me"
    text = summarize_milestones(view, viewer="session:me")
    assert "#n2 (by you) [friendly]" in text and text.count("(by you)") == 1
    assert "(by you)" not in summarize_milestones(view, viewer="session:other")
    assert len(text) <= 1500


def test_summarized_passes_the_viewer_through():
    from server.pm.milestone_tool_result import summarized
    view = _fixture_view()
    view["notes"]["asks"][0]["createdBy"] = "session:me"
    assert "#n1 (by you)" in summarized(view, "kd", viewer="session:me").text


def test_summarize_truncates_long_labels_and_lists():
    from server.pm.milestone_view import summarize_milestones
    view = _fixture_view()
    household = view["milestones"][0]
    household["gate"] = [dict(household["gate"][0], key=f"c{i}", label="x" * 200)
                         for i in range(20)]
    text = summarize_milestones(view)
    assert "x" * 60 + "…" in text and "x" * 61 not in text
    assert "+" in text and "more" in text
    assert len(text) <= 1500


def test_summarize_no_ladder():
    from server.pm.milestone_view import summarize_milestones
    assert summarize_milestones({"ladder": None}, product="kd") == (
        "kd: no milestone ladder (add docs/product/milestones.yaml)")


# ---------------------------------------------------------------- import order


@pytest.mark.parametrize("modules", [
    "import server.pm.milestone_view; import server.api.pm_milestones",
    "import server.api.pm_milestones",
    "import server.sessions.tools.milestone_note",
])
def test_milestone_modules_import_cold(modules):
    """A fresh interpreter importing the milestone view (or the routes) FIRST must
    not hit a partially initialised module: milestone_view/the routes once reached
    server.automations, whose registry re-imports milestone_refresh mid-load."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-c", modules], cwd=root, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
