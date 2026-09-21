# PR Doctor Priority over Backlog Runner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While PR Doctor has a pending (non-given-up) stuck PR for a product, Backlog Runner takes no *new* claims for that product — a hard, per-product admission gate that never interrupts an in-flight session.

**Architecture:** A new orchestrator method (`_pr_doctor_pending_products`) reuses PR Doctor's existing sweep scope + classify logic to compute which product slugs currently have a pending stuck PR. That set flows through the existing implementation-admission pipeline (`gather_implementation_snapshot` → `ImplementationSnapshot.decide` → `decide_implementation`) as a new refusal constraint, wired into the two real dispatch-decision call sites (`_backlog_admission` and the Capacity Governor's readout).

**Tech Stack:** Python, FastAPI/SQLAlchemy async, pytest (asyncio).

**Spec:** `docs/superpowers/specs/2026-09-21-pr-doctor-priority-design.md`

## Global Constraints

- Hard gate (not proportional): a gated product gets zero new claims, no partial throttling.
- Pending = `pr_doctor_engine.classify(info) is not None` and the target's durable state does not have `gave_up` truthy.
- Per-product only: unrelated products are never gated.
- Never refuses an `existing_slot=True` (already-running) claim — only new dispatches.
- No changes to `capacity_plan.py` / `capacity_apply.py` (the governor's own plan math is untouched).

---

### Task 1: `_pr_doctor_pending_products` on the orchestrator

**Files:**
- Modify: `server/sessions/orchestrator.py` (add a new method near `_pr_doctor_scope_pairs`, i.e. after line 13876)
- Test: `server/tests/test_pr_doctor_pending_products.py` (new file)

**Interfaces:**
- Consumes: `self._pr_doctor_scope_pairs() -> list[tuple[str, str]]` (existing), `self.pr_for(repo, branch) -> PullRequestInfo | None` (existing), `self._pr_population_pr(repo, branch) -> PullRequestInfo | None` (existing), `self._pr_doctor_ctx.get_target_state(target: str) -> dict` (existing `AutomationContext` method), `server.automations.pr_doctor_engine.classify(info) -> str | None` (existing), `server.automations.pr_doctor_engine.target_terminal(info) -> bool` (existing), `server.pm.repo_project.owning_projects_by_repo(db, repos: Collection[str]) -> dict[str, RepoOwner]` (existing; `RepoOwner.slug: str`).
- Produces: `SessionOrchestrator._pr_doctor_pending_products() -> frozenset[str]` (async) — the set of product slugs with at least one pending stuck PR. Task 4 and Task 5 call this.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pr_doctor_pending_products.py`:

```python
"""``_pr_doctor_pending_products`` — product slugs PR Doctor is currently
gating Backlog Runner for (spec 2026-09-21 pr-doctor-priority).

Reuses the ``_build_orch``/file-DB pattern from ``test_pr_doctor_sweep.py``.
"""
from __future__ import annotations

import asyncio

from server.tests.test_pr_doctor_sweep import _build_orch, _pr


async def _register_repo(repo: str, slug: str) -> None:
    from server.db import db_session
    from server.db.models import Project, ProjectRepo

    async with db_session() as db:
        project = Project(slug=slug, name=slug)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo))


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'pending.db'}")


