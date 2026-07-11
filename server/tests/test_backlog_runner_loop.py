"""BacklogRunnerLoop — the supervised control loop.

Every I/O dependency is INJECTED so the loop runs with no network / no real
sessions. These tests drive the loop entirely through fakes and assert on the
emitted activity feed + the spawn/claim recorders.
"""
from __future__ import annotations

import asyncio

from server.automations.backlog_selection import Batch
from server.automations.context import AutomationContext
from server.db import db_session, init_db


# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #

class FakeClock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_config(**over) -> dict:
    cfg = {
        "enabled_repos": [],
        "eligible_label": "ready",
        "max_concurrent": 2,
        "max_spawns_per_day": 10,
        "max_attempts": 2,
        "max_issues_per_pr": 1,
    }
    cfg.update(over)
    return cfg


async def _seed_row(automation_id: str = "backlog-runner") -> None:
    from server.db.models import Automation
    async with db_session() as db:
        db.add(Automation(id=automation_id))


def build_loop(*, mode="auto", config=None, clock=None,
               candidates=None, healthy="a", in_flight_counter=None,
               pr_status=None, pr_doctor_active=True, claim_result=True,
               candidate_count=None, heal_orphans_result=None):
    """Construct a BacklogRunnerLoop over recorder fakes.

    Returns (loop, recorders) where recorders exposes .spawns / .claims /
    .unclaims and lets tests mutate the candidate queue + in-flight count.
    """
    from server.automations.backlog_runner import BacklogRunnerLoop

    cfg = config if config is not None else make_config()
    clk = clock or FakeClock()
    ctx = AutomationContext("backlog-runner")

    class Recorders:
        def __init__(self):
            self.spawns: list = []
            self.claims: list = []
            self.releases: list = []
            self.needs_humans: list = []
            self.reconciles: list = []
            self.close_outs: list = []
            self.heals: list = []
            # candidate queue: pop from the front on each select_batch
            self.candidates: list = list(candidates or [])
            self.in_flight = 0

    rec = Recorders()

    async def read_config():
        return mode, cfg

    async def select_batch(_config):
        if not rec.candidates:
            return None
        return rec.candidates.pop(0)

    async def claim(batch):
        if claim_result:
            rec.claims.append(batch)
        return claim_result

    async def spawn(batch, account):
        rec.spawns.append((batch, account))
        rec.in_flight += 1
        return object()

    async def count_in_flight():
        if in_flight_counter is not None:
            return await in_flight_counter()
        return rec.in_flight

    async def healthy_account():
        return healthy

    async def pr_status_for(target):
        return (pr_status or {}).get(target)

    async def pr_doctor_active_fn(_repo):
        return pr_doctor_active

    async def release(target):
        rec.releases.append(target)

    async def needs_human(target):
        rec.needs_humans.append(target)

    async def reconcile(claimed_refs, live_refs):
        rec.reconciles.append((list(claimed_refs), set(live_refs)))

    async def close_out(target, info):
        rec.close_outs.append((target, info))

    async def count_candidates():
        return candidate_count if candidate_count is not None else 0

    async def heal_orphans(config, tracked):
        rec.heals.append((config, set(tracked)))
        return list(heal_orphans_result or [])

    loop = BacklogRunnerLoop(
        ctx,
        select_batch=select_batch,
        claim=claim,
        spawn=spawn,
        count_in_flight=count_in_flight,
        healthy_account=healthy_account,
        pr_status_for=pr_status_for,
        pr_doctor_active=pr_doctor_active_fn,
        read_config=read_config,
        clock=clk,
        release=release,
        needs_human=needs_human,
        reconcile=reconcile,
        close_out=close_out,
        count_candidates=count_candidates if candidate_count is not None else None,
        heal_orphans=heal_orphans if heal_orphans_result is not None else None,
    )
    return loop, rec, ctx


def batch(primary: str, issues=None) -> Batch:
    return Batch(primary=primary, issues=issues or [primary])


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_eligible_reflects_candidate_count_even_when_saturated(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'elig.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            candidate_count=7,          # GitHub says 7 ready issues exist
        )
        await loop.tick()               # fills the single slot
        counts = getattr(ctx, "backlog_counts", {})
        assert counts.get("eligible") == 7
        assert counts.get("in_flight") == 1

    asyncio.run(run())


