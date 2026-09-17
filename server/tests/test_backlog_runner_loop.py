"""BacklogRunnerLoop — the supervised control loop.

Every I/O dependency is INJECTED so the loop runs with no network / no real
sessions. These tests drive the loop entirely through fakes and assert on the
emitted activity feed + the spawn/claim recorders.
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

from server.automations.backlog_selection import Batch
from server.automations.context import AutomationContext
from server.db import db_session, init_db


@pytest.fixture(autouse=True)
def isolated_ownership_identity(monkeypatch):
    """The loop harness has synthetic refs and no GitHub/legacy population."""
    from server.automations.ownership import CanonicalTarget

    async def resolve(refs, *, kind, include_linked_issues=False):
        return tuple(CanonicalTarget("I_" + hashlib.sha256(ref.encode()).hexdigest(), ref)
                     for ref in refs)

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    monkeypatch.setattr("server.automations.ownership_reconciliation.reconcile_legacy_targets",
                        AsyncMock())


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
        "max_concurrent": 2,
        "max_spawns_per_day": 10,
        "max_issues_per_pr": 1,
    }
    cfg.update(over)
    return cfg


# Every batch the real queue hands ``_refill`` carries the slug of a product whose
# dispatch is ON (the queue read is policy-scoped, and ``QueueRow.product`` is not
# optional), so the harness's default batch does too — a product-less batch is a
# state the loop is entitled to refuse.
DEFAULT_PRODUCT = "p1"


def policy(**over) -> dict:
    """A product policy as ``read_policies`` hands it to the loop — the validated
    shape ``effective_policy`` returns, never a hand-rolled subset."""
    from server.automations.product_policy import effective_policy
    return effective_policy({"dispatch": "auto", **over})


async def _seed_row(automation_id: str = "backlog-runner") -> None:
    from server.db.models import Automation
    async with db_session() as db:
        db.add(Automation(id=automation_id))


async def _seed_product(slug: str = DEFAULT_PRODUCT, repo: str = "o/r") -> None:
    """The product a batch's outcome row is attributed to (``project_id``), plus a
    mirror issue so ``record_outcome`` has a body to fingerprint."""
    from server.db.models import Project, ProjectRepo, WorkItem
    async with db_session() as db:
        project = Project(slug=slug, name=slug)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo, is_primary=True))
        db.add(WorkItem(repo=repo, number=1, kind="issue", title="T", body="B"))


async def _outcomes(target: str) -> list:
    """Every dispatch-outcome row for *target*, oldest first."""
    from sqlalchemy import select

    from server.db.models import DispatchOutcome
    async with db_session() as db:
        return list((await db.execute(select(DispatchOutcome).where(
            DispatchOutcome.target_ref == target)
            .order_by(DispatchOutcome.created_at))).scalars().all())


async def _set_owned_state(ctx, target, state):
    """Seed current ownership metadata for policy tests, retaining existing tokens."""
    from server.automations.ownership import acquire
    from server.automations.target_identity import resolve_targets

    if ctx.automation_id != "backlog-runner":
        await ctx.set_target_state(target, state)
        return
    previous = await ctx.get_target_state(target)
    if previous.get("ownership_token"):
        state = {**state, "ownership_token": previous["ownership_token"]}
        await ctx.set_target_state(target, state)
        return
    targets = await resolve_targets(state.get("issues") or [target], kind="issue")
    async with acquire(targets, owner="backlog-runner", source="test", reason="fixture",
                       provenance={"origin_ref": target}) as tx:
        token = tx.token
    await ctx.set_target_state(target, {**state, "ownership_token": token})


async def _mark_session_ended(ctx, target: str, *, attempts: int | None = None) -> None:
    """Flip the claim row to "the session is over" (and optionally park it one
    attempt below the cap), the way a real ``session_ended`` probe would."""
    st = await ctx.get_target_state(target)
    st["_ended"] = True
    if attempts is not None:
        st["attempts"] = attempts
    await _set_owned_state(ctx, target, st)


def build_loop(*, mode="on", config=None, clock=None,
               candidates=None, healthy="a", in_flight_counter=None,
               pr_status=None, pr_doctor_active=True, announce=None,
               candidate_count=None, blocked_count=0, untriaged_count=0,
               per_product=None, count_read_failed=False,
               policies=None, seed_policies=None,
               refresh_groups=None, spawn=None, close_out_result=None,
               credential_alerts=None, verify_ready=None, queue_tail=None,
               wall_clock=None, needs_human_raises=False,
               spawn_result=None, session_ran=None, pr_doctor_attempts=None,
               pr_doctor_attempts_fn=None, session_gone=None):
    """Construct a BacklogRunnerLoop over recorder fakes.

    Returns (loop, recorders) where recorders exposes .spawns / .announced and
    lets tests mutate the candidate queue + in-flight count.
    """
    from server.automations.backlog_runner import BacklogRunnerLoop

    cfg = config if config is not None else make_config()
    clk = clock or FakeClock()
    ctx = AutomationContext("backlog-runner")

    class Recorders:
        def __init__(self):
            self.spawns: list = []
            self.announced: list = []
            self.needs_humans: list = []
            self.close_outs: list = []
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

    async def _default_announce(batch):
        rec.announced.append(batch)

    announce_fn = announce or _default_announce

    async def _default_spawn(batch, account, **_kw):
        rec.spawns.append((batch, account))
        rec.in_flight += 1
        # ``spawn_result`` stands in for the SessionRecord a real spawn returns —
        # the outcome-history tests need its id/model; every other test is happy
        # with the opaque default.
        return spawn_result if spawn_result is not None else object()

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

    async def needs_human(target, reason):
        rec.needs_humans.append((target, reason))
        if needs_human_raises:
            raise RuntimeError("gh comment failed (injected)")

    async def close_out(target, info):
        rec.close_outs.append((target, info))
        return close_out_result

    async def count_candidates():
        # The seam reports (eligible, blocked, untriaged, per_product) — the
        # dependency-gated and below-min-status skips this sweep, plus one row per
        # product, all surfaced on the Automation card. ``per_product is None`` is
        # the seam's own "the queue read FAILED" answer, distinct from ``{}``.
        return (candidate_count if candidate_count is not None else 0,
                blocked_count, untriaged_count,
                None if count_read_failed else (per_product or {}))

    async def session_ran_probe(_sid):
        # Did the session ever run a turn? Wired only when a test says so —
        # unwired, ``_session_ever_ran`` fails safe to True.
        return bool(session_ran)

    async def pr_doctor_attempts_for(pr_ref):
        # {``repo#N``: attempts}; a ref the map does not carry is UNKNOWN (None),
        # which is exactly what a cleared PR Doctor row reads as. A test that
        # wants the REAL seam (the orchestrator method over PR Doctor's own state
        # rows) passes it as ``pr_doctor_attempts_fn`` instead of a map.
        return (pr_doctor_attempts or {}).get(pr_ref)

    attempts_seam = pr_doctor_attempts_fn or (
        pr_doctor_attempts_for if pr_doctor_attempts is not None else None)

    async def read_policies():
        # The harness's default product, matching production: every batch the
        # queue produces carries the slug of a product whose dispatch is ON, so a
        # test that says nothing about policies gets exactly that. Its
        # ``max_in_flight`` is deliberately huge — the fleet-wide
        # ``max_concurrent`` is what most of these tests are about, and the
        # per-product ceiling has its own tests below.
        if policies is not None:
            return dict(policies)
        return {DEFAULT_PRODUCT: policy(max_in_flight=1000)}

    async def admission(batch, *, source, existing_slot, existing_token=None):
        from server.automations.backlog_runner import in_flight_by_product
        from server.automations.product_policy import decide_implementation, effective_policy
        from server.pm.repo_project import RepoOwner, RepoOwnership
        policies_now = await read_policies()
        current = effective_policy(policies_now.get(batch.product))
        owner = RepoOwner(batch.product or "missing", batch.product or "missing")
        coverage = RepoOwnership(batch.repo, batch.repo or "o/r", "unique", owner,
                                 (owner,), (), "fixture verified ownership")
        counts = in_flight_by_product(await ctx.list_target_states())
        decision = decide_implementation(
            ownership=coverage, requested_product_slug=batch.product, policy=current,
            trigger="automatic" if source == "automatic" else "manual",
            effect="proposal" if source == "automatic" and current["dispatch"] == "propose"
            else "implementation", runner_mode=mode,
            global_count=await count_in_flight(), global_cap=cfg["max_concurrent"],
            product_count=counts.get(batch.product, 0), daily_count=len(loop._spawns_today),
            daily_cap=cfg["max_spawns_per_day"], observed_at="fixture", existing_slot=existing_slot)
        return decision, current

    loop = BacklogRunnerLoop(
        ctx,
        select_batch=select_batch,
        announce=announce_fn,
        spawn=spawn,
        count_in_flight=count_in_flight,
        healthy_account=healthy_account,
        pr_status_for=pr_status_for,
        pr_doctor_active=pr_doctor_active_fn,
        read_config=read_config,
        read_policies=read_policies,
        seed_policies=seed_policies,
        admission=admission,
        clock=clk,
        needs_human=needs_human,
        session_ran=session_ran_probe if session_ran is not None else None,
        pr_doctor_attempts_for=attempts_seam,
        close_out=close_out,
        count_candidates=count_candidates if candidate_count is not None else None,
        refresh_groups=refresh_groups,
        credential_alerts=credential_alerts,
        verify_ready=verify_ready,
        queue_tail=queue_tail,
        wall_clock=wall_clock,   # None → the loop's real UTC clock
        session_gone=session_gone,
    )
    # These policy/outcome tests model an observed stopped actor. Native proof
    # and unknown lifecycles are exercised by test_backlog_ownership.py.
    loop._work_stopped = AsyncMock(return_value=True)

    async def handoff(target, st, _info):
        return await ctx.compare_target_state(target, st, {**st, "handed_off": True})

    loop._handoff = handoff
    return loop, rec, ctx


def batch(primary: str, issues=None, product=DEFAULT_PRODUCT) -> Batch:
    return Batch(primary=primary, issues=issues or [primary], product=product)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_eligible_reflects_candidate_count_even_when_saturated(tmp_path):
    from types import SimpleNamespace

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'elig.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            spawn_result=SimpleNamespace(id="live-session"),
            candidate_count=7,          # GitHub says 7 ready issues exist
        )
        await loop.tick()               # fills the single slot
        counts = getattr(ctx, "backlog_counts", {})
        assert counts.get("eligible") == 7
        assert counts.get("in_flight") == 1

    asyncio.run(run())


def test_blocked_count_surfaces_on_ctx(tmp_path):
    """Dependency-gated skips must be visible on the Automation card — the
    'why isn't the backlog draining' note — not just in the log."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'blocked.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("r#1")],
            candidate_count=2,
            blocked_count=3,            # 3 issues skipped as dependency-blocked
        )
        await loop.tick()
        counts = getattr(ctx, "backlog_counts", {})
        assert counts.get("blocked") == 3
        assert counts.get("eligible") == 2

    asyncio.run(run())


