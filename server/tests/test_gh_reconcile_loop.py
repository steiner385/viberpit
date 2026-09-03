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
          failure_counts=None, parked=None, products=None, load_facts_fn=None):
    # calls["order"] interleaves the journal and GraphQL seams so the write-ahead
    # ordering guarantee (record BEFORE mutate, every batch) is directly assertable.
    calls = {"record": [], "applied": [], "failed": [], "park": [], "sync": [], "mutate": [],
             "cache": [], "order": []}

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
        now_utc=lambda: T)
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


def test_ensure_fields_creates_missing_and_fixes_status_options():
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

    ctx = Ctx(); loop, calls = _loop(ctx, facts=sparse, mutate=mutate)
    asyncio.run(loop.tick())
    kinds = [m.kind for m in sum((r[0] for r in calls["record"]), [])]
    assert kinds == ["create_field", "create_field", "set_field_options"]
    assert calls["cache"] and calls["cache"][-1][1]["Status"]["options"]["Needs human"] == "O_Needs human"
    assert calls["cache"][-1][1]["Score"]["field_id"] == "F_sc"


def test_ensure_fields_is_noop_when_board_matches_spec():
    full = _facts(field_cache={**CACHE, "Status": {"field_id": "F_s",
                                                    "options": {n: f"O_{n}" for n in STATUS_OPTIONS}}},
                  items=[], mirror={}, open_issue_nodes={})
    ctx = Ctx(); loop, calls = _loop(ctx, facts=full)
    asyncio.run(loop.tick())
    assert calls["record"] == [] and calls["cache"] == []


def test_dry_run_surfaces_vocabulary_drift_without_touching_journal_rows():
    ctx = Ctx(); loop, calls = _loop(ctx, mode="dry_run", facts=_facts(field_cache={}))
    asyncio.run(loop.tick())
    assert calls["record"][0][2] is True                 # journaled as a dry-run record
    assert calls["mutate"] == [] and _batches(calls) == []
    assert calls["park"] == []                           # rows keep their dry_run state
    assert ctx.gh_reconcile_counts["parked"] == 2
    assert any(lvl == "error" and kind == "gh_reconcile_parked" for lvl, _, kind in ctx.events)
    assert any(d and d.get("dry_run") is True and d.get("error") for d in ctx.details)


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
