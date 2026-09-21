# Capacity Settings PR 6 — Pipeline Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-product **pipeline cap** (`max_pipeline_prs`): the most automation PRs a product may have open and unmerged at once; a full pipeline refuses NEW claims (never an existing one), the governor gives a pipeline-full product's slots to the next rank, and every surface (readout, Capacity table, plan card, Roadmap card) shows `pipeline n / cap`.

**Architecture:** One predicate `counts_toward_pipeline(state)` in `backlog_runner.py` (the rule `_backlog_repo_load` already applies: every durable target-state row except `gave_up` and token-less `handed_off`) feeds two bucketers, the existing per-repo `_backlog_repo_load` (fairness) and a new per-product `pipeline_by_product(states)` (the cap). `decide_implementation` gains `pipeline_count` / `pipeline_cap` and emits `product_pipeline_full` through the existing `capacity()` helper (so `existing_slot` → held, never refused). `ProductCapacity` carries `pipeline` / `pipeline_cap` so `_split` treats a full product as `eligible = 0`. The readout publishes `pipeline`, `pipelineCap`, `primaryRepo` per product; the web adds a Pipeline column + input to the one SaveBar form, links a full product's *Waiting on* to the Pipeline page (`?pipeline=<repo>`), and the Roadmap card's capacity line gains the pipeline clause. `ConfigField` gains `nullable` so a blank cap (unlimited) is a real `None`.

**Tech Stack:** Python 3.12 / FastAPI (`server/`), pytest; React 18 + TypeScript + vitest (`web/`).

**Spec:** `docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md` — C13, C23, D13, D5, D8, §5.2 items 4–5, §5.3, §6, §7 (`GET /api/capacity` `products[].pipeline` / `pipelineCap`, `product_pipeline_full`; `policy-schema` `max_pipeline_prs`), §7.1 (`PRODUCT_POLICY_SCHEMA` field; `capacity_plans.budget.pipeline`), §8 row **6**, §9 (`test_capacity_readout.py`, `test_product_admission.py`, `test_capacity_plan.py`, `CapacityDetail.test.tsx` rows), §11 item 3.

## Global Constraints

- **Branch:** `feat/capacity-pr6-pipeline-cap` from `origin/master` (`git fetch && git checkout -b feat/capacity-pr6-pipeline-cap origin/master`). Never branch off local `master`.
- **The field (D13, §7.1):** `ConfigField(key="max_pipeline_prs", label="Pipeline cap", type="int", default=None, min=0, nullable=True, surface="capacity", help="Most automation PRs this product may have open and unmerged at once, counting PRs handed off to PR Doctor. A full pipeline refuses new claims so a fast-claiming product cannot stack CI.")`. Blank (`None`) = unlimited: no constraint, never refused, `∞` on the web. `0` = a real cap (refuses every new claim).
- **The count (D13, C23):** per product, over the runner's durable target states, every row that `counts_toward_pipeline`: not `gave_up`, and not a `handed_off` row without an `ownership_token`. `handed_off` rows with a token COUNT (PR Doctor coverage makes no difference). Bucketed by the `product` recorded on the row. Same rule as `_backlog_repo_load`; one predicate.
- **Admission (D13, governor-spec D8):** constraint code `product_pipeline_full`, layer `product`; refuses NEW claims only — an `existing_slot` re-validation is `product_pipeline_held` (permitted). Text when full: `pipeline full: {count} PRs open (cap {cap})`.
- **Planner (D13):** a pipeline-full product (`pipeline_cap is not None and pipeline >= pipeline_cap`) is planned as `eligible = 0` (its cap = its in-flight floor); its slots go to the next rank; `budget["pipeline"] = {slug: {"open": n, "cap": cap}}` for every product with a cap; `reasons` gains `"{slug}: pipeline full ({open}/{cap} PRs open), its slots go to the next rank"`.
- **Vocabulary (D8):** **Pipeline cap**. Column header **Pipeline**; cell `n / cap`, `∞` when blank. Roadmap line clause `· pipeline n/cap` (`∞` when blank), before the period. No em dashes in new shipped copy.
- **Links (D10):** the Pipeline page is `/pipeline?pipeline=<org/repo>` (`web/src/features/pipeline/useFocusedPipeline.ts` reads `?pipeline=`); build the href with `NAV_BY_ID.pipeline.path` + `?pipeline=` + `encodeURIComponent(repo)`.
- **Tests run as** `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest …` from the worktree root; web as `(cd web && …)` subshells; everything in the foreground.
- **Never `git stash`.** Commit via `git commit -F <file>`; trailer `Co-Authored-By: Claude <model> <noreply@anthropic.com>`. Brand ratchet green; never raise a ceiling; no new `from ccswitch…` import lines.
- No schema migration: the policy is a JSON column, the plan's `budget` is JSON.

---

### Task 1: `ConfigField.nullable` and the `max_pipeline_prs` policy field

**Files:**
- Modify: `server/automations/base.py` (`ConfigField` dataclass + `to_dict`; `validate_config`)
- Modify: `server/automations/product_policy.py` (`PRODUCT_POLICY_SCHEMA`, after `capacity_mode`)
- Modify: `server/tests/test_automation_base.py`, `server/tests/test_pm_api.py::test_policy_schema_and_project_policy_round_trip`