def test_untriaged_count_surfaces_on_ctx(tmp_path):
    """Below-min-status skips are the OTHER "why isn't the backlog draining"
    answer: in the default Ready mode a board of untriaged items would read as a
    bare "0 eligible" — an empty backlog, not one waiting on triage (#990)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'untriaged.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[],
            candidate_count=0,
            untriaged_count=4,          # 4 board items still sit below Ready
        )
        await loop.tick()
        counts = getattr(ctx, "backlog_counts", {})
        assert counts.get("untriaged") == 4
        assert counts.get("eligible") == 0

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
            return "on", cfg

        async def read_policies():
            return {DEFAULT_PRODUCT: policy(max_in_flight=1000)}

        cand = [batch("r#1"), batch("r#2")]

        async def select_batch(_c):
            return cand.pop(0) if cand else None

        async def spawn(b, account, **_kw):
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
            ctx, select_batch=select_batch, spawn=spawn,
            count_in_flight=count_in_flight, healthy_account=healthy_account,
            pr_status_for=pr_status_for, pr_doctor_active=pr_doctor_active,
            read_config=read_config, read_policies=read_policies,
            clock=FakeClock(),
            admission=AsyncMock(return_value=(
                SimpleNamespace(permitted=True, product_slug=DEFAULT_PRODUCT,
                                                   configured_dispatch="auto"), policy())),
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


# --------------------------------------------------------------------------- #
# A refill that makes no claim says why (2026-09-16: 13 hours of nothing but   #
# ``groups_refreshed`` while six dead claims held every slot).                 #
# --------------------------------------------------------------------------- #

async def _seed_legacy_claim(ctx, target: str, *, session_id: str | None = "gone-sid") -> None:
    """A pre-ownership claim (#1648): ``claimed`` + ``session_id`` and NO
    ``ownership_token`` — the exact row shape found in prod, written before the
    ownership store existed."""
    state = {"repo": "o/r", "claimed": True, "attempts": 0, "issues": [target],
             "claimed_at": "2026-09-07T10:22:57+00:00"}
    if session_id is not None:
        state["session_id"] = session_id
    await ctx.set_target_state(target, state)


def test_refill_at_capacity_emits_stalled_once_naming_the_slot_holders(tmp_path):
    """Every slot held → one ``refill_stalled`` warn (reason=capacity) that names
    the rows holding the slots and flags the token-less legacy ones, debounced
    until the reason changes or a claim is attempted."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'stall1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        occupied = 2

        async def in_flight():
            return occupied

        loop, rec, ctx = build_loop(candidates=[batch("o/r#1"), batch("o/r#2")],
                                    in_flight_counter=in_flight,
                                    config=make_config(max_concurrent=2))
        await _seed_legacy_claim(ctx, "o/r#9")
        await loop.tick()
        await loop.tick()                      # same reason → no second event
        assert rec.spawns == []
        events = await ctx.list_events(kind="refill_stalled")
        assert len(events) == 1
        ev = events[0]
        assert ev["level"] == "warn"
        assert ev["detail"]["reason"] == "capacity"
        assert ev["detail"]["in_flight"] == 2 and ev["detail"]["max_concurrent"] == 2
        holders = {h["target"]: h for h in ev["detail"]["holders"]}
        assert holders["o/r#9"]["legacy"] is True
        assert holders["o/r#9"]["session_id"] == "gone-sid"
        assert "2/2" in ev["summary"]

        occupied = 0                           # both slots free → both candidates claimed
        await loop.tick()
        assert len(rec.spawns) == 2
        occupied = 2                           # …and a fresh stall is reported again
        await loop.tick()
        assert len(await ctx.list_events(kind="refill_stalled")) == 2

    asyncio.run(run())


def test_refill_with_planned_groups_but_nothing_claimable_emits_stalled(tmp_path):
    """A non-empty plan whose groups never intersect the live pool used to be
    silent forever (the starvation nudge just re-ran the grouper); an EMPTY plan
    is an idle backlog, not a stall, and stays quiet."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'stall2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        refresh, calls = _refresh_recorder()   # plans 3 groups
        loop, rec, ctx = build_loop(candidates=[], refresh_groups=refresh)
        await loop.tick()
        await loop.tick()
        events = await ctx.list_events(kind="refill_stalled")
        assert len(events) == 1
        assert events[0]["detail"] == {"reason": "no_batch", "groups_planned": 3}
        assert "3 group(s)" in events[0]["summary"]

        async def empty_plan(_config):
            return 0

        # Same DB and automation id, so the feed still holds the ONE event above.
        loop2, _rec2, ctx2 = build_loop(candidates=[], refresh_groups=empty_plan)
        await loop2.tick()
        assert len(await ctx2.list_events(kind="refill_stalled")) == 1

    asyncio.run(run())


def test_refill_stalled_on_a_cooling_target_names_it(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'stall3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        loop, rec, ctx = build_loop(candidates=[batch("o/r#1"), batch("o/r#1")], clock=clock)
        loop._spawn_backoff["o/r#1"] = (1, clock() + 600)
        await loop.tick()
        events = await ctx.list_events(kind="refill_stalled")
        assert len(events) == 1
        assert events[0]["target"] == "o/r#1"
        assert events[0]["detail"]["reason"] == "target_cooldown"
        assert events[0]["detail"]["retry_in_s"] == 600

    asyncio.run(run())


def test_legacy_claim_is_released_once_its_session_is_gone(tmp_path):
    """The prod shape: a pre-ownership row whose session row no longer exists.
    ``compare_target_state`` refuses token-less rows and nothing else reconciled
    them, so it held a slot forever. With the ``session_gone`` seam wired, the
    sweep discharges it (warn event, row deleted) and the next refill can claim."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        from server.automations.backlog_runner import is_in_flight

        async def in_flight_from_rows():
            return sum(is_in_flight(s) for s in (await ctx.list_target_states()).values())

        async def session_gone(sid):
            return sid == "gone-sid"

        loop, rec, ctx = build_loop(candidates=[batch("o/r#1")],
                                    in_flight_counter=in_flight_from_rows,
                                    config=make_config(max_concurrent=1),
                                    session_gone=session_gone)
        await _seed_legacy_claim(ctx, "o/r#9")
        await loop.tick()                      # refill stalls on the zombie; sweep discharges it
        assert await ctx.get_target_state("o/r#9") == {}
        released = await ctx.list_events(kind="legacy_claim_released")
        assert len(released) == 1
        assert released[0]["level"] == "warn"
        assert released[0]["target"] == "o/r#9"
        assert released[0]["detail"]["session_id"] == "gone-sid"
        await loop.tick()                      # the slot is free → the claim goes through
        assert [b.primary for b, _ in rec.spawns] == ["o/r#1"]

    asyncio.run(run())


def test_legacy_claim_with_no_session_id_is_released(tmp_path):
    """A pre-ownership claim that never recorded a session (an account wait or a
    crashed launcher) has nothing running to protect."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def session_gone(_sid):
            raise AssertionError("no session id → the probe must not be asked")

        loop, rec, ctx = build_loop(candidates=[], session_gone=session_gone)
        await _seed_legacy_claim(ctx, "o/r#9", session_id=None)
        await loop.tick()
        assert await ctx.get_target_state("o/r#9") == {}
        assert len(await ctx.list_events(kind="legacy_claim_released")) == 1

    asyncio.run(run())


def test_legacy_claim_is_kept_while_its_session_is_alive_or_the_seam_is_unwired(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def still_running(_sid):
            return False

        loop, rec, ctx = build_loop(candidates=[], session_gone=still_running)
        await _seed_legacy_claim(ctx, "o/r#9")
        before = await ctx.get_target_state("o/r#9")
        await loop.tick()
        assert await ctx.get_target_state("o/r#9") == before
        assert await ctx.list_events(kind="legacy_claim_released") == []

        unwired, _rec, ctx2 = build_loop(candidates=[])   # session_gone=None
        await _seed_legacy_claim(ctx2, "o/r#8")
        await unwired.tick()
        assert (await ctx2.get_target_state("o/r#8")).get("claimed") is True

    asyncio.run(run())


def test_legacy_release_never_touches_a_row_that_holds_no_slot(tmp_path):
    """A token-less ``handed_off`` row belongs to PR Doctor, not to a slot. The
    sweep skips ``gave_up`` itself, but ``handed_off`` reaches the release path,
    so the release is scoped by the shared ``is_in_flight`` predicate — deleting
    one would yank a target out from under the other automation for no gain."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy5.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def session_gone(_sid):
            return True

        loop, _rec, ctx = build_loop(candidates=[], session_gone=session_gone)
        await ctx.set_target_state("o/r#9", {"repo": "o/r", "handed_off": True,
                                             "issues": ["o/r#9"], "session_id": "gone-sid"})
        before = await ctx.get_target_state("o/r#9")
        await loop.tick()
        assert await ctx.get_target_state("o/r#9") == before
        assert await ctx.list_events(kind="legacy_claim_released") == []

    asyncio.run(run())


def test_legacy_release_never_touches_a_token_holding_row(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(candidates=[])
        await _set_owned_state(ctx, "o/r#9", {"repo": "o/r", "claimed": True, "attempts": 0,
                                              "issues": ["o/r#9"], "session_id": "s1"})
        owned = await ctx.get_target_state("o/r#9")
        assert owned.get("ownership_token")
        assert await ctx.release_legacy_target_state("o/r#9", owned) is False
        assert await ctx.get_target_state("o/r#9") == owned

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
        assert [t for t, _ in rec.needs_humans] == ["r#1"]  # GIVE-UP seam called
        gave = await ctx.list_events(kind="gave_up")
        assert len(gave) >= 1
        st = await ctx.get_target_state("r#1")
        # Phase D: the row SURVIVES, stamped — it is the needs-human signal.
        assert st["gave_up"] is True and st["needs_human_at"]
        assert "pr-doctor gave up" in st["reason"]

    asyncio.run(run())


def test_give_up_keeps_a_row_with_needs_human_at(tmp_path):
    """Phase D: exhausting ``max_attempts`` no longer deletes the row and writes a
    label — it keeps the row, stamped with the wall-clock time of the give-up and
    the reason. That row IS the needs-human signal (queue cooldown, board Status)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gu1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        loop, rec, ctx = build_loop(candidates=[], pr_status={},
                                    wall_clock=lambda: at,
                                    config=make_config())
        # A spawned target whose session ended with no PR, one attempt short of max.
        await _set_owned_state(ctx, "o/r#7", {
            "session_id": "s1", "repo": "o/r", "issues": ["o/r#7", "o/r#8"],
            "claimed": True, "attempts": 1, "_ended": True})

        await loop.tick()          # attempts → 2 == max → give up

        st = await ctx.get_target_state("o/r#7")
        assert st["gave_up"] is True and st["needs_human_at"] == at.isoformat()
        assert st["issues"] == ["o/r#7", "o/r#8"] and st["repo"] == "o/r"
        assert "max attempts" in st["reason"]
        assert rec.needs_humans == [("o/r#7", st["reason"])]
        # A give-up row is terminal, not a claim: it must not read as in flight.
        assert not st.get("claimed")

    asyncio.run(run())


def test_give_up_row_is_stamped_even_when_the_announce_fails(tmp_path):
    """The announce is a best-effort side channel (Task 3 makes it a real ``gh``
    comment); the ROW is the signal. If the announce raises after the row were
    written second, the sweep's per-target guard would swallow it and the target
    would sit claimed at max attempts, holding a slot and re-emitting ``gave_up``
    every sweep. Row first, announce guarded."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gu2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        loop, rec, ctx = build_loop(candidates=[], pr_status={},
                                    wall_clock=lambda: at, needs_human_raises=True,
                                    config=make_config())
        await _set_owned_state(ctx, "o/r#7", {
            "session_id": "s1", "repo": "o/r", "issues": ["o/r#7"],
            "claimed": True, "attempts": 1, "_ended": True})

        await loop.tick()          # must not raise out of the sweep

        st = await ctx.get_target_state("o/r#7")
        assert st["gave_up"] is True and st["needs_human_at"] == at.isoformat()
        assert not st.get("claimed")
        assert len(rec.needs_humans) == 1
        # Second sweep: the row is terminal, so no repeat give-up / announce.
        await loop.tick()
        assert len(rec.needs_humans) == 1
        assert len(await ctx.list_events(kind="gave_up")) == 1

    asyncio.run(run())


def test_sweep_expires_gave_up_rows_after_retry_days(tmp_path):
    """The runner owns the cooldown's end: once ``needs_human_retry_days`` has
    passed it deletes the row, which is what returns the issue to the pool."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        loop, _rec, ctx = build_loop(candidates=[],
                                     wall_clock=lambda: at + timedelta(days=8),
                                     config=make_config(needs_human_retry_days=7))
        await _set_owned_state(ctx, "o/r#7", {"repo": "o/r", "issues": ["o/r#7"],
                                             "gave_up": True,
                                             "needs_human_at": at.isoformat(),
                                             "reason": "x"})

        await loop.tick()

        assert await ctx.get_target_state("o/r#7") == {}
        events = await ctx.list_events(kind="needs_human_expired")
        assert len(events) == 1

        # retry_days = 0 → permanent handoff: never expired.
        await _set_owned_state(ctx, "o/r#8", {"repo": "o/r", "issues": ["o/r#8"],
                                             "gave_up": True,
                                             "needs_human_at": at.isoformat(),
                                             "reason": "x"})
        loop2, _, ctx2 = build_loop(candidates=[],
                                    wall_clock=lambda: at + timedelta(days=800),
                                    config=make_config(needs_human_retry_days=0))
        await loop2.tick()
        assert (await ctx2.get_target_state("o/r#8"))["gave_up"] is True

        # An undated (or unreadable) stamp expires IMMEDIATELY while retrying is
        # on: the queue already treats it as no cooldown, so a row the sweep kept
        # would park the issue in the card's needs-human gauge forever while it
        # was in fact being picked again.
        await _set_owned_state(ctx, "o/r#9", {"repo": "o/r", "issues": ["o/r#9"],
                                             "gave_up": True,
                                             "needs_human_at": "not-a-date"})
        loop3, _, ctx3 = build_loop(candidates=[],
                                    wall_clock=lambda: at,
                                    config=make_config(needs_human_retry_days=7))
        await loop3.tick()
        assert await ctx3.get_target_state("o/r#9") == {}

    asyncio.run(run())


def test_gave_up_row_inside_the_window_is_left_alone(tmp_path):
    """Still cooling down: no delete, no expiry event, and the sweep must not
    fall through to the give-up bookkeeping for a row that already gave up."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        loop, rec, ctx = build_loop(candidates=[], pr_status={},
                                    wall_clock=lambda: at + timedelta(days=3),
                                    config=make_config(needs_human_retry_days=7))
        row = {"repo": "o/r", "issues": ["o/r#7"], "gave_up": True,
               "needs_human_at": at.isoformat(), "reason": "x"}
        await _set_owned_state(ctx, "o/r#7", row)

        await loop.tick()

        assert {k: v for k, v in (await ctx.get_target_state("o/r#7")).items()
                if k != "ownership_token"} == row
        assert await ctx.list_events(kind="needs_human_expired") == []
        assert rec.needs_humans == []

    asyncio.run(run())


