# PR Doctor priority over Backlog Runner — design

Status: approved by user, ready for implementation plan.

## Problem

PR Doctor and Backlog Runner run as fully independent schedulers today. Neither is
aware of the other; they only collide at the shared account-rotation ceiling
(`AllAccountsBusy`), first-come-first-served. Directive: PR Doctor must always win —
available capacity is dedicated to fixing stuck PRs before Backlog Runner spawns new
work for the same product.

## Decisions (already approved)

1. **Hard gate, not proportional reservation.** While a product has a pending stuck
   PR, Backlog Runner takes zero *new* claims for that product. No partial
   throttling.
2. **Pending = any classified-stuck PR**, the moment `pr_doctor_engine.classify()`
   returns non-`None` for it — not only once a fix session is actually running.
   Exception: a PR PR Doctor has already given up on (`state["gave_up"]`, the
   `max_attempts`/`mechanical_attempts` ceiling) does **not** count. Otherwise one
   unfixable PR would permanently starve a product's backlog with no recovery path.
3. **Per-product, not global.** Only the product(s) owning the stuck PR's repo are
   gated; other products keep dispatching normally.
4. **Never touches in-flight sessions.** Matches the existing D8 invariant already
   used by every other capacity check in `decide_implementation` (`server/
   automations/product_policy.py`): admission refuses new claims, it never
   interrupts one already running. A Backlog Runner session already spawned for a
   now-gated product runs to completion; only its *next* new claim is blocked.

## Why the capacity governor is NOT the enforcement point

The original idea was to fold this into the Capacity Governor's per-product plan
(`capacity_plan.py` / `capacity_apply.py`), parallel to how the pipeline cap
(`max_pipeline_prs`) narrows `_plannable_eligible()`. That doesn't work as the *sole*
mechanism: `apply_product_plan()` only overlays a product's cap when its
`capacity_mode == "auto"` — a **pinned** product's operator-set cap is deliberately
never overridden (`capacity_apply.py:86`). A gate built only at the governor layer
would silently do nothing for pinned products, contradicting "always."

The pipeline cap actually gets its real enforcement teeth from a *second*,
mode-independent layer: a hard refusal inside `decide_implementation()`
(`product_pipeline_full`, `product_policy.py:479-481`), fed directly from policy
(`max_pipeline_prs`), bypassing the governor plan entirely. PR Doctor priority needs
the same treatment — a `decide_implementation()`-level refusal, not a governor-plan
adjustment — to work regardless of capacity mode.

## Architecture

One new admission-time constraint, threaded from PR Doctor's live state down to the
existing implementation-admission decision. No governor/plan changes needed — the
Capacity page gets this gate's reasoning for free, since `capacity_readout.
build_readout()` already calls `decide()` per product and surfaces the first refused
constraint as that product's `waitingOn`/binding text.

### 1. `server/sessions/orchestrator.py` — new `_pr_doctor_pending_products()`

```python
async def _pr_doctor_pending_products(self) -> frozenset[str]:
    """Product slugs with at least one PR Doctor considers stuck and has not
    given up on. Best-effort: any failure yields frozenset() rather than
    blocking admission — a stale PR Doctor read must never wedge Backlog
    Runner dispatch."""
