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
               candidate_count=None, heal_orphans_result=None,
               refresh_groups=None, spawn=None, close_out_result=None):
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

    async def _default_spawn(batch, account):
        rec.spawns.append((batch, account))
        rec.in_flight += 1
        return object()

    spawn = spawn or _default_spawn

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
        return close_out_result

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
        refresh_groups=refresh_groups,
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


def test_merged_pr_that_fails_linkage_is_denylisted_not_closed(tmp_path):
    """#642 incident: a merged PR with no linkage to any batch issue (shared-clone
    branch misattribution) must NOT close the batch or free the slot. The runner
    purges the poisoned branch snapshot and denylists the PR number so a
    re-claim / next sweep cannot re-trigger the bogus close."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ul1.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()
        merged = PullRequestInfo(number=42, url="", state="merged",
                                 checks="none", title="", merged=True)
        loop, rec, ctx = build_loop(pr_status={"r#1": merged},
                                    close_out_result="unlinked")
        await ctx.set_target_state(
            "r#1", {"session_id": "s1", "branch": "stale-branch",
                    "issues": ["r#1", "r#2"], "claimed": True})

        await loop.tick()

        assert [t for t, _ in rec.close_outs] == ["r#1"]
        st = await ctx.get_target_state("r#1")
        assert st, "state row must survive — the batch is NOT done"
        assert "branch" not in st, "poisoned branch snapshot must be purged"
        assert st.get("unlinked_prs") == [42]
        assert rec.needs_humans == []

        # Next sweep: the denylisted PR reads as "no PR" — close-out must not
        # re-fire (this is the 00:21:30Z→00:22:00Z re-close loop).
        await loop.tick()
        assert len(rec.close_outs) == 1
        assert (await ctx.get_target_state("r#1")), "row still survives"

    asyncio.run(run())


def test_close_out_retry_keeps_row_for_next_sweep(tmp_path):
    """An unverifiable linkage check (gh down) is retried: row untouched, no
    delete, no denylist — the next sweep runs close-out again."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ul2.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()
        merged = PullRequestInfo(number=42, url="", state="merged",
                                 checks="none", title="", merged=True)
        loop, rec, ctx = build_loop(pr_status={"r#1": merged},
                                    close_out_result="retry")
        state = {"session_id": "s1", "branch": "b1",
                 "issues": ["r#1", "r#2"], "claimed": True}
        await ctx.set_target_state("r#1", state)

        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["r#1"]
        assert await ctx.get_target_state("r#1") == state, "row untouched"

        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["r#1", "r#1"], "retried"

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


def test_propose_mode_writes_state_row_before_claim(tmp_path):
    """Propose-mode must write a durable state row (proposed=True) BEFORE calling
    claim — RECORDS-BEFORE-LABELS so a crash after the gh label write leaves a
    recoverable row rather than a row-less orphaned label."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'p1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            mode="propose",
            candidates=[batch("r#1")],
        )
        await loop.tick()

        assert rec.claims, "claim should have been called"
        st = await ctx.get_target_state("r#1")
        assert st.get("claimed") is True
        assert st.get("proposed") is True
        assert st.get("issues") == ["r#1"]

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


def test_propose_mode_deletes_state_row_on_claim_failure(tmp_path):
    """If the claim call fails (lost the race), the speculative state row must be
    deleted — a row with no label is worse than nothing (it holds a slot forever)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'p2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            mode="propose",
            candidates=[batch("r#1")],
            claim_result=False,
        )
        await loop.tick()

        assert rec.claims == []
        st = await ctx.get_target_state("r#1")
        assert st == {}, "speculative row must be deleted on claim failure"

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