def test_expiry_runs_before_refill_so_the_tick_that_frees_a_ref_spawns_it(tmp_path):
    """Expiry must happen BEFORE refill, not in the sweep that follows it.

    On the tick a cooldown passes, the queue already offers the ref: selection
    consumes its plan group, and with the row still present ``_refill``'s
    ``gave_up`` guard would discard the pick — the group burned until the next
    grouping refresh, the slot idle. One tick has to do both."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'exp_first.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#7")],
            wall_clock=lambda: at + timedelta(days=8),
            config=make_config(needs_human_retry_days=7))
        await _set_owned_state(ctx, "o/r#7", {"repo": "o/r", "issues": ["o/r#7"],
                                             "gave_up": True,
                                             "needs_human_at": at.isoformat(),
                                             "reason": "x"})

        await loop.tick()

        assert len(await ctx.list_events(kind="needs_human_expired")) == 1
        assert [b.primary for b, _acct in rec.spawns] == ["o/r#7"]
        # The consumed plan group produced the spawn rather than being discarded.
        assert rec.candidates == []
        assert [b.primary for b in rec.announced] == ["o/r#7"]

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
        await _set_owned_state(ctx,
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
        await _set_owned_state(ctx,
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


def test_close_out_unknown_keeps_exclusive_claim_without_replay(tmp_path):
    """An ambiguous close-out keeps ownership and needs explicit reconciliation."""
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
        await _set_owned_state(ctx, "r#1", state)

        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["r#1"]
        assert {k: v for k, v in (await ctx.get_target_state("r#1")).items()
                if k not in {"ownership_token", "closing_pr", "closeout_started",
                             "ownership_wait_reason"}} == state, "claim retained"

        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["r#1"], "unknown external work is not replayed"

    asyncio.run(run())


def test_close_out_escalated_leaves_a_gave_up_row(tmp_path):
    """The human gate fired for every issue (#784): the PR merged but a human has
    to finish and close the work. That is a handoff, not a completion — the row
    must survive as a stamped ``gave_up`` row (Phase D's needs-human signal),
    never be deleted as if the batch were done."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'esc.db'}"

    async def run():
        from server.github.pr_status import PullRequestInfo
        await init_db(db_url=db_url)
        await _seed_row()
        at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        merged = PullRequestInfo(number=42, url="", state="merged",
                                 checks="none", title="", merged=True)
        loop, rec, ctx = build_loop(pr_status={"r#1": merged},
                                    close_out_result="escalated",
                                    wall_clock=lambda: at)
        await _set_owned_state(ctx, "r#1", {"session_id": "s1", "repo": "r",
                                           "issues": ["r#1"], "claimed": True})

        await loop.tick()

        st = await ctx.get_target_state("r#1")
        assert st["gave_up"] is True and st["needs_human_at"] == at.isoformat()
        assert "human gate" in st["reason"]
        assert not st.get("claimed")
        assert [t for t, _ in rec.needs_humans] == ["r#1"]

    asyncio.run(run())


def test_announce_failure_never_blocks_the_spawn(tmp_path):
    """The claim is the durable row; the announce is a best-effort ``gh``
    comment. A comment that cannot be posted must never cost a spawn."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ann.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def raising_announce(_batch):
            raise RuntimeError("gh comment failed (injected)")

        loop, rec, ctx = build_loop(candidates=[batch("o/r#7")],
                                    announce=raising_announce)
        await loop.tick()

        assert len(rec.spawns) == 1
        assert await ctx.list_events(kind="unclaimed") == []

    asyncio.run(run())


def test_reconcile_retains_unresolved_claim_rows(tmp_path):
    """A missing session ID cannot prove a remote launch never started."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="off")
        await _set_owned_state(ctx, "r#1", {"claimed": True})                    # dangler
        await _set_owned_state(ctx, "r#2", {"claimed": True, "session_id": "s2"})  # live

        await loop.tick()

        states = await ctx.list_target_states()
        assert "r#1" in states, "missing session is not proof a launch never started"
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


def test_propose_dispatch_writes_state_row_before_claim(tmp_path):
    """A ``propose`` PRODUCT must write a durable state row (proposed=True) BEFORE
    calling claim — RECORDS-BEFORE-ANYTHING, so a crash after the announce leaves a
    recoverable row rather than a row-less orphaned comment."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'p1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            candidates=[batch("r#1", product="p1")],
            policies={"p1": policy(dispatch="propose")},
        )
        await loop.tick()

        assert rec.announced, "the claim announce should have been called"
        assert rec.spawns == [], "propose must not spawn"
        st = await ctx.get_target_state("r#1")
        assert st.get("claimed") is True
        assert st.get("proposed") is True
        assert st.get("issues") == ["r#1"]
        assert st.get("product") == "p1"

    asyncio.run(run())


def test_dispatch_is_per_product_propose_announces_auto_spawns(tmp_path):
    """One global mode, two products: the ``auto`` one gets a session, the
    ``propose`` one gets a pending-tap claim — on the SAME tick."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'perproduct.db'}"

    async def run():
        from types import SimpleNamespace
        await init_db(db_url=db_url)
        await _seed_row()
        spawned: list = []

        async def spawn(b, account, **_kw):
            spawned.append((b, account))
            # A real record: a spawn whose row never records a session id is the
            # dangling claim reconciliation deletes at the end of the tick.
            return SimpleNamespace(id=f"sess-{b.primary}", branch=None)

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=2), spawn=spawn,
            candidates=[batch("r#1", product="p1"), batch("k#2", product="p2")],
            policies={"p1": policy(dispatch="propose"), "p2": policy(dispatch="auto")},
        )
        await loop.tick()
        rec.spawns = spawned

        assert [b.primary for b, _acct in rec.spawns] == ["k#2"]
        assert (await ctx.get_target_state("r#1")).get("proposed") is True
        assert (await ctx.get_target_state("k#2")).get("proposed") is None
        # The claim records which product owns it — the sweep reads its policy back.
        assert (await ctx.get_target_state("k#2")).get("product") == "p2"

    asyncio.run(run())


