"""The synthesizer's lifecycle sweep (#3108): finished epics, initiatives and
programs close under the product's risk tolerance instead of reaching the owner
digest, and a merged artifact PR is noticed even after its session is gone."""
from __future__ import annotations

import pytest

from server.automations.sdlc_synthesizer import (
    EPIC_CLOSES_PER_TICK, SdlcSynthesizer, SdlcSynthesizerLoop)
from server.tests.test_sdlc_synthesizer_loop import Ctx, Recorder, _ini


class SweepRecorder(Recorder):
    def __init__(self, initiatives, programs=(), **kw):
        super().__init__(initiatives, **kw)
        self.programs = list(programs)
        self.epics_closed: list[tuple[str, str, dict]] = []
        self.close_epic_result = {"closed": True, "error": ""}
        self.initiatives_closed: list[tuple[str, str, dict]] = []
        self.close_initiative_result = True
        self.autoresolve = True

    async def list_programs(self):
        return self.programs

    async def close_epic(self, iid, epic, record):
        self.epics_closed.append((iid, epic, record))
        return self.close_epic_result

    async def close_initiative(self, iid, from_stage, record):
        self.initiatives_closed.append((iid, from_stage, record))
        return self.close_initiative_result


def _loop(rec, ctx=None, *, clock=None, wired=True):
    return SdlcSynthesizerLoop(
        ctx or rec,
        list_initiatives=rec.list_initiatives, list_artifacts=rec.list_artifacts,
        record_artifact=rec.record_artifact, set_status=rec.set_status, spawn=rec.spawn,
        pr_lookup=rec.pr_lookup, update_artifact=rec.update_artifact,
        read_config=rec.read_config, clock=clock or (lambda: 0.0),
        read_policies=rec.read_policies, live_sessions=rec.live_sessions,
        advance_stage=rec.advance_stage, autoresolve_enabled=rec.autoresolve_enabled,
        arm_auto_merge=rec.arm_auto_merge,
        list_programs=rec.list_programs if wired else None,
        close_epic=rec.close_epic if wired else None,
        close_initiative=rec.close_initiative if wired else None)


def _shipped(iid="i1", *, state="open", stage="deploy", done=3):
    return {**_ini(stage, "active", project_slug="kindash", origin_ref="o/r#1",
                   epic_repo="o/r", epic_number=5, epic_total=3, epic_done=done,
                   epic_state=state), "id": iid, "slug": f"slug-{iid}"}


def _program(**kw):
    return {"id": "p1", "slug": "prog", "repo": "o/r", "stage": "deploy", "status": "active",
            "epic_repo": "o/r", "epic_number": 1, "project_slug": "kindash",
            "epic_total": 2, "epic_done": 2, "epic_state": "open",
            "phases_total": 2, "phases_done": 2, **kw}


@pytest.mark.asyncio
async def test_a_finished_epic_is_closed_with_its_gate_recorded():
    rec = SweepRecorder([_shipped()])
    rec.policies = {"kindash": {"risk_tolerance": "bold"}}
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert [(i, e) for i, e, _ in rec.epics_closed] == [("i1", "o/r#5")]
    record = rec.epics_closed[0][2]
    assert (record["gate"], record["tolerance"]) == ("epic.close", "bold")
    assert record["detail"] == {"epic": "o/r#5", "subIssues": 3}
    assert rec.initiatives_closed == []                 # the NEXT tick closes the row
    counts = SdlcSynthesizer().status(ctx).counts
    assert (counts["auto_resolved"], counts["awaiting"]) == (1, 0)


@pytest.mark.asyncio
async def test_a_closed_epic_closes_its_initiative():
    rec = SweepRecorder([_shipped(state="closed", stage="maintain")])
    rec.policies = {"kindash": {"risk_tolerance": "balanced"}}
    await _loop(rec).tick()
    assert [(i, s, r["gate"]) for i, s, r in rec.initiatives_closed] == [
        ("i1", "maintain", "initiative.close")]
    assert rec.epics_closed == []


