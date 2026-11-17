"""Production delivery health (#3404): the pure rule and the refresh that stores the
readout, opens / clears the owner ask, and pushes once per prod SHA.

The acceptance replay: 2026-11-10, KinDash prod stale for two days with
cairnea/KinDash#21010 (``ci:regressed``) unowned raises ONE needs-you item naming
#21010; it clears when prod's SHA reaches a verified main.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import (MilestoneLadder, MilestoneMeasurement, MilestoneNote, Project,
                              ProjectRepo, RepoDeployConfig, Session as DbSession)
from server.pm import delivery_health as dh
from server.pm.delivery_health_refresh import parse_regressed, prod_env, refresh_delivery
from server.pm.milestone_asks import open_milestone_asks
from server.pm.milestone_view import build_milestones_view, summarize_milestones

REPO = "cairnea/KinDash"
STALE = "a" * 40      # prod's SHA from 11-08 on
MAIN = "b" * 40       # main's head on 11-10
NOV10 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
OLDEST = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)   # 57 h before NOV10

REGRESSED = dh.RegressedIssue(ref=f"{REPO}#21010", title="[flaky] family-hub-desk",
                              url=f"https://github.com/{REPO}/issues/21010")


def _facts(**kw) -> dh.DeliveryFacts:
    base: dict = dict(repo=REPO, branch="main", prod_sha=STALE, behind_commits=14,
                      oldest_unshipped_at=OLDEST, verified={STALE: "https://run/1"},
                      gate_workflows=("nightly.yml", "deploy-reattempt.yml"),
                      streak=0, regressed=(REGRESSED,))
    base.update(kw)
    return dh.DeliveryFacts(**base)


# ---------------------------------------------------------------- pure rule


def test_blocked_past_48h_with_an_unowned_regressed_issue_escalates_naming_it():
    v = dh.evaluate(_facts(), NOV10)
    assert v.state == dh.STATE_BLOCKED
    assert v.escalate and v.reasons == (dh.STATE_BLOCKED,)
    assert v.blockers == (REGRESSED,)
    assert round(v.hours_behind or 0) == 57
    text = dh.ask_text(_facts(), v)
    # The summary clips an ask at 60 characters: the blocker comes first.
    assert text.startswith(f"Prod delivery blocked by {REPO}#21010")
    assert "14 commit(s) / 57h behind main" in text
    assert dh.ask_url(_facts(), v) == REGRESSED.url


def test_an_owned_regressed_issue_suppresses_the_blocked_escalation():
    for owned in (dh.RegressedIssue(**{**REGRESSED.__dict__, "session": True}),
                  dh.RegressedIssue(**{**REGRESSED.__dict__, "prs": (f"{REPO}#21100",)})):
        v = dh.evaluate(_facts(regressed=(owned,)), NOV10)
        assert v.state == dh.STATE_BLOCKED and not v.escalate


def test_blocked_with_no_regressed_issue_still_escalates():
    v = dh.evaluate(_facts(regressed=()), NOV10)
    assert v.escalate and v.blockers == ()
    assert "no ci:regressed issue explains it" in dh.ask_text(_facts(regressed=()), v)


def test_under_48h_behind_is_only_behind():
    v = dh.evaluate(_facts(), OLDEST + timedelta(hours=47))
    assert v.state == dh.STATE_BEHIND and not v.escalate


def test_an_unverified_prod_sha_escalates_at_once():
    f = _facts(behind_commits=0, oldest_unshipped_at=None, verified={MAIN: "https://run/2"})
    v = dh.evaluate(f, NOV10)
    assert v.state == dh.STATE_UNVERIFIED and v.reasons == (dh.STATE_UNVERIFIED,)
    assert dh.ask_text(f, v) == (f"Prod runs {STALE[:7]}, which no green nightly.yml or "
                                 "deploy-reattempt.yml run verified.")


def test_no_gate_workflows_skips_the_verification_rule():
    v = dh.evaluate(_facts(behind_commits=0, gate_workflows=(), verified={}), NOV10)
    assert v.verified is None and v.state == dh.STATE_HEALTHY and not v.escalate


def test_unread_facts_never_escalate():
    assert dh.evaluate(_facts(prod_sha=None), NOV10).state == dh.STATE_UNKNOWN
    assert not dh.evaluate(_facts(prod_sha=None), NOV10).escalate
    # A failed regressed read cannot prove nobody is on it.
    assert not dh.evaluate(_facts(regressed=None), NOV10).escalate
    # A failed compare leaves the blocked rule unjudged.
    v = dh.evaluate(_facts(behind_commits=None, oldest_unshipped_at=None), NOV10)
    assert v.hours_behind is None and not v.escalate


def test_parse_regressed_marks_sessions_and_open_closing_prs():
    payload = {"data": {"search": {"nodes": [
        {"number": 21010, "title": "t", "url": "u", "repository": {"nameWithOwner": REPO},
         "closedByPullRequestsReferences": {"nodes": [
             {"number": 1, "state": "CLOSED", "repository": {"nameWithOwner": REPO}}]}},
        {"number": 21011, "title": "t2", "url": "u2", "repository": {"nameWithOwner": REPO},
         "closedByPullRequestsReferences": {"nodes": [
             {"number": 2, "state": "OPEN", "repository": {"nameWithOwner": REPO}}]}},
    ]}}}
    a, b = parse_regressed(payload, frozenset({f"{REPO}#21010"}))
    assert (a.session, a.prs, a.owned) == (True, (), True)
    assert (b.session, b.prs, b.owned) == (False, (f"{REPO}#2",), True)


def test_prod_env_picks_the_prod_environment():
    envs = json.dumps([{"name": "staging", "healthUrl": "https://qa/h"},
                       {"name": "production", "healthUrl": "https://prod/h"}])
    assert prod_env(envs)["healthUrl"] == "https://prod/h"
    assert prod_env(json.dumps([{"name": "qa", "healthUrl": "x"}])) is None
    assert prod_env("not json") is None


# ---------------------------------------------------------------- refresh


class _World:
    """GitHub, prod's health endpoint and the push, as of one replayed moment."""

    def __init__(self):
        self.project_id = ""
        self.prod = STALE
        self.main = MAIN
        self.behind = 14
        # Nightly runs, newest first: red on main since 11-08; the green run that
        # verified the SHA prod is still on.
        self.runs = [(MAIN, "failure"), (STALE, "success")]
        self.regressed = [21010]
        self.pushes: list[tuple] = []
        self.quiet = False

    async def http_get(self, url):
        assert url == "https://kindash.example/api/health"
        return SimpleNamespace(status_code=200, text=json.dumps({"commitSha": self.prod}))

    def gh_run(self, args, token=None):
        assert args[1].startswith(f"repos/{REPO}/compare/{self.prod}...main")
        behind = 0 if self.prod == self.main else self.behind
        commits = ([{"commit": {"committer": {"date": "2026-11-08T03:00:00Z"}}}]
                   if behind else [])
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"status": "ahead" if behind else "identical", "ahead_by": behind,
             "commits": commits}))

    def list_runs(self, repo, workflow, *, branch, event=None, limit=30):
        if workflow != "nightly.yml":
            return []
        return [{"id": i, "status": "completed", "conclusion": conclusion, "event": "schedule",
                 "head_sha": sha, "updated_at": "2026-11-10T03:00:00Z",
                 "html_url": f"https://run/{i}"} for i, (sha, conclusion) in enumerate(self.runs)]

    async def graphql(self, owner, query):
        assert owner == "cairnea" and 'label:\\"ci:regressed\\"' in query
        return {"data": {"search": {"nodes": [
            {"number": n, "title": "flaky", "url": f"https://github.com/{REPO}/issues/{n}",
             "repository": {"nameWithOwner": REPO},
             "closedByPullRequestsReferences": {"nodes": []}} for n in self.regressed]}}}

    async def notify(self, title, body, *, slug, url):
        self.pushes.append((title, body, slug, url))

    async def in_quiet_hours(self, now):
        return self.quiet

    async def refresh(self, now=NOV10, **kw):
        async def branch(_repo):
            return "main"
        return await refresh_delivery(
            self.project_id, graphql=self.graphql, list_runs=self.list_runs, now=now,
            notify=self.notify, in_quiet_hours=self.in_quiet_hours, http_get=self.http_get,
            gh_run=self.gh_run, default_branch=branch, **kw)