def test_dispatch_off_skips_the_batch_and_keeps_serving_other_products(tmp_path):
    """A product flipped OFF between the queue read and the claim takes no claim —
    and the skip is a ``continue``, not a ``break``: another product's batch is
    still perfectly workable on the same tick."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'dispatchoff.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=2),
            candidates=[batch("r#1", product="p-off"), batch("k#2", product="p-on")],
            policies={"p-off": policy(dispatch="off"), "p-on": policy()},
        )
        await loop.tick()

        assert [b.primary for b, _a in rec.spawns] == ["k#2"]
        assert [b.primary for b in rec.announced] == ["k#2"]
        assert await ctx.get_target_state("r#1") == {}     # no claim at all
        assert len(await ctx.list_events(kind="skipped_dispatch_off")) == 1

    asyncio.run(run())


def test_an_unknown_product_is_never_auto_spawned(tmp_path):
    """The dispatch fallback is fail-SAFE: a batch whose product the snapshot does
    not know resolves to the pure schema defaults (dispatch ``off``), so an
    unattended session is never started on a guess."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'unknownproduct.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            candidates=[batch("r#1", product="ghost")], policies={})
        await loop.tick()

        assert rec.spawns == [] and rec.announced == []
        assert await ctx.get_target_state("r#1") == {}

    asyncio.run(run())


def test_a_failed_policy_read_with_no_snapshot_skips_refill_but_still_sweeps(tmp_path):
    """Guessing dispatch either spawns for a product whose operator said "ask me
    first" or refuses one that said "go", so a first-tick read failure skips
    refill entirely. The SWEEP still runs — stalling the work that frees slots and
    escalates stuck targets on a DB blip is its own outage."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'policyreadfail.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def boom():
            raise RuntimeError("policy read down (injected)")

        loop, rec, ctx = build_loop(candidates=[batch("r#1")], candidate_count=3)
        loop._read_policies = boom
        selections: list[dict] = []

        async def counting_select(config):
            selections.append(config)
            return batch("r#1")
        loop._select_batch = counting_select
        # A dangling claim row (claimed, no session id, no proposed flag): the
        # sweep's reconciliation is what deletes it, so its absence afterwards
        # proves the sweep half of the tick ran.
        await _set_owned_state(ctx, "o/x#9", {"repo": "o/x", "claimed": True,
                                             "issues": ["o/x#9"]})

        await loop.tick()

        assert rec.spawns == [] and rec.announced == []
        # SKIPPED, not "ran and refused everything": the candidate/plan seam is
        # never touched, so no plan group is consumed and no pick is burnt. Without
        # this, the same visible outcome (no spawn, no announce) is also what a
        # refill that ran on guessed policies produces — every batch falling
        # through the fail-safe dispatch-off branch, one consumed group each.
        assert selections == []
        assert await ctx.list_events(kind="skipped_dispatch_off") == []
        assert (await ctx.get_target_state("o/x#9"))["claimed"] is True  # unknown launch retained
        assert getattr(ctx, "backlog_counts", {}).get("eligible") == 3

    asyncio.run(run())


def test_a_failed_policy_read_falls_back_to_the_last_good_snapshot(tmp_path):
    """Policies change rarely, so a snapshot one tick old is far better than none:
    a read failure AFTER a good tick keeps working from what it knows."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'policysnapshot.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        fail = {"now": False}

        async def maybe_boom():
            if fail["now"]:
                raise RuntimeError("policy read down (injected)")
            return {"p1": policy(max_in_flight=1000)}

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=5),
            candidates=[batch("r#1", product="p1")])
        loop._read_policies = maybe_boom

        await loop.tick()
        assert [b.primary for b, _a in rec.spawns] == ["r#1"]

        fail["now"] = True
        rec.candidates.append(batch("r#2", product="p1"))
        await loop.tick()
        assert [b.primary for b, _a in rec.spawns] == ["r#1", "r#2"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# max_in_flight is a PRODUCT ceiling, enforced in refill                       #
# --------------------------------------------------------------------------- #

def test_max_in_flight_caps_one_product_while_another_keeps_going(tmp_path):
    """The cap is per PRODUCT and skips only that product's batch — the freed slot
    goes to a product still under its own ceiling, never idles on its account."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'prodcap.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=4),
            candidates=[batch("a#1", product="capped"), batch("a#2", product="capped"),
                        batch("b#3", product="roomy")],
            policies={"capped": policy(max_in_flight=1),
                      "roomy": policy(max_in_flight=5)})

        await loop.tick()

        assert [b.primary for b, _a in rec.spawns] == ["a#1", "b#3"]
        assert len(await ctx.list_events(kind="skipped_product_cap")) == 1

    asyncio.run(run())


def test_max_in_flight_is_one_ceiling_across_all_of_a_products_repos(tmp_path):
    """The bug the per-repo aliasing had: a product owning N repos ran N × its cap
    while the card (product-aggregated) showed a number nothing enforced."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'prodcapnrepos.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=9),
            candidates=[batch(f"repo{i}#1", product="multi") for i in range(4)],
            policies={"multi": policy(max_in_flight=2)})

        await loop.tick()

        assert len(rec.spawns) == 2

    asyncio.run(run())


def test_a_zero_max_in_flight_pauses_the_product(tmp_path):
    """``max_in_flight: 0`` is the PAUSED setting (schema ``min=0``), not an
    "unlimited" sentinel: the product keeps the rest of its policy and takes no
    claim. The guard that exempted ``cap <= 0`` from the ceiling made 0 — and any
    hand-edited negative — mean the exact opposite of what it says."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'zerocap.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=4),
            candidates=[batch("a#1", product="paused"), batch("b#2", product="roomy")],
            policies={"paused": policy(max_in_flight=0),
                      "roomy": policy(max_in_flight=5)})

        await loop.tick()

        assert [b.primary for b, _a in rec.spawns] == ["b#2"]
        assert await ctx.get_target_state("a#1") == {}       # no claim at all
        events = await ctx.list_events(kind="skipped_product_cap")
        assert len(events) == 1 and "cap (0)" in events[0]["summary"]

    asyncio.run(run())


def test_the_spawn_gets_the_products_model_from_this_ticks_snapshot(tmp_path):
    """The session model travels WITH the claim: the loop hands the spawn seam the
    ``default_model`` off the same policy snapshot its dispatch decision was made
    on, instead of the spawn site re-reading policies and possibly getting a
    different (or failed, i.e. ``auto``) answer."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'spawnmodel.db'}"

    async def run():
        seen: list = []

        async def spawn(b, account, *, default_model, ownership_token):
            seen.append((b.primary, default_model))
            return object()

        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=4), spawn=spawn,
            candidates=[batch("a#1", product="pinned"), batch("b#2", product="assessed")],
            policies={"pinned": policy(default_model="opus"),
                      "assessed": policy(default_model="auto")})

        await loop.tick()

        assert seen == [("a#1", "opus"), ("b#2", "auto")]

    asyncio.run(run())


def test_a_proposed_claim_holds_its_products_slot(tmp_path):
    """A ``proposed`` row is a claim awaiting a human tap; it holds the product's
    slot exactly like a spawned session, or a propose-mode product would claim its
    whole backlog on one tick."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'proposecap.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=4),
            candidates=[batch("a#1", product="p1"), batch("a#2", product="p1")],
            policies={"p1": policy(dispatch="propose", max_in_flight=1)})

        await loop.tick()

        assert [b.primary for b in rec.announced] == ["a#1"]
        assert await ctx.get_target_state("a#2") == {}

    asyncio.run(run())


def test_max_attempts_is_read_from_the_products_policy(tmp_path):
    """Two claims, two products, two attempt caps: the one-attempt product gives
    up after its first failed session while the two-attempt product does not."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'attempts.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            candidates=[], pr_status={},
            policies={"p1": policy(max_attempts=1), "p2": policy(max_attempts=2)})
        for target, product in (("o/r#1", "p1"), ("o/k#2", "p2")):
            await _set_owned_state(ctx, target, {
                "session_id": f"s-{target}", "repo": target.split("#")[0],
                "issues": [target], "claimed": True, "attempts": 0,
                "product": product, "_ended": True})

        await loop.tick()

        assert [t for t, _r in rec.needs_humans] == ["o/r#1"]
        assert (await ctx.get_target_state("o/r#1"))["gave_up"] is True
        st2 = await ctx.get_target_state("o/k#2")
        assert not st2.get("gave_up") and st2["attempts"] == 1

    asyncio.run(run())


def test_policies_are_seeded_once_per_process_on_the_first_tick(tmp_path):
    """D10: the flip carries the retired ``enabled_repos`` settings onto the
    products exactly once — a per-tick seed would fight every human edit."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'seedonce.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        calls: list[int] = []

        async def seed():
            calls.append(1)
            return 2

        loop, rec, ctx = build_loop(candidates=[], seed_policies=seed)
        await loop.tick()
        await loop.tick()
        assert len(calls) == 1

    asyncio.run(run())


def test_a_failing_seed_does_not_stop_the_tick(tmp_path):
    """Best-effort by contract: the seed is a migration convenience, never a gate
    on the sweep that frees slots and escalates stuck work."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'seedfail.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def seed():
            raise RuntimeError("db down (injected)")

        loop, rec, ctx = build_loop(candidates=[batch("r#1")], seed_policies=seed)
        await loop.tick()
        assert len(rec.spawns) == 1

    asyncio.run(run())


def test_per_product_counts_reach_the_status_card(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'prodcounts.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        rows = {"p1": {"dispatch": "auto", "eligible": 4, "blocked": 0,
                       "untriaged": 1, "in_flight": 0, "cap": 2}}
        loop, rec, ctx = build_loop(candidates=[], candidate_count=4,
                                    per_product=rows)
        await loop.tick()
        assert getattr(ctx, "backlog_products") == rows
        # …and never mixed into the int-only counts the web types as numbers.
        assert "p1" not in getattr(ctx, "backlog_counts", {})

    asyncio.run(run())