```

Iterates `await self._pr_doctor_scope_pairs()` (the same (repo, branch) universe the
sweep already walks). For each pair: resolve `info` the same way `_pr_doctor_sweep`
does (`self.pr_for(repo, branch) or self._pr_population_pr(repo, branch)`), skip
`None`/terminal; read the durable per-target state
(`self._pr_doctor_ctx.get_target_state(f"{repo}#{info.number}")`); a repo is
"pending" when `pr_doctor_engine.classify(info) is not None and not
state.get("gave_up")`. Collect the set of pending repos, then batch-resolve to
product slugs with one call to `owning_projects_by_repo` (`server/pm/
repo_project.py`) — ambiguous/unresolved repos are silently dropped (same posture
`owning_projects_by_repo` already documents: "absence is not proof of no owner"; a
repo automations can't confidently attribute to one product should not block that
product's backlog for a reason nobody can name). Wrapped in `try/except Exception`
returning `frozenset()`, same resilience posture as `states`/`accounting` in
`gather_implementation_snapshot`.

### 2. `server/automations/product_admission.py`

- `gather_implementation_snapshot(...)` gains `pr_doctor_pending:
  Callable[[], Awaitable[frozenset[str]]] | None = None`. Read once per snapshot
  (best-effort, `except Exception: pending = frozenset()`), same pattern as the
  existing `states`/`accounting` reads a few lines below.
- `ImplementationSnapshot` gains a field `pr_doctor_pending: frozenset[str] =
  frozenset()`.
- `ImplementationSnapshot.decide()` computes
  `pr_doctor_gated = bool(owner and owner.owner and owner.owner.slug in
  self.pr_doctor_pending)` and passes `pr_doctor_gated=pr_doctor_gated` into
  `decide_implementation(...)`.
- `evaluate_implementation(...)` gains the same `pr_doctor_pending` parameter,
  threaded straight through to its `gather_implementation_snapshot(...)` call.

### 3. `server/automations/product_policy.py` — `decide_implementation()`

New parameter `pr_doctor_gated: bool = False`. New constraint, added alongside the
existing `capacity()` checks:

```python
if pr_doctor_gated:
    if existing_slot:
        add("pr_doctor_priority_held", "permitted",
            "This claim already holds its slot; PR Doctor's priority does not interrupt it.")
    else:
        add("pr_doctor_priority", "refused",
            "PR Doctor has a stuck pull request pending a fix for this product; "
            "capacity is reserved until it clears.")
```

Mirrors the existing `_held`/`_full` naming convention used by `capacity()`'s own
occupied-claim carve-out (D8).

### 4. Wiring the two real call sites

- `server/sessions/orchestrator.py::_backlog_admission()` (the actual gate every
  Backlog Runner dispatch — automatic tick and manual — goes through) passes
  `pr_doctor_pending=self._pr_doctor_pending_products` into its
  `evaluate_implementation(...)` call.
- `server/automations/capacity_wiring.py`'s `decide()` closure (used only to build
  the Capacity Governor's readout/UI) passes
  `pr_doctor_pending=orch._pr_doctor_pending_products` into its
  `gather_implementation_snapshot(...)` call, so the Capacity page shows the same
  refusal reason for a gated product.
- `_automation_before_execution`'s `evaluate_implementation(...)` call
  (`orchestrator.py:1921`, always `existing_slot=True`) is deliberately **not**
  wired — the gate never fires for an occupied claim, so wiring it there would be a
  no-op; leaving it out keeps the diff to call sites where it can matter.

### 5. `server/automations/capacity_readout.py::LAYER_OF_CODE`

Add explicit entries `"pr_doctor_priority": "product", "pr_doctor_priority_held":
"product"` for clarity, even though the `_layer_for` fallback already defaults
unmatched codes to `"product"`.

## Testing

- `server/tests/test_product_implementation_decision.py` — new cases for
  `decide_implementation(pr_doctor_gated=True, existing_slot=False)` → refused
  `pr_doctor_priority`; `existing_slot=True` → permitted `pr_doctor_priority_held`;
  `pr_doctor_gated=False` → neither code appears.
- `server/tests/test_product_admission.py` — `ImplementationSnapshot.decide()`
  produces the gated refusal when the resolved owner's slug is in
  `pr_doctor_pending`, and does not when it isn't (or ownership is unresolved).
- A new orchestrator-level test (alongside `server/tests/test_pr_doctor_sweep.py`'s
  existing fakes) for `_pr_doctor_pending_products()`: a classified-stuck PR with no
  `gave_up` state yields its product's slug; a `gave_up` target does not; a healthy
  PR does not.
- `server/tests/test_backlog_dispatch.py` (or wherever `_backlog_admission` /
  `_backlog_dispatch_batch` is exercised end-to-end) — a stuck PR for product X
  refuses a new dispatch for X while a dispatch for an unrelated product Y still
  succeeds.

## Out of scope

- No change to `capacity_plan.py` / `capacity_apply.py` — the governor's own
  per-product cap math is untouched; this gate is orthogonal to it and works
  identically for `auto` and `pinned` products.
- No change to PR Doctor's own scheduling, attempts ceilings, or fix behavior.
- No change to Backlog Runner's `max_concurrent`/fleet-wide config.
