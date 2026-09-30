# Decision escalation PR 1 — per-product skip breakdown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /api/pm/queue`, the Backlog Runner's status card and the assistant's `get_priority_queue` say per product how many issues are dispatchable and why the rest were skipped, instead of one fleet-wide `skipped` map.

**Architecture:** The queue route and the runner's `_backlog_query_queue` already rank one product at a time and get a `skipped_p` map per product from `rank()`; today both sum it away. This PR keeps the per-product map on the wire (`skippedByProduct`, `dispatchableByProduct`), threads it into the runner's `products_out` rows and `counts` (`held`), and lets the assistant tool narrow to one product. Pure additive payload changes; no ranking rule changes.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async (server), pytest; TypeScript types only on the web side.

**Spec:** `docs/superpowers/specs/2026-09-30-decision-escalation-design.md` §4.1, §6 (`get_priority_queue` row), §10 PR 1.

## Global Constraints

- Branch off `origin/master` (`git fetch && git checkout -b feat/queue-skip-breakdown origin/master`), never local `master`.
- Run tests with the main checkout's env: `env -u VIBERPIT_SESSIOND_HOST -u VIBERPIT_SESSIOND_CERT -u VIBERPIT_SESSIOND_KEY -u VIBERPIT_SESSIOND_CA -u VIBERPIT_DAEMON_ROLE -u CCSWITCH_DAEMON_ROLE /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …` (if `/home/tony/GitHub/viberpit/.venv` is absent, find the sibling checkout that has one: `ls -d /home/tony/GitHub/*/.venv`).
- Gate before pushing: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, the touched server test files, then the full `pytest server/tests/ -n auto --dist loadfile --timeout=300 -m "not slow"` in the background tee'd to `/tmp/pr1-full.log`. Web: `(cd web && npm run typecheck)`.
- Commit messages and PR bodies go in a file and are passed with `-F` / `--body-file` (the Bash hook denies command text pairing deploy words with prod words). End commits with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; end the PR body with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- No absolute date literals in tests. Never `func.now()`.
- `SKIP_REASONS` in `server/pm/queue.py` stays the single list of reasons; do not add a parallel enum.

## Review Focus

1. A product with a linked board but zero rows (nothing open) must appear in `dispatchableByProduct` with `0`, not vanish — an operator reading "KinDash: (missing)" cannot tell empty from unonboarded. Pinned in Task 1's second test.
2. `skippedByProduct` values must sum to the fleet `skipped` exactly, reason by reason — a drift between the two would send the assistant chasing a phantom. Pinned in Task 1.
3. `counts["held"]` on the runner card counts HELD_KINDS holds board-wide, which is a superset of the runner's own `needs_human` give-ups — the two tiles must both exist and the text must not spell either (spec 2026-09-28 §2). Pinned in Task 2.
4. `get_priority_queue(product=…)` for a slug with no entries must return an empty `entries` list and `{}` maps, not an error, so the model reports "nothing dispatchable" rather than retrying. Pinned in Task 3.
5. The `held` tile must survive the web's six-tile cap: the count key order is `in_flight, needs_human, eligible, held, blocked, untriaged, …`. Pinned in Task 2 (key-order assertion).

---

### Task 1: `GET /api/pm/queue` returns `skippedByProduct` and `dispatchableByProduct`

**Files:**
- Modify: `server/api/pm.py:913-951` (the `queue()` route body)
- Modify: `web/src/api/roadmap.ts:188-192` (`QueueView`)
- Test: `server/tests/test_pm_queue_api.py`

**Interfaces:**
- Produces: response keys `skippedByProduct: dict[str, dict[str, int]]` (slug → reason → count, only reasons with a non-zero count) and `dispatchableByProduct: dict[str, int]` (slug → `len(entries_p)`, every product that had at least one row). Task 3 consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_queue_api.py` (reuse the module's `_client`, `_seed_ready_and_blocked`, `_set_hold`, `IDENTITY_HEADERS`):

```python
def _seed_second_product_with_a_held_item(c) -> None:
    """A second onboarded product whose only open issue is parked by an
    owner_action hold, so the breakdown has two slugs with different reasons."""
    from server.db import db_session
    from server.db.models import GhProject, GhProjectItem, Project, ProjectRepo, WorkItem

    async def _seed_async() -> None:
        async with db_session() as db:
            proj = Project(slug="kindash", name="kindash")
            db.add(proj)
            await db.flush()
            db.add(ProjectRepo(project_id=proj.id, repo="steiner385/KinDash", is_primary=True))
            wi = WorkItem(repo="steiner385/KinDash", number=7, kind="issue",
                          title="held item", state="open", body="b",
                          url="https://example.com/k7",
                          hold_kind="owner_action", hold_by="tony@example.com",
                          hold_reason="waiting on the owner")
            db.add(wi)
            await db.flush()
            ghp = GhProject(project_id=proj.id, gh_project_number=8, title="kindash")
            db.add(ghp)
            await db.flush()
            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi.id,
                                 status="Ready", score=1.0))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_seed_async)