def test_a_failed_count_read_publishes_unknown_not_an_empty_product_map(tmp_path):
    """"We don't know" and "no product is opted in" are different sentences on the
    card: a failed read publishes ``None``, which ``status()`` renders as
    unavailable rather than as the idle "no product has dispatch on"."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'countfail.db'}"

    async def run():
        from server.automations.backlog_runner import BacklogRunner
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(candidates=[], candidate_count=0,
                                    count_read_failed=True)
        await loop.tick()

        assert getattr(ctx, "backlog_products", "absent") is None
        status = BacklogRunner().status(ctx)
        assert "unavailable" in status.text
        assert status.text != "idle — no product has dispatch on"

    asyncio.run(run())


def test_a_pre_flip_claim_without_a_product_keeps_the_default_attempt_cap(tmp_path):
    """A claim written before the flip carries no ``product``. The sweep must
    still charge and cap its attempts — at the pure schema default (2), which IS
    the global ``max_attempts`` those claims ran under."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'preflipattempts.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(candidates=[], pr_status={}, policies={})
        await _set_owned_state(ctx, "o/r#1", {
            "session_id": "s-1", "repo": "o/r", "issues": ["o/r#1"],
            "claimed": True, "attempts": 1, "_ended": True})   # no "product"

        await loop.tick()

        assert [t for t, _r in rec.needs_humans] == ["o/r#1"]
        assert (await ctx.get_target_state("o/r#1"))["gave_up"] is True

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
            config=make_config(max_concurrent=1),
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


def test_propose_mode_row_not_treated_as_dangler(tmp_path):
    """A proposed-mode pending-tap row (claimed=True, proposed=True, no session_id)
    must NOT be treated as a crash-between-claim-and-spawn dangler — its row must
    survive until the user taps or the sweep finishes with it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'p3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="off")
        # A pending-tap proposed row
        await _set_owned_state(ctx, "r#1", {"claimed": True, "proposed": True,
                                            "issues": ["r#1"]})
        # A real crash-between-claim-and-spawn dangler (no proposed, no session_id)
        await _set_owned_state(ctx, "r#2", {"claimed": True, "issues": ["r#2"]})

        await loop.tick()

        states = await ctx.list_target_states()
        assert "r#1" in states, "proposed row must survive reconciliation"
        assert "r#2" in states, "unresolved launch must survive reconciliation"

    asyncio.run(run())


def test_closed_unmerged_after_handoff_escalates_to_needs_human(tmp_path):
    """A PR that was handed off to PR Doctor but then closed unmerged must
    escalate to needs-human and leave a stamped ``gave_up`` row — the runner
    doesn't re-spawn indefinitely."""
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

        assert [t for t, _ in rec.needs_humans] == ["r#1"]
        st = await ctx.get_target_state("r#1")
        assert st["gave_up"] is True and "PR closed unmerged" in st["reason"]

    asyncio.run(run())


def test_handed_off_row_with_no_session_id_is_not_reconciled_away(tmp_path):
    """A handed_off state row with no session_id (seeded directly, no spawn path)
    must survive reconciliation — handed_off rows are sweep-owned, not danglers."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ho4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        # No candidates — only reconciliation and sweep run.
        loop, rec, ctx = build_loop(mode="on", candidates=[])
        # Seed a handed_off row with no session_id (e.g. from a prior worktree
        # that handed off before recording the spawn, or a direct DB seed).
        await _set_owned_state(ctx, "r#1", {
            "claimed": True,
            "handed_off": True,
            "issues": ["r#1"],
            "repo": "r",
        })

        await loop.tick()

        # The row must survive: handed_off rows are sweep-owned, never danglers.
        st = await ctx.get_target_state("r#1")
        assert st.get("handed_off") is True

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Pre-dispatch live re-verification (spec 003 §7a safety net, fails closed)    #
# --------------------------------------------------------------------------- #

async def _live_check_results() -> list[str]:
    """``detail.result`` of every ``skipped_live_check`` event, oldest first
    (``ctx.list_events`` does not surface ``detail``)."""
    from sqlalchemy import select
    from server.db.models import AutomationEvent
    async with db_session() as db:
        rows = (await db.execute(select(AutomationEvent).where(
            AutomationEvent.kind == "skipped_live_check").order_by(
            AutomationEvent.id))).scalars().all()
    return [(r.detail or {})["result"] for r in rows]


def _scripted_verify(results: list):
    """A ``verify_ready`` seam that answers from *results* in order and records
    the batches it was asked about."""
    asked: list = []

    async def verify_ready(batch):
        asked.append(batch)
        return results.pop(0)

    verify_ready.asked = asked  # type: ignore[attr-defined]
    return verify_ready


def test_live_check_failure_skips_backs_off_and_retries_after_cooldown(tmp_path):
    """``None`` (transport/parse failure) is NOT "ready": no claim, no spawn,
    one ``skipped_live_check`` event, and the target cools down exactly like a
    failed spawn — the next tick must not re-check it until the backoff expires."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clk = FakeClock()
        verify = _scripted_verify([None, True])
        loop, rec, ctx = build_loop(
            clock=clk, verify_ready=verify,
            candidates=[batch("o/r#7"), batch("o/r#7"), batch("o/r#7")])
        await loop.tick()
        assert rec.announced == [] and rec.spawns == []
        assert await _live_check_results() == ["check_failed"]
        assert "o/r#7" in loop._spawn_backoff
        # Cooling down: the same top pick is handed back, gated before any
        # second live check, and nothing spawns.
        await loop.tick()
        assert rec.spawns == [] and len(verify.asked) == 1
        # Cooldown over → re-checked live, now ready → claimed and spawned.
        clk.advance(3600)
        await loop.tick()
        assert len(verify.asked) == 2
        assert [b.primary for b in rec.announced] == ["o/r#7"]
        assert len(rec.spawns) == 1
        assert "o/r#7" not in loop._spawn_backoff

    asyncio.run(run())


def test_live_check_not_ready_skips_with_not_ready_result(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(verify_ready=_scripted_verify([False]),
                                    candidates=[batch("o/r#7")])
        await loop.tick()
        assert rec.announced == [] and rec.spawns == []
        assert await _live_check_results() == ["not_ready"]
        assert "o/r#7" in loop._spawn_backoff

    asyncio.run(run())


def test_live_check_gates_propose_dispatch_claims_too(tmp_path):
    """A ``propose`` product's claim row is written before the user tap — a stale
    pick must not be claimed there either."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(policies={"p1": policy(dispatch="propose")},
                                    verify_ready=_scripted_verify([False]),
                                    candidates=[batch("o/r#7", product="p1")])
        await loop.tick()
        assert rec.announced == []
        assert await ctx.get_target_state("o/r#7") == {}
        assert len(await ctx.list_events(kind="skipped_live_check")) == 1

    asyncio.run(run())


def test_live_check_seam_raising_is_treated_as_check_failed(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()

        async def verify_ready(_batch):
            raise RuntimeError("gh exploded")

        loop, rec, ctx = build_loop(verify_ready=verify_ready,
                                    candidates=[batch("o/r#7")])
        await loop.tick()
        assert rec.announced == [] and rec.spawns == []
        assert await _live_check_results() == ["check_failed"]

    asyncio.run(run())


def test_live_check_true_proceeds_to_claim_and_spawn(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        verify = _scripted_verify([True])
        loop, rec, ctx = build_loop(verify_ready=verify, candidates=[batch("o/r#7")])
        await loop.tick()
        assert [b.primary for b in verify.asked] == ["o/r#7"]
        assert [b.primary for b in rec.announced] == ["o/r#7"]
        assert len(rec.spawns) == 1
        assert await ctx.list_events(kind="skipped_live_check") == []

    asyncio.run(run())


def test_live_check_absent_seam_keeps_legacy_behaviour(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(candidates=[batch("o/r#7")])
        await loop.tick()
        assert [b.primary for b in rec.announced] == ["o/r#7"]
        assert len(rec.spawns) == 1
        assert await ctx.list_events(kind="skipped_live_check") == []

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
            mode="on", candidates=[batch("r#1"), batch("r#2")],
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
        loop, rec, ctx = build_loop(mode="on", candidates=[],
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
            mode="on", clock=clock, candidates=[batch("r#1"), batch("r#2")],
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
        loop, rec, ctx = build_loop(mode="on", clock=clock, candidates=[],
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
        loop, rec, ctx = build_loop(mode="on", clock=clock, candidates=[],
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


def test_group_refresh_failure_event_carries_the_error_and_reemits_when_it_changes(tmp_path):
    """The two ``groups_refresh_failed`` events of 2026-09-16 read only "retrying
    shortly" — the RuntimeError was in the journal and nowhere else. The event
    carries the exception now, and a DIFFERENT error is news even mid-streak."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr7.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        clock = FakeClock()
        message = ["grouping model returned no plan for a non-empty backlog"]

        async def refresh(_config):
            raise RuntimeError(message[0])

        loop, rec, ctx = build_loop(mode="on", clock=clock, candidates=[],
                                    refresh_groups=refresh)

        async def failures():
            from sqlalchemy import select
            from server.db.models import AutomationEvent
            async with db_session() as db:
                return (await db.execute(select(AutomationEvent).where(
                    AutomationEvent.kind == "groups_refresh_failed")
                    .order_by(AutomationEvent.ts))).scalars().all()

        await loop.tick()
        clock.advance(130)
        await loop.tick()                       # same error → debounced
        events = await failures()
        assert len(events) == 1
        assert events[0].detail == {
            "error": "RuntimeError: grouping model returned no plan for a non-empty backlog"}
        assert "no plan for a non-empty backlog" in events[0].summary

        message[0] = "sdk down"
        clock.advance(130)
        await loop.tick()                       # a different error → a second event
        events = await failures()
        assert len(events) == 2
        assert events[1].detail == {"error": "RuntimeError: sdk down"}

    asyncio.run(run())


def test_cold_start_publishes_counts_before_the_grouping_refresh(tmp_path):
    """The card read "Workload counts unavailable" (counts=None) for the whole first
    tick of every daemon process, because counts were published only at the END of
    a tick and the cold-start tick opens with the grouping model call over the
    entire backlog (~10 min, longer on a failure + retry). The cheap DB counts go
    up FIRST; the grouping call must find them already on the ctx."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr8.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        seen: list = []

        async def refresh(_config):
            seen.append(getattr(ctx, "backlog_counts", None))
            return 3

        loop, rec, ctx = build_loop(mode="on", candidates=[], candidate_count=7,
                                    refresh_groups=refresh)
        assert getattr(ctx, "backlog_counts", None) is None
        await loop.tick()
        assert seen == [{"in_flight": 0, "needs_human": 0, "eligible": 7,
                         "blocked": 0, "untriaged": 0, "unassigned_in_flight": 0}]

    asyncio.run(run())


def test_counts_are_published_even_when_the_tick_raises(tmp_path):
    """A raising sweep used to leave ``backlog_counts`` unset for the process's
    lifetime (the stash sat after it). The measured counts still go up; the
    failure is reported through ``last_refresh_failed_at`` as before."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gr9.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(mode="on", candidates=[], candidate_count=2)

        async def boom():
            raise RuntimeError("sweep exploded (injected)")

        loop._sweep = boom
        await loop.tick()
        assert getattr(ctx, "backlog_counts")["eligible"] == 2
        assert getattr(ctx, "last_refresh_failed_at", None) is not None

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

        async def failing_spawn(b, account, **_kw):
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

        async def failing_spawn(b, account, **_kw):
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

        async def flaky_spawn(b, account, **_kw):
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

        async def enospc_spawn(b, account, **_kw):
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

        async def failing_spawn(b, account, **_kw):
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

        async def alternating_spawn(b, account, **_kw):
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


# --------------------------------------------------------------------------- #
# Credential-refresh notifications (issue #670)                               #
# --------------------------------------------------------------------------- #

def test_stale_credentials_emit_refresh_event(tmp_path):
    """A repo with stale git credentials emits a dashboard event carrying the
    token-settings link, once, and does not re-emit on a second tick."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cred1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        alerts = [{"repo": "acme/widget", "since": 1.0,
                   "error": "Invalid username or token",
                   "settingsUrl": "https://github.com/settings/tokens"}]
        loop, _, ctx = build_loop(credential_alerts=lambda: list(alerts))

        await loop.tick()
        events = await ctx.list_events(kind="credential_refresh_needed")
        assert len(events) == 1
        assert events[0]["level"] == "error"
        assert events[0]["target"] == "acme/widget"
        # The link rides in detail so the dashboard can deep-link the fix.
        from sqlalchemy import select
        from server.db.models import AutomationEvent
        async with db_session() as db:
            row = (await db.execute(select(AutomationEvent).where(
                AutomationEvent.kind == "credential_refresh_needed"))).scalars().one()
        assert row.detail["settingsUrl"] == "https://github.com/settings/tokens"

        # A second tick with the same alert must NOT re-emit (debounced).
        await loop.tick()
        assert len(await ctx.list_events(kind="credential_refresh_needed")) == 1

    asyncio.run(run())


def test_credentials_recovering_emits_restored_event(tmp_path):
    """Once the stale alert clears (a fetch succeeded), the loop emits a
    'restored' event and re-arms so a future failure alerts again."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cred2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        alerts = [{"repo": "acme/widget", "since": 1.0, "error": "403",
                   "settingsUrl": "https://github.com/settings/tokens"}]
        loop, _, ctx = build_loop(credential_alerts=lambda: list(alerts))

        await loop.tick()                       # goes stale
        alerts.clear()                          # a fetch succeeded → registry cleared
        await loop.tick()                       # recovers
        restored = await ctx.list_events(kind="credential_restored")
        assert len(restored) == 1
        assert restored[0]["level"] == "info"
        assert restored[0]["target"] == "acme/widget"

        # Re-arm proven: a subsequent failure emits a fresh refresh event.
        alerts.append({"repo": "acme/widget", "since": 2.0, "error": "403",
                       "settingsUrl": "https://github.com/settings/tokens"})
        await loop.tick()
        assert len(await ctx.list_events(kind="credential_refresh_needed")) == 2

    asyncio.run(run())