**Interfaces:**
- Produces: `ConfigField.nullable: bool = False` (published by `to_dict` as `"nullable"`); `validate_config` accepts `None` for a nullable field (skipping the type and `min` checks) and still rejects `None` for a non-nullable one; `effective_policy(raw)["max_pipeline_prs"]` is `None` or an `int >= 0`. Tasks 2–3 read it.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_automation_base.py`:
```python
def test_nullable_field_accepts_none_and_skips_the_floor():
    """Spec 2026-09-20 D13: a blank pipeline cap is a real None (unlimited), not 0.
    ``nullable`` is published like ``surface`` and ``deprecated``; a non-nullable
    int still rejects None."""
    nullable = ConfigField(key="cap", label="Cap", type="int", default=None, min=0, nullable=True)
    strict = ConfigField(key="n", label="N", type="int", default=1, min=0)
    assert nullable.to_dict()["nullable"] is True and strict.to_dict()["nullable"] is False
    assert validate_config([nullable], {"cap": None}) == {"cap": None}
    assert validate_config([nullable], {}) == {"cap": None}
    assert validate_config([nullable], {"cap": 3}) == {"cap": 3}
    with pytest.raises(ValueError, match="below the minimum"):
        validate_config([nullable], {"cap": -1})
    with pytest.raises(ValueError, match="expected int"):
        validate_config([strict], {"n": None})
```
(`pytest` and `validate_config` are already imported in that file; check.)

In `test_pm_api.py::test_policy_schema_and_project_policy_round_trip`, after the `weight` assertions add:
```python
        # Spec 2026-09-20 D13: the pipeline cap is nullable (blank = unlimited),
        # Capacity-owned, floor 0.
        assert fields["max_pipeline_prs"]["label"] == "Pipeline cap"
        assert fields["max_pipeline_prs"]["default"] is None
        assert fields["max_pipeline_prs"]["nullable"] is True
        assert fields["max_pipeline_prs"]["min"] == 0
        assert fields["max_pipeline_prs"]["surface"] == "capacity"