def test_queue_endpoint_breaks_skips_down_per_product():
    """Spec 2026-09-30 §4.1: the per-product maps the route already computes stay
    on the wire, and they sum to the fleet map reason by reason."""
    with _client() as c:
        _seed_ready_and_blocked(c)
        _seed_second_product_with_a_held_item(c)

        body = c.get("/api/pm/queue", headers=IDENTITY_HEADERS).json()

        assert body["skippedByProduct"] == {"viberpit": {"blocked": 1},
                                            "kindash": {"held": 1}}
        assert body["dispatchableByProduct"] == {"viberpit": 1, "kindash": 0}
        assert body["skipped"] == {"blocked": 1, "held": 1}
        summed: dict[str, int] = {}
        for per_reason in body["skippedByProduct"].values():
            for reason, n in per_reason.items():
                summed[reason] = summed.get(reason, 0) + n
        assert summed == body["skipped"]


def test_queue_endpoint_lists_a_product_with_nothing_dispatchable_as_zero():
    """A product whose every row is skipped reads as 0, never as absent — absent
    would be indistinguishable from "not onboarded"."""
    with _client() as c:
        _seed_ready_and_blocked(c)
        _set_hold(c, "steiner385/viberpit", 100)

        body = c.get("/api/pm/queue?respect_dependencies=false",
                     headers=IDENTITY_HEADERS).json()

        assert body["dispatchableByProduct"] == {"viberpit": 0}
        assert body["skippedByProduct"]["viberpit"]["held"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<venv>/bin/python -m pytest server/tests/test_pm_queue_api.py -k "per_product or nothing_dispatchable" -v`
Expected: FAIL with `KeyError: 'skippedByProduct'`.

- [ ] **Step 3: Keep the per-product maps in the route**

In `server/api/pm.py`, inside `queue()`, replace the loop body and the return:

```python
    entries: list = []
    skipped: dict[str, int] = {}
    # Spec 2026-09-30 §4.1: the per-product map is kept beside the fleet sum. It
    # is the SAME ``skipped_p`` the sum is built from, so the two cannot disagree.
    skipped_by_product: dict[str, dict[str, int]] = {}
    dispatchable_by_product: dict[str, int] = {}
    for slug in sorted(by_product):
        # A product with no row of its own (an unmirrored slug) reads as the policy
        # SCHEMA's defaults, never as a hand-written 0.
        policy = policies.get(slug) or effective_policy(None)
        entries_p, skipped_p = rank(
            by_product[slug], min_status=min_status,
            respect_dependencies=respect_dependencies, now=now,
            # D8: THE bounce threshold, from this product's own policy — the same
            # reading the runner's pool uses.
            give_up_after_bounces=int(policy["give_up_after_bounces"]),
            extra_skip_labels=frozenset(
                lbl.strip().lower() for lbl in policy["skip_labels"]))
        entries.extend(entries_p)
        skipped_by_product[slug] = dict(skipped_p)
        dispatchable_by_product[slug] = len(entries_p)
        for reason, n in skipped_p.items():
            skipped[reason] = skipped.get(reason, 0) + n
    # Merge with ``rank``'s OWN key rather than a hand-copied tuple: ranking per
    # product and re-sorting on score alone would break every cross-product tie by
    # product slug (the loop order) instead of by issue age.
    entries.sort(key=rank_key)

    return {
        "entries": [{
            "ref": e.ref,
            "repo": e.repo,
            "number": e.number,
            "product": e.product,
            "title": e.title,
            "url": e.url,
            "score": e.score,
        } for e in entries],
        "skipped": skipped,
        "skippedByProduct": skipped_by_product,
        "dispatchableByProduct": dispatchable_by_product,
        "generatedAt": now.isoformat(),
    }
```

Extend the route docstring with one sentence: `` ``skippedByProduct`` / ``dispatchableByProduct`` carry the per-slug maps the fleet ``skipped`` is summed from (spec 2026-09-30 §4.1). ``

- [ ] **Step 4: Run the tests to verify they pass**

Run: `<venv>/bin/python -m pytest server/tests/test_pm_queue_api.py -v`
Expected: all PASS (the pre-existing tests still assert `body["skipped"] == {"blocked": 1}`, unchanged).

- [ ] **Step 5: Web type**

In `web/src/api/roadmap.ts` replace the `QueueView` interface:

```ts
export interface QueueView {
  entries: QueueEntry[]
  /** Fleet-wide skip counts by reason (`server/pm/queue.py:SKIP_REASONS`). */
  skipped: Record<string, number>
  /** The same counts per product slug — they sum to `skipped` reason by reason. */
  skippedByProduct: Record<string, Record<string, number>>
  /** Dispatchable entries per product slug; a product with none reads 0, not absent. */
  dispatchableByProduct: Record<string, number>
  generatedAt: string
}
```

Run: `(cd web && npm run typecheck)` — Expected: clean (`skipped` was `unknown`; nothing reads it).

- [ ] **Step 6: Commit**

```bash
git add server/api/pm.py server/tests/test_pm_queue_api.py web/src/api/roadmap.ts
git commit -F /tmp/pr1-c1.txt
```
`/tmp/pr1-c1.txt`: `feat(pm): GET /api/pm/queue keeps the per-product skip maps it already computes` + blank line + one paragraph + the Co-Authored-By trailer.

---

### Task 2: Runner status rows carry the full skip map and a `held` count

**Files:**
- Modify: `server/sessions/orchestrator.py:6878-6885` (`_backlog_query_queue` `products_out` row) and `:7638-7642` (`_backlog_count_candidates` default row)
- Modify: `server/automations/backlog_runner.py:1043-1048` (`_stash_counts` counts) and `:426-436` (`status()` products rows)
- Modify: `web/src/api/automations.ts:6-13` (`AutomationStatusProduct`)
- Test: `server/tests/test_backlog_query_queue.py`, `server/tests/test_backlog_runner_automation.py`

**Interfaces:**
- Produces: `products_out[slug]` gains `"held": int` and `"skipped": dict[str, int]`; `ctx.backlog_counts` gains `"held"` placed right after `"eligible"`; `AutomationStatus.products[i]` gains `held`, `blocked`, `skipped`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_backlog_query_queue.py` (reuse `_init`, `_seed_product`, `_seed_item`, `_orch_with_ctx`, `_config`):

```python
def test_query_queue_reports_held_and_the_full_skip_map_per_product(tmp_path):
    """Spec 2026-09-30 §4.1: the per-product row carries every skip reason, not
    three of nine, and ``held`` is its own key because it is what the escalation
    read model and the card's tile both read."""
    async def run():
        await _init(tmp_path, "heldmap.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=1.0)                              # eligible
        await _seed_item("o/r", 2, score=2.0, hold_kind="owner_action")    # held
        await _seed_item("o/r", 3, score=3.0, hold_kind="decision")        # held
        await _seed_item("o/r", 4, status="Backlog", score=4.0)            # untriaged
        orch = _orch_with_ctx()
        per_product: dict = {}
        await orch._backlog_query_queue(_config(), products_out=per_product)
        row = per_product["viberpit"]
        assert row["eligible"] == 1
        assert row["held"] == 2
        assert row["skipped"] == {"held": 2, "status": 1}
        assert row["untriaged"] == 1  # the existing flat keys are unchanged

    asyncio.run(run())


def test_count_candidates_default_row_has_held_and_skipped_for_an_off_product(tmp_path):
    async def run():
        await _init(tmp_path, "heldoff.db")
        await _seed_product("viberpit", "o/r")
        await _seed_product("kindash", "o/k", policy={"dispatch": "off"})
        await _seed_item("o/r", 1, score=1.0)
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        _e, _b, _u, per_product = await orch._backlog_count_candidates()
        assert per_product["kindash"]["held"] == 0
        assert per_product["kindash"]["skipped"] == {}

    asyncio.run(run())
```

Append to `server/tests/test_backlog_runner_automation.py`:

```python
def test_status_products_rows_carry_held_blocked_and_the_skip_map():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 12, "held": 4, "blocked": 2},
        backlog_products={
            "kwgt-cairn": {"dispatch": "auto", "eligible": 12, "blocked": 2,
                           "untriaged": 0, "bounced": 0, "held": 4,
                           "skipped": {"held": 4, "blocked": 2},
                           "in_flight": 1, "cap": 1}})
    status = BacklogRunner().status(ctx)
    assert status.products == [{"slug": "kwgt-cairn", "dispatch": "auto", "inFlight": 1,
                                "cap": 1, "eligible": 12, "bounced": 0, "held": 4,
                                "blocked": 2, "skipped": {"held": 4, "blocked": 2}}]
    # The counts are tiles, not text (spec 2026-09-28 §2): neither held nor
    # needs-human is spelled in the sentence.
    assert "held" not in status.text and "needs" not in status.text
```

Find the existing `_stash_counts` test (`grep -n "_stash_counts\|backlog_counts\[" server/tests/test_backlog_runner_loop.py`) and add beside it:

```python
@pytest.mark.asyncio
async def test_stash_counts_publishes_held_right_after_eligible():
    """``held`` must survive the web's six-tile cap on ``status.counts``
    (countTiles.ts:MAX_COUNT_TILES), so it is placed before blocked/untriaged."""
    loop, ctx = _loop_with_counts(  # use whatever helper the neighbouring test uses
        per_product={"viberpit": {"dispatch": "auto", "eligible": 1, "blocked": 0,
                                  "untriaged": 0, "bounced": 0, "held": 3,
                                  "skipped": {"held": 3}, "cap": 2},
                     "kindash": {"dispatch": "auto", "eligible": 0, "blocked": 0,
                                 "untriaged": 0, "bounced": 0, "held": 2,
                                 "skipped": {"held": 2}, "cap": 2}})
    await loop._stash_counts({})
    counts = ctx.backlog_counts
    assert counts["held"] == 5
    keys = list(counts)
    assert keys.index("held") == keys.index("eligible") + 1
```

If no such helper exists, build the loop the way the nearest `_stash_counts` test in `test_backlog_runner_loop.py` does (a `BacklogRunnerLoop` with `_ctx = SimpleNamespace()`, `list_target_states` returning `{}`, and `_count_candidates` set to an async lambda returning `(1, 0, 0, per_product)`); the assertion block is what matters.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<venv>/bin/python -m pytest server/tests/test_backlog_query_queue.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_runner_loop.py -k "held or skip_map" -v`
Expected: FAIL with `KeyError: 'held'` / products row mismatch.

- [ ] **Step 3: Thread the map through**

`server/sessions/orchestrator.py`, `_backlog_query_queue` (~6878):

```python
            if products_out is not None:
                products_out[slug] = {
                    "dispatch": policy["dispatch"],
                    "eligible": len(entries_p),
                    "blocked": skipped_p.get("blocked", 0),
                    "untriaged": skipped_p.get("status", 0),
                    "bounced": skipped_p.get("needs_spec", 0),
                    # Spec 2026-09-30 §4.1: ``held`` is its own key (the tile and
                    # the escalation read model both read it) and the whole map
                    # rides along so no reason is dropped on the way to the card.
                    "held": skipped_p.get("held", 0),
                    "skipped": dict(skipped_p),
                }
```

`_backlog_count_candidates` default row (~7638):

```python
            row = per_product.setdefault(slug, {
                "dispatch": policy["dispatch"], "eligible": 0,
                "blocked": 0, "untriaged": 0, "bounced": 0,
                "held": 0, "skipped": {},
            })
```

`server/automations/backlog_runner.py`, `_stash_counts` (~1043):

```python
                eligible, blocked, untriaged, per_product = await self._count_candidates()
                if per_product is not None:
                    # ``held`` sits right after ``eligible`` so it survives the
                    # web's six-tile cap (spec 2026-09-30 §4.1); it counts every
                    # HELD_KINDS hold on the boards, a superset of the runner's
                    # own ``needs_human`` give-ups.
                    counts.update(
                        eligible=eligible,
                        held=sum(int(r.get("held", 0)) for r in per_product.values()),
                        blocked=blocked, untriaged=untriaged)
```

`status()` products rows (~434):

```python
            products_out.append({"slug": slug, "dispatch": p.get("dispatch"),
                                 "inFlight": p.get("in_flight", 0), "cap": p.get("cap", 0),
                                 "eligible": p.get("eligible", 0), "bounced": bounced,
                                 "held": p.get("held", 0), "blocked": p.get("blocked", 0),
                                 "skipped": dict(p.get("skipped") or {})})
```

The existing test `test_status_appends_bounced_count_and_publishes_products` asserts the exact old row; update its expected dict to include `"held": 0, "blocked": 0, "skipped": {}`.

`web/src/api/automations.ts`:

```ts
export interface AutomationStatusProduct {
  slug: string
  dispatch: string
  inFlight: number
  cap: number
  eligible: number
  bounced: number
  /** Items parked by an owner_action / decision hold (spec 2026-09-30 §4.1). */
  held?: number
  blocked?: number
  /** Every skip reason for this product, keyed as `server/pm/queue.py:SKIP_REASONS`. */
  skipped?: Record<string, number>
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `<venv>/bin/python -m pytest server/tests/test_backlog_query_queue.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_runner_loop.py server/tests/test_automations_api.py server/tests/test_backlog_runner_e2e.py -v` and `(cd web && npm run typecheck)`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/automations/backlog_runner.py web/src/api/automations.ts server/tests/test_backlog_query_queue.py server/tests/test_backlog_runner_automation.py server/tests/test_backlog_runner_loop.py
git commit -F /tmp/pr1-c2.txt
```
Message: `feat(runner): status rows carry held and the full per-product skip map`.

---

### Task 3: `get_priority_queue` narrows to a product and passes the breakdown through

**Files:**
- Modify: `server/assistant/tools.py:762-778`
- Modify: `scripts/eval_assistant.py:503` (`_queue()` fixture)
- Test: `server/tests/test_assistant_tools.py:914-950`

**Interfaces:**
- Consumes: Task 1's `skippedByProduct` / `dispatchableByProduct`.
- Produces: tool argument `product` (optional string, a product slug).

- [ ] **Step 1: Write the failing test**

Append after `test_get_priority_queue_omits_min_status_when_unset` in `server/tests/test_assistant_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_priority_queue_narrows_to_one_product_and_keeps_the_breakdown():
    """Spec 2026-09-30 §6: ``product`` narrows entries AND the per-product maps in
    the handler (one endpoint, presentation-only filtering, like list_prs' repo)."""
    def handler(req: httpx.Request):
        return httpx.Response(200, json={
            "entries": [{"ref": "o/k#1", "product": "kindash", "score": 3},
                        {"ref": "o/r#2", "product": "viberpit", "score": 2}],
            "skipped": {"held": 5, "blocked": 1},
            "skippedByProduct": {"kindash": {"held": 4}, "viberpit": {"held": 1, "blocked": 1}},
            "dispatchableByProduct": {"kindash": 1, "viberpit": 1},
            "generatedAt": "2026-09-05T00:00:00+00:00",
        })

    tools = build_tools(_api(handler))
    out = await _tool(tools, "get_priority_queue").handler({"product": "kindash"})
    data = json.loads(out["content"][0]["text"])["data"]
    assert [e["ref"] for e in data["entries"]] == ["o/k#1"]
    assert data["skippedByProduct"] == {"kindash": {"held": 4}}
    assert data["dispatchableByProduct"] == {"kindash": 1}
    assert data["skipped"] == {"held": 4}, "the fleet map is re-summed for the narrowed scope"

    # An unknown slug is an empty answer, not an error: the model should say
    # "nothing dispatchable", not retry.
    out = await _tool(tools, "get_priority_queue").handler({"product": "nope"})
    assert out.get("is_error") is not True
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["entries"] == [] and data["skippedByProduct"] == {}
    assert data["dispatchableByProduct"] == {} and data["skipped"] == {}
```

Also fix the stale payload in `test_get_priority_queue_forwards_min_status_and_caps_entries`: change `{"deps": 4, "status": 9}` to `{"blocked": 4, "status": 9}` in both the fixture and the assertion (the reason string is `blocked`; `deps` never existed).

- [ ] **Step 2: Run the test to verify it fails**

Run: `<venv>/bin/python -m pytest server/tests/test_assistant_tools.py -k priority_queue -v`
Expected: the new test FAILS (entries not narrowed).

- [ ] **Step 3: Implement**

Replace the tool in `server/assistant/tools.py`:

```python
    @spec_tool("get_priority_queue",
               "The global priority queue — what agents will pick up next, ranked across "
               "every onboarded product. `skipped` counts why items did NOT make the "
               "queue, keyed by reason: 'held' (an owner_action or decision hold — see "
               "list_holds / list_decisions), 'blocked' (an open dependency), 'status' "
               "(below the product's minimum board status), 'epic_open', 'assigned', "
               "'claimed', 'live_session', 'bot', 'needs_spec'. `skippedByProduct` and "
               "`dispatchableByProduct` give the same per product slug. `product` narrows "
               "everything to one slug. min_status limits to items at or above a status "
               "('backlog' or 'ready'; default 'ready').",
               _schema(min_status=str, product=str))
    @guarded
    async def get_priority_queue(args: dict[str, Any]) -> Any:
        params = {"min_status": args["min_status"]} if args.get("min_status") else None
        data = await api.get("/api/pm/queue", params=params)
        entries = data.get("entries") or []
        by_product = dict(data.get("skippedByProduct") or {})
        dispatchable = dict(data.get("dispatchableByProduct") or {})
        skipped = dict(data.get("skipped") or {})
        product = (args.get("product") or "").strip()
        if product:
            # Presentation-only narrowing (one endpoint, like list_prs' repo filter):
            # the fleet map is re-summed from the kept slug so the two never disagree.
            entries = [e for e in entries if e.get("product") == product]
            by_product = {k: v for k, v in by_product.items() if k == product}
            dispatchable = {k: v for k, v in dispatchable.items() if k == product}
            skipped = dict(by_product.get(product) or {})
        # `skipped` (reason -> count) is small and is the whole answer to "what is
        # blocked?", so only `entries` is cut. The drop count rides the envelope, so a
        # model seeing 50 rows never reports "50 items queued".
        return Trimmed({**data, "entries": entries[:_QUEUE_ENTRY_CAP], "skipped": skipped,
                        "skippedByProduct": by_product,
                        "dispatchableByProduct": dispatchable},
                       max(0, len(entries) - _QUEUE_ENTRY_CAP))
```

(`list_holds` / `list_decisions` are named in the description ahead of PR 3 landing them; if PR 3 is not merged when this ships, keep the description but drop the parenthetical "see …" clause and re-add it in PR 3.)

In `scripts/eval_assistant.py`, `_queue()`: add `"skippedByProduct"` and `"dispatchableByProduct"` keys computed from the fixture's own entries so evals see the real shape — e.g. after building `entries`, `by = {}`; for each entry `by.setdefault(e["product"], 0); by[e["product"]] += 1`; return `{..., "skippedByProduct": {slug: dict(existing skipped) for one slug}, "dispatchableByProduct": by}`. Read the function first and keep whatever `skipped` it already returns.

Add `get_priority_queue` to `_ALL_OPTIONAL_TOOLS` in `server/tests/test_assistant_tools.py` (every argument is now optional — it already was, but the list did not name it; the schema test must validate `{}`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `<venv>/bin/python -m pytest server/tests/test_assistant_tools.py server/tests/test_assistant_conformance.py server/tests/test_eval_assistant_fixtures.py server/tests/test_eval_assistant_grader.py -v`
Expected: PASS (no new tool, so the doc counts and the case hash are untouched).

- [ ] **Step 5: Commit**

```bash
git add server/assistant/tools.py scripts/eval_assistant.py server/tests/test_assistant_tools.py
git commit -F /tmp/pr1-c3.txt
```
Message: `feat(assistant): get_priority_queue narrows to a product and names the real skip reasons`.

---

### Task 4: Gate, push, PR

- [ ] **Step 1:** `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` — Expected: clean.
- [ ] **Step 2:** `<venv>/bin/python -m pytest tests/ -q` (core) — Expected: PASS.
- [ ] **Step 3:** Full server suite in the background: `nohup env -u … <venv>/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -m "not slow" -q > /tmp/pr1-full.log 2>&1 &` — wait for it; Expected: no failures beyond the known full-suite-only flakes listed in the memory note `rocky-ipc-entrypoint-test-fails-on-clean-master.md` (re-run any red file alone).
- [ ] **Step 4:** `(cd web && npm run typecheck && npm run lint)` — Expected: clean.
- [ ] **Step 5:** Check in-flight PRs touching `server/api/pm.py` queue route or `backlog_runner.py status`: the GraphQL query in `.agents/instructions/core.md` § "Before implementing…" with path regex `server/api/pm.py|backlog_runner.py|assistant/tools.py`. Coordinate or rebase.
- [ ] **Step 6:** `git push -u origin feat/queue-skip-breakdown`; `gh pr create --title "feat(pm): per-product skip breakdown on the queue, the runner card and get_priority_queue" --body-file /tmp/pr1-body.md`; body: what/why, `Refs docs/superpowers/specs/2026-09-30-decision-escalation-design.md §4.1 (PR 1 of 6)`, test list, the trailer. Then `gh pr merge <#> --squash --auto`.