def test_credential_check_runs_even_when_off(tmp_path):
    """A credential problem must surface regardless of runner mode — the alert
    is emitted even when the runner is off."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cred3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        alerts = [{"repo": "acme/widget", "since": 1.0, "error": "403",
                   "settingsUrl": "https://github.com/settings/tokens"}]
        loop, _, ctx = build_loop(mode="off", credential_alerts=lambda: list(alerts))
        await loop.tick()
        assert len(await ctx.list_events(kind="credential_refresh_needed")) == 1

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Queue provenance on the status card (#990)                                  #
# --------------------------------------------------------------------------- #

def _tail_of(entries: list):
    """A ``queue_tail`` seam returning a fixed, fresh-copied ranked pool."""
    def queue_tail():
        return list(entries)
    return queue_tail


async def _picked_events() -> "list[dict | None]":
    """``detail`` of every ``picked`` event, oldest first (``ctx.list_events``
    does not surface ``detail``)."""
    from sqlalchemy import select
    from server.db.models import AutomationEvent
    async with db_session() as db:
        rows = (await db.execute(select(AutomationEvent).where(
            AutomationEvent.kind == "picked").order_by(
            AutomationEvent.id))).scalars().all()
    return [r.detail for r in rows]


def test_picked_event_emitted_on_successful_spawn(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pk1.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        tail = [
            {"ref": "o/r#1", "score": 12.5, "product": "widget"},
            {"ref": "o/r#2", "score": 10.0, "product": "widget"},
            {"ref": "o/r#3", "score": 9.0, "product": "gadget"},
            {"ref": "o/r#4", "score": 8.0, "product": "gadget"},
        ]
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("o/r#1")],
            queue_tail=_tail_of(tail),
        )
        await loop.tick()
        assert len(rec.spawns) == 1
        events = await _picked_events()
        assert len(events) == 1
        assert events[0] == {
            "ref": "o/r#1", "score": 12.5, "product": "widget",
            "over": ["o/r#2", "o/r#3", "o/r#4"],
        }
        assert loop._last_pick == events[0]
        assert getattr(ctx, "backlog_last_pick", None) == events[0]

    asyncio.run(run())


def test_picked_event_excludes_bundled_issues_from_over(tmp_path):
    """A batch bundling two issues into one PR must exclude BOTH from the
    'over' runners-up list, not just the primary."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pk2.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        tail = [
            {"ref": "o/r#1", "score": 12.5, "product": "widget"},
            {"ref": "o/r#2", "score": 11.0, "product": "widget"},
            {"ref": "o/r#3", "score": 9.0, "product": "gadget"},
            {"ref": "o/r#4", "score": 8.0, "product": "gadget"},
        ]
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("o/r#1", issues=["o/r#1", "o/r#2"])],
            queue_tail=_tail_of(tail),
        )
        await loop.tick()
        events = await _picked_events()
        assert len(events) == 1
        assert events[0]["over"] == ["o/r#3", "o/r#4"]

    asyncio.run(run())


def test_picked_event_pick_missing_from_tail_reports_none(tmp_path):
    """The ranked pool can have refreshed since selection — the spawned
    primary may no longer be in the tail. Score/product report None but the
    event still fires."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pk3.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        tail = [
            {"ref": "o/r#2", "score": 11.0, "product": "widget"},
            {"ref": "o/r#3", "score": 9.0, "product": "gadget"},
        ]
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("o/r#1")],
            queue_tail=_tail_of(tail),
        )
        await loop.tick()
        events = await _picked_events()
        assert len(events) == 1
        assert events[0] == {
            "ref": "o/r#1", "score": None, "product": None,
            "over": ["o/r#2", "o/r#3"],
        }

    asyncio.run(run())


def test_queue_tail_raising_does_not_disturb_spawn_bookkeeping(tmp_path):
    """Provenance is best-effort (#990): a raising ``queue_tail`` must NOT
    abort ``_refill`` mid-spawn — the spawn already succeeded and the state
    row must still get finalized with its session_id, or the row is left
    looking like a crash-between-claim-and-spawn dangler that
    ``_reconcile_dangling`` would release or re-spawn."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pk5.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()

        def raising_queue_tail():
            raise RuntimeError("tail read blew up")

        spawned: list = []

        async def spawn(b, account, **_kw):
            spawned.append((b, account))
            return SimpleNamespace(id="s-picked-1", branch="b-picked-1")

        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("o/r#1")],
            queue_tail=raising_queue_tail,
            spawn=spawn,
        )
        await loop.tick()  # must not raise

        assert len(spawned) == 1  # the spawn itself still happened
        assert await _picked_events() == []  # no picked event
        assert loop._last_pick is None
        st = await ctx.get_target_state("o/r#1")
        assert st.get("session_id") == "s-picked-1"  # state row still finalized

    asyncio.run(run())


def test_no_queue_tail_seam_emits_no_picked_event(tmp_path):
    """Existing/legacy loop construction (no ``queue_tail``) must keep
    behaving exactly as before — no picked event, no last-pick state."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pk4.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("o/r#1")],
        )
        await loop.tick()
        assert len(rec.spawns) == 1
        assert await _picked_events() == []
        assert loop._last_pick is None
        assert getattr(ctx, "backlog_last_pick", None) is None

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Dispatch outcomes (spec 2026-09-12 product automation policy §6; phase 3)     #
# --------------------------------------------------------------------------- #

def test_spawn_records_a_running_outcome(tmp_path):
    """Every dispatch leaves a durable row the moment it starts, so an issue that
    keeps bouncing is countable even while its session is still in flight."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_spawn.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, _ctx = build_loop(
            candidates=[batch("o/r#1")],
            spawn_result=SimpleNamespace(id="sess-1", branch="b1"),
            policies={DEFAULT_PRODUCT: policy(max_in_flight=1000,
                                              default_model="opus")})

        await loop.tick()

        assert len(rec.spawns) == 1
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "running" and row.attempt_no == 0
        assert row.session_id == "sess-1"
        # The model is the PRODUCT's, from the same policy snapshot that decided
        # this batch may spawn.
        assert row.model == "opus"
        assert row.project_id is not None and row.body_fingerprint is not None
        assert row.started_at is not None and row.ended_at is None

    asyncio.run(run())