def test_a_classified_stuck_pr_gates_its_product(tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _register_repo("o/r", "product-a")

        stuck = _pr(checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset({"product-a"})

    asyncio.run(run())


def test_a_gave_up_target_does_not_gate(tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _register_repo("o/r", "product-a")

        stuck = _pr(number=9, checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])
        await orch._pr_doctor_ctx.set_target_state("o/r#9", {"gave_up": True})

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset()

    asyncio.run(run())


def test_a_healthy_pr_does_not_gate(tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _register_repo("o/r", "product-a")

        healthy = _pr(checks="passing", merge_state_status="CLEAN", mergeable="MERGEABLE")
        orch = _build_orch(cache={("o/r", "feat/x"): healthy}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset()

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=server/.. .venv/bin/python -m pytest server/tests/test_pr_doctor_pending_products.py -v` (from the repo root; adjust the venv path to whichever prebuilt env this checkout uses per the main-checkout note in `.agents/instructions/core.md`).

Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute '_pr_doctor_pending_products'` (or similar) on `test_a_classified_stuck_pr_gates_its_product`. Note `AutomationContext.set_target_state` must already exist (it's used across the pr-doctor test suite, e.g. `test_pr_doctor_step.py`) — if that specific call errors instead, check its exact name via `grep -n "def set_target_state\|async def set_target_state" server/automations/context.py` and fix the test call, not the production code.

- [ ] **Step 3: Write the minimal implementation**

In `server/sessions/orchestrator.py`, immediately after the `_pr_doctor_scope_pairs` method (ends at line 13876 as of this plan; find it by its docstring "``(repo, branch)`` pairs PR Doctor should evaluate" if the line has drifted), add:

```python
    async def _pr_doctor_pending_products(self) -> frozenset[str]:
        """Product slugs with at least one PR Doctor considers stuck and has
        not given up on (spec 2026-09-21 pr-doctor-priority). Backlog Runner's
        admission gate reads this to refuse NEW claims for a gated product —
        never an existing one (D8: admission refuses new claims, it never
        kills a session). Best-effort: any failure yields frozenset() rather
        than blocking Backlog Runner dispatch on a PR Doctor read.
        """
        from server.automations import pr_doctor_engine as engine
        from server.pm.repo_project import owning_projects_by_repo
        try:
            pending_repos: set[str] = set()
            for repo, branch in await self._pr_doctor_scope_pairs():
                info = self.pr_for(repo, branch) or self._pr_population_pr(repo, branch)
                if info is None or engine.target_terminal(info):
                    continue
                if engine.classify(info) is None:
                    continue
                state = await self._pr_doctor_ctx.get_target_state(f"{repo}#{info.number}")
                if state.get("gave_up"):
                    continue
                pending_repos.add(repo)
            if not pending_repos:
                return frozenset()
            async with db_session() as db:
                owners = await owning_projects_by_repo(db, pending_repos)
            return frozenset(owner.slug for owner in owners.values())
        except Exception:
            log.warning("orchestrator._pr_doctor_pending_products_failed", exc_info=True)
            return frozenset()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: same command as Step 2.

Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pr_doctor_pending_products.py
git commit -m "$(cat <<'EOF'
feat(pr-doctor): compute which products have a pending stuck PR

Reuses the existing sweep scope + classify() to find product slugs
PR Doctor is currently working on, so Backlog Runner's admission gate
(next commits) can defer to it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `decide_implementation` gains the `pr_doctor_gated` constraint

**Files:**
- Modify: `server/automations/product_policy.py` (the `decide_implementation` function, currently starting at line 369)
- Test: `server/tests/test_product_implementation_decision.py`

**Interfaces:**
- Consumes: nothing new from other tasks.
- Produces: `decide_implementation(..., pr_doctor_gated: bool = False, ...)` — a new keyword-only parameter. When `True` and `existing_slot=False`, adds a `refused` constraint coded `pr_doctor_priority`. When `True` and `existing_slot=True`, adds a `permitted` constraint coded `pr_doctor_priority_held`. When `False`, adds neither. Task 3's `ImplementationSnapshot.decide()` calls this with the new argument.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_product_implementation_decision.py` (after `test_ruling_20_an_occupied_claim_is_never_refused_on_capacity`, i.e. after line 120's block — check the actual end of that function before inserting so the new tests are top-level, not nested):

```python
def test_pr_doctor_priority_refuses_a_new_claim_only():
    """spec 2026-09-21: PR Doctor priority never touches an existing_slot
    claim (D8), but refuses a brand new one for a gated product."""
    gated_new = decision(pr_doctor_gated=True)
    assert gated_new.state == "refused"
    assert any(c.code == "pr_doctor_priority" and c.state == "refused"
               for c in gated_new.constraints)

    gated_held = decision(pr_doctor_gated=True, existing_slot=True)
    assert gated_held.permitted
    assert any(c.code == "pr_doctor_priority_held" and c.state == "permitted"
               for c in gated_held.constraints)
    assert not any(c.code == "pr_doctor_priority" for c in gated_held.constraints)


def test_pr_doctor_priority_absent_when_not_gated():
    result = decision(pr_doctor_gated=False)
    codes = {c.code for c in result.constraints}
    assert "pr_doctor_priority" not in codes
    assert "pr_doctor_priority_held" not in codes
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_product_implementation_decision.py -v -k pr_doctor_priority`

Expected: FAIL — `TypeError: decide_implementation() got an unexpected keyword argument 'pr_doctor_gated'`.

- [ ] **Step 3: Write the minimal implementation**

In `server/automations/product_policy.py`, add the parameter to `decide_implementation`'s signature (currently ending `pipeline_existing_slot: bool = False,\n) -> ImplementationDecision:` around line 389-390):

```python
    pipeline_count: int | None = None,
    pipeline_cap: int | None = None,
    pipeline_existing_slot: bool = False,
    pr_doctor_gated: bool = False,
) -> ImplementationDecision:
```

Then, immediately after the existing pipeline-cap block (currently lines 479-481:
```python
    if pipeline_cap is not None:
        capacity("product_pipeline", pipeline_count, pipeline_cap, occupied=pipeline_existing_slot,
                 full_text=f"pipeline full: {pipeline_count} claims and PRs open (cap {pipeline_cap})")
```
), add:

```python
    # spec 2026-09-21 pr-doctor-priority: PR Doctor always wins available
    # capacity for a product with a pending stuck PR. Same D8 carve-out as
    # every other capacity check — an occupied claim is never refused.
    if pr_doctor_gated:
        if existing_slot:
            add("pr_doctor_priority_held", "permitted",
                "This claim already holds its slot; PR Doctor's priority does not interrupt it.")
        else:
            add("pr_doctor_priority", "refused",
                "PR Doctor has a stuck pull request pending a fix for this product; "
                "capacity is reserved until it clears.")
```

Also update the docstring's param list is not required (existing docstring doesn't enumerate every param), so no docstring change needed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_product_implementation_decision.py -v`

Expected: all tests in the file PASS (the two new ones plus every pre-existing one — the new parameter defaults to `False`, so no existing call site changes behavior).

- [ ] **Step 5: Commit**

```bash
git add server/automations/product_policy.py server/tests/test_product_implementation_decision.py
git commit -m "$(cat <<'EOF'
feat(admission): add a PR-Doctor-priority refusal to decide_implementation

New pr_doctor_gated param: refuses a brand new claim for a product PR
Doctor has a pending stuck PR for, but never an already-running one
(same D8 carve-out every other capacity check uses).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Thread `pr_doctor_pending` through `ImplementationSnapshot` and `gather_implementation_snapshot`/`evaluate_implementation`

**Files:**
- Modify: `server/automations/product_admission.py`
- Test: `server/tests/test_product_admission.py`

**Interfaces:**
- Consumes: `decide_implementation(..., pr_doctor_gated: bool = False)` from Task 2.
- Produces:
  - `ImplementationSnapshot` dataclass gains a 6th field `pr_doctor_pending: frozenset[str] = frozenset()` (after `accounting`).
  - `gather_implementation_snapshot(..., pr_doctor_pending: Callable[[], Awaitable[frozenset[str]]] | None = None)`.
  - `evaluate_implementation(..., pr_doctor_pending: Callable[[], Awaitable[frozenset[str]]] | None = None)`.
  - Task 4 and Task 5 pass `pr_doctor_pending=orch._pr_doctor_pending_products` (Task 1's method) into these two functions at their real call sites.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_product_admission.py` (near the other `ImplementationSnapshot` tests, after `test_existing_slot_must_belong_to_the_current_product_and_token`):

```python
def test_pr_doctor_pending_product_refuses_a_new_claim_only():
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 5, "max_spawns_per_day": 5}, {}, {},
        frozenset({"product"}))
    new_claim, _ = snapshot.decide("org/repo")
    assert not new_claim.permitted
    assert any(c.code == "pr_doctor_priority" for c in new_claim.constraints)

    held, _ = snapshot.decide("org/repo", existing_slot=True, existing_token="t")
    codes = {c.code for c in held.constraints}
    assert "pr_doctor_priority" not in codes
    assert "pr_doctor_priority_held" in codes


def test_pr_doctor_pending_product_absent_by_default():
    snapshot = admission.ImplementationSnapshot(
        coverage(), "on", {"max_concurrent": 5, "max_spawns_per_day": 5}, {}, {})
    decision, _ = snapshot.decide("org/repo")
    codes = {c.code for c in decision.constraints}
    assert "pr_doctor_priority" not in codes and "pr_doctor_priority_held" not in codes


@pytest.mark.asyncio
async def test_gather_implementation_snapshot_reads_pr_doctor_pending(monkeypatch):
    from server.automations import runner_config
    monkeypatch.setattr(admission, "gather_product_coverage", AsyncMock(return_value=coverage()))
    monkeypatch.setattr(runner_config, "read_automation_config", AsyncMock(return_value=(
        "auto", {"max_concurrent": 5, "max_spawns_per_day": 5})))
    monkeypatch.setattr(admission, "read_dispatch_accounting", AsyncMock(return_value={}))
    context = AsyncMock()
    context.list_target_states.return_value = {}

    async def pending():
        return frozenset({"product"})

    snapshot = await admission.gather_implementation_snapshot(
        ["org/repo"], context=context, pr_doctor_pending=pending)

    assert snapshot.pr_doctor_pending == frozenset({"product"})


@pytest.mark.asyncio
async def test_gather_implementation_snapshot_pr_doctor_pending_failure_is_empty(monkeypatch):
    from server.automations import runner_config
    monkeypatch.setattr(admission, "gather_product_coverage", AsyncMock(return_value=coverage()))
    monkeypatch.setattr(runner_config, "read_automation_config", AsyncMock(return_value=(
        "auto", {"max_concurrent": 5, "max_spawns_per_day": 5})))
    monkeypatch.setattr(admission, "read_dispatch_accounting", AsyncMock(return_value={}))
    context = AsyncMock()
    context.list_target_states.return_value = {}

    async def broken():
        raise RuntimeError("pr doctor read failed")

    snapshot = await admission.gather_implementation_snapshot(
        ["org/repo"], context=context, pr_doctor_pending=broken)

    assert snapshot.pr_doctor_pending == frozenset()
```

`AsyncMock` and `pytest` are already imported at the top of the file; no new imports needed for the test file itself.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_product_admission.py -v -k pr_doctor_pending`

Expected: FAIL — `TypeError: ImplementationSnapshot.__init__() takes ... positional arguments but 6 were given` on the first two tests, and `TypeError: gather_implementation_snapshot() got an unexpected keyword argument 'pr_doctor_pending'` on the other two.

- [ ] **Step 3: Write the minimal implementation**

In `server/automations/product_admission.py`:

1. Update the import line near the top from:
```python
from collections.abc import Collection, Mapping
```
to:
```python
from collections.abc import Awaitable, Callable, Collection, Mapping
```

2. In the `ImplementationSnapshot` dataclass (currently):
```python
@dataclass(frozen=True)
class ImplementationSnapshot:
    coverage: ProductCoverage
    runner_mode: str | None
    config: dict
    states: dict[str, dict] | None
    accounting: dict[str, datetime] | None
```
add the new field:
```python
@dataclass(frozen=True)
class ImplementationSnapshot:
    coverage: ProductCoverage
    runner_mode: str | None
    config: dict
    states: dict[str, dict] | None
    accounting: dict[str, datetime] | None
    pr_doctor_pending: frozenset[str] = frozenset()
```

3. In `ImplementationSnapshot.decide()`, right before the `decision = decide_implementation(` call, add:
```python
        pr_doctor_gated = bool(owner and owner.owner and owner.owner.slug in self.pr_doctor_pending)
```
and add `pr_doctor_gated=pr_doctor_gated,` as a new keyword argument inside that `decide_implementation(...)` call (any position among the keyword args is fine, e.g. right after `pipeline_existing_slot=pipeline_occupied,`).

4. Update `gather_implementation_snapshot`'s signature from:
```python
async def gather_implementation_snapshot(
    repos: Collection[str] | None = None, *, requested_product: str | None = None,
    context: AutomationContext | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
) -> ImplementationSnapshot:
```
to:
```python
async def gather_implementation_snapshot(
    repos: Collection[str] | None = None, *, requested_product: str | None = None,
    context: AutomationContext | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
    pr_doctor_pending: Callable[[], Awaitable[frozenset[str]]] | None = None,
) -> ImplementationSnapshot:
```
and, right before the function's final `return ImplementationSnapshot(coverage, fold_global_mode(mode), config, states, accounting)` line, add:
```python
    try:
        pending = await pr_doctor_pending() if pr_doctor_pending is not None else frozenset()
    except Exception:
        pending = frozenset()
```
then change the return to:
```python
    return ImplementationSnapshot(coverage, fold_global_mode(mode), config, states, accounting, pending)
```

5. Update `evaluate_implementation`'s signature from:
```python
async def evaluate_implementation(
    repo: str, *, requested_product: str | None = None,
    trigger: Literal["automatic", "manual"] = "automatic",
    effect: Literal["implementation", "proposal", "dispatch"] = "implementation",
    existing_slot: bool = False, existing_token: str | None = None,
    context: AutomationContext | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
    target_refs: Collection[str] | None = None,
) -> tuple[ImplementationDecision, dict]:
```
to:
```python
async def evaluate_implementation(
    repo: str, *, requested_product: str | None = None,
    trigger: Literal["automatic", "manual"] = "automatic",
    effect: Literal["implementation", "proposal", "dispatch"] = "implementation",
    existing_slot: bool = False, existing_token: str | None = None,
    context: AutomationContext | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
    target_refs: Collection[str] | None = None,
    pr_doctor_pending: Callable[[], Awaitable[frozenset[str]]] | None = None,
) -> tuple[ImplementationDecision, dict]:
```
and update its internal call from:
```python
    snapshot = await gather_implementation_snapshot(
        repos, requested_product=requested_product, context=context, verified_repos=verified_repos)
```
to:
```python
    snapshot = await gather_implementation_snapshot(
        repos, requested_product=requested_product, context=context, verified_repos=verified_repos,
        pr_doctor_pending=pr_doctor_pending)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_product_admission.py -v`

Expected: all tests PASS, including every pre-existing one (the new field/param both default such that old callers are unaffected).

- [ ] **Step 5: Commit**

```bash
git add server/automations/product_admission.py server/tests/test_product_admission.py
git commit -m "$(cat <<'EOF'
feat(admission): thread PR-Doctor-pending products through the snapshot

ImplementationSnapshot, gather_implementation_snapshot and
evaluate_implementation all gain an optional pr_doctor_pending source,
defaulting to no gate so existing callers are unaffected until wired.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire the gate into the real Backlog Runner dispatch path

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_backlog_admission`, currently lines 7650-7657)
- Test: `server/tests/test_backlog_dispatch.py` (find the existing `_backlog_admission` coverage in this file — grep `_backlog_admission` in it first to locate the right section to extend)

**Interfaces:**
- Consumes: `self._pr_doctor_pending_products` (Task 1), `evaluate_implementation(..., pr_doctor_pending=...)` (Task 3).
- Produces: every real Backlog Runner dispatch decision (automatic tick and manual) now defers to PR Doctor.

- [ ] **Step 1: Write the failing test**

First inspect how `_backlog_admission` is already tested:

```bash
grep -n "_backlog_admission\|evaluate_implementation" server/tests/test_backlog_dispatch.py
```

Add a new test to `server/tests/test_backlog_dispatch.py` that monkeypatches `server.automations.product_admission.evaluate_implementation` (or the module orchestrator imports it from — match whatever pattern the existing tests in that file already use for stubbing this call, since `_backlog_admission` does a local `from server.automations.product_admission import evaluate_implementation` inside the method) to capture its keyword arguments, and asserts `pr_doctor_pending` was passed the orchestrator's own `_pr_doctor_pending_products` bound method:

```python
@pytest.mark.asyncio
async def test_backlog_admission_passes_pr_doctor_pending_through(monkeypatch):
    import server.automations.product_admission as admission_mod
    from server.automations.backlog_selection import Batch

    captured = {}

    async def fake_evaluate(*args, **kwargs):
        captured.update(kwargs)
        from server.automations.product_policy import ImplementationDecision
        return ImplementationDecision(
            state="permitted", constraints=(), configured_dispatch="auto",
            policy_source="product_policy", product_id=None, product_slug=None,
            canonical_repo=None, approval_required=False, observed_at="2026-09-21T00:00:00Z",
            policy_revision="x", remaining_checks=()), {}

    monkeypatch.setattr(admission_mod, "evaluate_implementation", fake_evaluate)

    orch = _build_backlog_orch()  # use this file's existing minimal-orchestrator helper
    batch = Batch(primary="o/r#1", issues=["o/r#1"], repo="o/r", titles={}, product="product-a",
                  primary_hold_kind=None)

    await orch._backlog_admission(batch, source="automatic", existing_slot=False)

    assert captured["pr_doctor_pending"] == orch._pr_doctor_pending_products
```

Note: `_build_backlog_orch` is a placeholder name — before writing this test, grep the file for its actual minimal-orchestrator constructor helper (e.g. it may be called `_orch()`, `_build_orch()`, or a fixture) and use the real one, keeping every other field the helper already sets. If the file constructs `SessionOrchestrator` a different way (e.g. via a fixture), adapt the test to that pattern instead of introducing a second one — do not add a second helper if one already exists in this file.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_backlog_dispatch.py -v -k pr_doctor_pending_through`

Expected: FAIL — `KeyError: 'pr_doctor_pending'` (the kwarg isn't passed yet).

- [ ] **Step 3: Write the minimal implementation**

In `server/sessions/orchestrator.py`, change `_backlog_admission` from:

```python
    async def _backlog_admission(self, batch, *, source: str, existing_slot: bool,
                                 existing_token: str | None = None):
        from server.automations.product_admission import evaluate_implementation
        return await evaluate_implementation(
            batch.repo, requested_product=batch.product,
            trigger="automatic" if source == "automatic" else "manual",
            effect="dispatch", existing_slot=existing_slot, context=self._backlog_ctx,
            target_refs=[batch.primary, *batch.issues], existing_token=existing_token)
```

to:

```python
    async def _backlog_admission(self, batch, *, source: str, existing_slot: bool,
                                 existing_token: str | None = None):
        from server.automations.product_admission import evaluate_implementation
        return await evaluate_implementation(
            batch.repo, requested_product=batch.product,
            trigger="automatic" if source == "automatic" else "manual",
            effect="dispatch", existing_slot=existing_slot, context=self._backlog_ctx,
            target_refs=[batch.primary, *batch.issues], existing_token=existing_token,
            pr_doctor_pending=self._pr_doctor_pending_products)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_backlog_dispatch.py -v`

Expected: all tests in the file PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_backlog_dispatch.py
git commit -m "$(cat <<'EOF'
feat(backlog-runner): defer every real dispatch to PR Doctor priority

_backlog_admission — the gate every automatic tick and manual dispatch
goes through — now passes PR Doctor's pending-products read into
admission, so a product with a stuck PR takes no new backlog claims.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Surface the gate on the Capacity Governor readout, and end-to-end coverage

**Files:**
- Modify: `server/automations/capacity_wiring.py` (the `decide` closure inside `sources_for`, currently lines 83-89)
- Modify: `server/automations/capacity_readout.py` (`LAYER_OF_CODE`, currently lines 31-44)
- Test: `server/tests/test_capacity_readout.py` (find the existing per-product `binding`/`waitingOn` coverage in this file to mirror)

**Interfaces:**
- Consumes: `orch._pr_doctor_pending_products` (Task 1), `gather_implementation_snapshot(..., pr_doctor_pending=...)` (Task 3).
- Produces: the Capacity page shows `pr_doctor_priority` as a gated product's `waitingOn`/binding reason, same mechanism every other refusal code already uses.

- [ ] **Step 1: Write the failing test**

First inspect how the Capacity readout's `decide` seam is faked in tests:

```bash
grep -n "class.*CapacitySources\|CapacitySources(\|def decide\b" server/tests/test_capacity_readout.py
```

Add a test mirroring the file's existing pattern for a product-level refusal (e.g. however it already tests `dispatch_off` or `product_capacity_full` showing up as `waitingOn`), but for the new code. The shape should be: build a `CapacitySources` (or whatever fake harness the file uses) where the `decide` callable is `product_admission`'s real `ImplementationSnapshot.decide` wired to a snapshot carrying `pr_doctor_pending={"product-a"}`, call `build_readout`, and assert the `product-a` row's `constraint == "pr_doctor_priority"` and its text appears in `waitingOn`. If the existing file fakes `decide` directly as a bare async callable instead of going through a real snapshot, follow that simpler existing pattern instead — return `(decision, {})` from a fake `decide` where `decision` has a `pr_doctor_priority` refused constraint, matching whatever `ImplementationDecision`-construction helper the file already has (reuse it; do not hand-roll a second one).

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_capacity_readout.py -v -k pr_doctor`

Expected: FAIL on the assertion that `waitingOn`/`constraint` reflects the new code (the readout logic already surfaces *any* refused constraint generically via `refused[0].code`/`.text` at `capacity_readout.py:162-164`, so this test should fail only because nothing upstream produces the `pr_doctor_priority` code yet in the fake `decide`'s wiring — confirm the failure reason matches this, not an unrelated error, before proceeding).

- [ ] **Step 3: Write the minimal implementation**

1. In `server/automations/capacity_wiring.py`, change the `decide` closure from:
```python
    async def decide(repo: str, slug: str):
        nonlocal _snapshot
        if _snapshot is None:
            all_products = await products()
            repos = sorted({p["primaryRepo"] for p in all_products if p.get("primaryRepo")})
            _snapshot = await gather_implementation_snapshot(repos, context=orch._backlog_ctx)
        return _snapshot.decide(repo, requested_product=slug, trigger="automatic", effect="dispatch")
```
to:
```python
    async def decide(repo: str, slug: str):
        nonlocal _snapshot
        if _snapshot is None:
            all_products = await products()
            repos = sorted({p["primaryRepo"] for p in all_products if p.get("primaryRepo")})
            _snapshot = await gather_implementation_snapshot(
                repos, context=orch._backlog_ctx, pr_doctor_pending=orch._pr_doctor_pending_products)
        return _snapshot.decide(repo, requested_product=slug, trigger="automatic", effect="dispatch")
```

2. In `server/automations/capacity_readout.py`, add two entries to `LAYER_OF_CODE` (currently lines 31-44), inserting them next to the other `product`-layer codes for readability:
```python
LAYER_OF_CODE = {"product_capacity_full": "product", "product_pipeline_full": "product",
                 "runner_ceiling_full": "runner",
                 "daily_budget_full": "runner", "dispatch_off": "product",
                 "approval_required": "product", "runner_off": "runner",
                 "no_account_headroom": "account", "hosts_full": "host",
                 "global_cap_full": "global",
                 "pr_doctor_priority": "product",
                 # Ruling 20 (product_policy.capacity()): an occupied claim's
                 # ceiling constraint is now "permitted" (`*_held`), so it never
                 # refuses and never binds — these entries exist only so a
                 # future reader of one of these codes (e.g. a diagnostic view
                 # that doesn't filter by state) maps it to the same layer as
                 # its `_full` sibling rather than the "product" default.
                 "runner_ceiling_held": "runner", "product_capacity_held": "product",
                 "product_pipeline_held": "product", "pr_doctor_priority_held": "product"}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_capacity_readout.py server/tests/test_capacity_apply_seams.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Run the full targeted regression set for this feature**

Run:
```bash
.venv/bin/python -m pytest \
  server/tests/test_pr_doctor_pending_products.py \
  server/tests/test_product_implementation_decision.py \
  server/tests/test_product_admission.py \
  server/tests/test_backlog_dispatch.py \
  server/tests/test_capacity_readout.py \
  server/tests/test_capacity_apply_seams.py \
  server/tests/test_pr_doctor_sweep.py \
  server/tests/test_pm_policy_coverage.py \
  server/tests/test_runner_config.py \
  -v
```
Expected: all PASS. (This list is every test file this plan's tasks touched or that imports the changed symbols, per the grep run at plan-writing time — re-grep for `decide_implementation(`, `gather_implementation_snapshot`, `evaluate_implementation(`, and `ImplementationSnapshot(` across `server/tests/` before this step in case another file also calls these with positional args that could break from the new fields; fix any positional-call breakage found by converting the affected call to keyword args rather than reordering the dataclass/function.)

- [ ] **Step 6: Run `ruff` and `mypy` on every file this plan touched**

Run:
```bash
ruff check server/sessions/orchestrator.py server/automations/product_policy.py \
  server/automations/product_admission.py server/automations/capacity_wiring.py \
  server/automations/capacity_readout.py
mypy server/ --ignore-missing-imports
```
Expected: both clean. Fix any reported issue in the touched files before continuing (per this repo's standing rule that a green `pytest` run is not a green gate without `ruff`/`mypy`).

- [ ] **Step 7: Commit**

```bash
git add server/automations/capacity_wiring.py server/automations/capacity_readout.py server/tests/test_capacity_readout.py
git commit -m "$(cat <<'EOF'
feat(capacity): surface PR Doctor priority on the Capacity Governor readout

The governor's own decide() call now carries PR Doctor's pending set
too, so a gated product's Capacity page row shows the real reason
(pr_doctor_priority) instead of looking falsely idle/eligible.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final steps (after all tasks)

- [ ] Run the full server suite locally per `.agents/instructions/core.md`'s test-slot protocol before pushing (acquire a test slot, run `pytest server/tests/` in the same shell as the acquire/trap, with `CCSWITCH_SESSIOND_*`/`CCSWITCH_DAEMON_ROLE` unset).
- [ ] `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/` (core CLI suite) — the full four-step `lint-and-test` gate, not just `server/tests/`.
- [ ] Push the branch, open a PR (non-draft) with a body summarizing the change and linking `docs/superpowers/specs/2026-09-21-pr-doctor-priority-design.md`, and arm auto-merge (`gh pr merge <#> --squash --auto`) per this repo's completion workflow.
- [ ] Before opening the PR, check for in-flight PRs touching the same files (`server/sessions/orchestrator.py`, `server/automations/product_admission.py`, `server/automations/product_policy.py`, `server/automations/capacity_wiring.py`, `server/automations/capacity_readout.py`) via the GraphQL query in `.agents/instructions/core.md`'s "in flight" section, and rebase/coordinate if one exists.
