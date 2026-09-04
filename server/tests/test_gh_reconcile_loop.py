"""GhReconcileLoop (spec 003 §6) — converge cycle with every safety rail.

Canned-seam mold (mirrors ``test_pm_sync_projects.py``): every seam is a fake that
records its calls, so the loop's ordering guarantees (journal-before-mutate, cap
halts before any write, fence/kill-switch refuse the cycle) are asserted directly.

Rate budget: GitHub's ``rateLimit`` is a Query-root field, so it is fetched once per
cycle through the same ``mutate`` seam with ``build_rate_limit_query()`` — the fake
below answers that document, and mutate-call assertions account for it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from server.automations.gh_reconcile import GhReconcile, GhReconcileLoop
from server.pm.desired_state import STATUS_OPTIONS, ItemFacts
from server.pm.mirror_store import ProductSyncView
from server.pm.reconcile_diff import MirrorItem, Mutation
from server.pm.reconcile_inputs import ProductFacts

T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
REPO = "steiner385/cc-account-switcher"
PRODUCT = ProductSyncView("p1", "ccswitch", "ccswitch", "ghdb1", "PVT_1", [REPO])
# Status covers the full spec vocabulary so the default fixture never triggers Task 9's
# ensure_fields bootstrap (test_ensure_fields_* below exercise that path directly with their
# own sparse caches).
CACHE = {"Status": {"field_id": "F_s", "options": {n: f"O_{n}" for n in STATUS_OPTIONS}},
         "Score": {"field_id": "F_sc", "options": {}},
         "Stage": {"field_id": "F_st", "options": {"build": "O_build"}}}
RATE_OK = {"data": {"rateLimit": {"cost": 1, "remaining": 5000, "resetAt": None}}}


class Ctx:
    def __init__(self): self.events = []; self.details = []

    async def emit(self, level, summary, **kw):
        self.events.append((level, summary, kw.get("kind")))
        self.details.append(kw.get("detail"))


def _item(number, **over):
    base = dict(ref=f"{REPO}#{number}", issue_state="open", issue_closed_at=None,
                triage_state="classified", classified_at=T, reach=4, impact=3, effort=2,
                confidence=80, session_status=None, session_at=None, merged_pr_at=None,
                open_blocker_refs=(), blockers_at=None, needs_human_label=False,
                initiative_stage=None, initiative_stage_at=None)
    base.update(over)
    return ItemFacts(**base)


def _facts(**over):
    base = dict(project_id="p1", slug="ccswitch", owner="steiner385", gh_project_db_id="ghdb1",
                project_node_id="PVT_1", field_cache=CACHE, mirror_synced_at=T,
                items=[_item(5)],
                mirror={f"{REPO}#5": MirrorItem(f"{REPO}#5", "PVTI_5", "Backlog", None, None,
                                                T - timedelta(days=1))},
                open_issue_nodes={f"{REPO}#5": "I_5"}, deps=[], node_ids={f"{REPO}#5": "I_5"},
                newest_decision_at=T, last_applied_at=None)
    base.update(over)
    return ProductFacts(**base)


def _loop(ctx, *, mode="apply", config=None, facts=None, mutate=None, fence=True, enabled=True,
          failure_counts=None, parked=None, products=None, load_facts_fn=None,
          own_values=None, dry_run_keys=None):
    # calls["order"] interleaves the journal and GraphQL seams so the write-ahead
    # ordering guarantee (record BEFORE mutate, every batch) is directly assertable.
    calls = {"record": [], "applied": [], "failed": [], "park": [], "sync": [], "mutate": [],
             "cache": [], "order": [], "prune": [], "dry_keys": [], "prune_applied": []}

    async def read_config(): return mode, (config or {})
    async def list_products(): return list(products) if products is not None else [PRODUCT]

    async def load_facts(product, now):
        if load_facts_fn is not None:
            return load_facts_fn(product)
        return facts or _facts()

    async def mutate_(owner, doc):
        calls["mutate"].append((owner, doc))
        calls["order"].append("rate" if doc.startswith("query { rateLimit") else "mutate")
        if mutate is not None:
            return mutate(doc)
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        return {"data": {f"m{i}": {"ok": True} for i in range(20)}}

    async def j_record(muts, batch_id, now, dry):
        calls["record"].append((muts, batch_id, dry))
        calls["order"].append("record")
        return [f"r{i}" for i in range(len(muts))]

    async def j_applied(ids, now): calls["applied"].extend(ids)
    async def j_failed(rid, err, now): calls["failed"].append((rid, err))
    async def j_park(rid, reason, now): calls["park"].append((rid, reason))
    async def j_fc(key): return (failure_counts or {}).get(key, 0)
    async def j_parked(key): return key in (parked or set())
    async def j_keys(): return set()
    async def j_own(): return dict(own_values or {})

    async def j_dry_keys(since):
        calls["dry_keys"].append(since)
        return set(dry_run_keys or set())

    async def j_prune(older): calls["prune"].append(older); return 0
    async def j_prune_applied(older): calls["prune_applied"].append(older); return 0
    async def j_recover(older): return 0
    async def request_sync(pid): calls["sync"].append(pid)
    async def sha_fence(): return fence
    async def save_cache(gid, cache): calls["cache"].append((gid, cache))

    loop = GhReconcileLoop(
        ctx, read_config=read_config, list_products=list_products, load_facts=load_facts,
        mutate=mutate_, journal_record=j_record, journal_applied=j_applied, journal_failed=j_failed,
        journal_park=j_park, journal_failure_count=j_fc, journal_parked=j_parked,
        journal_applied_keys=j_keys, journal_recover=j_recover, request_sync=request_sync,
        sha_fence=sha_fence, kill_switch=lambda: enabled, save_field_cache=save_cache,
        now_utc=lambda: T, journal_own_values=j_own, journal_dry_run_keys=j_dry_keys,
        journal_prune_dry_run=j_prune, journal_prune_applied=j_prune_applied)
    return loop, calls


def _docs(calls):
    return [doc for _owner, doc in calls["mutate"]]


def _batches(calls):
    return [doc for doc in _docs(calls) if doc.startswith("mutation")]


def test_apply_journals_before_mutating_and_marks_applied():
    ctx = Ctx(); loop, calls = _loop(ctx)
    asyncio.run(loop.tick())
    assert len(calls["record"]) == 1 and calls["record"][0][2] is False
    muts = calls["record"][0][0]
    assert [(m.field, m.new_value) for m in muts] == [("Status", "Ready"), ("Score", 480.0)]
    docs = _docs(calls)
    # one rate-budget query per cycle, then the batch
    assert len(docs) == 2 and docs[0].startswith("query { rateLimit")
    assert "m0: updateProjectV2ItemFieldValue" in docs[1]
    # write-ahead: the batch is journaled BEFORE it is sent
    assert calls["order"] == ["rate", "record", "mutate"]
    assert calls["order"].index("record") < calls["order"].index("mutate")
    assert calls["applied"] == ["r0", "r1"] and calls["sync"] == ["p1"]
    assert ctx.gh_reconcile_counts["applied"] == 2


def test_dry_run_journals_but_never_mutates():
    ctx = Ctx(); loop, calls = _loop(ctx, mode="dry_run")
    asyncio.run(loop.tick())
    assert calls["record"][0][2] is True and calls["mutate"] == [] and calls["applied"] == []
    assert ctx.gh_reconcile_counts["dry_run"] == 1 and ctx.gh_reconcile_counts["planned"] == 2


def test_kill_switch_and_fence_refuse_the_cycle():
    for kw, key in (({"enabled": False}, "disabled"), ({"fence": False}, "fenced")):
        ctx = Ctx(); loop, calls = _loop(ctx, **kw)
        asyncio.run(loop.tick())
        assert calls["record"] == [] and calls["mutate"] == []
        assert ctx.gh_reconcile_counts[key] == 1


def test_freshness_gate_requests_sync_and_skips():
    stale = _facts(mirror_synced_at=T - timedelta(hours=1), newest_decision_at=T)
    ctx = Ctx(); loop, calls = _loop(ctx, facts=stale)
    asyncio.run(loop.tick())
    assert calls["sync"] == ["p1"] and calls["record"] == [] and _batches(calls) == []
    assert ctx.gh_reconcile_counts["skipped_stale"] == 1


def test_never_synced_product_is_stale_and_requests_sync():
    """No mirror sync ever ⇒ the empty mirror is absence of evidence: converging against
    it would re-add every open issue to a board that may already hold them."""
    unsynced = _facts(mirror_synced_at=None, newest_decision_at=None, mirror={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=unsynced)
    asyncio.run(loop.tick())
    assert calls["sync"] == ["p1"] and calls["record"] == [] and _batches(calls) == []
    assert ctx.gh_reconcile_counts["skipped_stale"] == 1
    assert ctx.gh_reconcile_counts["planned"] == 0


def test_blast_cap_halts_loudly_without_partial_apply():
    many = _facts(items=[_item(n) for n in range(1, 8)], mirror={},
                  open_issue_nodes={f"{REPO}#{n}": f"I_{n}" for n in range(1, 8)})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=many, config={"max_mutations_per_cycle": 3})
    asyncio.run(loop.tick())
    assert calls["record"] == [] and _batches(calls) == []
    assert ctx.gh_reconcile_counts["halted_cap"] == 1
    assert any(lvl == "error" and kind == "gh_reconcile_cap" for lvl, _, kind in ctx.events)


def test_cap_halt_notifies_once_per_overshoot_not_once_per_tick():
    """Same latch as the incomplete-board park, same reason: the halt applies nothing, so
    the identical oversized set is re-planned every 120 s until a human raises the cap.
    A *different* overshoot is news and gets through; the count ticks either way."""
    def over(n):
        return _facts(items=[_item(i) for i in range(1, n + 1)], mirror={},
                      open_issue_nodes={f"{REPO}#{i}": f"I_{i}" for i in range(1, n + 1)})

    states = [over(7), over(7), over(7), over(8)]   # ...then one more issue arrives
    ctx = Ctx(); seen = iter(states)
    loop, _calls = _loop(ctx, config={"max_mutations_per_cycle": 3},
                         load_facts_fn=lambda p: next(seen))
    for _ in states:
        asyncio.run(loop.tick())
    # three identical overshoots -> one push; the fourth is a different one -> a second push
    assert len([e for e in ctx.events if e[2] == "gh_reconcile_cap"]) == 2
    assert ctx.gh_reconcile_counts["halted_cap"] == 1      # counts tick every cycle


def test_cap_halt_latch_resets_once_the_product_fits_again():
    many = _facts(items=[_item(n) for n in range(1, 8)], mirror={},
                  open_issue_nodes={f"{REPO}#{n}": f"I_{n}" for n in range(1, 8)})
    states = [many, _facts(), many]          # over cap -> fits -> over cap again
    ctx = Ctx(); seen = iter(states)
    loop, _calls = _loop(ctx, config={"max_mutations_per_cycle": 3},
                         load_facts_fn=lambda p: next(seen))
    for _ in states:
        asyncio.run(loop.tick())
    assert len([e for e in ctx.events if e[2] == "gh_reconcile_cap"]) == 2


def test_migrate_raises_cap():
    many = _facts(items=[_item(n) for n in range(1, 8)], mirror={},
                  open_issue_nodes={f"{REPO}#{n}": f"I_{n}" for n in range(1, 8)})
    ctx = Ctx(); loop, calls = _loop(
        ctx, facts=many, config={"max_mutations_per_cycle": 3, "migrate": True, "migrate_cap": 100})
    asyncio.run(loop.tick())
    assert len(calls["record"]) == 1 and len(calls["record"][0][0]) == 7


def test_per_alias_failure_marks_failed_and_second_failure_parks():
    def mutate(doc):
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        return {"data": {"m0": None, "m1": {"ok": True}},
                "errors": [{"path": ["m0"], "message": "nope"}]}

    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate)
    asyncio.run(loop.tick())
    assert calls["failed"] == [("r0", "nope")] and calls["applied"] == ["r1"]
    # already failed once -> this failure parks
    key = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:x").key()
    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate, failure_counts={key: 1})
    asyncio.run(loop.tick())
    assert calls["park"] == [("r0", "failed twice: nope")]
    assert any(kind == "gh_reconcile_parked" for _, _, kind in ctx.events)


def test_unknown_option_parks_the_batch_and_later_batches_still_apply():
    """A drifted vocabulary poisons only its own batch: rows are parked, an error is
    emitted, and the remaining batches of the cycle are still sent."""
    refs = [f"{REPO}#{n}" for n in range(1, 22)]
    # #1's Stage decision ("deploy") has no option id in CACHE (which only has "build") ->
    # unknown-option drift, isolated to its own batch. Status now covers the full spec
    # vocabulary (Task 9), so #1's mirror row already carries its matching Status ("Ready")
    # and contributes only the drifted Stage mutation, not a second Status one.
    items = ([_item(1, initiative_stage="deploy", initiative_stage_at=T)]
             + [_item(n) for n in range(2, 22)])
    # score already matches, so items #2-21 each contribute exactly one Status mutation;
    # #1 contributes its Stage mutation instead.
    mirror = {r: MirrorItem(r, f"PVTI_{i + 1}", "Ready" if i == 0 else "Backlog", 480.0, None,
                            T - timedelta(days=1))
              for i, r in enumerate(refs)}
    drifted = _facts(items=items, mirror=mirror, open_issue_nodes={}, node_ids={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=drifted)
    asyncio.run(loop.tick())
    assert [len(m) for m, _, _ in calls["record"]] == [20, 1]      # chunked at MAX_ALIASES
    assert len(calls["park"]) == 20
    assert all(reason.startswith("field vocabulary drift") for _, reason in calls["park"])
    assert any(lvl == "error" and kind == "gh_reconcile_parked" for lvl, _, kind in ctx.events)
    # the second batch was still built and sent, and applied
    assert len(_batches(calls)) == 1
    assert calls["applied"] == ["r0"]
    assert ctx.gh_reconcile_counts["parked"] == 20
    assert ctx.gh_reconcile_counts["applied"] == 1


def test_ensure_fields_creates_missing_and_extends_status_options():
    sparse = _facts(field_cache={"Status": {"field_id": "F_s", "options": {"Todo": "O_t", "Done": "O_d"}}},
                    items=[], mirror={}, open_issue_nodes={})

    def mutate(doc):
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        if "createProjectV2Field" in doc and '"Score"' in doc:
            return {"data": {"createProjectV2Field": {"projectV2Field": {"id": "F_sc", "name": "Score"}}}}
        if "createProjectV2Field" in doc:
            return {"data": {"createProjectV2Field": {"projectV2Field": {"id": "F_st", "name": "Stage",
                    "options": [{"id": "O_i", "name": "intake"}]}}}}
        return {"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F_s", "name": "Status",
                "options": [{"id": f"O_{n}", "name": n} for n in STATUS_OPTIONS]}}}}

    ctx = Ctx(); loop, calls = _loop(ctx, facts=sparse, mutate=mutate, config={"migrate": True})
    asyncio.run(loop.tick())
    kinds = [m.kind for m in sum((r[0] for r in calls["record"]), [])]
    assert kinds == ["create_field", "create_field", "set_field_options"]
    assert calls["cache"] and calls["cache"][-1][1]["Status"]["options"]["Needs human"] == "O_Needs human"
    assert calls["cache"][-1][1]["Score"]["field_id"] == "F_sc"


def test_status_option_repair_is_a_union_and_never_drops_a_human_column():
    """updateProjectV2Field REPLACES the option set, so the document must carry the
    board's existing options (in their order) plus the missing spec names — a column
    a human added ("Icebox") must survive the migration, and must survive it with its
    option **id** intact (#987): without the id GitHub mints a new one and every item
    sitting in that column loses its Status."""
    human = _facts(field_cache={"Score": {"field_id": "F_sc", "options": {}},
                                "Stage": {"field_id": "F_st", "options": {}},
                                "Status": {"field_id": "F_s",
                                           "options": {"Icebox": "O_i", "Backlog": "O_b"}}},
                   items=[], mirror={}, open_issue_nodes={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=human, config={"migrate": True})
    asyncio.run(loop.tick())
    doc = [d for d in _docs(calls) if "updateProjectV2Field" in d][0]
    assert '{id: "O_i", name: "Icebox"' in doc               # id echoed -> id preserved
    assert '{id: "O_b", name: "Backlog"' in doc
    # spec names the board doesn't have yet carry no id: they are genuinely new options
    assert all(f'{{name: "{n}"' in doc for n in STATUS_OPTIONS if n != "Backlog")
    assert doc.index('"Icebox"') < doc.index('"Ready"')      # existing options keep their order
    (m,) = calls["record"][0][0]
    assert m.new_value[:2] == ["Icebox", "Backlog"] and set(STATUS_OPTIONS) <= set(m.new_value)


def test_ensure_fields_is_noop_when_board_matches_spec():
    full = _facts(field_cache={**CACHE, "Status": {"field_id": "F_s",
                                                    "options": {n: f"O_{n}" for n in STATUS_OPTIONS}}},
                  items=[], mirror={}, open_issue_nodes={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=full, config={"migrate": True})
    asyncio.run(loop.tick())
    assert calls["record"] == [] and calls["cache"] == []


def test_incomplete_board_parks_the_product_when_migrate_is_off():
    """Bootstrap is a migration act (it REPLACES the Status option set), so it never
    runs unattended. Outside migrate an incomplete board is not repaired: the product
    parks loudly and writes nothing, because a set_field for a missing field/option
    cannot be built safely."""
    sparse = _facts(field_cache={"Status": {"field_id": "F_s", "options": {"Todo": "O_t"}}})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=sparse)
    asyncio.run(loop.tick())
    assert calls["record"] == [] and _batches(calls) == [] and calls["cache"] == []
    assert ctx.gh_reconcile_counts["parked"] == 1 and ctx.gh_reconcile_counts["planned"] == 0
    assert any(lvl == "error" and kind == "gh_reconcile_parked" for lvl, _, kind in ctx.events)
    missing = [d["missing"] for d in ctx.details if d and "missing" in d][0]
    assert "Score" in missing and "Stage" in missing
    assert any(m.startswith("Status options: ") and "Ready" in m for m in missing)


def _parked_events(ctx):
    return [s for lvl, s, kind in ctx.events if lvl == "error" and kind == "gh_reconcile_parked"]


def test_incomplete_board_park_emit_is_latched_per_product():
    """The loop ticks every two minutes and "board fields incomplete" needs ONE human action
    to clear, so an unlatched emit is an error push every cycle, forever. The park is still
    COUNTED every cycle (the status card must keep showing it) — only the notification is
    one-shot, and only for as long as the gap stays the same."""
    cache = {"Status": {"field_id": "F_s", "options": {"Todo": "O_t"}}}
    ctx = Ctx(); loop, calls = _loop(ctx, load_facts_fn=lambda p: _facts(field_cache=cache))
    for _ in range(3):
        asyncio.run(loop.tick())
        assert ctx.gh_reconcile_counts["parked"] == 1     # counted on every cycle
    assert len(_parked_events(ctx)) == 1                  # emitted once

    # A CHANGED gap is new information and speaks again.
    cache["Score"] = {"field_id": "F_sc", "options": {}}
    asyncio.run(loop.tick())
    assert len(_parked_events(ctx)) == 2
    assert "Score" not in _parked_events(ctx)[1]

    # A gap that RESOLVES clears the latch, so the next regression is loud again — a latch that
    # only ever set would silence the second occurrence of a real problem.
    current = [{"Status": {"field_id": "F_s", "options": {"Todo": "O_t"}}}]
    ctx2 = Ctx(); loop2, _ = _loop(ctx2, load_facts_fn=lambda p: _facts(field_cache=current[0]))
    asyncio.run(loop2.tick())
    assert len(_parked_events(ctx2)) == 1
    current[0] = CACHE                                    # human bootstraps the board
    asyncio.run(loop2.tick())
    assert len(_parked_events(ctx2)) == 1 and ctx2.gh_reconcile_counts["parked"] == 0
    current[0] = {"Status": {"field_id": "F_s", "options": {"Todo": "O_t"}}}   # …and it regresses
    asyncio.run(loop2.tick())
    assert len(_parked_events(ctx2)) == 2


def test_half_done_bootstrap_stops_before_cascading_into_item_batches():
    """A bootstrap mutation can fail (or already be parked) and ``_ensure_fields`` returns
    whatever cache it managed to build. Re-checking the gap stops the cycle HERE: every
    set_field for the still-missing Score would raise UnknownOption in build_batch and park a
    whole 20-row batch, burying the one real failure under twenty derived ones."""
    sparse = _facts(field_cache={"Stage": {"field_id": "F_st", "options": {"build": "O_b"}},
                                 "Status": {"field_id": "F_s",
                                            "options": {n: f"O_{n}" for n in STATUS_OPTIONS}}})

    def mutate(doc):
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        return {"errors": [{"message": "boom"}]}          # Score creation fails

    ctx = Ctx(); loop, calls = _loop(ctx, facts=sparse, mutate=mutate, config={"migrate": True})
    asyncio.run(loop.tick())
    # Exactly one bootstrap mutation was attempted; no item batch followed it.
    assert [m.kind for m in sum((r[0] for r in calls["record"]), [])] == ["create_field"]
    assert len(_batches(calls)) == 1 and "createProjectV2Field" in _batches(calls)[0]
    assert calls["park"] == []                            # no journal row parked for drift
    assert ctx.gh_reconcile_counts["planned"] == 1        # the bootstrap row only
    assert ctx.gh_reconcile_counts["failed"] == 1 and ctx.gh_reconcile_counts["parked"] == 1
    assert any("bootstrap did not complete" in s for s in _parked_events(ctx))
    assert calls["sync"] == []                            # nothing applied -> no re-ingest


def test_dry_run_is_exempt_from_the_post_bootstrap_gap_check():
    """A dry run writes nothing, so ``_ensure_fields`` returns the cache unchanged by
    construction — gating on the post-bootstrap gap would erase the preview that the enable
    sequence's step 2 depends on. It previews the bootstrap AND the item mutations it can
    still render; drift on the not-yet-created fields shows up as "would park"."""
    sparse = _facts(field_cache={"Stage": {"field_id": "F_st", "options": {"build": "O_b"}},
                                 "Status": {"field_id": "F_s",
                                            "options": {n: f"O_{n}" for n in STATUS_OPTIONS}}})
    ctx = Ctx(); loop, calls = _loop(ctx, mode="dry_run", facts=sparse, config={"migrate": True})
    asyncio.run(loop.tick())
    kinds = [m.kind for m in sum((r[0] for r in calls["record"]), [])]
    assert "create_field" in kinds and "set_field" in kinds
    assert calls["mutate"] == [] and calls["park"] == []
    assert not any("bootstrap did not complete" in s for s in _parked_events(ctx))


def test_post_bootstrap_gap_emit_is_latched_too():
    """Same one-shot rule as the migrate-off park: a board whose bootstrap keeps failing
    (fail, fail → parked, then skipped as parked) must not push an error every 120 s."""
    sparse = _facts(field_cache={"Status": {"field_id": "F_s", "options": {"Todo": "O_t"}}})

    def mutate(doc):
        return RATE_OK if doc.startswith("query { rateLimit") else {"errors": [{"message": "boom"}]}

    ctx = Ctx(); loop, _calls = _loop(ctx, facts=sparse, mutate=mutate, config={"migrate": True})
    for _ in range(3):
        asyncio.run(loop.tick())
        assert ctx.gh_reconcile_counts["parked"] >= 1        # counted every cycle
    assert len(_parked_events(ctx)) == 1


def test_bootstrap_failure_parks_and_is_then_never_replanned():
    """A bootstrap mutation gets the same no-blind-retry treatment as an item one:
    fail once → journaled failed; fail twice → parked; parked → never planned again."""
    sparse = _facts(field_cache={"Stage": {"field_id": "F_st", "options": {}},
                                 "Status": {"field_id": "F_s",
                                            "options": {n: f"O_{n}" for n in STATUS_OPTIONS}}},
                    items=[], mirror={}, open_issue_nodes={})

    def mutate(doc):
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        return {"errors": [{"message": "boom"}]}

    key = Mutation("create_field", "PVT_1", "Score", None, {"dataType": "NUMBER"},
                   "fields:ccswitch").key()
    fcs, parked = {}, set()
    ctx = Ctx(); loop, calls = _loop(ctx, facts=sparse, mutate=mutate, config={"migrate": True},
                                     failure_counts=fcs, parked=parked)
    asyncio.run(loop.tick())
    assert calls["failed"] == [("r0", "boom")] and calls["park"] == []
    fcs[key] = 1                                    # the journal now knows about failure #1
    asyncio.run(loop.tick())
    assert calls["park"] == [("r0", "failed twice: boom")]
    assert any(lvl == "error" and kind == "gh_reconcile_parked" for lvl, _, kind in ctx.events)
    parked.add(key)
    before = (len(calls["record"]), len(calls["mutate"]))
    asyncio.run(loop.tick())
    # third tick: only the rate-budget query goes out; nothing journaled, nothing mutated
    assert len(calls["record"]) == before[0] and len(calls["mutate"]) == before[1] + 1
    assert _docs(calls)[-1].startswith("query { rateLimit")


def test_dry_run_surfaces_vocabulary_drift_without_touching_journal_rows():
    # #5's Stage decision ("deploy") has no option id in CACHE (which only has "build") ->
    # unknown-option drift. Status/Score already match the mirror, so the drifted Stage
    # mutation is the batch.
    drifted = _facts(items=[_item(5, initiative_stage="deploy", initiative_stage_at=T)],
                     mirror={f"{REPO}#5": MirrorItem(f"{REPO}#5", "PVTI_5", "Ready", 480.0, None,
                                                     T - timedelta(days=1))},
                     open_issue_nodes={})
    ctx = Ctx(); loop, calls = _loop(ctx, mode="dry_run", facts=drifted)
    asyncio.run(loop.tick())
    assert calls["record"][0][2] is True                 # journaled as a dry-run record
    assert calls["mutate"] == [] and _batches(calls) == []
    assert calls["park"] == []                           # rows keep their dry_run state
    assert ctx.gh_reconcile_counts["parked"] == 1
    assert any(lvl == "error" and kind == "gh_reconcile_parked" for lvl, _, kind in ctx.events)
    assert any(d and d.get("dry_run") is True and d.get("error") for d in ctx.details)


def test_dry_run_drift_emit_is_latched_like_the_incomplete_board_one():
    """Sibling of the incomplete-board latch. An apply parks the drifted journal rows, so the
    same drift is never replanned and its emit is naturally one-shot. A dry run parks nothing
    by design, so it re-plans identical drift every two minutes — without a latch, leaving dry
    run on (which the review step asks for) is an error push forever. Still counted every cycle."""
    cache = dict(CACHE)
    drifted = dict(items=[_item(5, initiative_stage="deploy", initiative_stage_at=T)],
                   mirror={f"{REPO}#5": MirrorItem(f"{REPO}#5", "PVTI_5", "Ready", 480.0, None,
                                                   T - timedelta(days=1))},
                   open_issue_nodes={})
    ctx = Ctx(); loop, _ = _loop(ctx, mode="dry_run",
                                 load_facts_fn=lambda p: _facts(field_cache=cache, **drifted))
    for _ in range(3):
        asyncio.run(loop.tick())
        assert ctx.gh_reconcile_counts["parked"] == 1     # counted every cycle
    assert len(_parked_events(ctx)) == 1                  # emitted once

    # Board vocabulary gains the option -> drift resolves, latch clears, a later drift speaks.
    cache["Stage"] = {"field_id": "F_st", "options": {"build": "O_b", "deploy": "O_d"}}
    asyncio.run(loop.tick())
    assert len(_parked_events(ctx)) == 1 and ctx.gh_reconcile_counts["parked"] == 0
    cache["Stage"] = {"field_id": "F_st", "options": {"build": "O_b"}}
    asyncio.run(loop.tick())
    assert len(_parked_events(ctx)) == 2


def test_apply_mode_drift_emit_is_not_latched():
    """The latch is dry-run only: an apply parks the row, so a repeat emit means a genuinely
    new row hit the same drift — that must stay loud."""
    drifted = _facts(items=[_item(5, initiative_stage="deploy", initiative_stage_at=T)],
                     mirror={f"{REPO}#5": MirrorItem(f"{REPO}#5", "PVTI_5", "Ready", 480.0, None,
                                                     T - timedelta(days=1))},
                     open_issue_nodes={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=drifted)
    for n in (1, 2):
        asyncio.run(loop.tick())
        assert len(_parked_events(ctx)) == n
    assert calls["park"] == [("r0", "field vocabulary drift: Stage='deploy' has no option id")] * 2


def test_dry_run_dedupes_journal_rows_and_prunes_stale_ones():
    """A dry run re-plans the same set every tick. Rows already journaled inside the
    24h window are not re-recorded (though they still count as planned), and rows past
    that window are pruned at the top of the cycle."""
    ctx = Ctx(); loop, calls = _loop(ctx, mode="dry_run")
    asyncio.run(loop.tick())
    first = calls["record"][0][0]
    assert [m.field for m in first] == ["Status", "Score"]
    assert calls["prune"] == [T - timedelta(hours=24)]
    assert calls["dry_keys"] == [T - timedelta(hours=24)]
    # second tick: the journal already holds both keys -> nothing new is recorded
    ctx2 = Ctx(); loop2, calls2 = _loop(ctx2, mode="dry_run",
                                        dry_run_keys={m.key() for m in first})
    asyncio.run(loop2.tick())
    assert calls2["record"] == []
    assert ctx2.gh_reconcile_counts["planned"] == 2      # still reported as planned
    assert calls2["prune"] == [T - timedelta(hours=24)]


def test_apply_mode_neither_prunes_nor_reads_dry_run_keys_beyond_the_prune():
    """Only a dry run produces dry-run rows, so the dedupe read is dry-run-only; the
    prune runs every cycle (an operator who leaves dry run then switches to apply
    still gets the stale rows cleaned up)."""
    ctx = Ctx(); loop, calls = _loop(ctx)
    asyncio.run(loop.tick())
    assert calls["dry_keys"] == [] and calls["prune"] == [T - timedelta(hours=24)]


def test_own_past_write_is_replanned_when_the_decision_falls_back():
    """The un-sticking rule, end of the seam: the mirror holds "In progress" stamped at
    OUR write time; the session is over so the decision is Ready with an older
    classified_at. With own_values wired the push happens; without it the field is stuck."""
    ended = _facts(items=[_item(5)],
                   mirror={f"{REPO}#5": MirrorItem(f"{REPO}#5", "PVTI_5", "In progress", 480.0,
                                                   None, T + timedelta(hours=1))})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=ended)
    asyncio.run(loop.tick())
    assert calls["record"] == []                                  # sticky without the seam
    ctx = Ctx(); loop, calls = _loop(ctx, facts=ended,
                                     own_values={("PVTI_5", "Status"): "In progress"})
    asyncio.run(loop.tick())
    assert [(m.field, m.new_value) for m in calls["record"][0][0]] == [("Status", "Ready")]


def test_one_products_failure_does_not_stop_the_next():
    other = ProductSyncView("p2", "other", "other", "ghdb2", "PVT_2", [REPO])

    def load(product):
        if product.project_id == "p1":
            raise RuntimeError("facts exploded")
        return _facts(project_id="p2")

    ctx = Ctx(); loop, calls = _loop(ctx, products=[PRODUCT, other], load_facts_fn=load)
    asyncio.run(loop.tick())
    assert len(calls["record"]) == 1 and calls["sync"] == ["p2"]
    assert any(lvl == "warn" and kind == "gh_reconcile_error" for lvl, _, kind in ctx.events)
    assert ctx.gh_reconcile_counts["products"] == 2
    assert ctx.gh_reconcile_counts["applied"] == 2


def test_parked_mutations_are_never_replanned():
    key = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:x").key()
    ctx = Ctx(); loop, calls = _loop(ctx, parked={key})
    asyncio.run(loop.tick())
    assert [m.field for m in calls["record"][0][0]] == ["Score"]


def test_rate_budget_skips_cycle_when_low():
    def mutate(doc):
        return {"data": {"rateLimit": {"cost": 5, "remaining": 100, "resetAt": None}}}

    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate, config={"min_rate_remaining": 500})
    asyncio.run(loop.tick())
    assert calls["record"] == []
    assert len(calls["mutate"]) == 1 and _docs(calls)[0].startswith("query { rateLimit")
    assert ctx.gh_reconcile_counts["skipped_rate"] == 1


def test_status_card_reads_counts():
    class C:
        gh_reconcile_counts = {"products": 2, "planned": 3, "applied": 3, "failed": 0, "parked": 1,
                               "skipped_stale": 0, "skipped_rate": 0, "halted_cap": 0, "fenced": 0,
                               "disabled": 0, "dry_run": 0}
    s = GhReconcile().status(C())
    assert "3 applied" in s.text and "1 parked" in s.text and s.level == "warn"
    assert GhReconcile().status(None).level == "idle"


def test_field_gap_needs_a_field_id_not_just_a_cache_key():
    """``_render`` raises UnknownOption for a cached field with no ``field_id``, so a bare
    key must not read as "present" — a half-written cache entry would otherwise sail past
    the gate and park every item row in the batch."""
    assert GhReconcileLoop._field_gap(CACHE) == []
    half = {**CACHE, "Score": {"options": {}}}                    # key present, id missing
    assert GhReconcileLoop._field_gap(half) == ["Score"]
    assert GhReconcileLoop._field_gap({**CACHE, "Stage": {"field_id": ""}}) == ["Stage"]
    # A Status field we cannot address at all is reported as the field, not as its options.
    assert GhReconcileLoop._field_gap({**CACHE, "Status": {"options": {}}}) == ["Status"]


def test_bootstrap_repairs_exactly_what_the_gap_reports():
    """``_ensure_fields`` and ``_field_gap`` must agree on what "missing" means. If the gate
    refuses on a field_id the bootstrap considers present (because the KEY is there), the
    product parks on every tick with nothing that could ever fix it."""
    half = _facts(field_cache={**CACHE, "Score": {"options": {}}},   # key, but no field_id
                  items=[], mirror={}, open_issue_nodes={})

    def mutate(doc):
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        return {"data": {"createProjectV2Field": {"projectV2Field": {"id": "F_sc",
                                                                     "name": "Score"}}}}

    ctx = Ctx(); loop, calls = _loop(ctx, facts=half, mutate=mutate, config={"migrate": True})
    asyncio.run(loop.tick())
    assert [(m.kind, m.field) for m in sum((r[0] for r in calls["record"]), [])] == \
        [("create_field", "Score")]
    assert calls["cache"][-1][1]["Score"]["field_id"] == "F_sc"
    assert ctx.gh_reconcile_counts["parked"] == 0            # repaired, not parked


def test_document_level_error_fails_the_batch_without_ever_parking_it():
    """A 502/abuse-limit error has no ``path``: it fails all 20 aliases at once and judges
    none of them. Recording it as an ordinary failure would park the whole batch on the
    second blip, so it is marked transient — visible, retried, never counted."""
    def mutate(doc):
        if doc.startswith("query { rateLimit"):
            return RATE_OK
        return {"data": None, "errors": [{"message": "502 Bad Gateway"}]}

    # failure_counts says both mutations have already failed once: an ordinary failure here
    # WOULD park them (see test_per_alias_failure_marks_failed_and_second_failure_parks).
    key = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:x").key()
    ctx = Ctx(); loop, calls = _loop(ctx, mutate=mutate, failure_counts={key: 1})
    asyncio.run(loop.tick())
    assert calls["park"] == [] and calls["applied"] == []
    assert [rid for rid, _ in calls["failed"]] == ["r0", "r1"]
    assert all(err.startswith("transient: ") and "502" in err for _, err in calls["failed"])
    assert ctx.gh_reconcile_counts["failed"] == 2 and ctx.gh_reconcile_counts["parked"] == 0
    assert any(lvl == "warn" and kind == "gh_reconcile_error" for lvl, _, kind in ctx.events)


def test_mode_off_clears_the_status_card():
    """An automation that is switched off is not "12 applied, 1 parked" — leaving the last
    run's counts stashed makes a dormant loop read as a live one."""
    ctx = Ctx(); loop, _ = _loop(ctx)
    asyncio.run(loop.tick())
    assert ctx.gh_reconcile_counts["applied"] == 2
    off, _ = _loop(ctx, mode="off")
    asyncio.run(off.tick())
    assert ctx.gh_reconcile_counts is None
    assert GhReconcile().status(ctx).text == "idle"


def test_superseded_applied_rows_are_pruned_every_cycle():
    ctx = Ctx(); loop, calls = _loop(ctx)
    asyncio.run(loop.tick())
    assert calls["prune_applied"] == [T - timedelta(days=30)]