def test_tops_up_to_max_concurrent_and_no_further(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, _ = build_loop(
            config=make_config(max_concurrent=2),
            candidates=[batch("r#1"), batch("r#2"), batch("r#3")],
        )
        await loop.tick()
        assert len(rec.spawns) == 2  # topped up to max_concurrent
        # Second tick with a full in-flight set spawns nothing more.
        await loop.tick()
        assert len(rec.spawns) == 2

    asyncio.run(run())


def test_non_reentrant_no_double_spawn(tmp_path):
    """Two concurrent tick()s during a slow spawn → exactly max_concurrent spawns."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        from server.automations.backlog_runner import BacklogRunnerLoop

        cfg = make_config(max_concurrent=1)
        ctx = AutomationContext("backlog-runner")
        spawns: list = []
        in_flight = {"n": 0}
        gate = asyncio.Event()

        async def read_config():
            return "auto", cfg

        cand = [batch("r#1"), batch("r#2")]

        async def select_batch(_c):
            return cand.pop(0) if cand else None

        async def claim(_b):
            return True

        async def spawn(b, account):
            await gate.wait()  # slow spawn: hold the lock
            spawns.append(b)
            in_flight["n"] += 1
            return object()

        async def count_in_flight():
            return in_flight["n"]

        async def healthy_account():
            return "a"

        async def pr_status_for(_t):
            return None

        async def pr_doctor_active(_r):
            return True

        loop = BacklogRunnerLoop(
            ctx, select_batch=select_batch, claim=claim, spawn=spawn,
            count_in_flight=count_in_flight, healthy_account=healthy_account,
            pr_status_for=pr_status_for, pr_doctor_active=pr_doctor_active,
            read_config=read_config, clock=FakeClock(),
        )

        t1 = asyncio.create_task(loop.tick())
        t2 = asyncio.create_task(loop.tick())
        await asyncio.sleep(0.05)  # both tasks contend for the lock
        gate.set()
        await asyncio.gather(t1, t2)
        assert len(spawns) == 1  # exactly max_concurrent, no double-spawn

    asyncio.run(run())


def test_mode_off_no_spawn(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, _ = build_loop(mode="off", candidates=[batch("r#1")])
        await loop.tick()
        assert rec.spawns == []

    asyncio.run(run())


def test_no_healthy_account_emits_skipped_debounced(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(healthy=None, candidates=[batch("r#1")])
        await loop.tick()
        await loop.tick()  # debounced: no second event
        assert rec.spawns == []
        events = await ctx.list_events(kind="skipped_no_account")
        assert len(events) == 1  # debounced to a single event

    asyncio.run(run())


def test_circuit_breaker_caps_and_resets_next_day(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clk = FakeClock()
        # cap of 2 spawns/day, but plenty of concurrency + candidates
        loop, rec, ctx = build_loop(
            clock=clk,
            config=make_config(max_concurrent=5, max_spawns_per_day=2),
            candidates=[batch(f"r#{i}") for i in range(10)],
        )
        await loop.tick()
        assert len(rec.spawns) == 2  # hit the daily cap
        # Free the slots so refill is not concurrency-bound, then tick again.
        rec.in_flight = 0
        await loop.tick()
        assert len(rec.spawns) == 2  # circuit open — no more spawns
        opened = await ctx.list_events(kind="circuit_open")
        assert len(opened) >= 1
        # Advance a full day → the rolling window empties → spawning resumes.
        clk.advance(24 * 3600 + 1)
        rec.in_flight = 0
        await loop.tick()
        assert len(rec.spawns) == 4  # reset: 2 more spawns

    asyncio.run(run())


def test_handoff_frees_slot_next_tick(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'f.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()

        open_auto = PullRequestInfo(
            number=1, url="", state="open", checks="pending", title="",
            auto_merge_enabled=True,
        )
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1"), batch("r#2")],
            pr_status={"r#1": open_auto},
            pr_doctor_active=True,
        )
        await loop.tick()
        assert len(rec.spawns) == 1  # slot full with r#1
        # r#1's PR is open + auto_merge armed and pr-doctor covers it →
        # should_free_slot() frees the slot; the sweep marks the row handed_off
        # (it does NOT delete it — the close-out must still run when the PR
        # merges) and the next tick refills with r#2.
        rec.in_flight = 0  # orchestrator-side count excludes handed_off rows
        await loop.tick()
        assert len(rec.spawns) == 2
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True  # row survives handoff
        # A third tick must not re-emit handoff for the already-flagged row.
        await loop.tick()
        handoffs = await ctx.list_events(kind="handoff")
        assert len(handoffs) == 1

    asyncio.run(run())


def test_terminal_watch_gave_up_unclaims_and_needs_human(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'g.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        # PR-doctor row + a gave_up event for the handed-off target.
        from server.db.models import Automation, AutomationEvent
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
            db.add(AutomationEvent(
                automation_id="pr-doctor", level="warn", summary="gave up",
                kind="gave_up", target="r#1", detail={"origin_ref": "r#1"}))

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            pr_status={},  # no live PR status
        )
        await loop.tick()  # spawn r#1, record handoff state
        assert len(rec.spawns) == 1
        # Next sweep sees the pr-doctor gave_up for r#1 → runner gives up too.
        await loop.tick()
        assert "r#1" in rec.needs_humans  # GIVE-UP (needs-human) seam called
        assert "r#1" not in rec.releases  # give-up is NOT a plain release
        gave = await ctx.list_events(kind="gave_up")
        assert len(gave) >= 1
        st = await ctx.get_target_state("r#1")
        assert st == {}  # state deleted

    asyncio.run(run())


def test_restart_safety_counts_existing_rows(tmp_path):
    """A fresh loop counts existing origin='backlog-runner' rows via count_in_flight."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        from server.db.models import Session
        from sqlalchemy import func, select

        # Pre-existing non-terminal runner sessions (as if from before a restart).
        async with db_session() as db:
            db.add(Session(id="s1", name="x", account_name="a", cwd="/tmp",
                           status="idle", origin="backlog-runner", origin_ref="r#1"))
            db.add(Session(id="s2", name="y", account_name="a", cwd="/tmp",
                           status="idle", origin="backlog-runner", origin_ref="r#2"))

        async def count_in_flight():
            async with db_session() as db:
                return (await db.execute(
                    select(func.count()).select_from(Session).where(
                        Session.origin == "backlog-runner",
                        Session.status != "ended"))).scalar_one()

        loop, rec, _ = build_loop(
            config=make_config(max_concurrent=2),
            candidates=[batch("r#3")],
            in_flight_counter=count_in_flight,
        )
        await loop.tick()
        assert rec.spawns == []  # already at max_concurrent from the DB → no spawn

    asyncio.run(run())


def test_merged_pr_closes_out_issues_then_frees_slot(tmp_path):
    """A merged PR that never carried `Fixes #N` leaves its issues open —
    the sweep must close them out (comment + close) before dropping the row."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'g.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()
        merged = PullRequestInfo(number=42, url="", state="merged",
                                 checks="none", title="", merged=True)
        loop, rec, ctx = build_loop(pr_status={"r#1": merged})
        await ctx.set_target_state(
            "r#1", {"session_id": "s1", "issues": ["r#1", "r#2"], "claimed": True})

        await loop.tick()

        assert rec.close_outs == [("r#1", merged)]
        assert await ctx.list_target_states() == {}

    asyncio.run(run())


def test_reconcile_deletes_dangling_claim_rows(tmp_path):
    """A claimed row that never recorded a session_id (crash between claim and
    spawn) must be deleted after reconciliation releases its label — otherwise
    it holds a concurrency slot forever (KinDash#9957 sat for 6 days)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="off")
        await ctx.set_target_state("r#1", {"claimed": True})                    # dangler
        await ctx.set_target_state("r#2", {"claimed": True, "session_id": "s2"})  # live

        await loop.tick()

        assert rec.reconciles, "reconcile seam should run"
        states = await ctx.list_target_states()
        assert "r#1" not in states, "dangling claim row must be deleted"
        assert "r#2" in states, "a row with a spawned session must survive"

    asyncio.run(run())


def test_merged_after_handoff_still_runs_close_out(tmp_path):
    """The handoff leak (KinDash lockout): a PR that merges AFTER handoff must
    still close out the batch's issues. The row survives handoff exactly so
    this path can run; only the merge deletes it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho1.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()

        pr_status = {"r#1": PullRequestInfo(
            number=1, url="", state="open", checks="pending", title="",
            auto_merge_enabled=True)}
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            pr_status=pr_status,
            pr_doctor_active=True,
        )
        await loop.tick()   # spawn
        await loop.tick()   # handoff (row flagged, not deleted)
        assert (await ctx.get_target_state("r#1")).get("handed_off") is True
        assert rec.close_outs == []

        # The PR merges while handed off → close-out runs, row deleted.
        pr_status["r#1"] = PullRequestInfo(
            number=1, url="", state="merged", checks="passing", title="")
        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["r#1"]
        assert await ctx.get_target_state("r#1") == {}

    asyncio.run(run())


def test_handed_off_row_never_charges_attempts(tmp_path):
    """After handoff the runner session has ended; a transient PR-lookup miss
    (info=None) must NOT burn attempts or escalate to needs-human."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho2.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()

        pr_status = {"r#1": PullRequestInfo(
            number=1, url="", state="open", checks="pending", title="",
            auto_merge_enabled=True)}
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1, max_attempts=1),
            candidates=[batch("r#1")],
            pr_status=pr_status,
            pr_doctor_active=True,
        )
        await loop.tick()   # spawn
        await loop.tick()   # handoff
        # PR status blips to None (poller cache miss) for two sweeps.
        del pr_status["r#1"]
        await loop.tick()
        await loop.tick()
        assert rec.needs_humans == []
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True
        assert st.get("attempts", 0) == 0

    asyncio.run(run())


