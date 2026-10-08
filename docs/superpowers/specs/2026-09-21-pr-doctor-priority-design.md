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
2. **Pending = a PR PR Doctor will actually act on** — `pr_doctor_engine.plan_recovery(
   failure_mode, mode, config, state)` yields `Mechanical` or `SessionFix` — so
   notify-only, propose-only, disabled and given-up cases never gate. Otherwise one
   unfixable or un-actionable PR, or PR Doctor being switched off entirely, would
   permanently starve a product's backlog with no recovery path.
3. **Per-product, not global.** Only the product(s) owning the stuck PR's repo are
   gated; other products keep dispatching normally.
4. **Never touches in-flight sessions.** Matches the existing D8 invariant already
   used by every other capacity check in `decide_implementation` (`server/
   automations/product_policy.py`): admission refuses new claims, it never
   interrupts one already running. A Backlog Runner session already spawned for a
   now-gated product runs to completion; only its *next* new claim is blocked.

5. **Amended 2026-09-22 (#2202): pending also requires that PR Doctor can CLAIM the
   PR.** Decision 2 asks whether PR Doctor *decides* to fix a PR; the busy /
   foreign-owner ownership checks run later, at dispatch. A PR whose claim is refused
   therefore gated its product for a repair that could never start — on 2026-09-22
   one product took 70 `skipped_pr_doctor_priority` refusals in 20 minutes while
   every PR Doctor claim on those same PRs was refused (`ownership_wait` ×463), a
   cross-automation deadlock with nothing live in either. So a target is dropped from
   the pending set when the newest `ownership_wait` event for it recorded a claim
   refusal (`_pr_doctor_refusal_detail`) at the PR's *current* head SHA, PR Doctor
   holds no `ownership_token` for it, and no attempt has been recorded at that head.
   Evidence PR Doctor already produced, not a second copy of the ownership rules:
   re-deriving claimability here would need the PR's linked-issue node IDs (a GitHub
   round trip per PR on the admission hot path) and a parallel implementation of
   `acquire`'s conflict conditions.
   - A refusal under PR Doctor's OWN token (`prior PR execution has not stopped`) is
     its predecessor transfer waiting for work to stop: its own lifecycle, which
     clears itself, so that still gates. A predecessor that never attests stopped is
     the attestation layer's defect, not the gate's.
   - `StaleOwnership` / `TargetIdentityError` are races the next sweep tick retries
     and are deliberately NOT recorded — one blip must not un-gate a whole head SHA.

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
# Signature amended by decision 5 (#2202): a mapping, so the refusal can name
# the PRs; MEMBERSHIP is still the whole gate.
async def _pr_doctor_pending_products(self) -> dict[str, tuple[str, ...]]:
    """Product slug -> the PR refs PR Doctor will actually act on (plan_recovery
    yields Mechanical or SessionFix, and the claim is not known-refused).
    Best-effort: any failure yields {} rather than blocking admission — a stale
    PR Doctor read must never wedge Backlog Runner dispatch."""
```

Iterates `await self._pr_doctor_scope_pairs()` (the same (repo, branch) universe the
sweep already walks). For each pair: resolve `info` via the shared
`_pr_doctor_info_for(repo, branch)` helper (same resolve-plus-stale-cache-reconcile
logic as `_pr_doctor_sweep` and `PrDoctorActionProvider.actionable`), skip
`None`/terminal; read the durable per-target state from one batched
`self._pr_doctor_ctx.list_target_states()` call taken before the loop; classify with
`pr_doctor_engine.classify(info)` and, if non-`None`, call `pr_doctor_engine.
plan_recovery(failure_mode, mode, config, state)` (mode/config read once, via the
same `_pr_autofix_mode()`/`_pr_doctor_config()` sources the sweep uses) — a repo is
"pending" only when that call returns `Mechanical` or `SessionFix`. Collect the set
of pending repos, then batch-resolve to product slugs with one call to
`owning_projects_by_repo` (`server/pm/
repo_project.py`) — ambiguous/unresolved repos are silently dropped (same posture
`owning_projects_by_repo` already documents: "absence is not proof of no owner"; a
repo automations can't confidently attribute to one product should not block that
product's backlog for a reason nobody can name). Wrapped in `try/except Exception`
returning `{}`, same resilience posture as `states`/`accounting` in
`gather_implementation_snapshot`.

Per decision 5, each pending target is bucketed claimable vs unclaimable through
`_pr_doctor_claim_unclaimable(state, refusals.get(target), info)`, reading
`_pr_doctor_ownership_waits()` (newest `ownership_wait` event per target). Only the
claimable refs reach the returned mapping; any product with dropped targets is
logged once per evaluation with both lists, since before #2202 nothing tied a
runner "capacity reserved" skip to the PR Doctor refusal causing it.

A claimable target whose newest `ownership_wait` is the attached-session wait
(`detail.attached_session`) is also unclaimable when that session is usage-stalled
(`is_usage_stalled`). The check reads the in-memory record first, then the shared
`sessions` row in one batched read (2026-10-08, KinDash#18420).

### 2. `server/automations/product_admission.py`

- `gather_implementation_snapshot(...)` gains `pr_doctor_pending:
  Callable[[], Awaitable[Mapping[str, tuple[str, ...]]]] | None = None`. Read once
  per snapshot (best-effort, `except Exception: pending = {}`), same pattern as the
  existing `states`/`accounting` reads a few lines below.
- `ImplementationSnapshot` gains a field `pr_doctor_pending: Mapping[str,
  tuple[str, ...]]`, defaulting empty.
- `ImplementationSnapshot.decide()` computes `pr_doctor_gated` from MEMBERSHIP of
  the owning product's slug (an entry mapped to `()` still gates) and passes
  `pr_doctor_gated=` plus `pr_doctor_pending_refs=` into
  `decide_implementation(...)`.
- `evaluate_implementation(...)` gains the same `pr_doctor_pending` parameter,
  threaded straight through to its `gather_implementation_snapshot(...)` call.

### 3. `server/automations/product_policy.py` — `decide_implementation()`

New parameters `pr_doctor_gated: bool = False` and `pr_doctor_pending_refs:
tuple[str, ...] = ()` (the latter from #2202 — refs supplied, the refused text names
up to three of them so the Capacity page's `waitingOn` says which PRs reserve the
product; no refs keeps the original sentence). New constraint, added alongside the
existing `capacity()` checks:

```python
if pr_doctor_gated:
    if existing_slot:
        add("pr_doctor_priority_held", "permitted",
            "This claim already holds its slot; PR Doctor's priority does not interrupt it.")
    else:
        add("pr_doctor_priority", "refused",
            _pr_doctor_priority_text(pr_doctor_pending_refs))
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