def test_propose_mode_row_not_treated_as_dangler(tmp_path):
    """A proposed-mode pending-tap row (claimed=True, proposed=True, no session_id)
    must NOT be treated as a crash-between-claim-and-spawn dangler — reconciliation
    should leave its label intact until the user taps or the runner reconciles."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'p3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="off")
        # A pending-tap proposed row
        await ctx.set_target_state("r#1", {"claimed": True, "proposed": True,
                                            "issues": ["r#1"]})
        # A real crash-between-claim-and-spawn dangler (no proposed, no session_id)
        await ctx.set_target_state("r#2", {"claimed": True, "issues": ["r#2"]})

        await loop.tick()

        assert rec.reconciles, "reconcile seam should run"
        # r#1 (proposed) must be in live_refs — not released
        claimed_refs, live_refs = rec.reconciles[-1]
        assert "r#1" in live_refs, "proposed row must be treated as live"
        assert "r#2" not in live_refs, "dangler must not be in live_refs"

        states = await ctx.list_target_states()
        assert "r#1" in states, "proposed row must survive reconciliation"
        assert "r#2" not in states, "dangling row must be deleted"

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


def test_orphan_sweep_runs_when_mode_propose(tmp_path):
    """Propose mode now writes state rows (proposed=True) before the gh label
    write, so the heal sees propose-mode claims as tracked refs (not orphans)
    and can safely widen its gate to mode != 'off'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="propose", heal_orphans_result=[])
        await loop.tick()
        assert len(rec.heals) == 1

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


# --------------------------------------------------------------------------- #
# Grouping-optimizer refresh gating (_maybe_refresh_groups)                    #
# --------------------------------------------------------------------------- #

def _refresh_recorder(fail: bool = False):
    calls: list = []

    async def refresh(config):
        calls.append(config)
        if fail:
            raise RuntimeError("model down (injected)")
        return 3

    return refresh, calls


def test_group_refresh_runs_on_first_enabled_tick_only(tmp_path):
    """Cold start refreshes immediately; a satisfied loop (batches flowing)
    does NOT refresh again inside the interval."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        refresh, calls = _refresh_recorder()
        loop, rec, ctx = build_loop(
            mode="auto", candidates=[batch("r#1"), batch("r#2")],
            refresh_groups=refresh)
        await loop.tick()
        assert len(calls) == 1          # cold start → refresh before refill
        await loop.tick()
        assert len(calls) == 1          # in-interval, not starved → no refresh

    asyncio.run(run())


def test_group_refresh_nudged_when_starved(tmp_path):
    """select_batch returning None (no live plan group) nudges a refresh on the
    NEXT tick — the wait-for-the-grouper contract."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        refresh, calls = _refresh_recorder()
        loop, rec, ctx = build_loop(mode="auto", candidates=[],
                                    refresh_groups=refresh)
        await loop.tick()               # cold start refresh + starved refill
        assert len(calls) == 1
        assert rec.spawns == []         # nothing selected → the runner WAITED
        await loop.tick()               # the starvation nudge forces a refresh
        assert len(calls) == 2

    asyncio.run(run())