def test_orphan_sweep_throttled_to_interval(tmp_path):
    """The heal pass runs at most once per _ORPHAN_SWEEP_INTERVAL — every tick
    would burn a gh search + up to `limit` cross-ref lookups."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os1.db'}"

    async def run():
        from server.automations.backlog_runner import _ORPHAN_SWEEP_INTERVAL
        await init_db(db_url=db_url)
        await _seed_row()
        clk = FakeClock()
        loop, rec, ctx = build_loop(clock=clk, heal_orphans_result=[])
        await loop.tick()
        await loop.tick()
        assert len(rec.heals) == 1  # second tick inside the window → skipped
        clk.advance(_ORPHAN_SWEEP_INTERVAL + 1)
        await loop.tick()
        assert len(rec.heals) == 2

    asyncio.run(run())


def test_orphan_sweep_passes_tracked_refs_and_emits_events(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            heal_orphans_result=[("o/r#7", "closed"), ("o/r#9", "reclaimed")])
        # A live batch row tracking two issues — both must be reported tracked.
        await ctx.set_target_state("o/r#1", {
            "claimed": True, "issues": ["o/r#1", "o/r#2"], "session_id": "s1"})
        await loop.tick()
        _config, tracked = rec.heals[0]
        assert {"o/r#1", "o/r#2"} <= tracked
        assert len(await ctx.list_events(kind="orphan_closed")) == 1
        assert len(await ctx.list_events(kind="orphan_reclaimed")) == 1

    asyncio.run(run())


def test_orphan_sweep_skipped_when_mode_off(tmp_path):
    """Mode off = the automation must take NO autonomous GitHub action —
    including healing (label writes are mutations)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="off", heal_orphans_result=[])
        await loop.tick()
        assert rec.heals == []

    asyncio.run(run())