def test_the_propose_path_records_no_outcome(tmp_path):
    """A propose-mode claim is not a dispatch: nothing ran, so there is no
    outcome to finalise later (the tap that spawns records its own)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_propose.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, _ctx = build_loop(
            candidates=[batch("o/r#1")],
            policies={DEFAULT_PRODUCT: policy(dispatch="propose",
                                              max_in_flight=1000)})

        await loop.tick()

        assert rec.spawns == []
        assert await _outcomes("o/r#1") == []

    asyncio.run(run())


def test_session_ended_without_pr_finalises_no_pr(tmp_path):
    """The session is over and there is no PR — that attempt is finished, and the
    row says so (this is the bounce the queue's ``needs_spec`` skip counts)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_nopr.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, _rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1"))

        await loop.tick()                      # spawn → running
        await _mark_session_ended(ctx, "o/r#1")
        await loop.tick()                      # sweep charges the attempt

        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "no_pr"
        assert row.cause == "session ended without a mergeable PR"
        assert row.ended_at is not None and row.duration_s is not None
        assert row.pr_number is None and row.first_pass_merge is None
        # The claim row is still there, one attempt poorer — the outcome row is
        # history, not the state machine.
        assert (await ctx.get_target_state("o/r#1"))["attempts"] == 1

    asyncio.run(run())


def test_merged_pr_finalises_merged_with_first_pass_from_pr_doctor(tmp_path):
    """``first_pass_merge`` is PR Doctor's attempt count at the moment the sweep
    sees the merge: 0 fixes → a first-pass merge, >0 → not, and an UNKNOWN count
    (a cleared PR Doctor row) stays ``None``. Never guessed (R3-4)."""

    async def one(name: str, attempts: dict, expected):
        from types import SimpleNamespace

        from server.github.pr_status import PullRequestInfo

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
        await _seed_row()
        await _seed_product()
        merged = PullRequestInfo(number=12, url="https://example/pr/12",
                                 state="merged", checks="none", title="",
                                 merged=True)
        loop, rec, _ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={"o/r#1": merged},
            spawn_result=SimpleNamespace(id="sess-1"),
            pr_doctor_attempts=attempts)

        await loop.tick()          # spawn, then the same tick's sweep sees the merge

        assert [t for t, _info in rec.close_outs] == ["o/r#1"]
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "merged"
        assert row.pr_number == 12 and row.pr_url == "https://example/pr/12"
        assert row.first_pass_merge is expected
        assert row.ended_at is not None

    async def run():
        await one("oc_m0.db", {"o/r#12": 0}, True)    # PR Doctor never had to act
        await one("oc_m2.db", {"o/r#12": 2}, False)   # two fixes carried it home
        await one("oc_mu.db", {}, None)               # unknown — never guessed

    asyncio.run(run())


def test_a_doctor_row_without_counters_still_reads_as_a_first_pass_merge(tmp_path):
    """The composition the fake map cannot check (R3-7): the REAL seam over PR
    Doctor's own state row. ``record_attempt`` only ever writes ``total_attempts``
    / ``total_mechanical`` starting at 1, so a tracked PR the doctor never had to
    act on carries NEITHER key — and that is precisely a first-pass merge. Reading
    a missing counter as UNKNOWN made ``first_pass_merge is True`` unreachable in
    production."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_first_pass.db'}"

    async def run():
        from types import SimpleNamespace

        from server.db.models import Automation
        from server.github.pr_status import PullRequestInfo
        from server.sessions.orchestrator import SessionOrchestrator

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))

        orch = SessionOrchestrator.__new__(SessionOrchestrator)
        orch._pr_doctor_ctx = AutomationContext("pr-doctor")
        # What PR Doctor's row looks like for a PR it tracked and never touched.
        await orch._pr_doctor_ctx.set_target_state("o/r#12", {"last_head_sha": "abc"})

        merged = PullRequestInfo(number=12, url="https://example/pr/12",
                                 state="merged", checks="none", title="", merged=True)
        loop, _rec, _ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={"o/r#1": merged},
            spawn_result=SimpleNamespace(id="sess-1"),
            pr_doctor_attempts_fn=orch._backlog_pr_doctor_attempts_for)

        await loop.tick()

        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        assert rows[0].outcome == "merged" and rows[0].first_pass_merge is True

    asyncio.run(run())


def test_a_doctor_rescued_merge_is_not_a_first_pass_merge(tmp_path):
    """The regression R3-10 asks for: ``first_pass_merge`` must survive PR
    Doctor's own recovery clear.

    PR Doctor WIPES its per-target row when CI goes green after an attempt
    (``_on_pr_change``), and every SUCCESSFUL session fix ends in exactly that
    failing→passing transition — while auto-merge only ever lands a green PR. So
    at merge time the row was counter-less in precisely the case the metric
    exists to detect, and a doctor-rescued merge was branded a first pass.

    Driven through the real writers end to end: ``record_attempt`` (what
    ``_pr_doctor_step``'s SessionFix branch persists) → the real ``_on_pr_change``
    recovery clear → ``apply_rearm`` re-writing a fresh row on the next poll →
    the runner's sweep seeing the merge. The doctored PR must read False; a
    tracked PR the doctor never touched must still read True."""

    async def one(name: str, *, doctored: bool, expected: bool):
        from types import SimpleNamespace

        from server.automations import pr_doctor_engine as engine
        from server.db.models import Automation
        from server.github.pr_status import PullRequestInfo
        from server.sessions.orchestrator import SessionOrchestrator

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
        await _seed_row()
        await _seed_product()
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))

        def _info(**over):
            base = dict(number=12, url="https://example/pr/12", state="open",
                        checks="failing", title="", head_sha="sha1")
            base.update(over)
            return PullRequestInfo(**base)

        failing = _info()
        orch = SessionOrchestrator.__new__(SessionOrchestrator)
        orch._pr_doctor_ctx = AutomationContext("pr-doctor")
        orch._pushed_pr = set()
        orch._records = {}
        orch._pr_poller = SimpleNamespace(  # type: ignore[assignment]
            cached=lambda _repo, _branch: failing)

        ctx = orch._pr_doctor_ctx
        st = engine.apply_rearm(await ctx.get_target_state("o/r#12"), failing)
        if doctored:
            # Exactly what _pr_doctor_step persists after a real injection.
            st = engine.record_attempt(st, engine.SessionFix(kind="failing_ci"), "sha1")
            st["attempted"] = True
        await _set_owned_state(ctx, "o/r#12", st)

        # The fix lands and CI turns green — the recovery clear fires here.
        await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
        # The doctor's give-up ceiling IS reset by the clear (unchanged
        # semantics: a recovered PR earns a fresh budget).
        assert (await ctx.get_target_state("o/r#12")).get("total_attempts", 0) == 0

        # The next poll cycle re-arms the still-open, now-green PR.
        green = _info(checks="passing", head_sha="sha2")
        await _set_owned_state(ctx,
            "o/r#12", engine.apply_rearm(await ctx.get_target_state("o/r#12"), green))

        merged = PullRequestInfo(number=12, url="https://example/pr/12",
                                 state="merged", checks="none", title="", merged=True)
        loop, _rec, _ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={"o/r#1": merged},
            spawn_result=SimpleNamespace(id="sess-1"),
            pr_doctor_attempts_fn=orch._backlog_pr_doctor_attempts_for)

        await loop.tick()

        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        assert rows[0].outcome == "merged"
        assert rows[0].first_pass_merge is expected

    async def run():
        await one("oc_rescued.db", doctored=True, expected=False)
        await one("oc_untouched.db", doctored=False, expected=True)

    asyncio.run(run())


def test_pr_doctor_gave_up_finalises_pr_gave_up(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_prgu.db'}"

    async def run():
        from types import SimpleNamespace

        from server.db.models import Automation, AutomationEvent

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        async with db_session() as db:
            db.add(Automation(id="pr-doctor"))
            db.add(AutomationEvent(
                automation_id="pr-doctor", level="warn", summary="gave up",
                kind="gave_up", target="o/r#1", detail={"origin_ref": "o/r#1"}))

        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1"))

        await loop.tick()
        await loop.tick()

        assert [t for t, _r in rec.needs_humans] == ["o/r#1"]
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        assert rows[0].outcome == "pr_gave_up"
        assert rows[0].cause == "pr-doctor gave up"
        assert (await ctx.get_target_state("o/r#1"))["gave_up"] is True

    asyncio.run(run())


def test_pr_closed_unmerged_finalises_abandoned(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_closed.db'}"

    async def run():
        from types import SimpleNamespace

        from server.github.pr_status import PullRequestInfo

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        closed = PullRequestInfo(number=9, url="https://example/pr/9",
                                 state="closed", checks="none", title="")
        loop, rec, _ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={"o/r#1": closed},
            spawn_result=SimpleNamespace(id="sess-1"))

        await loop.tick()

        assert [t for t, _r in rec.needs_humans] == ["o/r#1"]
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "abandoned" and row.cause == "PR closed unmerged"
        assert row.pr_number == 9 and row.pr_url == "https://example/pr/9"

    asyncio.run(run())


def test_max_attempts_give_up_finalises_needs_human(tmp_path):
    """The PRODUCTION walk to the cap, sweep by sweep: nothing re-dispatches
    between them, so the sweep that charges the last attempt is escalating the
    very dispatch the previous sweep already closed as ``no_pr``. That row is
    UPGRADED in place — one row for one session, ending ``needs_human``. Jumping
    the state row straight to the cap hid this: the escalation then found a live
    ``running`` row that production never has (R3-7)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_needs_human.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1"),
            policies={DEFAULT_PRODUCT: policy(max_in_flight=1000, max_attempts=2)})

        await loop.tick()                       # spawn → running
        await _mark_session_ended(ctx, "o/r#1")
        await loop.tick()                       # sweep: attempt 1 of 2 → no_pr
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1 and rows[0].outcome == "no_pr"
        assert rec.needs_humans == []
        ended_at = rows[0].ended_at

        await loop.tick()                       # sweep: attempt 2 = the cap → give up

        assert [t for t, _r in rec.needs_humans] == ["o/r#1"]
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1                   # never two bounce rows for one session
        assert rows[0].outcome == "needs_human"
        assert "max attempts" in (rows[0].cause or "")
        assert rows[0].ended_at == ended_at     # the dispatch ended when the session did
        assert (await ctx.get_target_state("o/r#1"))["gave_up"] is True

    asyncio.run(run())