@pytest.mark.asyncio
async def test_cautious_or_the_kill_switch_writes_nothing():
    for policies, switch in (({"kindash": {"risk_tolerance": "cautious"}}, True),
                             ({"kindash": {"risk_tolerance": "bold"}}, False)):
        rec = SweepRecorder([_shipped(), _shipped("i2", state="closed")], [_program()])
        rec.policies, rec.autoresolve = policies, switch
        await _loop(rec).tick()
        assert (rec.epics_closed, rec.initiatives_closed) == ([], [])


@pytest.mark.asyncio
async def test_unwired_seams_leave_the_close_to_a_human():
    rec = SweepRecorder([_shipped()], [_program()])
    rec.policies = {"kindash": {"risk_tolerance": "bold"}}
    ctx = Ctx()
    await _loop(rec, ctx, wired=False).tick()
    assert SdlcSynthesizer().status(ctx).counts["awaiting"] == 1


@pytest.mark.asyncio
async def test_epic_closes_trickle_and_a_refusal_waits_an_hour():
    rows = [_shipped(f"i{n}") for n in range(EPIC_CLOSES_PER_TICK + 2)]
    rec = SweepRecorder(rows)
    rec.policies = {"kindash": {"risk_tolerance": "bold"}}
    await _loop(rec).tick()
    assert len(rec.epics_closed) == EPIC_CLOSES_PER_TICK

    rec = SweepRecorder([_shipped()])
    rec.policies = {"kindash": {"risk_tolerance": "bold"}}
    rec.close_epic_result = {"closed": False, "error": "has open sub-issues"}
    now = [0.0]
    loop = _loop(rec, clock=lambda: now[0])
    await loop.tick()
    now[0] = 60.0
    await loop.tick()
    assert len(rec.epics_closed) == 1
    refused = [e for e in rec.events if e.get("kind") == "gate_refused"]
    assert len(refused) == 1 and refused[0]["detail"]["gate"] == "epic.close"
    now[0] = 3601.0
    await loop.tick()
    assert len(rec.epics_closed) == 2


@pytest.mark.asyncio
async def test_a_stale_initiative_close_is_not_counted():
    rec = SweepRecorder([_shipped(state="closed")])
    rec.policies = {"kindash": {"risk_tolerance": "bold"}}
    rec.close_initiative_result = False
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    counts = SdlcSynthesizer().status(ctx).counts
    assert (counts["auto_resolved"], counts["awaiting"]) == (0, 1)


@pytest.mark.asyncio
async def test_a_program_rolls_up_once_its_phases_are_done():
    rec = SweepRecorder([], [_program()])
    rec.policies = {"kindash": {"risk_tolerance": "balanced"}}
    await _loop(rec).tick()
    assert [(i, e) for i, e, _ in rec.epics_closed] == [("p1", "o/r#1")]

    rec = SweepRecorder([], [_program(epic_state="closed"), _program(id="p2", phases_done=1)])
    rec.policies = {"kindash": {"risk_tolerance": "balanced"}}
    await _loop(rec).tick()
    assert [(i, s) for i, s, _ in rec.initiatives_closed] == [("p1", "deploy")]


@pytest.mark.asyncio
async def test_a_sessionless_artifact_with_a_pr_is_still_enriched():
    """A row recorded with a PR but no session (or whose session the poller no
    longer watches) is looked up by its PR, so its merge advances the stage."""
    rec = SweepRecorder([_ini("plan", "awaiting_human")])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "plan", "pr_number": 9,
                            "pr_url": "https://github.com/o/r/pull/9",
                            "commit_sha": None, "session_id": None}]
    rec.pr_infos["a1"] = {"number": 9, "url": "u", "state": "merged", "sha": "abc"}
    await _loop(rec).tick()
    assert rec.updates == [("a1", {"commit_sha": "abc"})]