```
and after the existing PATCH round-trip (the `weight: 4` one from PR 4) add three PATCHes on the same slug (mirror that PATCH's exact body/headers shape):
```python
        r = c.patch(f"/api/pm/projects/{slug}/policy", json={"policy": {"max_pipeline_prs": 6}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200 and r.json()["automationPolicy"]["max_pipeline_prs"] == 6
        r = c.patch(f"/api/pm/projects/{slug}/policy", json={"policy": {"max_pipeline_prs": None}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200 and r.json()["automationPolicy"]["max_pipeline_prs"] is None
        r = c.patch(f"/api/pm/projects/{slug}/policy", json={"policy": {"max_pipeline_prs": -1}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 422
```
(If the round-trip test's later audit-count assertion depends on the number of PATCHes — PR 4's implementer had to bump it — adjust it the same way and say so in the report.)

- [ ] **Step 2: Run to verify they fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_automation_base.py server/tests/test_pm_api.py::test_policy_schema_and_project_policy_round_trip -q`
Expected: FAIL (`nullable` unexpected kwarg; `KeyError: 'max_pipeline_prs'`).

- [ ] **Step 3: Implement**

`server/automations/base.py` — after `deprecated`:
```python
    # Spec 2026-09-20 D13: ``None`` is a legal stored value meaning "no cap"
    # (the pipeline cap). Validation lets None through without the type or
    # ``min`` check; a non-nullable field still rejects it. Published so the
    # browser renders "blank = unlimited" instead of coercing blank to 0.
    nullable: bool = False
```
`to_dict` gains `"nullable": self.nullable`. In `validate_config`, inside `if f.key in config:` before the `_type_ok` check:
```python
            if value is None and f.nullable:
                out[f.key] = None
                continue
```

`server/automations/product_policy.py` — after the `capacity_mode` field:
```python
    # Spec 2026-09-20 D13 (C23): the pin bounds SESSIONS, not PRs in CI — once PR
    # Doctor covers a repo a slot frees at hand-off, so a fast-claiming product
    # can stack any number of PRs in CI. This caps the product's OPEN, UNMERGED
    # automation PRs (counted from the runner's durable target states: every row
    # except gave_up and token-less handed_off, i.e. the same rule as the
    # per-repo fairness load). Blank = unlimited; 0 refuses every new claim.
    ConfigField(key="max_pipeline_prs", label="Pipeline cap", type="int", default=None, min=0,
                nullable=True, surface="capacity",
                help="Most automation PRs this product may have open and unmerged at once, "
                     "counting PRs handed off to PR Doctor. A full pipeline refuses new "
                     "claims so a fast-claiming product cannot stack CI."),
```
Update the module docstring's field list sentence (`its pin and cap mode`) → `its pin, cap mode and pipeline cap`.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command plus `server/tests/test_backlog_policy_seams.py server/tests/test_automations_api.py server/tests/test_capacity_readout.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/automations/base.py server/automations/product_policy.py server/tests/test_automation_base.py server/tests/test_pm_api.py && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/automations/base.py server/automations/product_policy.py --ignore-missing-imports && python3 scripts/brand_ratchet.py`

```bash
git add server/automations/base.py server/automations/product_policy.py server/tests/test_automation_base.py server/tests/test_pm_api.py
git commit -F /tmp/pr6-t1-msg.txt
```
`/tmp/pr6-t1-msg.txt`:
```
feat(policy): max_pipeline_prs — a nullable, Capacity-owned pipeline cap

Spec 2026-09-20 D13. ConfigField.nullable lets a blank cap be a real
None (unlimited) instead of 0.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 2: One pipeline predicate, the per-product count, and admission

**Files:**
- Modify: `server/automations/backlog_runner.py` (beside `is_in_flight` / `in_flight_by_product`, ~line 200-235)
- Modify: `server/sessions/orchestrator.py:6463-6492` (`_backlog_repo_load`)
- Modify: `server/automations/product_policy.py` (`decide_implementation` signature + `capacity()` helper)
- Modify: `server/automations/product_admission.py` (`ImplementationSnapshot.decide`)
- Modify: `server/automations/capacity_readout.py:31` (`LAYER_OF_CODE`)
- Modify tests: `server/tests/test_backlog_count_in_flight.py`, `server/tests/test_product_admission.py`, `server/tests/test_capacity_readout.py` (`LAYER_OF_CODE` test near the `runner_ceiling` entries)

**Interfaces:**
- Consumes: Task 1's `effective_policy(raw)["max_pipeline_prs"]`.
- Produces: `counts_toward_pipeline(state: dict) -> bool`; `pipeline_by_product(states: dict) -> dict[str, int]`; `decide_implementation(..., pipeline_count: int | None = None, pipeline_cap: int | None = None)` emitting `product_pipeline_{unknown,held,full,available}` only when `pipeline_cap is not None`; `LAYER_OF_CODE["product_pipeline_full"] == "product"`. Task 3 reuses `pipeline_by_product` in the readout.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_backlog_count_in_flight.py` — append (the file already has `_init`, `_orch_with_ctx`, `asyncio`):
```python
def test_pipeline_by_product_shares_the_repo_load_rule(tmp_path):
    """Spec 2026-09-20 D13/C23: the per-product pipeline count applies the SAME
    row rule as ``_backlog_repo_load`` (one predicate, ``counts_toward_pipeline``):
    handed_off rows with a token count (PR Doctor coverage makes no difference),
    gave_up and token-less handed_off rows do not; a row with no product counts
    toward no product."""
    from server.automations.backlog_runner import counts_toward_pipeline, pipeline_by_product

    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        await ctx.set_target_state("o/r#1", {"claimed": True, "repo": "o/r", "product": "kindash"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "repo": "o/r", "product": "kindash",
                                             "handed_off": True, "ownership_token": "t"})
        await ctx.set_target_state("o/r#4", {"claimed": True, "repo": "o/r", "product": "kindash",
                                             "handed_off": True})          # legacy: no token
        await ctx.set_target_state("o/k#3", {"gave_up": True, "repo": "o/k", "product": "viberpit"})
        await ctx.set_target_state("o/x#9", {"claimed": True, "repo": "o/x"})   # pre-flip: no product
        states = await ctx.list_target_states()
        assert pipeline_by_product(states) == {"kindash": 2}
        assert await orch._backlog_repo_load() == {"o/r": 2, "o/x": 1}
        assert [counts_toward_pipeline(states[k]) for k in ("o/r#1", "o/r#2", "o/r#4", "o/k#3")] \
            == [True, True, False, False]

    asyncio.run(run())
```

`server/tests/test_product_admission.py` — the file's `coverage()` helper (line 11) takes no arguments and registers `org/repo` to the product slug `"product"` with policy `{"dispatch": "auto"}`; existing tests build `admission.ImplementationSnapshot(coverage(), "on", {...config...}, states, accounting)`. Add a sibling helper that takes the policy, and append:
```python
def _coverage(policy: dict) -> admission.ProductCoverage:
    owner = RepoOwner("p", "product")
    return admission.ProductCoverage(
        {"org/repo": RepoOwnership("org/repo", "org/repo", "unique", owner, (owner,), (), "unique")},
        {"product": policy}, datetime.now(timezone.utc).isoformat())


def _states(n_open: int, *, product: str = "product", handed_off: bool = False) -> dict:
    """*n_open* claims for *product* on org/repo; with ``handed_off`` each has freed
    its SLOT (PR Doctor carries the PR) but still counts toward the pipeline."""
    return {f"org/repo#{i}": {"claimed": True, "repo": "org/repo", "product": product,
                              "ownership_token": f"t{i}", "session_id": f"s{i}",
                              **({"handed_off": True} if handed_off else {})}
            for i in range(1, n_open + 1)}


def test_pipeline_full_refuses_a_new_claim_only(monkeypatch):
    """Spec 2026-09-20 §9: pipeline >= cap refuses a NEW claim with
    product_pipeline_full; an existing_slot re-validation is never refused on it."""
    cov = coverage(policy={"dispatch": "auto", "max_in_flight": 9, "max_pipeline_prs": 2})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(2), {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code: c for c in decision.constraints}
    assert not decision.permitted and codes["product_pipeline_full"].state == "refused"
    assert codes["product_pipeline_full"].text == "pipeline full: 2 PRs open (cap 2)"
    held, _ = snapshot.decide("org/repo", existing_slot=True, existing_token="t1")
    held_codes = {c.code: c.state for c in held.constraints}
    assert "product_pipeline_full" not in held_codes and held_codes["product_pipeline_held"] == "permitted"


def test_blank_pipeline_cap_never_refuses():
    cov = coverage(policy={"dispatch": "auto", "max_in_flight": 9})   # max_pipeline_prs absent → None
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(50), {})
    decision, _ = snapshot.decide("org/repo")
    assert not any(c.code.startswith("product_pipeline") for c in decision.constraints)
    assert decision.permitted


def test_handed_off_prs_count_toward_the_pipeline_regardless_of_pr_doctor():
    """C23: the pin frees a slot at hand-off when PR Doctor covers the repo; the
    pipeline cap does not — a handed-off PR is still open and unmerged."""
    cov = coverage(policy={"dispatch": "auto", "max_in_flight": 9, "max_pipeline_prs": 2})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                _states(2, handed_off=True), {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code: c.state for c in decision.constraints}
    assert codes["product_pipeline_full"] == "refused"
    assert codes["product_capacity_available"] == "permitted"     # the slots ARE free


def test_pipeline_count_unknown_when_states_are_unreadable():
    cov = coverage(policy={"dispatch": "auto", "max_pipeline_prs": 2})
    snapshot = admission.ImplementationSnapshot(cov, "on", {"max_concurrent": 9, "max_spawns_per_day": 99},
                                                None, {})
    decision, _ = snapshot.decide("org/repo")
    assert {c.code for c in decision.constraints} >= {"product_pipeline_unknown"}
```
In the four tests above, every `coverage(policy={...})` call is `_coverage({...})` (the helper defined just above them); the product slug is `"product"` throughout.

`server/tests/test_capacity_readout.py` — find the test asserting `LAYER_OF_CODE["runner_ceiling_full"] == "runner"` and add `assert LAYER_OF_CODE["product_pipeline_full"] == "product"` to it.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_backlog_count_in_flight.py server/tests/test_product_admission.py server/tests/test_capacity_readout.py -q -m "not slow"`
Expected: FAIL (`ImportError: counts_toward_pipeline`; no `product_pipeline_*` codes; `KeyError`).

- [ ] **Step 3: Implement**

`server/automations/backlog_runner.py` — after `in_flight_by_product`:
```python
def counts_toward_pipeline(state: dict) -> bool:
    """THE row rule for "this claim is still in the product's pipeline" (spec
    2026-09-20 D13/C23), shared by the per-repo fairness load
    (``SessionOrchestrator._backlog_repo_load``) and the per-product pipeline cap
    (``pipeline_by_product``). Unlike ``is_in_flight`` (slot occupancy), a
    ``handed_off`` row still counts: PR Doctor carries the PR now, but it is open
    and unmerged, and stacking such PRs in CI is exactly what the cap bounds.
    Excluded: terminal ``gave_up`` rows (needs-human, no active work) and a
    token-less ``handed_off`` row (pre-ownership, #1648/#1891: the sweep cannot
    close it out, so counting it would pin the count high for nothing)."""
    if state.get("gave_up"):
        return False
    if state.get("handed_off") and not state.get("ownership_token"):
        return False
    return True


def pipeline_by_product(states: dict) -> dict[str, int]:
    """``{product slug: open, unmerged automation PRs}`` over the runner's durable
    target states — ``counts_toward_pipeline`` per row, bucketed by the product
    recorded ON the claim (a pre-flip row with no product charges no product)."""
    out: dict[str, int] = {}
    for st in states.values():
        if not counts_toward_pipeline(st):
            continue
        slug = st.get("product")
        if slug:
            out[slug] = out.get(slug, 0) + 1
    return out
```

`server/sessions/orchestrator.py::_backlog_repo_load` — replace the loop body's two `if … continue` checks with `if not counts_toward_pipeline(st): continue` (lazy import `from server.automations.backlog_runner import counts_toward_pipeline` at the top of the method, matching the file's lazy-import style) and shorten the docstring's last two sentences to: "Which rows count is ``backlog_runner.counts_toward_pipeline`` — the same rule the per-product pipeline cap uses (spec 2026-09-20 D13)."

`server/automations/product_policy.py`:
- `decide_implementation` signature: after `daily_cap: int | None,` add `pipeline_count: int | None = None, pipeline_cap: int | None = None,` (keyword-only already). Docstring: add "``pipeline_cap`` None means no pipeline cap: no constraint is emitted at all (D13, blank = unlimited)."
- `capacity()` helper: add a keyword `full_text: str | None = None`; the refused line becomes `add(f"{name}_full", "refused", full_text or f"The {name.replace('_', ' ')} limit is reached ({count}/{cap}).")`.
- After the `daily_budget` call:
```python
    # Spec 2026-09-20 D13: open, unmerged automation PRs vs the product's pipeline
    # cap. Blank cap = unlimited: nothing is emitted. `occupied` keeps governor-spec
    # D8 — a claim that already holds its slot is never refused by a cap.
    if pipeline_cap is not None:
        capacity("product_pipeline", pipeline_count, pipeline_cap, occupied=existing_slot,
                 full_text=f"pipeline full: {pipeline_count} PRs open (cap {pipeline_cap})")
```

`server/automations/product_admission.py::ImplementationSnapshot.decide` — import `pipeline_by_product` beside `in_flight_by_product`; after `product_count` add:
```python
        pipeline_count = None
        if self.states is not None:
            pipeline_count = pipeline_by_product(self.states).get(
                owner.owner.slug if owner and owner.owner else "", 0)
```
and pass `pipeline_count=pipeline_count, pipeline_cap=policy["max_pipeline_prs"]` to `decide_implementation`.

`server/automations/capacity_readout.py::LAYER_OF_CODE` — add `"product_pipeline_full": "product",` after `"product_capacity_full": "product",`.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command plus `server/tests/test_backlog_policy_seams.py server/tests/test_backlog_query_queue.py server/tests/test_product_policy*.py -q -m "not slow"` (glob what exists).
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/ && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/automations/backlog_runner.py server/automations/product_policy.py server/automations/product_admission.py server/automations/capacity_readout.py server/sessions/orchestrator.py --ignore-missing-imports && python3 scripts/brand_ratchet.py`

```bash
git add server/automations/backlog_runner.py server/sessions/orchestrator.py server/automations/product_policy.py server/automations/product_admission.py server/automations/capacity_readout.py server/tests/test_backlog_count_in_flight.py server/tests/test_product_admission.py server/tests/test_capacity_readout.py
git commit -F /tmp/pr6-t2-msg.txt
```
`/tmp/pr6-t2-msg.txt`:
```
feat(admission): product_pipeline_full — a full pipeline refuses new claims

Spec 2026-09-20 D13/C23. counts_toward_pipeline is the one row rule shared
by the per-repo fairness load and the new per-product pipeline count;
decide_implementation emits product_pipeline_* only when a cap is set and
never refuses an existing slot.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 3: The planner gives a full product's slots away; the readout publishes the pipeline

**Files:**
- Modify: `server/automations/capacity_plan.py` (`ProductCapacity`, `_split`, `plan_capacity`)
- Modify: `server/automations/capacity_readout.py` (`build_readout` products loop ~136-180, `readout_to_inputs`)
- Modify tests: `server/tests/test_capacity_plan.py` (`_prod` helper + new tests), `server/tests/test_capacity_readout.py` (`_PRODUCTS` fixture + new test)

**Interfaces:**
- Consumes: Task 2's `pipeline_by_product`; Task 1's policy field.
- Produces: `ProductCapacity(..., pipeline: int = 0, pipeline_cap: int | None = None)`; readout `products[]` gain `"pipeline": int`, `"pipelineCap": int | None`, `"primaryRepo": str | None`; plan `budget["pipeline"]: {slug: {"open": int, "cap": int}}` (only products with a cap); `reasons` entry `"{slug}: pipeline full ({open}/{cap} PRs open), its slots go to the next rank"`. Task 4 reads the three readout fields.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_capacity_plan.py` — extend `_prod` with `pipeline=0, pipeline_cap=None` passed through to `ProductCapacity`, then append:
```python
def test_pipeline_full_product_keeps_its_floor_and_gives_slots_to_the_next_rank():
    """Spec 2026-09-20 D13 / §9: a pipeline-full product is planned as eligible 0 —
    its cap is its in-flight count — and the slots it would have taken go to the
    next rank; the budget and reasons say so."""
    i = _inputs(products=(_prod("kindash", 1, 50, in_flight=1, pipeline=6, pipeline_cap=6),
                          _prod("viberpit", 2, 30, pipeline=1, pipeline_cap=8),
                          _prod("ghostdriver", 3, 5)))
    plan = plan_capacity(i, push_level="max")           # fleet 6 (runner ceiling)
    assert plan.per_product["kindash"] == 1             # floor only
    assert plan.per_product["viberpit"] + plan.per_product["ghostdriver"] == 5
    assert plan.budget["pipeline"] == {"kindash": {"open": 6, "cap": 6},
                                       "viberpit": {"open": 1, "cap": 8}}
    assert "kindash: pipeline full (6/6 PRs open), its slots go to the next rank" in plan.reasons


def test_blank_pipeline_cap_changes_nothing():
    base = plan_capacity(_inputs(), push_level="max")
    with_zero_count = plan_capacity(_inputs(products=(
        _prod("kindash", 1, 50, pipeline=40), _prod("viberpit", 2, 30), _prod("ghostdriver", 3, 5))),
        push_level="max")
    assert with_zero_count.per_product == base.per_product
    assert with_zero_count.budget["pipeline"] == {}
```

`server/tests/test_capacity_readout.py` — give `_PRODUCTS[0]["policy"]` a `"max_pipeline_prs": 3`, and add a second fixture product without a cap if the file's helpers allow (keep `_PRODUCTS` length-compatible with existing assertions — several tests assert `len(r["products"])`; prefer adding the cap to the existing product only). The `_sources`/`_make_sources` `target_states` seams return `{"o/r#1": {...}}` — append:
```python
def test_readout_publishes_the_pipeline_count_cap_and_repo():
    """Spec 2026-09-20 §7/§9: products[].pipeline counts every row that
    counts_toward_pipeline (handed-off rows included, gave_up and token-less
    handed_off excluded), pipelineCap is the policy's max_pipeline_prs (None when
    blank), primaryRepo rides along for the Waiting-on link."""
    states = {
        "o/r#1": {"product": "viberpit", "session_id": "s1", "ownership_token": "t1"},
        "o/r#2": {"product": "viberpit", "session_id": "s2", "ownership_token": "t2", "handed_off": True},
        "o/r#3": {"product": "viberpit", "handed_off": True},                 # no token: excluded
        "o/r#4": {"product": "viberpit", "gave_up": True},                     # terminal: excluded
    }

    async def live(_sid):
        return "running"          # a live session: no defect for the token-bearing rows

    async def run():
        return await build_readout(_make_sources(target_states=states, session_status=live))

    r = asyncio.run(run())
    row = r["products"][0]
    assert row["pipeline"] == 2
    assert row["pipelineCap"] == 3
    assert row["primaryRepo"] == "o/r"
    inputs = readout_to_inputs(r)
    assert inputs.products[0].pipeline == 2 and inputs.products[0].pipeline_cap == 3
```
(`_make_sources`' `session_status` default returns `None`, which the readout treats as a DEAD claim, so the token-bearing rows above need the `live` seam; `_PRODUCTS[0]["policy"]` is `{"dispatch": "auto"}` today — make it `{"dispatch": "auto", "max_pipeline_prs": 3}`.)

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_capacity_plan.py server/tests/test_capacity_readout.py -q`
Expected: FAIL (`ProductCapacity` has no `pipeline`; `KeyError: 'pipeline'`).

- [ ] **Step 3: Implement**

`server/automations/capacity_plan.py`:
- `ProductCapacity` gains, after `eligible: int`: `pipeline: int = 0; pipeline_cap: int | None = None`.
- Add above `_split`:
```python
def pipeline_full(p: ProductCapacity) -> bool:
    """Spec 2026-09-20 D13: at or over its pipeline cap (blank cap = never)."""
    return p.pipeline_cap is not None and p.pipeline >= p.pipeline_cap


def _plannable_eligible(p: ProductCapacity) -> int:
    # A pipeline-full product cannot take a new claim (admission refuses it), so
    # planning slots for it would leave them idle: it is planned as eligible 0
    # and keeps only its in-flight floor; the freed slots go to the next rank.
    return 0 if pipeline_full(p) else p.eligible
```
- In `_split`, every `p.eligible` read becomes `_plannable_eligible(p)` (the `working` filter, the pinned `min(p.pin, …)`, and the round-robin `caps[p.slug] < …`).
- In `plan_capacity`, after `budget["reservePct"] = reserve`:
```python
    budget["pipeline"] = {p.slug: {"open": p.pipeline, "cap": p.pipeline_cap}
                          for p in inputs.products if p.pipeline_cap is not None}
```
and after the fleet reason is appended, before the `per_product` loop:
```python
    for p in ordered:
        if pipeline_full(p) and p.dispatch != "off":
            reasons.append(f"{p.slug}: pipeline full ({p.pipeline}/{p.pipeline_cap} PRs open), "
                           "its slots go to the next rank")
```

`server/automations/capacity_readout.py`:
- import `pipeline_by_product` from `.backlog_runner`; after `by_product = in_flight_by_product(states)` add `pipeline_by = pipeline_by_product(states)`.
- In the products loop's dict: `"pipeline": pipeline_by.get(slug, 0), "pipelineCap": policy["max_pipeline_prs"], "primaryRepo": p.get("primaryRepo"),` (after `"eligible"`).
- The `ProductCapacity(...)` built for `product_order` and `readout_to_inputs` both gain `pipeline=r["pipeline"], pipeline_cap=r["pipelineCap"]` (positional style in `readout_to_inputs`: `p["inFlight"], p["eligible"], p["pipeline"], p["pipelineCap"]`).
- The Waiting-on text for a full product already comes from `decide()`'s `refused[0].text` (Task 2's `full_text`), so no readout copy is needed; but a product with `constraint == "product_pipeline_full"` must still be ordered and counted like any refused product — no special case.

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_capacity_plan.py server/tests/test_capacity_readout.py server/tests/test_capacity_governor_loop.py server/tests/test_capacity_api.py server/tests/test_capacity_apply.py server/tests/test_capacity_apply_seams.py server/tests/test_capacity_store.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `ruff check server/`, `mypy server/automations/capacity_plan.py server/automations/capacity_readout.py --ignore-missing-imports`, brand ratchet.

```bash
git add server/automations/capacity_plan.py server/automations/capacity_readout.py server/tests/test_capacity_plan.py server/tests/test_capacity_readout.py
git commit -F /tmp/pr6-t3-msg.txt
```
`/tmp/pr6-t3-msg.txt`:
```
feat(governor): a pipeline-full product gives its slots to the next rank

Spec 2026-09-20 D13. ProductCapacity carries pipeline/pipeline_cap; _split
plans a full product as eligible 0 (its in-flight floor); the plan budget
records {slug: {open, cap}} and reasons name the full product. The readout
publishes pipeline, pipelineCap and primaryRepo per product.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 4: The web — Pipeline column + input, the Waiting-on link, the Roadmap clause

**Files:**
- Modify: `web/src/api/capacity.ts` (`CapacityProduct`), `web/src/api/automations.ts` (`ConfigField.nullable?`)
- Modify: `web/src/features/delivery/capacity/runnerForm.ts` (`PIPELINE_KEY`, seed), `CapacityDetail.tsx` (row cell, header, save loop, waiting-on link), `__fixtures__/capacity.ts`, `CapacityDetail.test.tsx`
- Modify: `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` (`capacityLine`), `AutomationPolicyCard.test.tsx`
- Modify: any other `CapacityProduct` literal (`grep -rn "effectiveCap:" web/src`): `capacityStack.test.ts` if it builds one

**Interfaces:**
- Consumes: readout `products[].pipeline` / `pipelineCap` / `primaryRepo`; the policy key `max_pipeline_prs` (null = unlimited).
- Produces: nothing downstream.

- [ ] **Step 1: Write the failing tests**

`CapacityDetail.test.tsx` (read `setup()` and the `CAPACITY` fixture first; the fixture's two products gain `pipeline: 3, pipelineCap: 6, primaryRepo: 'org/kindash'` (kindash) and `pipeline: 2, pipelineCap: null, primaryRepo: 'org/viberpit'` (viberpit)):
```ts
  it('D13: the Pipeline column shows n / cap, ∞ when the cap is blank, with one input per row in the same form', () => {
    setup()
    expect(screen.getByRole('columnheader', { name: 'Pipeline' })).toBeInTheDocument()
    const kindash = screen.getByTestId('capacity-product-kindash')
    expect(within(kindash).getByText('3 /')).toBeInTheDocument()
    expect(within(kindash).getByLabelText('KinDash pipeline cap')).toHaveValue(6)
    const viberpit = screen.getByTestId('capacity-product-viberpit')
    expect(within(viberpit).getByText('2 /')).toBeInTheDocument()
    expect(within(viberpit).getByLabelText('ViberPit pipeline cap')).toHaveValue(null)
    expect(within(viberpit).getByLabelText('ViberPit pipeline cap')).toHaveAttribute('placeholder', '∞')
    expect(screen.getAllByRole('button', { name: 'Save' })).toHaveLength(1)
  })

  it('D13: saving a pipeline cap patches max_pipeline_prs; clearing it sends null', async () => {
    const { qc } = setup()
    qc.setQueryData(QK.project('kindash'), { automationPolicyUpdatedAt: 'stamp-kindash' })
    fireEvent.change(screen.getByLabelText('KinDash pipeline cap'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalledTimes(1))
    expect(updateProductPolicy).toHaveBeenCalledWith('kindash', {
      policy: { max_pipeline_prs: null }, expectedUpdatedAt: 'stamp-kindash',
    })
  })

  it('D13: a pin and a pipeline cap edited on the same row go in ONE policy patch', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('KinDash pipeline cap'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalledTimes(1))
    expect(updateProductPolicy.mock.calls[0]![1]).toMatchObject({ policy: { max_in_flight: 5, max_pipeline_prs: 9 } })
  })

  it('D13: a negative or fractional pipeline cap is refused with the row named', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('KinDash pipeline cap'), { target: { value: '-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText(/KinDash: pipeline cap must be a whole number ≥ 0/)).toBeInTheDocument())
    expect(updateProductPolicy).not.toHaveBeenCalled()
  })

  it('D13: a pipeline-full product links its Waiting on text to the Pipeline page filtered to its repo', () => {
    setup({ readout: { ...CAPACITY, products: [
      { ...CAPACITY.products[0]!, pipeline: 6, pipelineCap: 6, constraint: 'product_pipeline_full', waitingOn: 'pipeline full: 6 PRs open (cap 6)' },
      CAPACITY.products[1]!,
    ] } })
    const link = within(screen.getByTestId('capacity-product-kindash')).getByRole('link', { name: 'pipeline full: 6 PRs open (cap 6)' })
    expect(link).toHaveAttribute('href', '/pipeline?pipeline=org%2Fkindash')
  })
```
(Use the file's existing `setup({ readout })` override shape — see the `reservePctByLevel` test near line 193 — and its existing `updateProductPolicy` mock + `waitFor`/`within` imports.)

`AutomationPolicyCard.test.tsx`: the `CAPACITY` fixture's products gain `pipeline: 3, pipelineCap: 6, primaryRepo: 'org/kindash'` (kindash) and `pipeline: 0, pipelineCap: null, primaryRepo: null` (other); the expected strings become `'Capacity: rank 2 · auto (effective cap 4) · 1 in flight · pipeline 3/6.'` and `'Capacity: unranked · pinned (pin 2) · 0 in flight · pipeline 0/∞.'`; the no-row sentence becomes `'Capacity: rank, cap mode, pin and pipeline cap are set on Delivery → Capacity.'`.

`__fixtures__/capacity.ts`: both products gain the three fields (values as above). Any `CapacityProduct` literal in `capacityStack.test.ts`: add `pipeline: 0, pipelineCap: null, primaryRepo: null`.

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/delivery/capacity src/features/roadmap/detail/AutomationPolicyCard.test.tsx)`
Expected: FAIL (no Pipeline column; old line text).

- [ ] **Step 3: Implement**

`web/src/api/capacity.ts` `CapacityProduct` — after `eligible: number`:
```ts
  /** Open, unmerged automation PRs (spec 2026-09-20 D13): every runner claim except
   *  gave_up and token-less handed_off, so a handed-off PR still counts. */
  pipeline: number
  /** The policy's `max_pipeline_prs`; null = unlimited. */
  pipelineCap: number | null
  /** For the Waiting-on link to the Pipeline page; null when no primary repo is registered. */
  primaryRepo: string | null
```
`web/src/api/automations.ts` `ConfigField`: `nullable?: boolean` after `deprecated?`, comment "null is a legal stored value (blank = unlimited)".

`runnerForm.ts`: `export const PIPELINE_KEY = (slug: string) => \`pipeline:${slug}\``; in `seedCapacityDraft`, beside the pin seed: `draft[PIPELINE_KEY(p.slug)] = p.pipelineCap == null ? '' : String(p.pipelineCap)`. Add and export:
```ts
/** Blank = unlimited (null); otherwise a whole number ≥ 0. Throws the row-named
 *  message the form's banner shows. */
export function parsePipelineCap(raw: string, name: string): number | null {
  if (raw.trim() === '') return null
  const n = Number(raw)
  if (!Number.isInteger(n) || n < 0) throw new Error(`${name}: pipeline cap must be a whole number ≥ 0`)
  return n
}
```

`CapacityDetail.tsx`:
- Header: add `<th scope="col" className="px-2 py-1.5">Pipeline</th>` after `Pin`.
- Row props: add `pipelineValue: string` and `onPipelineChange: (value: string) => void` (wired like `pinValue`/`onPinChange` with `PIPELINE_KEY`). Cell after the pin cell:
```tsx
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-1 whitespace-nowrap">
          <span className="tabular-nums text-sm text-ink">{product.pipeline} /</span>
          <Input
            aria-label={`${name} pipeline cap`}
            type="number"
            size="sm"
            className="w-16"
            min={0}
            step={1}
            placeholder="∞"
            value={pipelineValue}
            onChange={(e) => onPipelineChange(e.target.value)}
          />
        </div>
      </td>
```
- Waiting-on cell:
```tsx
      <td className="px-2 py-1.5 text-xs text-ink-muted">
        {product.constraint === 'product_pipeline_full' && product.primaryRepo && product.waitingOn
          ? <Link to={`${NAV_BY_ID.pipeline.path}?pipeline=${encodeURIComponent(product.primaryRepo)}`} className="text-accent hover:underline">{product.waitingOn}</Link>
          : (product.waitingOn ?? '—')}
      </td>
```
(`import { NAV_BY_ID } from '@/shell/navDefs'`, as `routes.tsx` does.)
- Save loop: replace the per-product body so pin and pipeline cap build ONE patch:
```ts
      for (const product of readout.products) {
        const name = product.name ?? product.slug
        const policy: { max_in_flight?: number; max_pipeline_prs?: number | null } = {}
        const rawPin = draft[PIN_KEY(product.slug)]
        if (rawPin !== undefined) {
          if (rawPin.trim() === '') throw new Error(`${name}: pin must be a whole number ≥ 0`)
          const next = Number(rawPin)
          if (!Number.isInteger(next) || next < 0) throw new Error(`${name}: pin must be a whole number ≥ 0`)
          if (next !== product.pin) policy.max_in_flight = next
        }
        const rawCap = draft[PIPELINE_KEY(product.slug)]
        if (rawCap !== undefined) {
          const cap = parsePipelineCap(rawCap, name)
          if (cap !== product.pipelineCap) policy.max_pipeline_prs = cap
        }
        if (Object.keys(policy).length === 0) continue
        // (the existing stamp read, updateProductPolicy call with `policy`, 409 handling and setQueryData follow unchanged)
```
Keep the existing comments about the cleared-pin rule, the stamp and the 409 path with the code they explain.

`AutomationPolicyCard.tsx::capacityLine`:
```ts
function capacityLine(row: CapacityProduct | undefined): string {
  if (!row) return 'Capacity: rank, cap mode, pin and pipeline cap are set on Delivery → Capacity.'
  const rank = row.rank === null ? 'unranked' : `rank ${row.rank}`
  const cap = row.capacityMode === 'pinned' ? `pinned (pin ${row.pin})` : `auto (effective cap ${row.effectiveCap})`
  const pipeline = `pipeline ${row.pipeline}/${row.pipelineCap ?? '∞'}`
  return `Capacity: ${rank} · ${cap} · ${row.inFlight} in flight · ${pipeline}.`
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `(cd web && npx vitest run src/features/delivery/capacity src/features/roadmap && npm run typecheck && npx eslint src/api src/features/delivery/capacity src/features/roadmap --max-warnings 0)`; then the FULL `(cd web && npm run test 2>&1 | tee /tmp/pr6-t4-vitest.log; echo "exit ${PIPESTATUS[0]}")`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/capacity.ts web/src/api/automations.ts web/src/features/delivery/capacity/runnerForm.ts web/src/features/delivery/capacity/CapacityDetail.tsx web/src/features/delivery/capacity/__fixtures__/capacity.ts web/src/features/delivery/capacity/CapacityDetail.test.tsx web/src/features/roadmap/detail/AutomationPolicyCard.tsx web/src/features/roadmap/detail/AutomationPolicyCard.test.tsx
git commit -F /tmp/pr6-t4-msg.txt
```
(plus `capacityStack.test.ts` if touched.) `/tmp/pr6-t4-msg.txt`:
```
feat(web): Pipeline column + cap input on the Capacity table; pipeline clause on the Roadmap card

Spec 2026-09-20 D13/§5.2/§5.3. One policy patch per dirty row carries
pin and pipeline cap together; blank = unlimited (null); a full product's
Waiting on links to the Pipeline page filtered to its repo.

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 5: Feature docs

**Files:**
- Modify: `docs/product/features/capacity-governor.md` (after the "One reserve (PR 5)" bullet)
- Modify: `docs/product/features/product-management-layer.md:308` ("twelve fields" → "thirteen fields"; add the field's description after the `capacity_mode` clause, or after `max_in_flight`'s if `capacity_mode` is not listed)
- Modify: `docs/product/features/autopilot-backlog-runner.md` (the `_backlog_repo_load` / fairness paragraph, ~line 120-125): one sentence naming `counts_toward_pipeline` as the shared rule

- [ ] **Step 1: Edit**

`capacity-governor.md`, after the PR 5 bullet:
```markdown
- **Pipeline cap (PR 6)** — Since spec PR 6 (2026-09-20, D13/C23) a product policy carries
  `max_pipeline_prs` (**Pipeline cap**, nullable; blank = unlimited): the most automation PRs the
  product may have open and unmerged at once, counted from the runner's durable target states by
  `backlog_runner.counts_toward_pipeline` (every row except `gave_up` and token-less `handed_off`,
  so a PR handed off to PR Doctor still counts — the pin frees a slot at hand-off, the pipeline cap
  does not). Admission emits `product_pipeline_full` (layer `product`) for a NEW claim at or over
  the cap and never refuses an existing slot; the planner treats a full product as `eligible = 0`
  (its in-flight floor) and gives its slots to the next rank, recording `budget.pipeline`
  `{slug: {open, cap}}` and a reason. The readout's `products[]` carry `pipeline`, `pipelineCap`
  and `primaryRepo`; the Capacity table's **Pipeline** column (`n / cap`, `∞` when blank) edits the
  cap in the same form as the pins, a full product's *Waiting on* links to the Pipeline page
  filtered to its repo, and the Roadmap card's capacity line gains `pipeline n/cap`.
```
`product-management-layer.md`: `has twelve fields` → `has thirteen fields`; after the `max_in_flight` clause's closing parenthesis add `` , `max_pipeline_prs` (default blank = unlimited, minimum 0 — the product's **pipeline cap**: the most automation PRs it may have open and unmerged at once, counting PRs handed off to PR Doctor; a full pipeline refuses new claims, spec 2026-09-20 D13) ``.
`autopilot-backlog-runner.md`: after the sentence about `_backlog_repo_load` counting handed-off work, add: "Which rows count is one predicate, `backlog_runner.counts_toward_pipeline`, shared with the per-product **pipeline cap** (`max_pipeline_prs`, spec 2026-09-20 D13) that refuses a new claim once a product's open, unmerged PRs reach it."

- [ ] **Step 2: Check and commit**

Run: `python3 scripts/brand_ratchet.py`
```bash
git add docs/product/features/capacity-governor.md docs/product/features/product-management-layer.md docs/product/features/autopilot-backlog-runner.md
git commit -F /tmp/pr6-t5-msg.txt
```
`/tmp/pr6-t5-msg.txt`:
```
docs(capacity): PR 6 — the per-product pipeline cap

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

---

### Task 6: Gate, rebase, PR

Same gate as the five earlier PRs; run by the coordinating session after the whole-branch review's fix wave.

- [ ] `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` · brand ratchet.
- [ ] Full server suite (detached, test-slot loop), tee'd to `/tmp/pr6-server-tests.log`.
- [ ] `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`; `rm -rf web/dist`.
- [ ] `git fetch && git rebase origin/master`; re-run ruff + ratchet + the touched-area tests; in-flight PR check by file path.
- [ ] Push; `gh pr create --title "feat(capacity): per-product pipeline cap (spec PR 6)" --body-file /tmp/pr6-body.md --base master`; `gh pr merge <#> --squash --auto`. Body: D13/C23 summary, `Refs #2060`, the Claude Code trailer; note the spec series is complete.