async def _seed(world: _World, *, deploy: bool = True) -> str:
    async with db_session() as db:
        p = Project(slug="kindash", name="KinDash", automation_policy={
            "delivery_verified_workflows": ["nightly.yml", "deploy-reattempt.yml"]})
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True))
        db.add(MilestoneLadder(project_id=p.id, title="Launch readiness", definition={}))
        if deploy:
            db.add(RepoDeployConfig(repo=REPO, enabled=True, environments=json.dumps([
                {"name": "production", "healthUrl": "https://kindash.example/api/health"}])))
        world.project_id = p.id
        return p.id


async def _asks(project_id: str) -> list[MilestoneNote]:
    async with db_session() as db:
        return (await open_milestone_asks(db, [project_id])).get(project_id, [])


async def _readout() -> MilestoneMeasurement | None:
    async with db_session() as db:
        return (await db.execute(select(MilestoneMeasurement).where(
            MilestoneMeasurement.scope == dh.SCOPE))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_replay_nov_10_raises_one_ask_naming_21010_and_clears_on_promotion(isolated_db):
    world = _World()
    pid = await _seed(world)

    out = await world.refresh()
    assert out is not None and out.ask == "opened" and out.notified
    asks = await _asks(pid)
    assert len(asks) == 1
    assert asks[0].text.startswith(f"Prod delivery blocked by {REPO}#21010")
    assert asks[0].url == f"https://github.com/{REPO}/issues/21010"
    assert len(world.pushes) == 1
    assert world.pushes[0][2:] == ("kindash", "/roadmap/kindash/milestones")
    row = await _readout()
    assert row is not None and row.status == dh.STATE_BLOCKED
    assert row.value["blockers"] == [f"{REPO}#21010"] and row.value["streak"] == 0

    # The next tick, still stale: the same one ask, no second push.
    await world.refresh(now=NOV10 + timedelta(minutes=10))
    assert len(await _asks(pid)) == 1 and len(world.pushes) == 1

    # The roadmap_needs_you tool and the summary read the same ask and readout.
    async with db_session() as db:
        ladder = (await db.execute(select(MilestoneLadder))).scalar_one()
        notes = (await db.execute(select(MilestoneNote))).scalars().all()
        rows = (await db.execute(select(MilestoneMeasurement))).scalars().all()
    view = build_milestones_view(ladder, [], rows, notes=notes, now=NOV10,
                                 primary_repo=REPO, auto_refresh=True)
    assert view["delivery"]["state"] == dh.STATE_BLOCKED
    summary = summarize_milestones(view, product="kindash")
    assert f"blocked by {REPO}#21010" in summary and "Open asks (1)" in summary

    # The Nightly goes green on main and prod reaches it: the ask clears itself.
    world.runs = [(MAIN, "success")] + world.runs
    world.prod = MAIN
    out = await world.refresh(now=NOV10 + timedelta(hours=1))
    assert out is not None and out.ask == "cleared" and out.verdict.state == dh.STATE_HEALTHY
    assert await _asks(pid) == []
    async with db_session() as db:
        note = (await db.execute(select(MilestoneNote))).scalar_one()
    assert (note.status, note.resolved_by) == ("done", dh.ASK_AUTHOR)
    assert len(world.pushes) == 1


@pytest.mark.asyncio
async def test_a_live_session_on_the_regressed_issue_clears_the_ask(isolated_db):
    world = _World()
    pid = await _seed(world)
    await world.refresh()
    async with db_session() as db:
        db.add(DbSession(name="fix", status="in_progress", origin_ref=f"{REPO}#21010",
                         account_name="a", cwd="/tmp"))
    out = await world.refresh(now=NOV10 + timedelta(minutes=10))
    assert out is not None and out.ask == "cleared" and await _asks(pid) == []


@pytest.mark.asyncio
async def test_an_ask_a_person_dropped_stays_dropped_until_the_next_promotion(isolated_db):
    world = _World()
    pid = await _seed(world)
    await world.refresh()
    async with db_session() as db:
        note = (await db.execute(select(MilestoneNote))).scalar_one()
        note.status, note.resolved_by = "dropped", "tony@example.com"
    await world.refresh(now=NOV10 + timedelta(minutes=10))
    assert await _asks(pid) == []
    # A new stale SHA is a new episode: asked again, pushed again.
    world.prod = "c" * 40
    await world.refresh(now=NOV10 + timedelta(minutes=20))
    asks = await _asks(pid)
    assert len(asks) == 1 and asks[0].ref == f"{dh.ASK_REF_PREFIX}ccccccc"
    assert len(world.pushes) == 2


@pytest.mark.asyncio
async def test_quiet_hours_defer_the_push_but_not_the_ask(isolated_db):
    world = _World()
    world.quiet = True
    pid = await _seed(world)
    out = await world.refresh()
    assert out is not None and out.ask == "opened" and not out.notified
    assert len(await _asks(pid)) == 1 and world.pushes == []
    world.quiet = False
    out = await world.refresh(now=NOV10 + timedelta(minutes=10))
    assert out is not None and out.notified and len(world.pushes) == 1


@pytest.mark.asyncio
async def test_dry_run_writes_and_sends_nothing(isolated_db):
    world = _World()
    pid = await _seed(world)
    out = await world.refresh(dry_run=True)
    assert out is not None and out.verdict.escalate
    assert await _asks(pid) == [] and await _readout() is None and world.pushes == []


@pytest.mark.asyncio
async def test_a_product_without_a_prod_deploy_environment_does_not_take_part(isolated_db):
    world = _World()
    await _seed(world, deploy=False)
    assert await world.refresh() is None
    assert await _readout() is None


@pytest.mark.asyncio
async def test_the_owner_digest_lists_the_open_delivery_ask(isolated_db):
    from server.api.pm_digest import _delivery_asks
    from server.pm import digest as dg

    world = _World()
    await _seed(world)
    await world.refresh()
    async with db_session() as db:
        found = await _delivery_asks(db)
    assert [slug for slug, _n in found] == ["kindash"]
    item = dg.from_delivery_ask(product="kindash", note_id=found[0][1].id,
                                text=found[0][1].text, url=found[0][1].url)
    assert item.kind == dg.KIND_DELIVERY and item.recommendation is None
    assert f"{REPO}#21010" in item.why
    assert dg.parse_id(item.id) == (dg.ID_DELIVERY, found[0][1].id)