def test_release_after_never_ran_finalises_abandoned(tmp_path):
    """A session that never ran a turn is released rather than escalated (#1100);
    the dispatch still happened, so its row closes as ``abandoned``, not
    ``needs_human`` — it is no evidence the issue needs a human."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_never_ran.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1"),
            session_ran=False,
            policies={DEFAULT_PRODUCT: policy(max_in_flight=1000, max_attempts=2)})

        await loop.tick()                       # spawn → running
        await _mark_session_ended(ctx, "o/r#1")
        await loop.tick()                       # sweep: attempt 1 of 2 → no_pr
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1 and rows[0].outcome == "no_pr"

        await loop.tick()                       # sweep: the cap → release

        assert rec.needs_humans == []                 # released, not escalated
        assert await ctx.get_target_state("o/r#1") == {}
        # The same row this dispatch already bounced, upgraded — not a second one.
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        assert rows[0].outcome == "abandoned"
        assert rows[0].cause == "session never ran a turn"

    asyncio.run(run())


def test_handoff_does_not_finalise(tmp_path):
    """A handoff is not terminal: PR Doctor now carries the PR, and the merge (or
    its give-up) is what finalises the row. Closing it at handoff would count a
    landed PR as a bounce."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_handoff.db'}"

    async def run():
        from types import SimpleNamespace

        from server.github.pr_status import PullRequestInfo

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        open_auto = PullRequestInfo(number=12, url="https://example/pr/12",
                                    state="open", checks="pending", title="",
                                    auto_merge_enabled=True)
        pr_status = {"o/r#1": open_auto}
        loop, _rec, ctx = build_loop(
            config=make_config(max_concurrent=1),
            candidates=[batch("o/r#1")], pr_status=pr_status,
            pr_doctor_active=True,
            spawn_result=SimpleNamespace(id="sess-1"),
            pr_doctor_attempts={"o/r#12": 0})

        await loop.tick()
        assert (await ctx.get_target_state("o/r#1")).get("handed_off") is True
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1 and rows[0].outcome == "running"
        assert rows[0].ended_at is None

        pr_status["o/r#1"] = PullRequestInfo(
            number=12, url="https://example/pr/12", state="merged", checks="none",
            title="", merged=True)
        await loop.tick()

        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        assert rows[0].outcome == "merged" and rows[0].first_pass_merge is True

    asyncio.run(run())


def test_a_failing_outcome_write_never_blocks_the_sweep(tmp_path):
    """Spec §13: history is best-effort, dispatch is not. A seam that raises must
    leave the give-up path byte-for-byte what it was before outcomes existed."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_boom.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1"),
            policies={DEFAULT_PRODUCT: policy(max_in_flight=1000, max_attempts=2)})

        await loop.tick()

        async def boom(*_a, **_kw):
            raise RuntimeError("outcome write blew up (injected)")

        ctx.finalize_outcome = boom          # type: ignore[method-assign]

        await _mark_session_ended(ctx, "o/r#1", attempts=1)
        await loop.tick()                    # must not raise out of the sweep

        assert [t for t, _r in rec.needs_humans] == ["o/r#1"]
        st = await ctx.get_target_state("o/r#1")
        assert st["gave_up"] is True and st["needs_human_at"]
        # The row stayed ``running``: unfinalised history is the acceptable loss.
        rows = await _outcomes("o/r#1")
        assert len(rows) == 1 and rows[0].outcome == "running"

    asyncio.run(run())


def test_a_failing_outcome_write_never_blocks_a_spawn(tmp_path):
    """The other half of §13: the record at spawn cannot cost the spawn."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_boom2.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1", branch="b1"))

        async def boom(*_a, **_kw):
            raise RuntimeError("outcome write blew up (injected)")

        ctx.record_outcome = boom            # type: ignore[method-assign]

        await loop.tick()                    # must not raise out of the refill

        assert len(rec.spawns) == 1
        assert await _outcomes("o/r#1") == []
        # The claim row is still finalised with the spawned session id.
        assert (await ctx.get_target_state("o/r#1"))["session_id"] == "sess-1"

    asyncio.run(run())


def test_a_lost_spawn_time_write_is_recovered_at_finalisation(tmp_path):
    """Spec §13's other half: "the outcome write fails at spawn: the spawn
    proceeds; the sweep inserts the row at finalisation with ``attempt_no`` from
    the target state". The dispatch ran and bounced — losing its row would lose
    that bounce forever, and D8 would never reach the threshold for an issue that
    really is unworkable.

    Exactly ONE row after the sweep: the fallback is a last resort, not a second
    writer."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'oc_recover.db'}"

    async def run():
        from types import SimpleNamespace

        await init_db(db_url=db_url)
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(
            candidates=[batch("o/r#1")], pr_status={},
            spawn_result=SimpleNamespace(id="sess-1", branch="b1"),
            policies={DEFAULT_PRODUCT: policy(max_in_flight=1000, max_attempts=3)})

        async def boom(*_a, **_kw):
            raise RuntimeError("spawn-time outcome write blew up (injected)")

        ctx.record_outcome = boom            # type: ignore[method-assign]

        await loop.tick()
        assert len(rec.spawns) == 1
        assert await _outcomes("o/r#1") == []      # the dispatch left no history

        # The session ends with no PR: the sweep's bounce branch finalises it.
        await _mark_session_ended(ctx, "o/r#1")
        await loop.tick()

        rows = await _outcomes("o/r#1")
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == "no_pr" and row.cause
        # The claim's own facts, not invented ones.
        assert row.session_id == "sess-1" and row.attempt_no == 0
        assert row.project_id is not None
        # And it counts: the fingerprint is the issue's CURRENT body.
        assert row.body_fingerprint is not None
        # Nobody knows when this dispatch began — only that it is over.
        assert row.started_at is None and row.duration_s is None
        assert row.ended_at is not None

        # A second sweep of the same ended session must not add another row.
        await loop.tick()
        assert len(await _outcomes("o/r#1")) == 1

    asyncio.run(run())


def test_outcome_fallback_is_none_for_a_claim_that_never_dispatched():
    """A propose claim (no ``session_id``) reaching a give-up branch must not mint
    a fallback row: nothing ran, so a bounce against that body would be invented."""
    from server.automations.backlog_runner import BacklogRunnerLoop
    assert BacklogRunnerLoop._outcome_fallback(
        {"product": "p1", "proposed": True, "attempts": 0}) is None
    assert BacklogRunnerLoop._outcome_fallback(
        {"product": "p1", "session_id": "s1", "attempts": 1}) == {
        "product_slug": "p1", "session_id": "s1", "model": None, "attempt_no": 1}


def test_display_counts_share_claim_snapshot_and_keep_off_product_work():
    from unittest.mock import AsyncMock

    from server.automations.backlog_runner import BacklogRunner

    async def run():
        rows = {"p1": {"dispatch": "off", "eligible": 0, "cap": 2}}
        loop, _, ctx = build_loop(candidates=[], candidate_count=0, per_product=rows)
        ctx.list_target_states = AsyncMock(return_value={
            "proposal": {"product": "p1", "proposed": True},
            "legacy": {},
            "deleted-product": {"product": "deleted"},
            "done": {"product": "p1", "handed_off": True},
            "parked": {"product": "p1", "gave_up": True},
        })
        loop._count_in_flight = AsyncMock(return_value=99)
        await loop._stash_counts({})
        status = BacklogRunner().status(ctx)
        assert status.counts["in_flight"] == 3
        assert status.counts["unassigned_in_flight"] == 2
        assert status.products[0]["inFlight"] == 1
        assert status.products[0]["dispatch"] == "off"
        assert "3 work in flight" in status.text
        assert "2 unattributed" in status.text
        ctx.list_target_states.assert_awaited_once()
        loop._count_in_flight.assert_not_awaited()

    asyncio.run(run())


def test_failed_claim_read_does_not_publish_zero_work():
    from unittest.mock import AsyncMock

    from server.automations.backlog_runner import BacklogRunner

    async def run():
        loop, _, ctx = build_loop(candidates=[], candidate_count=0)
        ctx.list_target_states = AsyncMock(side_effect=RuntimeError("read failed"))
        await loop._stash_counts({})
        status = BacklogRunner().status(ctx)
        assert "in_flight" not in status.counts
        assert "needs_human" not in status.counts
        assert "unavailable" in status.text
        assert "idle" not in status.text
        assert status.products is None

    asyncio.run(run())


def test_unattributed_work_stays_visible_without_opted_in_products():
    from unittest.mock import AsyncMock

    from server.automations.backlog_runner import BacklogRunner

    async def run():
        loop, _, ctx = build_loop(candidates=[], candidate_count=0)
        ctx.list_target_states = AsyncMock(return_value={"legacy": {"proposed": True}})
        await loop._stash_counts({})
        status = BacklogRunner().status(ctx)
        assert status.counts["in_flight"] == status.counts["unassigned_in_flight"] == 1
        assert status.products == []
        assert "1 work in flight" in status.text
        assert "1 unattributed" in status.text
        assert status.level == "active"

    asyncio.run(run())


def test_queue_failure_preserves_measured_workload_but_not_false_queue_zeroes():
    from unittest.mock import AsyncMock

    from server.automations.backlog_runner import BacklogRunner

    async def run():
        loop, _, ctx = build_loop(candidates=[], candidate_count=0, count_read_failed=True)
        ctx.list_target_states = AsyncMock(return_value={"live": {"product": "p1"}})
        await loop._stash_counts({})
        status = BacklogRunner().status(ctx)
        assert status.counts == {"in_flight": 1, "needs_human": 0}
        assert status.products is None
        assert "1 work in flight" in status.text
        assert "queue read failed" in status.text

    asyncio.run(run())


def test_wait_reason_uses_refill_decision_and_clears(tmp_path):
    from server.automations.backlog_runner import BacklogRunner

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
        await _seed_row()
        cfg = make_config(max_concurrent=2, max_spawns_per_day=1)
        loop, rec, ctx = build_loop(config=cfg)
        await loop.tick()
        assert ctx.backlog_waiting_reason is None  # configured limits aren't waits
        rec.in_flight = 2
        await loop.tick()
        assert ctx.backlog_waiting_reason == "capacity"
        loop._spawns_today = [loop._clock()]
        await loop.tick()
        assert ctx.backlog_waiting_reason == "budget"
        rec.in_flight = 0
        loop._spawns_today = []
        await loop.tick()
        assert ctx.backlog_waiting_reason is None
        assert BacklogRunner().status(ctx).waiting_reason is None

    asyncio.run(run())


def test_missing_admission_seam_never_claims_or_spawns(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}")
        await _seed_row()
        loop, rec, ctx = build_loop(candidates=[batch("o/r#1")])
        loop._admission = None
        await loop.tick()
        assert rec.spawns == []
        assert await ctx.get_target_state("o/r#1") == {}
        from server.automations.backlog_runner import BacklogRunner
        assert BacklogRunner().status(ctx).level == "unknown"
        assert "admission_unavailable" in BacklogRunner().status(ctx).text

    asyncio.run(run())