def test_orphan_sweep_skipped_when_mode_propose(tmp_path):
    """Propose mode claims the batch (label + comment) but writes no durable state
    row — the heal would read every propose-mode claim as an orphan and strip it,
    causing an hourly re-claim and comment spam. The heal must be excluded."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="propose", heal_orphans_result=[])
        await loop.tick()
        assert rec.heals == []

    asyncio.run(run())


def test_closed_unmerged_after_handoff_escalates_to_needs_human(tmp_path):
    """A PR that was handed off to PR Doctor but then closed unmerged must
    escalate to needs-human and delete the state row — the runner doesn't
    re-spawn indefinitely."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho3.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()

        pr_status = {"r#1": PullRequestInfo(
            number=1, url="", state="open", checks="pending", title="",
            auto_merge_enabled=True)}
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            pr_status=pr_status,
            pr_doctor_active=True,
        )
        # Tick 1: spawn r#1
        await loop.tick()
        assert len(rec.spawns) == 1
        # Tick 2: handoff (auto_merge armed + pr_doctor active)
        rec.in_flight = 0
        await loop.tick()
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True

        # PR closes unmerged after handoff
        pr_status["r#1"] = PullRequestInfo(
            number=1, url="", state="closed", checks="none", title="", merged=False)
        await loop.tick()

        assert "r#1" in rec.needs_humans
        assert await ctx.get_target_state("r#1") == {}

    asyncio.run(run())


def test_handed_off_row_with_no_session_id_is_not_reconciled_away(tmp_path):
    """A handed_off state row with no session_id (seeded directly, no spawn path)
    must survive reconciliation — handed_off rows are sweep-owned, not danglers.
    The reconcile seam must see r#1 in live_refs, not released."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        # No candidates — only reconciliation and sweep run.
        loop, rec, ctx = build_loop(mode="auto", candidates=[])
        # Seed a handed_off row with no session_id (e.g. from a prior worktree
        # that handed off before recording the spawn, or a direct DB seed).
        await ctx.set_target_state("r#1", {
            "claimed": True,
            "handed_off": True,
            "issues": ["r#1"],
            "repo": "r",
        })

        await loop.tick()

        # The row must survive: handed_off rows are sweep-owned, never danglers.
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True

        # Reconcile must have been called and r#1 must appear in live_refs.
        assert rec.reconciles, "reconcile seam should have been called"
        _claimed, live = rec.reconciles[-1]
        assert "r#1" in live, "handed_off row must be carried as live, not dangled"

    asyncio.run(run())