def test_group_refresh_interval_elapsed_refreshes(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        refresh, calls = _refresh_recorder()
        loop, rec, ctx = build_loop(
            mode="auto", clock=clock, candidates=[batch("r#1"), batch("r#2")],
            config=make_config(grouping_interval_minutes=30),
            refresh_groups=refresh)
        await loop.tick()
        assert len(calls) == 1
        clock.advance(30 * 60)
        await loop.tick()
        assert len(calls) == 2

    asyncio.run(run())


def test_group_refresh_failure_backs_off_nudges_and_debounces_event(tmp_path):
    """A failing refresh must NOT be re-driven every tick by the starvation
    nudge (that would hammer the model once per tick); it retries after a SHORT
    fixed cooldown (~2 min, #666) rather than a full grouping interval, and the
    warn event is emitted once, not per failure."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        refresh, calls = _refresh_recorder(fail=True)
        loop, rec, ctx = build_loop(mode="auto", clock=clock, candidates=[],
                                    refresh_groups=refresh)
        await loop.tick()               # cold start → refresh fails
        await loop.tick()               # nudged, but backing off → NO retry
        clock.advance(60)
        await loop.tick()               # 60s < 120s cooldown → still no retry
        assert len(calls) == 1
        clock.advance(70)               # 130s > 120s cooldown → nudge retries
        await loop.tick()               # retry (fails again)
        assert len(calls) == 2

        from sqlalchemy import select
        from server.db.models import AutomationEvent
        async with db_session() as db:
            warns = (await db.execute(select(AutomationEvent).where(
                AutomationEvent.kind == "groups_refresh_failed"))).scalars().all()
        assert len(warns) == 1          # debounced across consecutive failures

    asyncio.run(run())


def test_group_refresh_emits_refreshed_event_on_count_change(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr5.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        refresh, calls = _refresh_recorder()
        loop, rec, ctx = build_loop(mode="auto", clock=clock, candidates=[],
                                    refresh_groups=refresh)
        await loop.tick()
        clock.advance(31 * 60)
        await loop.tick()               # same count (3) → no duplicate event

        from sqlalchemy import select
        from server.db.models import AutomationEvent
        async with db_session() as db:
            events = (await db.execute(select(AutomationEvent).where(
                AutomationEvent.kind == "groups_refreshed"))).scalars().all()
        assert len(events) == 1
        assert events[0].detail == {"groups": 3}

    asyncio.run(run())


def test_group_refresh_skipped_when_mode_off(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr6.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        refresh, calls = _refresh_recorder()
        loop, rec, ctx = build_loop(mode="off", candidates=[batch("r#1")],
                                    refresh_groups=refresh)
        await loop.tick()
        assert calls == []              # a disabled runner never groups

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Spawn-failure backoff + disk-exhaustion circuit breaker (2026-07-21)         #
# --------------------------------------------------------------------------- #

def test_spawn_failure_backs_off_target(tmp_path):
    """A failed spawn is NOT retried on the next tick; the target is retried
    only after the 60s first-failure cooldown elapses (#666)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bo.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts: list[float] = []

        async def failing_spawn(b, account):
            attempts.append(clock())
            raise RuntimeError("git worktree add --detach failed: boom")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1"), batch("r#1"), batch("r#1")],
            spawn=failing_spawn, clock=clock,
        )
        await loop.tick()
        assert len(attempts) == 1
        clock.advance(30)               # < 60s first-failure cooldown
        await loop.tick()
        assert len(attempts) == 1       # still cooling down — no retry churn
        clock.advance(31)               # past the window
        await loop.tick()
        assert len(attempts) == 2       # retried once eligible

    asyncio.run(run())


def test_spawn_backoff_ramps_per_consecutive_failure(tmp_path):
    """Per-target cooldown ramps 60s (1st failure) → 300s (2nd) as a target
    keeps failing (#666). The 3rd consecutive failure trips the GLOBAL spawn
    circuit (covered by ``test_three_consecutive_failures_trip_circuit``); the
    per-target ramp beyond tier 2 is unit-tested in
    ``test_spawn_backoff_delay_schedule`` to keep the two mechanisms decoupled."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bo2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts: list[float] = []

        async def failing_spawn(b, account):
            attempts.append(clock())
            raise RuntimeError("boom")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")] * 5,
            spawn=failing_spawn, clock=clock,
        )
        await loop.tick()                       # fail #1 → wait 60s
        assert len(attempts) == 1
        clock.advance(61)
        await loop.tick()                       # fail #2 → wait 300s
        assert len(attempts) == 2
        clock.advance(61)
        await loop.tick()                       # 61s < 300s: no attempt
        assert len(attempts) == 2
        clock.advance(300)
        await loop.tick()                       # 361s elapsed → attempt #3
        assert len(attempts) == 3

    asyncio.run(run())


def test_spawn_backoff_delay_schedule(monkeypatch):
    """The per-target cooldown schedule: 60s, 300s, then the (configurable) max
    for the 3rd+ consecutive failure, every tier clamped by the max (#666)."""
    from server.automations import backlog_runner as br

    monkeypatch.delenv("CCSWITCH_BACKLOG_MAX_BACKOFF_S", raising=False)
    assert br._spawn_backoff_delay(1) == 60.0
    assert br._spawn_backoff_delay(2) == 300.0
    assert br._spawn_backoff_delay(3) == 900.0     # default max
    assert br._spawn_backoff_delay(4) == 900.0     # plateaus at the max
    assert br._spawn_backoff_delay(10) == 900.0

    # CCSWITCH_BACKLOG_MAX_BACKOFF_S overrides the 3rd+ tier and clamps earlier
    # tiers that would exceed it.
    monkeypatch.setenv("CCSWITCH_BACKLOG_MAX_BACKOFF_S", "120")
    assert br._spawn_backoff_delay(1) == 60.0      # below the cap — unchanged
    assert br._spawn_backoff_delay(2) == 120.0     # 300s tier clamped to the cap
    assert br._spawn_backoff_delay(3) == 120.0

    # A bogus / non-positive override falls back to the default, never 0.
    monkeypatch.setenv("CCSWITCH_BACKLOG_MAX_BACKOFF_S", "not-a-number")
    assert br._spawn_backoff_delay(3) == 900.0
    monkeypatch.setenv("CCSWITCH_BACKLOG_MAX_BACKOFF_S", "0")
    assert br._spawn_backoff_delay(3) == 900.0


def test_spawn_success_clears_backoff(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bo3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        calls = {"n": 0}

        async def flaky_spawn(b, account):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return object()

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")] * 3,
            spawn=flaky_spawn, clock=clock,
        )
        await loop.tick()                       # fails → backoff recorded
        assert loop._spawn_backoff
        clock.advance(61)
        await loop.tick()                       # succeeds
        assert loop._spawn_backoff == {}        # success wipes the slate

    asyncio.run(run())


def test_enospc_spawn_failure_trips_circuit_immediately(tmp_path):
    """A disk-exhaustion error opens the global spawn circuit on the FIRST
    failure — other targets are NOT attempted while it is open."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cb.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts: list[str] = []

        async def enospc_spawn(b, account):
            attempts.append(b.primary)
            raise RuntimeError(
                "git worktree add --detach failed: error: unable to create "
                "file x: No space left on device")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=2),
            candidates=[batch("r#1"), batch("r#2"), batch("r#3")],
            spawn=enospc_spawn, clock=clock,
        )
        await loop.tick()
        assert attempts == ["r#1"]              # circuit opened mid-tick
        clock.advance(60)
        await loop.tick()
        assert attempts == ["r#1"]              # open — no new targets tried
        opened = await ctx.list_events(kind="spawn_circuit_open")
        assert len(opened) == 1                 # alert emitted exactly once
        clock.advance(1800)
        await loop.tick()
        assert len(attempts) == 2               # cooldown over — probe allowed

    asyncio.run(run())


def test_three_consecutive_failures_trip_circuit(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cb2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        attempts: list[str] = []

        async def failing_spawn(b, account):
            attempts.append(b.primary)
            raise RuntimeError("boom")          # no disk marker

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=3),
            candidates=[batch("r#1"), batch("r#2"), batch("r#3"), batch("r#4")],
            spawn=failing_spawn, clock=clock,
        )
        await loop.tick()                       # three distinct targets fail
        assert attempts == ["r#1", "r#2", "r#3"]
        clock.advance(60)
        await loop.tick()
        assert attempts == ["r#1", "r#2", "r#3"]  # circuit open — no attempts

    asyncio.run(run())


def test_spawn_success_resets_consecutive_failures(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cb3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        calls = {"n": 0}

        async def alternating_spawn(b, account):
            calls["n"] += 1
            if calls["n"] % 2:
                raise RuntimeError("boom")
            return object()

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch(f"r#{i}") for i in range(1, 10)],
            spawn=alternating_spawn, clock=clock,
        )
        for _ in range(6):                      # fail, ok, fail, ok, fail, ok
            await loop.tick()
            clock.advance(3700)                 # clear per-target backoffs
        assert loop._spawn_circuit_until is None  # never 3 consecutive fails

    asyncio.run(run())
