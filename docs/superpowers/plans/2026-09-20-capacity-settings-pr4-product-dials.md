# Capacity Settings PR 4 — One Owner for Product Dials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delivery → Capacity becomes the ONE editor for a product's rank, cap mode and pin; the Roadmap card's *Capacity* legend becomes *Dispatch* with a read-only capacity line, and the product policy's `weight` is deprecated (rank is the one priority mechanism).

**Architecture:** Server side, `ConfigField` gains a `deprecated` flag (a sibling of PR 3's `surface`), the product policy schema marks `max_in_flight` / `capacity_mode` as `surface="capacity"` with the D8 labels (*Pin*, *Cap mode*) and `weight` as `deprecated=True`; the `weighted` selection strategy stops reading weights and orders like `priority`, and `priority`'s repo order becomes rank order (`priority_rank`, then product age) so "rank order" has one definition. Web side, the generic Config tab hides deprecated fields, and the Roadmap card renders capacity-surfaced and deprecated fields as neither controls nor rows: a single read-only line built from the capacity readout (`useCapacity`) with a link to Delivery → Capacity replaces them.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (`server/`), pytest; React 18 + TypeScript + React Query, vitest + testing-library (`web/`).

**Spec:** `docs/superpowers/specs/2026-09-20-capacity-settings-structure-design.md` — §5.2 item 4, §5.3, §7 (`GET /api/automations` `deprecated`; `policy-schema` `weight` accepted but ignored), §8 row **4**, §9 (`AutomationPolicyCard.test.tsx`), §11 decisions D5, D6, D8. Removal issue for `weight`: **#2103** (already filed).

## Global Constraints

- **Branch:** `feat/capacity-pr4-product-dials`, created from `origin/master` (`git fetch && git checkout -b feat/capacity-pr4-product-dials origin/master`). Never branch off local `master`. PR 3 (#2102) is merging independently; this PR touches none of its files except `server/automations/base.py` (an additive field) and `AutomationDetail.test.tsx` (a new test).
- **Vocabulary (D8):** the field labels are exactly **Pin** (`max_in_flight`), **Cap mode** (`capacity_mode`), **Rank**. "Max in flight (pin)" and "In-flight cap" are retired labels. Constraint codes are untouched in this PR.
- **`weight` is deprecated, not retired (D6):** it stays in `PRODUCT_POLICY_SCHEMA` (so a stored row carrying it still validates and a stale client's PATCH with it is accepted), nothing reads it, and both cards hide it. Retirement is #2103, one release later.
- **No em dashes in shipped copy** (house rule pinned by `AutomationPolicyCard.test.tsx` "never a bare em dash"). The spec's read-only line is rendered as two sentences: `Capacity: rank 2 · auto (effective cap 4) · 1 in flight.` then a link `Adjust on Delivery → Capacity`. The middle-dot `·` separator is the existing readout idiom (`capacityStack.ts`).
- **Links** go through `deliveryPath('capacity')` (`web/src/features/delivery/deliveryNavState.ts`), never a literal path.
- **Rank order** = `server/automations/capacity_plan.py:product_order` semantics: `priority_rank` ascending, unranked last, `Project.created_at` (then slug) as the tiebreak. It is the ONE ordering both the governor and the runner's `priority` strategy use after this PR.
- **Tests run as** `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest …` from the worktree root; web commands as `(cd web && npm run …)` subshells (never a persisted `cd`; the Bash hook is cwd-relative). `web/node_modules` is a symlink to the main checkout's.
- **Never `git stash`** (the stash stack is shared with other sessions). Commit messages via `git commit -F <file>` ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Brand ratchet:** `python scripts/brand_ratchet.py` must stay green; never raise a ceiling (hand-edit only entries it reports as `shrunk`).
- Pipeline cap (`max_pipeline_prs`, `pipeline 3/6` in the spec's line) is **PR 6**; this PR's line omits the pipeline clause.

---

### Task 1: `ConfigField.deprecated`, the D8 labels and `surface` on the product dials

**Files:**
- Modify: `server/automations/base.py:15-37` (the `ConfigField` dataclass and `to_dict`)
- Modify: `server/automations/product_policy.py:63-73` (three `ConfigField`s)
- Modify: `server/tests/test_pm_api.py:274-290` (`test_policy_schema_and_project_policy_round_trip`)
- Test: `server/tests/test_automation_base.py` (new test appended)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ConfigField.deprecated: bool = False`, emitted by `to_dict()` under `"deprecated"` (Task 3 types it on the web); `PRODUCT_POLICY_SCHEMA` entries `max_in_flight` (label `"Pin"`, `surface="capacity"`), `capacity_mode` (label `"Cap mode"`, `surface="capacity"`), `weight` (`deprecated=True`) — Task 4 filters the card by exactly these two flags.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_automation_base.py`:

```python
def test_config_field_publishes_deprecated_default_false():
    """``deprecated`` (spec 2026-09-20 D6) is a sibling of ``surface``: published on
    every field so the web can hide a deprecated dial without a per-key list."""
    f = ConfigField(key="k", label="K", type="int", default=1)
    assert f.to_dict()["deprecated"] is False
    assert ConfigField(key="k", label="K", type="int", default=1,
                       deprecated=True).to_dict()["deprecated"] is True
```

(`ConfigField` is already imported at the top of that file.)

In `server/tests/test_pm_api.py::test_policy_schema_and_project_policy_round_trip`, after the line `assert fields["weight"]["min"] == 0` add:

```python
        # Spec 2026-09-20 D5/D6/D8: Delivery → Capacity owns the pin and the cap
        # mode (the Roadmap card shows them read-only); ``weight`` is deprecated
        # (#2103) — still in the schema so a stored row validates, read by nothing.
        assert fields["max_in_flight"]["surface"] == "capacity"
        assert fields["max_in_flight"]["label"] == "Pin"
        assert fields["capacity_mode"]["surface"] == "capacity"
        assert fields["capacity_mode"]["label"] == "Cap mode"
        assert fields["weight"]["deprecated"] is True
        assert fields["dispatch"]["surface"] == "config"
        assert fields["dispatch"]["deprecated"] is False
```

Then find, in the same test, the existing `PATCH /api/pm/projects/{slug}/policy` call (it follows the schema assertions; it patches a policy and asserts the round trip) and add one more PATCH right after it that proves a deprecated key is still accepted:

```python
        # A stale client (or a stored row) still carrying ``weight`` is accepted,
        # not 422'd: deprecated ≠ retired until #2103.
        r = c.patch(f"/api/pm/projects/{slug}/policy", json={"policy": {"weight": 4}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["automationPolicy"]["weight"] == 4
```

Use whatever variable the test already uses for the project slug and the PATCH body shape it already uses (read the test first; match its `expectedUpdatedAt` handling if it sends one — the existing PATCH in that test is the template).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_automation_base.py::test_config_field_publishes_deprecated_default_false server/tests/test_pm_api.py::test_policy_schema_and_project_policy_round_trip -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'deprecated'` and `KeyError: 'deprecated'` / label mismatch.

- [ ] **Step 3: Implement**

`server/automations/base.py` — after the `surface` field:

```python
    # Spec 2026-09-20 D6: a dial on its way out. Still validated (a stored row
    # carrying it must not read as corrupt) and still stored, but read by
    # nothing and hidden by every editor; the removal issue retires it one
    # release later. ``deprecated`` ≠ retired: a retired key is DROPPED by
    # ``validate_config`` (``retired_config_keys``), a deprecated one is kept.
    deprecated: bool = False

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type,
                "default": self.default, "help": self.help, "choices": self.choices,
                "min": self.min, "surface": self.surface, "deprecated": self.deprecated}
```

`server/automations/product_policy.py` — replace the three fields (keep the `min=0` comment block above `max_in_flight`):

```python
    ConfigField(key="max_in_flight", label="Pin", type="int", default=1, min=0,
                surface="capacity",
                help="This product's concurrent runner sessions: a hard cap while Cap mode is "
                     "pinned, the fallback while auto and no plan exists. 0 pauses issue "
                     "dispatch without losing the rest of its policy."),
    ConfigField(key="capacity_mode", label="Cap mode", type="enum", default="auto",
                choices=["auto", "pinned"], surface="capacity",
                help="auto: the capacity governor sets this product's cap from the fleet's "
                     "real headroom and its rank. pinned: the pin is a hard cap the governor "
                     "respects."),
    # Deprecated (spec 2026-09-20 D6, removal #2103): rank is the one priority
    # mechanism; the ``weighted`` selection strategy now orders by rank. Kept in
    # the schema for one release so a stored row still validates.
    ConfigField(key="weight", label="Weight (contested slot share)", type="int", default=1, min=0,
                deprecated=True,
                help="Deprecated: nothing reads it. Rank on Delivery → Capacity decides "
                     "priority. Removed after one release (#2103)."),
```

Also update the module docstring's first paragraph: replace `its in-flight cap and weight` with `its pin and cap mode`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command, then `… -m pytest server/tests/test_pm_api.py server/tests/test_automation_base.py server/tests/test_automations_api.py server/tests/test_backlog_policy_seams.py -q`
Expected: PASS (the seams file still reads `weight` through the schema; it is untouched until Task 2).

- [ ] **Step 5: Lint and commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/automations/base.py server/automations/product_policy.py server/tests/test_pm_api.py server/tests/test_automation_base.py && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/automations/base.py server/automations/product_policy.py --ignore-missing-imports`

```bash
git add server/automations/base.py server/automations/product_policy.py server/tests/test_pm_api.py server/tests/test_automation_base.py
git commit -F /tmp/pr4-t1-msg.txt
```
with `/tmp/pr4-t1-msg.txt`:
```
feat(policy): ConfigField.deprecated; Pin / Cap mode owned by Capacity; weight deprecated

Spec 2026-09-20 D5/D6/D8. max_in_flight and capacity_mode carry
surface="capacity" with the D8 labels; weight stays in the schema
(deprecated=True, removal #2103) so stored rows keep validating.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 2: `weighted` falls back to rank order; delete the weight read

**Files:**
- Modify: `server/automations/backlog_selection.py:63-122` (`order_live_groups`)
- Modify: `server/automations/backlog_grouping.py:393-440` (`plan_to_batch` signature + docstring + the `order_live_groups` call)
- Modify: `server/automations/backlog_runner.py:86-153` (`select_batch` signature, docstring, call) and `:261-270` (the `selection_strategy` help)
- Modify: `server/sessions/orchestrator.py:6108-6118` (`_rows` ordering), `:6259-6293` (`_backlog_select_batch` + `_backlog_repo_knobs`), `:6963-6964` and `:7034-7035` (legacy seed's `repo_weights`)
- Modify tests: `server/tests/test_order_live_groups.py:65-90`, `server/tests/test_backlog_select_batch.py:243-253`, `server/tests/test_backlog_policy_seams.py` (`_seed_product`, the translate test at 365-409, the seed tests at ~425-448 and ~735-756)

**Interfaces:**
- Consumes: Task 1's schema (the `weight` key still validates, so a seeded/stored `weight` is harmless).
- Produces: `order_live_groups(items, *, repo_of, strategy, repo_load, repo_order, caps)` — no `weights`; `plan_to_batch(..., strategy, repo_order, caps)` — no `weights`; `select_batch(config, *, query, repo_load, paused_repos, caps, repo_order, load_plan, mark_consumed)` — no `weights`; `SessionOrchestrator._backlog_repo_order(self) -> list[str]` replaces `_backlog_repo_knobs`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_order_live_groups.py` — replace the whole `weighted` block (the banner comment and the four `test_weighted_*` functions) with:

```python
# --------------------------------------------------------------------------- #
# weighted — deprecated (spec 2026-09-20 D6, #2103): orders exactly like priority #
# --------------------------------------------------------------------------- #

def test_weighted_orders_by_repo_order_like_priority():
    """A stored ``selection_strategy: weighted`` keeps working, as rank order:
    the product weight it used to divide load by is deprecated and read by nothing."""
    weighted = order_live_groups(["c", "a", "b"], repo_of=ident, strategy="weighted",
                                 repo_order=["a", "b", "c"], repo_load={"a": 9, "b": 0})
    priority = order_live_groups(["c", "a", "b"], repo_of=ident, strategy="priority",
                                 repo_order=["a", "b", "c"], repo_load={"a": 9, "b": 0})
    assert weighted == priority == ["a", "b", "c"]


def test_order_live_groups_has_no_weights_parameter():
    import inspect
    assert "weights" not in inspect.signature(order_live_groups).parameters
```

`server/tests/test_backlog_select_batch.py` — replace `test_strategy_weighted_favors_higher_weight` with:

```python
@pytest.mark.asyncio
async def test_strategy_weighted_is_rank_order():
    """'weighted' is deprecated (spec 2026-09-20 D6): it serves repos in
    ``repo_order`` exactly like 'priority' — o/b first here although the plan
    puts o/a's group first and o/a carries less load."""
    result = await select_batch(
        make_config(selection_strategy="weighted"),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
        repo_order=["o/b", "o/a"], repo_load={"o/a": 0, "o/b": 3},
    )
    assert result is not None and result.primary == "o/b#2"
```

`server/tests/test_backlog_policy_seams.py`:

(a) give `_seed_product` a rank: change its signature to
```python
async def _seed_product(slug: str, repo: str, *, policy: dict | None = None,
                        board: bool = True, rank: int | None = None) -> None:
```
and the `Project(...)` construction to `Project(slug=slug, name=slug, automation_policy=policy, priority_rank=rank)`.

(b) in `test_select_batch_translates_policies_into_repo_keyed_knobs`: rename it `test_select_batch_translates_policies_into_rank_ordered_repos`, change its docstring's first sentence to "this seam is the ONE place a product policy becomes a per-repo order — rank first (``priority_rank``), product age as the tiebreak; ``weight`` is deprecated (D6) and no longer translated.", seed `beta` with `rank=1` (alpha stays unranked, gamma off), and replace the two assertions on `weights` / `repo_order` with:
```python
        assert "weights" not in seen                   # deprecated (spec 2026-09-20 D6)
        assert "caps" not in seen                      # a PRODUCT ceiling, not a repo one
        assert seen["repo_order"] == ["o/b", "o/a"]     # rank 1 first; unranked after, by age
```

(c) in the `_backlog_seed_policies` test near line 425 (`raw = {"enabled_repos": ["o/a", "o/b"], "repo_caps": {"o/a": 4}, "repo_weights": {"o/a": 3}, …`): keep `"repo_weights": {"o/a": 3}` in `raw` (a real pre-flip row carries it) but drop `"weight": 3` from the expected `alpha` dict, and change the last two beta assertions to `assert "max_in_flight" not in beta and "weight" not in beta` / `assert effective_policy(beta)["max_in_flight"] == 1`. Add a comment: `# repo_weights is NOT carried over: weight is deprecated (spec 2026-09-20 D6).`

(d) in `test_seed_once_…below_min…` near line 735-756: expected raw policy becomes `{"dispatch": "auto"}`; delete the `assert policy["weight"] == 4` line; keep the `max_in_flight == 1` assertion.

Then grep the file for any other `"weight"` expectation (`grep -n '"weight"' server/tests/test_backlog_policy_seams.py`) and apply the same rule: a legacy seed never writes `weight`; a policy passed to `_seed_product` may still carry it (it validates).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -i HOME=/tmp/viberpit-test-home PATH=/home/tony/GitHub/viberpit-deploy/.venv/bin:/usr/bin:/bin CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest server/tests/test_order_live_groups.py server/tests/test_backlog_select_batch.py server/tests/test_backlog_policy_seams.py -q -m "not slow"`
Expected: FAIL — the signature test, the weighted-ordering test (load/weight still sorts `b` first), the translate test (`weights` in `seen`, order by age), the seed tests (`weight` written).

- [ ] **Step 3: Implement**

`server/automations/backlog_selection.py` — `order_live_groups`:
- Remove the `weights` parameter.
- Replace the docstring's `weighted` bullet with: `* ``weighted`` — DEPRECATED (spec 2026-09-20 D6, removal #2103): the product weight it divided load by is read by nothing; it orders exactly like ``priority``. Kept as a value so a stored config keeps working.`
- Replace the strategy branches so `priority` and `weighted` share one path:
```python
    if strategy in ("priority", "weighted"):
        order = repo_order or []
        rank = {r: i for i, r in enumerate(order)}
        return sorted(items, key=lambda it: (rank.get(repo_of(it), len(order)),
                                             load.get(repo_of(it), 0)))
```
and delete the old `if strategy == "weighted":` block.

`server/automations/backlog_grouping.py` — `plan_to_batch`: remove `weights` from the signature, the docstring's mention (`a ``weighted`` where the only live repos are weight-0` → delete that clause, leaving the `concurrency-cap` example), and the `order_live_groups(...)` call.

`server/automations/backlog_runner.py` — `select_batch`: remove `weights` from the signature and the `plan_to_batch(...)` call; rewrite the docstring's `weights / caps / repo_order:` paragraph as `caps / repo_order:` — "The per-repo strategy inputs — explicit parameters, never config reads (the config keys that used to carry them are retired). The orchestrator derives ``repo_order`` from the PRODUCT policies in rank order (``_backlog_repo_order``: ``priority_rank`` first, product age as the tiebreak). It passes no ``caps``: …" (keep the rest of that paragraph). The `selection_strategy` help becomes:
```python
                    help="How a freed slot chooses among the repos of the products "
                         "with work. 'balanced': fewest in-flight runs first (equal "
                         "rotation). 'priority': serve products in rank order (Rank on "
                         "Delivery → Capacity; unranked products last, oldest first). "
                         "'weighted': deprecated, same as priority. 'concurrency-cap': "
                         "never exceed a product's pin."),
```

`server/sessions/orchestrator.py`:
- `_rows` (≈6108-6118): rank order, not age order. Replace the `born = …` / `views = sorted(…)` lines and the comment above them with:
```python
            # Which products are on, and each one's repos, in RANK order
            # (spec 2026-09-20 D6: ``priority_rank`` first, unranked last,
            # ``Project.created_at`` then slug as the tiebreak — the same key
            # ``capacity_plan.product_order`` sorts by) so the ``priority``
            # strategy and the governor agree on who is first.
            meta = {pid: (rank, born) for pid, rank, born in s.execute(
                sa_select(Project.id, Project.priority_rank, Project.created_at)).all()}
            views = sorted(list_product_views(s),
                           key=lambda v: (meta.get(v.project_id, (None, None))[0] is None,
                                          meta.get(v.project_id, (None, None))[0] or 0,
                                          meta.get(v.project_id, (None, None))[1] or _EPOCH,
                                          v.slug or v.project_id))
```
- `_backlog_select_batch`: the docstring sentence `so each ON product's ``weight`` / ``max_in_flight`` is applied to every repo it owns, in product age order` → `so each ON product's repos are served in rank order`; `weights, repo_order = self._backlog_repo_knobs()` → `repo_order = self._backlog_repo_order()`; drop `weights=weights,` from the `select_batch(...)` call.
- Replace `_backlog_repo_knobs` with:
```python
    def _backlog_repo_order(self) -> list[str]:
        """The ON products' repos in rank order, for the ``priority`` strategy
        (and its deprecated alias ``weighted``, spec 2026-09-20 D6).

        Derived from the policy/repo snapshot ``_backlog_query_queue`` just took
        (already rank-ordered there), so selection can never bias toward a
        product the pool no longer contains. A product with no repos contributes
        nothing; a repo shared by two products keeps its first (higher-ranked)
        position. ``max_in_flight`` is deliberately NOT translated here — it is a
        per-product ceiling enforced in ``BacklogRunnerLoop._refill``."""
        from server.automations.product_policy import is_on
        repo_order: list[str] = []
        for slug, repos in self._backlog_last_repos.items():
            if not is_on(self._backlog_last_policies.get(slug)):
                continue
            for repo in repos:
                if repo not in repo_order:
                    repo_order.append(repo)
        return repo_order
```
- Legacy seed: delete `weights = raw.get("repo_weights") or {}` (≈6964) and the two lines `if (weight := _int("weight", weights.get(repo))) is not None: legacy["weight"] = weight` (≈7034-7035); update the nearby comment `The per-repo caps/weights were keyed by repo` → `The per-repo caps were keyed by repo (repo_weights is not carried over: weight is deprecated, spec 2026-09-20 D6)`. Then `grep -n "_backlog_repo_knobs\|weights" server/sessions/orchestrator.py` — every remaining hit must be unrelated (host weight / `live_session_load`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command plus `server/tests/test_backlog_runner_automation.py server/tests/test_backlog_grouping*.py server/tests/test_capacity_readout.py server/tests/test_pm_api.py` (glob whatever grouping test files exist: `ls server/tests/test_backlog_grouping*`).
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

Run: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check server/ && /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m mypy server/automations/backlog_selection.py server/automations/backlog_grouping.py server/automations/backlog_runner.py server/sessions/orchestrator.py --ignore-missing-imports && python3 scripts/brand_ratchet.py`

```bash
git add server/automations/backlog_selection.py server/automations/backlog_grouping.py server/automations/backlog_runner.py server/sessions/orchestrator.py server/tests/test_order_live_groups.py server/tests/test_backlog_select_batch.py server/tests/test_backlog_policy_seams.py
git commit -F /tmp/pr4-t2-msg.txt
```
`/tmp/pr4-t2-msg.txt`:
```
refactor(runner): weighted strategy falls back to rank order; delete the weight read

Spec 2026-09-20 D6. order_live_groups/plan_to_batch/select_batch lose
their weights parameter; priority (and its deprecated alias weighted)
serve repos in priority_rank order with product age as the tiebreak,
the same key capacity_plan.product_order uses. The legacy seed no
longer carries repo_weights into a product's weight.

Refs #2103.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 3: Web types + the Config tab hides deprecated fields

**Files:**
- Modify: `web/src/api/automations.ts:27-42` (`ConfigField`)
- Modify: `web/src/features/delivery/automation/ConfigPanel.tsx:41-43` (the `configSchema.map`)
- Test: `web/src/features/delivery/automation/AutomationDetail.test.tsx` (new test after the D7 test at line 78)

**Interfaces:**
- Consumes: Task 1's `deprecated` on the wire.
- Produces: `ConfigField.deprecated?: boolean` (Task 4 reads it).

- [ ] **Step 1: Write the failing test**

In `AutomationDetail.test.tsx`, after the `'Config tab: a Capacity-owned field is read-only and links there …'` test, add (read the file's `AUTOMATIONS[1]` / `mount` helpers first — the fixture is a backlog-runner automation with a `configSchema` array):

```ts
  it('Config tab: a deprecated field is hidden entirely (spec 2026-09-20 D6)', () => {
    const runner = {
      ...AUTOMATIONS[1],
      configSchema: [
        ...AUTOMATIONS[1].configSchema,
        { key: 'old_knob', label: 'Old knob', type: 'int' as const, default: 1, help: '', choices: null, deprecated: true },
      ],
      config: { ...AUTOMATIONS[1].config, old_knob: 3 },
    }
    mount(runner, '/delivery/automations/backlog-runner?tab=config')
    expect(screen.queryByText('Old knob')).toBeNull()
    expect(screen.queryByRole('spinbutton', { name: 'Old knob' })).toBeNull()
    // The surrounding fields still render.
    expect(screen.getByRole('spinbutton', { name: 'Max issues per PR' })).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `(cd web && npx vitest run src/features/delivery/automation/AutomationDetail.test.tsx)`
Expected: FAIL — a typecheck error on `deprecated` is fine for vitest (it strips types), so the runtime failure is `Old knob` being found.

- [ ] **Step 3: Implement**

`web/src/api/automations.ts` — after `surface?: 'config' | 'capacity'`:
```ts
  /** On its way out (spec 2026-09-20 D6): still stored and validated, read by
   *  nothing, hidden by every editor. Optional so older fixtures still compile. */
  deprecated?: boolean
```

`ConfigPanel.tsx` — change `{a.configSchema.map((field) => (` to
```tsx
        {a.configSchema.filter((field) => !field.deprecated).map((field) => (
```
with a one-line comment above: `{/* A deprecated dial is hidden, not read-only (spec 2026-09-20 D6): there is nothing to set anywhere. */}`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/features/delivery/automation/ && npm run typecheck && npx eslint src/api/automations.ts src/features/delivery/automation/ConfigPanel.tsx src/features/delivery/automation/AutomationDetail.test.tsx --max-warnings 0)`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/automations.ts web/src/features/delivery/automation/ConfigPanel.tsx web/src/features/delivery/automation/AutomationDetail.test.tsx
git commit -F /tmp/pr4-t3-msg.txt
```
`/tmp/pr4-t3-msg.txt`:
```
feat(web): ConfigField.deprecated; the Config tab hides a deprecated dial

Spec 2026-09-20 D6.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 4: The Roadmap card — *Dispatch* legend and the read-only capacity line

**Files:**
- Modify: `web/src/features/roadmap/detail/AutomationPolicyCard.tsx` (imports, `groupFields`, the first fieldset)
- Modify: `web/src/features/roadmap/detail/AutomationPolicyCard.test.tsx` (fixture flags, mock, legend/line tests)
- Modify: `web/src/features/roadmap/detail/ProductDetail.test.tsx:32-…` and `web/src/features/roadmap/RoadmapPage.test.tsx:33-…` (their `vi.mock('@/api/hooks')` objects gain `useCapacity`)

**Interfaces:**
- Consumes: `ConfigField.surface` / `.deprecated` (Tasks 1, 3); `useCapacity()` from `@/api/hooks` (`web/src/api/hooks.ts:1242`, returns `UseQueryResult<CapacityReadout>`; `readout.products: CapacityProduct[]` with `slug, rank, capacityMode, pin, effectiveCap, inFlight` — `web/src/api/capacity.ts:62-76`); `deliveryPath('capacity')`.
- Produces: nothing downstream.

- [ ] **Step 1: Write the failing tests**

`AutomationPolicyCard.test.tsx`:

(a) Fixture: give the three capacity dials their flags and D8 labels —
```ts
  { key: 'max_in_flight', label: 'Pin', type: 'int', default: 1, help: '', choices: null, surface: 'capacity' },
  { key: 'capacity_mode', label: 'Cap mode', type: 'enum', default: 'auto', help: '', choices: ['auto', 'pinned'], surface: 'capacity' },
  { key: 'weight', label: 'Weight', type: 'int', default: 1, help: '', choices: null, deprecated: true },
```
(delete the "grouping test needs a field…" comment above `capacity_mode`; it no longer applies.)

(b) Mock: add to the hoisted `h` object `capacity: { data: undefined as unknown, isPending: false, isError: false }`, to the `vi.mock('@/api/hooks')` object `useCapacity: () => h.capacity,`, and to `beforeEach` `Object.assign(h.capacity, { data: CAPACITY, isPending: false, isError: false })` with, above the mock:
```ts
// The readout row the card's read-only capacity line reads (spec 2026-09-20 D5).
const CAPACITY = {
  products: [
    { slug: 'kindash', name: 'KinDash', rank: 2, createdAt: null, dispatch: 'auto', capacityMode: 'auto', pin: 1, effectiveCap: 4, inFlight: 1, eligible: 9, constraint: null, waitingOn: null, defect: false },
    { slug: 'other', name: 'Other', rank: null, createdAt: null, dispatch: 'off', capacityMode: 'pinned', pin: 2, effectiveCap: 2, inFlight: 0, eligible: 0, constraint: 'dispatch_off', waitingOn: 'off', defect: false },
  ],
}
```

(c) Existing tests to change:
- first test: delete `expect(screen.getByLabelText('Max in flight')).toHaveValue(1)` and `expect(screen.getByLabelText('Weight')).toHaveValue(1)`; the `labels` regex drops `Max in flight|Weight`.
- `'reads existing policy values over the defaults'`: replace the two `Max in flight` / `Weight` expectations with `expect(screen.getByLabelText('Minimum board status')).toHaveValue('triaged')` and set `automationPolicy: { dispatch: 'propose', queue_min_status: 'triaged' }`.
- `'disables every field…'` and `'disables all settings while a save is pending'`: replace `Max in flight` with `Minimum board status`.
- grouping test: legends become `['Dispatch', 'Models', 'Gates']`; delete the `Max in flight` / `In-flight cap` / `Weight` lines; the other `Capacity` expectations become `'Dispatch'`.
- `'links the Capacity group to the fleet capacity page'`: rename to `'the Dispatch group carries the read-only capacity line linking to the capacity page'` and assert:
```ts
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Capacity: rank 2 · auto (effective cap 4) · 1 in flight.')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Adjust on Delivery → Capacity' })
    expect(link).toHaveAttribute('href', '/delivery?scope=capacity')
    expect(legendFor(link)).toBe('Dispatch')
    expect(screen.queryByRole('link', { name: "See the fleet's capacity" })).toBeNull()
```
- `'places a field the grouping lists do not recognize into Capacity…'` → `…into Dispatch…`, expecting `'Dispatch'`.
- `'preserves the exact revision without rounding microseconds'`: change the control to `screen.getByLabelText('Dispatch')` with `{ target: { value: 'auto' } }` and the expectation to `policy: { dispatch: 'auto' }`.

(d) New tests, in the main `describe`:
```ts
  it('renders no control for a Capacity-owned or deprecated field (spec 2026-09-20 D5/D6)', () => {
    renderCard(<AutomationPolicyCard product={product} project={makeProject({ automationPolicy: { max_in_flight: 3, weight: 7 } })} />)
    expect(screen.queryByLabelText('Pin')).toBeNull()
    expect(screen.queryByLabelText('Cap mode')).toBeNull()
    expect(screen.queryByLabelText('Weight')).toBeNull()
    expect(screen.queryByText('Weight')).toBeNull()
    // …and the line reads the READOUT (effective cap 4), not the stored pin (3).
    expect(screen.getByText('Capacity: rank 2 · auto (effective cap 4) · 1 in flight.')).toBeInTheDocument()
  })

  it('a pinned product says pin, an unranked one says unranked', () => {
    renderCard(<AutomationPolicyCard product={{ slug: 'other', name: 'Other' } as ProductNode} project={makeProject({ slug: 'other' })} />)
    expect(screen.getByText('Capacity: unranked · pinned (pin 2) · 0 in flight.')).toBeInTheDocument()
  })

  it('without a readout row the line still links to the capacity page', () => {
    Object.assign(h.capacity, { data: undefined })
    renderCard(<AutomationPolicyCard product={product} project={makeProject()} />)
    expect(screen.getByText('Capacity: rank, cap mode and pin are set on Delivery → Capacity.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Adjust on Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
```

`ProductDetail.test.tsx` and `RoadmapPage.test.tsx`: add `useCapacity: () => ({ data: undefined, isPending: false, isError: false }),` to each file's `vi.mock('@/api/hooks', () => ({ … }))` object (next to `usePolicySchema`). Without it the card throws `useCapacity is not a function` inside those suites.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap/detail/AutomationPolicyCard.test.tsx src/features/roadmap/detail/ProductDetail.test.tsx src/features/roadmap/RoadmapPage.test.tsx)`
Expected: the card tests FAIL (legend `Capacity`, controls present, no line); the other two PASS (their mocks now cover the hook the card does not yet call).

- [ ] **Step 3: Implement**

`AutomationPolicyCard.tsx`:

Imports: add `useCapacity` to the `@/api/hooks` import; add `import type { CapacityProduct } from '@/api/capacity'`.

Replace the grouping block (the `MODELS_FIELD_KEYS` comment through `groupFields`) with:
```tsx
// Findability (spec 2026-09-12 D9, reshaped by 2026-09-20 D5): the dials group
// under three legends. *Dispatch* holds what this card OWNS (dispatch, reach,
// eligibility); rank, cap mode and pin are owned by Delivery → Capacity
// (`surface: 'capacity'`) and appear here as one read-only line; a deprecated
// field (D6) appears nowhere. A key this schema does not yet know about falls
// into Dispatch rather than vanishing.
const MODELS_FIELD_KEYS = new Set(['default_model', 'ladder_top', 'unattended_model_floor'])
const GATES_FIELD_KEYS = new Set([
  'risk_tolerance', 'triage_auto_accept', 'give_up_after_bounces', 'max_initiatives_in_flight',
])

interface FieldGroups {
  dispatch: ConfigField[]
  models: ConfigField[]
  gates: ConfigField[]
}

function groupFields(fields: ConfigField[]): FieldGroups {
  const groups: FieldGroups = { dispatch: [], models: [], gates: [] }
  for (const field of fields) {
    if (field.deprecated || field.surface === 'capacity') continue
    if (MODELS_FIELD_KEYS.has(field.key)) groups.models.push(field)
    else if (GATES_FIELD_KEYS.has(field.key)) groups.gates.push(field)
    else groups.dispatch.push(field)
  }
  return groups
}

/** The one sentence that replaces the rank / cap mode / pin controls (spec
 *  2026-09-20 §5.3), read from the capacity READOUT so it shows the effective
 *  cap the governor actually set, not the stored pin. Two sentences and a
 *  middle dot, never an em dash (house rule). */
export function capacityLine(row: CapacityProduct | undefined): string {
  if (!row) return 'Capacity: rank, cap mode and pin are set on Delivery → Capacity.'
  const rank = row.rank === null ? 'unranked' : `rank ${row.rank}`
  const cap = row.capacityMode === 'pinned' ? `pinned (pin ${row.pin})` : `auto (effective cap ${row.effectiveCap})`
  return `Capacity: ${rank} · ${cap} · ${row.inFlight} in flight.`
}
```

In the component: after `const gates = useSdlcGates()` add
```tsx
  // Unpolled: the roadmap card is not a live surface; `QK.capacity` is
  // invalidated by every dial mutation (spec D10), which is what keeps it honest.
  const capacity = useCapacity()
  const capacityRow = capacity.data?.products.find((p) => p.slug === product.slug)
```

Replace the first inner fieldset with:
```tsx
        <fieldset className="flex flex-col gap-3">
          <legend className="text-sm font-medium">Dispatch</legend>
          {groups.dispatch.map(renderField)}
          <p className="text-xs text-ink-muted">
            {capacityLine(capacityRow)}{' '}
            <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Adjust on Delivery → Capacity</Link>
          </p>
        </fieldset>
```
(the old `See the fleet's capacity` link is deleted; `groups.capacity` no longer exists.)

Update the header comment of the file (`every PRODUCT_POLICY_SCHEMA field rendered…`) to `every editable PRODUCT_POLICY_SCHEMA field rendered … (Capacity-owned dials appear as one read-only line, deprecated ones not at all — spec 2026-09-20 D5/D6)`. Also the `orderedFields` comment: `not buried among caps and weights` → `not buried among the other dials`.

Note the `{capacityLine(capacityRow)}{' '}<Link>` shape: the tests match the sentence with `getByText` (exact text of the text node) and the link by role, so keep the sentence and the link as separate nodes inside the one `<p>`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/features/roadmap/ && npm run typecheck && npx eslint src/features/roadmap --max-warnings 0)`
Expected: PASS. Then the FULL web suite: `(cd web && npm run test 2>&1 | tee /tmp/pr4-t4-vitest.log; echo "exit ${PIPESTATUS[0]}")` — other suites mock `@/api/hooks` without a spread and may render the card.

- [ ] **Step 5: Commit**

```bash
git add web/src/features/roadmap/detail/AutomationPolicyCard.tsx web/src/features/roadmap/detail/AutomationPolicyCard.test.tsx web/src/features/roadmap/detail/ProductDetail.test.tsx web/src/features/roadmap/RoadmapPage.test.tsx
git commit -F /tmp/pr4-t4-msg.txt
```
`/tmp/pr4-t4-msg.txt`:
```
feat(web): Roadmap card's Dispatch legend + read-only capacity line

Spec 2026-09-20 D5: rank, cap mode and pin are edited on Delivery →
Capacity only; the card shows one line from the readout and links
there. Deprecated fields (D6) render nowhere.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 5: Feature docs

**Files:**
- Modify: `docs/product/features/capacity-governor.md:61-67` (add a PR 4 paragraph directly after the PR 3 one)
- Modify: `docs/product/features/product-management-layer.md:316-317` (the `weight` clause)
- Modify: `docs/product/features/autopilot-backlog-runner.md:120-121`

- [ ] **Step 1: Edit**

`capacity-governor.md`, after the `**Runner form and settings surface (PR 3)**` bullet:
```markdown
- **One owner for product dials (PR 4)** — Since spec PR 4 (2026-09-20) a product's rank, cap mode
  (`capacity_mode`) and pin (`max_in_flight`) are edited on Delivery → Capacity only: both policy
  fields carry `surface = "capacity"` with the D8 labels **Cap mode** / **Pin**, and the Roadmap
  card's *Capacity* legend became *Dispatch* (dispatch, reach, eligibility) with one read-only
  line built from the readout ("Capacity: rank 2 · auto (effective cap 4) · 1 in flight.") linking
  there. The product `weight` is deprecated (`ConfigField.deprecated`, removal #2103): nothing
  reads it, both cards hide it, a stored row still validates. The `weighted` selection strategy
  orders like `priority`, and `priority` now serves products in **rank order** (`priority_rank`,
  then product age) — the same key `capacity_plan.product_order` uses.
```

`product-management-layer.md`: replace the clause `` `weight` (default 1, minimum 0 — its share when a freed slot is contested under the `weighted` selection strategy), `` with `` `weight` (deprecated since spec 2026-09-20 D6, removal #2103 — read by nothing; rank on Delivery → Capacity is the priority mechanism), ``.

`autopilot-backlog-runner.md:120-121`: replace `, or a repo paused by weight. `weight` comes from each ON product's policy, applied to every repo it owns and handed to the selection strategies below as a per-repo weight/order (never a cap).` with `. The `priority` strategy (and its deprecated alias `weighted`, spec 2026-09-20 D6) serves each ON product's repos in rank order (`priority_rank`, then product age).`

- [ ] **Step 2: Check and commit**

Run: `python3 scripts/brand_ratchet.py && git diff --stat`

```bash
git add docs/product/features/capacity-governor.md docs/product/features/product-management-layer.md docs/product/features/autopilot-backlog-runner.md
git commit -F /tmp/pr4-t5-msg.txt
```
`/tmp/pr4-t5-msg.txt`:
```
docs(capacity): PR 4 — product dials have one owner; weight deprecated

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 6: Gate, rebase, PR

Same gate as #2080 / #2097 / #2102. Not a subagent task — run by the coordinating session after the whole-branch review's fix wave.

- [ ] `ruff check src/ server/` · `mypy server/ --ignore-missing-imports` · `pytest tests/` (core, with `PYTHONPATH=$PWD/src`) · `python scripts/brand_ratchet.py`.
- [ ] Full server suite with the test-slot acquire loop in the SAME shell, tee'd to `/tmp/pr4-server-tests.log`: `pytest server/tests/ -n auto --dist loadfile --timeout=300`.
- [ ] `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`, tee'd; then `rm -rf web/dist`.
- [ ] `git fetch && git rebase origin/master`; re-run ruff + ratchet; in-flight PR check by file path (the GraphQL query in `core.md`).
- [ ] `git push -u origin feat/capacity-pr4-product-dials`; `gh pr create --title "feat(capacity): one owner for product dials, weight deprecated (spec PR 4)" --body-file /tmp/pr4-body.md --base master`; `gh pr merge <#> --squash --auto`. Body: summary bullets (D5, D6, D8, rank order ruling), verification list, `Refs #2060, #2103`, the Claude Code trailer.
