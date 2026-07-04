"""End-to-end Backlog Runner loop — REAL loop + REAL Plan B seams, zero network,
zero real sessions.

The whole loop rides the real ``BacklogRunnerLoop`` wired to the real selection /
claim / spawn seams (``select_batch`` → ``rank_and_batch``, ``claim_batch`` /
``unclaim_batch`` / ``label_needs_human``) but with:

- a FAKE ``gh`` (:class:`FakeGh`) — every GitHub call is scripted, no network;
- a canned rank (:func:`fake_rank`) — the real Haiku model is NEVER called;
- a fake ``create_session`` that writes a DB row — no real PTY / SDK session.

Acceptance criterion: the full path (select→claim→spawn, in-flight cap,
spawn-failure unclaim, ended-without-PR give-up, PR-Doctor handoff give-up,
de-dup) runs with NO network and NO real sessions.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

from server.automations import backlog_github
from server.automations.backlog_runner import BacklogRunnerLoop, select_batch
from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.github.pr_status import PullRequestInfo
from server.tests.fakes.fake_backlog import FakeGh


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #

class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_config(**over) -> dict:
    cfg = {
        "enabled_repos": ["o/r"],
        "eligible_label": "ready",
        "max_concurrent": 1,
        "max_spawns_per_day": 10,
        "max_attempts": 2,
        "max_issues_per_pr": 1,
    }
    cfg.update(over)
    return cfg


def _candidates_proc(refs: list[tuple[str, int, str]]) -> subprocess.CompletedProcess:
    """Scripted `gh api graphql` search response for the candidate query."""
    nodes = [{"number": n, "title": t, "repository": {"nameWithOwner": repo}}
             for (repo, n, t) in refs]
    payload = {"data": {"issues": {"nodes": nodes}}}
    return subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=json.dumps(payload), stderr="")


class LabelAwareGh(FakeGh):
    """FakeGh that tracks in-progress labels and reflects them in the candidate
    query — a claimed issue drops out of the candidate list, exactly like the
    real ``-label:in-progress`` search gate."""

    def __init__(self, candidate_refs):
        super().__init__()
        self._refs = list(candidate_refs)  # (repo, number, title)
        self.in_progress: set[str] = set()

    def __call__(self, args, token=None):
        self.calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            live = [(r, n, t) for (r, n, t) in self._refs
                    if f"{r}#{n}" not in self.in_progress]
            return _candidates_proc(live)
        if args[:2] == ["issue", "edit"]:
            number, repo = args[2], args[4]
            ref = f"{repo}#{number}"
            if "--add-label" in args and "in-progress" in args:
                self.in_progress.add(ref)
            if "--remove-label" in args and "in-progress" in args:
                self.in_progress.discard(ref)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


async def _seed_rows() -> None:
    from server.db.models import Automation
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))


class Harness:
    """A real loop over real seams with a fake gh + fake create_session."""

    def __init__(self, *, config, clock, candidate_refs, healthy="acct",
                 spawn_raises=False, pr_status=None, pr_doctor_active=True):
        self.gh = LabelAwareGh(candidate_refs)
        self.config = config
        self.clock = clock
        self.spawn_raises = spawn_raises
        self._pr_status = pr_status or {}
        self._pr_doctor_active = pr_doctor_active
        self.created: list[dict] = []
        self.ended_ids: set[str] = set()
        self.ctx = AutomationContext("backlog-runner", create_session=self._create_session)
        self.loop = BacklogRunnerLoop(
            self.ctx,
            select_batch=self._select_batch,
            claim=self._claim,
            spawn=self._spawn,
            count_in_flight=self._count_in_flight,
            healthy_account=lambda: self._healthy(healthy),
            pr_status_for=self._pr_status_for,
            pr_doctor_active=self._pr_doctor_active_fn,
            read_config=self._read_config,
            clock=clock,
            release=self._release,
            needs_human=self._needs_human,
            session_ended=self._session_ended,
            reconcile=self._reconcile,
        )
        self.released: list[str] = []
        self.needs_humaned: list[str] = []

    async def _read_config(self):
        return "auto", self.config

    async def _select_batch(self, config):
        # Canned rank: preserve candidate order (lowest number first). The real
        # Haiku model is never called.
        def _rank(cs, _cfg):
            return [c["ref"] for c in sorted(cs, key=lambda c: c["number"])]
        return await select_batch(config, gh_run=self.gh, rank=_rank)

    async def _claim(self, batch) -> bool:
        return await asyncio.to_thread(backlog_github.claim_batch, batch, gh_run=self.gh)

    async def _batch_for(self, target: str):
        from server.automations.backlog_selection import Batch
        st = await self.ctx.get_target_state(target)
        return Batch(primary=target, issues=st.get("issues") or [target],
                     repo=target.split("#", 1)[0])

    async def _release(self, target: str) -> None:
        """RELEASE-only: remove in-progress, NO needs-human."""
        self.released.append(target)
        batch = await self._batch_for(target)
        await asyncio.to_thread(backlog_github.unclaim_batch, batch, gh_run=self.gh)

    async def _needs_human(self, target: str) -> None:
        """GIVE-UP: add needs-human + remove in-progress."""
        self.needs_humaned.append(target)
        batch = await self._batch_for(target)
        await asyncio.to_thread(backlog_github.label_needs_human, batch, gh_run=self.gh)

    async def _reconcile(self, claimed_refs, live_refs) -> None:
        await asyncio.to_thread(backlog_github.reconcile_dangling,
                                list(claimed_refs), set(live_refs), gh_run=self.gh)

    async def _spawn(self, batch, account: str):
        if self.spawn_raises:
            raise RuntimeError("clone failed (injected)")
        # No real clone / SDK session: route through the context spawner, which
        # de-dups on (origin, origin_ref) and emits `spawned` with detail.issues.
        return await self.ctx.spawn_session(
            origin_ref=batch.primary, dedupe_key=batch.primary,
            repo=batch.repo, cwd="/tmp", account=account,
            name=f"backlog {batch.primary}", initial_prompt="do it",
            detail={"issues": batch.issues})

    async def _create_session(self, *, origin, origin_ref, **kw):
        async with db_session() as db:
            row = DbSession(name=kw.get("name", origin_ref), account_name="acct",
                            cwd="/tmp", status="idle", repo=kw.get("repo"),
                            branch=kw.get("branch") or "backlog/branch",
                            origin=origin, origin_ref=origin_ref)
            db.add(row)
            await db.flush()
            self.created.append({"id": row.id, "ref": origin_ref})
            return row

    async def _count_in_flight(self) -> int:
        from sqlalchemy import func, select
        async with db_session() as db:
            return (await db.execute(select(func.count()).select_from(DbSession).where(
                DbSession.origin == "backlog-runner",
                DbSession.status != "ended"))).scalar_one()

    async def _healthy(self, name):
        return name

    async def _pr_status_for(self, target):
        return self._pr_status.get(target)

    async def _pr_doctor_active_fn(self, _repo):
        return self._pr_doctor_active

    async def _session_ended(self, session_id: str) -> bool:
        return session_id in self.ended_ids

    async def end_session(self, ref: str) -> None:
        """Mark the runner session for *ref* ended (status + probe)."""
        from sqlalchemy import select, update
        async with db_session() as db:
            sid = (await db.execute(select(DbSession.id).where(
                DbSession.origin_ref == ref,
                DbSession.origin == "backlog-runner"))).scalar_one()
            await db.execute(update(DbSession).where(DbSession.id == sid)
                             .values(status="ended"))
        self.ended_ids.add(sid)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Full-path selection → claim → spawn                                          #
# --------------------------------------------------------------------------- #

def test_auto_selects_claims_spawns_one_batch(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one"), ("o/r", 2, "two")])
        await h.loop.tick()
        # Exactly one batch spawned (in-flight cap = 1).
        assert len(h.created) == 1
        # It claimed via the in-progress label on the primary.
        claim_calls = [c for c in h.gh.calls if c[:2] == ["issue", "edit"]
                       and "--add-label" in c and "in-progress" in c]
        assert claim_calls, "expected an in-progress claim label call"
        # `spawned` event carries detail.issues.
        events = await _events("spawned")
        assert events and events[0].detail and events[0].detail.get("issues")

    run(go())


def test_in_flight_cap_respected(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cap.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one"), ("o/r", 2, "two")])
        await h.loop.tick()
        await h.loop.tick()  # still full → no new spawn
        assert len(h.created) == 1

    run(go())


# --------------------------------------------------------------------------- #
# Spawn failure → unclaim, attempt NOT consumed                                #
# --------------------------------------------------------------------------- #

def test_spawn_failure_unclaims_and_no_attempt(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'sf.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")], spawn_raises=True)
        await h.loop.tick()
        assert h.created == []               # no session written
        assert "o/r#1" in h.released         # claim RELEASED (back to pool)
        assert "o/r#1" not in h.needs_humaned  # NOT escalated to needs-human
        assert h.loop._spawns_today == []    # no daily-cap slot consumed
        st = await h.ctx.get_target_state("o/r#1")
        assert st == {}                      # no attempt / state recorded
        # A transient spawn failure must leave NO needs-human label — only the
        # in-progress claim is removed (released to the pool).
        nh = [c for c in h.gh.calls if "--add-label" in c and "needs-human" in c]
        assert nh == [], "spawn failure must NOT add needs-human"
        removed = [c for c in h.gh.calls if c[:2] == ["issue", "edit"]
                   and "--remove-label" in c and "in-progress" in c]
        assert removed, "spawn failure must remove in-progress (release)"
        # An `unclaimed` event was emitted for the release.
        unc = await _events("unclaimed")
        assert unc

    run(go())


# --------------------------------------------------------------------------- #
# Ended-without-PR → attempts++ → give-up at max                               #
# --------------------------------------------------------------------------- #

def test_ended_without_pr_increments_then_gives_up(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gu.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1, max_attempts=2),
                    clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")], pr_status={})
        await h.loop.tick()                  # spawn o/r#1, attempts=0
        await h.end_session("o/r#1")         # session ends, no PR
        await h.loop.tick()                  # sweep: attempts -> 1 (< max), keep row
        st = await h.ctx.get_target_state("o/r#1")
        assert st.get("attempts") == 1
        assert st.get("_ended") is True
        await h.loop.tick()                  # sweep again: attempts -> 2 == max → give up
        st2 = await h.ctx.get_target_state("o/r#1")
        assert st2 == {}                     # state deleted on give-up
        assert "o/r#1" in h.needs_humaned    # GIVE-UP path escalated
        # needs-human label was applied via label_needs_human.
        nh = [c for c in h.gh.calls if "--add-label" in c and "needs-human" in c]
        assert nh, "expected needs-human escalation"
        gave = await _events("gave_up")
        assert gave

    run(go())


# --------------------------------------------------------------------------- #
# PR-Doctor gave_up on a handed-off target → needs-human                       #
# --------------------------------------------------------------------------- #

def test_pr_doctor_gave_up_target_needs_human(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pd.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        from server.db.models import Automation, AutomationEvent
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
            db.add(AutomationEvent(automation_id="pr-doctor", level="warn",
                                   summary="gave up", kind="gave_up",
                                   target="o/r#1", detail={"origin_ref": "o/r#1"}))
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")], pr_status={})
        await h.loop.tick()                  # spawn o/r#1
        await h.loop.tick()                  # sweep sees pr-doctor gave_up → give up
        assert "o/r#1" in h.needs_humaned
        nh = [c for c in h.gh.calls if "--add-label" in c and "needs-human" in c]
        assert nh
        st = await h.ctx.get_target_state("o/r#1")
        assert st == {}

    run(go())


# --------------------------------------------------------------------------- #
# De-dup: two ticks / one primary → one session                                #
# --------------------------------------------------------------------------- #

def test_dedup_two_ticks_one_session(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'dd.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        # in-flight cap high, but only one candidate → a second tick must NOT
        # spawn a duplicate for the same primary.
        h = Harness(config=make_config(max_concurrent=2), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")], pr_status={})
        await h.loop.tick()
        await h.loop.tick()
        rows = [c for c in h.created if c["ref"] == "o/r#1"]
        assert len(rows) == 1

    run(go())


# --------------------------------------------------------------------------- #
# Handoff frees a slot                                                          #
# --------------------------------------------------------------------------- #

def test_handoff_frees_slot(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        open_auto = PullRequestInfo(number=1, url="", state="open", checks="pending",
                                    title="", auto_merge_enabled=True)
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one"), ("o/r", 2, "two")],
                    pr_status={"o/r#1": open_auto}, pr_doctor_active=True)
        await h.loop.tick()
        assert len(h.created) == 1
        # o/r#1's PR is open+auto-merge and pr-doctor covers it → handoff frees the
        # slot; ending the session drops the in-flight count so #2 can spawn.
        await h.end_session("o/r#1")
        await h.loop.tick()
        assert len(h.created) == 2

    run(go())


async def _events(kind: str):
    from sqlalchemy import select
    from server.db.models import AutomationEvent
    async with db_session() as db:
        return (await db.execute(select(AutomationEvent).where(
            AutomationEvent.automation_id == "backlog-runner",
            AutomationEvent.kind == kind).order_by(AutomationEvent.id))).scalars().all()


# --------------------------------------------------------------------------- #
# Endpoint: POST /backlog-runner/trigger (generic action surface)              #
# --------------------------------------------------------------------------- #

H = {"Tailscale-User-Login": "testuser@example.com"}


class _FakeOrch:
    def __init__(self, status="spawned"):
        self._status = status
        self.calls: list[tuple[str, str, str]] = []

    async def automation_trigger(self, automation_id: str, item_id: str, kind: str) -> dict:
        self.calls.append((automation_id, item_id, kind))
        level = "ok" if self._status == "spawned" else "warn"
        message = "Spawned implementation session" if self._status == "spawned" else "No longer a current candidate"
        return {"status": self._status, "level": level, "message": message}


def _client(orch):
    import os
    from contextlib import contextmanager
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    @contextmanager
    def _ctx():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                     "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=real)
            app.state.sessions = orch
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c
    return _ctx()


def test_implement_requires_auth():
    with _client(_FakeOrch()) as c:
        r = c.post("/api/automations/backlog-runner/trigger",
                   json={"item_id": "o/r#1", "kind": "implement"})
        assert r.status_code == 401


def test_implement_spawns_on_valid_primary():
    orch = _FakeOrch(status="spawned")
    with _client(orch) as c:
        r = c.post("/api/automations/backlog-runner/trigger",
                   json={"item_id": "o/r#1", "kind": "implement"}, headers=H)
        assert r.status_code == 200
        assert r.json()["status"] == "spawned"
        assert orch.calls == [("backlog-runner", "o/r#1", "implement")]


def test_implement_unknown_primary_returns_warn_status():
    orch = _FakeOrch(status="unknown")
    with _client(orch) as c:
        r = c.post("/api/automations/backlog-runner/trigger",
                   json={"item_id": "o/r#999", "kind": "implement"}, headers=H)
        assert r.status_code == 200
        assert r.json()["status"] == "unknown"


# --------------------------------------------------------------------------- #
# I3: `claimed` event emitted on a successful claim                            #
# --------------------------------------------------------------------------- #

def test_claimed_event_emitted_on_claim(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'clm.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")])
        await h.loop.tick()
        claimed = await _events("claimed")
        assert claimed, "expected a `claimed` event after a successful claim"
        assert claimed[0].target == "o/r#1"

    run(go())


# --------------------------------------------------------------------------- #
# I2: status() reflects live counts after a tick                               #
# --------------------------------------------------------------------------- #

def test_status_reflects_non_idle_counts_after_tick(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'stat.db'}"

    async def go():
        from server.automations.backlog_runner import BacklogRunner
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")])
        # Before any tick: idle.
        assert BacklogRunner().status(h.ctx).level == "idle"
        await h.loop.tick()
        st = BacklogRunner().status(h.ctx)
        assert st.level == "active"
        assert "1 implementing" in st.text
        assert getattr(h.ctx, "backlog_counts")["in_flight"] == 1

    run(go())


# --------------------------------------------------------------------------- #
# C2(b): a dangling in-progress claim (no live session, no state) is           #
# reconciled away by the sweep.                                                #
# --------------------------------------------------------------------------- #

def test_sweep_reconciles_dangling_claim(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'rec.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")])
        # Simulate a crash-after-claim: label written + a durable `claimed` state
        # row with NO session_id (spawn never completed), and no live session.
        h.gh.in_progress.add("o/r#5")
        await h.ctx.set_target_state("o/r#5", {
            "repo": "o/r", "issues": ["o/r#5"], "claimed": True, "attempts": 0})
        # Drive only the reconciliation path (sweep runs it every tick).
        await h.loop._reconcile_dangling()
        # The dangling claim's in-progress label was removed (released to pool).
        removed = [c for c in h.gh.calls if c[:2] == ["issue", "edit"]
                   and "--remove-label" in c and "in-progress" in c
                   and c[2] == "5"]
        assert removed, "dangling claim #5 should be released"
        assert "o/r#5" not in h.gh.in_progress

    run(go())


def test_reconcile_leaves_live_claim_alone(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'recl.db'}"

    async def go():
        await init_db(db_url=db_url)
        await _seed_rows()
        h = Harness(config=make_config(max_concurrent=1), clock=FakeClock(),
                    candidate_refs=[("o/r", 1, "one")])
        # A claimed row WITH a live session_id must not be reconciled.
        h.gh.in_progress.add("o/r#9")
        await h.ctx.set_target_state("o/r#9", {
            "repo": "o/r", "issues": ["o/r#9"], "claimed": True,
            "session_id": "sess-live", "attempts": 0})
        await h.loop._reconcile_dangling()
        assert "o/r#9" in h.gh.in_progress, "live claim must stay labeled"

    run(go())


# --------------------------------------------------------------------------- #
# I1: the real orchestrator claim seam posts a linking comment                 #
# --------------------------------------------------------------------------- #

def test_backlog_claim_posts_linking_comment(tmp_path, monkeypatch):
    import os as _os

    from server.automations.backlog_selection import Batch
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    async def go():
        monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'cmt.db'}")
        _os.environ["CCSWITCH_DEV"] = "1"
        await init_db(db_url=_os.environ["CCSWITCH_DB_URL"])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        gh = LabelAwareGh([("o/r", 1, "one")])
        orch._gh_run = gh
        batch = Batch(primary="o/r#1", issues=["o/r#1"], repo="o/r")
        ok = await orch._backlog_claim(batch)
        assert ok is True
        comments = [c for c in gh.calls if c[:2] == ["issue", "comment"]]
        assert comments, "expected a linking comment on claim"
        body = comments[0][comments[0].index("--body") + 1]
        assert "Backlog Runner" in body and "o/r#1" in body

    run(go())
