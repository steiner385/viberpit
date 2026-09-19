# Capacity governor phase 2 — act mode: the runner reads the plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the governor is in `act` mode and the kill switch is on, the Backlog Runner's fleet
concurrency and every auto-mode product's in-flight cap come from the current `capacity_plans` row.
Pinned products, the runner's `max_concurrent` ceiling and the global cap are never exceeded. A
plan that is not applied, older than 10 minutes, or written while the kill switch is off is absent.

**Architecture:** One pure module (`capacity_apply.py`) decides whether a plan is live and overlays
it onto the two things the runner already reads: the per-product raw policies and the runner
config. Three seams call it: `gather_product_coverage` (admission), `policies_by_slug` (queue scope
and the card), `_backlog_read_config` (fleet ceiling). The readout's `effectiveCap` reads the same
overlay so the page and admission agree (D6). No new tables, no new routes.

**Tech Stack:** Python 3.12 · SQLAlchemy · pytest · React + Vitest (one small web change).

**Spec:** `docs/superpowers/specs/2026-09-19-capacity-governor-design.md` §6.3 (applying the plan),
§6.4 (failure modes: stale plan absent, kill switch), D3 (ceilings never raised), D5 (plan row is
merged at read time), §10 item 2. Prerequisite: issue #2031 (Task 1). Phase 1 plan:
`docs/superpowers/plans/2026-09-19-capacity-governor-phase1.md`.

## Global Constraints

- **Ceilings never rise:** `max_concurrent` after overlay is `min(stored, plan.fleetConcurrency)`; a
  product's effective cap is the plan value only for `capacity_mode == "auto"`; pinned products keep
  `max_in_flight`. A product absent from the plan keeps its stored value.
- **A plan is live iff** `applied is True` AND `computedAt` is within `PLAN_MAX_AGE_S = 600` of now
  AND `get_settings().capacity_governor_enabled` is True. Anything else = no overlay (today's
  behaviour).
- **Never write the plan into `Project.automation_policy` or the automation row.** Overlay at read
  time only; the overlaid dict is a copy.
- Brand ratchet: no `ccswitch` string in new files. DB clock rule. `env -u …` test scrub as in phase 1.
  Test command: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_SOCKET -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA -u CCSWITCH_DAEMON_ROLE -u VIBERPIT_SESSIOND_ENDPOINT CCSWITCH_DEV=1 PYTHONPATH=$PWD:$PWD/src /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest <files> -q -p no:cacheprovider`.
- Commit messages via `-F <file>` with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: fix the planner's in-flight floor (#2031)

**Files:** Modify `server/automations/capacity_plan.py::_split`; Test `server/tests/test_capacity_plan.py`.

**Interfaces:** `_split(fleet, products) -> dict[slug, cap]` unchanged signature. Invariants after
this task: every cap ≤ `max(p.eligible, p.in_flight)`; the round robin only distributes slots ABOVE
each product's in-flight floor; `sum(caps) ≤ max(fleet, Σ in_flight of working products)`.

- [ ] Step 1: failing tests (append to `test_capacity_plan.py`):

```python
def test_floor_never_lifts_a_cap_above_eligible_or_the_fleet_sum():
    from server.automations.capacity_plan import _split
    prods = [_prod("kindash", 1, eligible=2, in_flight=4), _prod("viberpit", 2, eligible=10)]
    caps = _split(5, prods)
    assert caps["kindash"] == 4                      # floor: never below in-flight
    assert caps["viberpit"] == 1                     # 5 - 4 already held = 1 slot left
    assert sum(caps.values()) == 5

def test_pinned_floor_is_deducted_before_the_round_robin():
    from server.automations.capacity_plan import _split
    prods = [_prod("viberpit", 1, eligible=1, in_flight=3, mode="pinned", pin=1),
             _prod("kindash", 2, eligible=10)]
    caps = _split(4, prods)
    assert caps["viberpit"] == 3 and caps["kindash"] == 1 and sum(caps.values()) == 4
```

- [ ] Step 2: run → FAIL. Step 3: implement — seed every working product's cap with its floor and
  deduct floors from `fleet` before distributing:

```python
def _split(fleet: int, products: list[ProductCapacity]) -> dict[str, int]:
    working = [p for p in products if p.dispatch != "off" and p.eligible > 0]
    caps: dict[str, int] = {}
    for p in working:
        if p.capacity_mode == "pinned":
            caps[p.slug] = max(min(p.pin, p.eligible), p.in_flight)
        else:
            caps[p.slug] = p.in_flight
        fleet -= caps[p.slug]
    auto = [p for p in working if p.capacity_mode != "pinned"]
    changed = True
    while fleet > 0 and changed:
        changed = False
        for p in auto:
            if fleet <= 0:
                break
            if caps[p.slug] < p.eligible:
                caps[p.slug] += 1
                fleet -= 1
                changed = True
    return caps
```

  Update `test_never_below_in_flight_or_above_eligible` and `test_hysteresis_applies_on_second_tick`
  expectations only if the algorithm above changes them (it should not for `in_flight=0` fixtures).
- [ ] Step 4: run `test_capacity_plan.py` → PASS; ruff; commit `fix(capacity): distribute only above the in-flight floor (#2031)` with `Fixes #2031` in the body.

---

### Task 2: `capacity_apply.py` — the pure overlay

**Files:** Create `server/automations/capacity_apply.py`; Test `server/tests/test_capacity_apply.py`.

**Interfaces (produces):**

```python
PLAN_MAX_AGE_S = 600
def plan_is_live(plan: dict | None, *, now: datetime, enabled: bool, max_age_s: int = PLAN_MAX_AGE_S) -> bool
def apply_product_plan(policies: Mapping[str, dict | None], plan: dict | None) -> dict[str, dict | None]
    # for each slug: if plan has perProduct[slug] and effective_policy(raw)["capacity_mode"] == "auto":
    #   {**(raw or {}), "max_in_flight": int(plan["perProduct"][slug])}  (a NEW dict; raw None → stays None: dispatch off anyway)
def apply_fleet_plan(config: Mapping[str, Any], plan: dict | None) -> dict
    # {**config, "max_concurrent": min(int(config["max_concurrent"]), int(plan["fleetConcurrency"]))} when plan has fleetConcurrency
```

`plan_is_live`: `plan` truthy, `plan.get("applied") is True`, `enabled`, and `computedAt` parses
(`datetime.fromisoformat`; naive → assume UTC) to within `max_age_s` of `now`. Callers pass a plan
only when `plan_is_live` said yes; the two `apply_*` functions themselves treat `plan=None` as "no
change" and never raise on a malformed plan (log warning, return the input copy).

- [ ] Tests (write first): live/applied-false/stale-11-min/kill-switch-off/missing-computedAt →
  `plan_is_live` truth table; auto product overlaid, pinned untouched, absent slug untouched, None raw
  stays None, input dicts not mutated; fleet min in both directions; malformed plan → unchanged.
- [ ] Implement; run; ruff + mypy on the file; commit `feat(capacity): pure plan overlay (plan_is_live, apply_product_plan, apply_fleet_plan)`.

---

### Task 3: the three read seams

**Files:**
- Modify `server/automations/capacity_store.py` (add `def read_plan_sync(db) -> dict | None` sharing `_to_dict`, and `async def live_plan() -> dict | None` / `def live_plan_sync(db) -> dict | None` that return the plan only when `plan_is_live(plan, now=datetime.now(timezone.utc), enabled=get_settings().capacity_governor_enabled)`).
- Modify `server/automations/product_admission.py::gather_product_coverage` — after reading `policies`, `policies = apply_product_plan(policies, await live_plan())`.
- Modify `server/automations/product_policy.py::policies_by_slug(db)` — read `live_plan_sync(db)` and return `{slug: effective_policy(raw) for slug, raw in apply_product_plan(rows, plan).items()}`. (`effective_policy` is applied after the overlay, as today.)
- Modify `server/sessions/orchestrator.py::_backlog_read_config` — `config = apply_fleet_plan(config, await live_plan())` before returning.
- Test `server/tests/test_capacity_apply_seams.py` (`isolated_db`): write two `Project` rows (auto + pinned policies), a fresh applied plan row via `write_plan` → `gather_product_coverage()` shows the auto product's `max_in_flight` from the plan and the pinned one unchanged; `policies_by_slug` (via `db.run_sync`) agrees; a stale row (set `computed_at` 11 min back directly) → no overlay; `monkeypatch` `get_settings().capacity_governor_enabled = False` → no overlay; `_backlog_read_config` on a `SessionOrchestrator` built the way `test_backlog_policy_seams.py` builds one returns `max_concurrent == min(stored, plan)`; and admission end-to-end: with the plan giving the auto product cap 2 and one in-flight claim in `automation_target_state`, `gather_implementation_snapshot(...).decide(repo, requested_product=slug, trigger="automatic", effect="dispatch")` is `permitted` on `product_capacity` (cap 2), and with cap 1 it is refused with `product_capacity_full`.
- Also run `server/tests/test_backlog_policy_seams.py server/tests/test_backlog_query_queue.py server/tests/test_product_implementation_decision.py server/tests/test_pm_api.py server/tests/test_backlog_dispatch.py` — none may change behaviour with no plan row present.
- Commit `feat(capacity): the runner reads a live plan — admission, queue scope, fleet ceiling`.

---

### Task 4: readout `effectiveCap` and the plan card's staleness

**Files:** Modify `server/automations/capacity_readout.py` (`effectiveCap` = `plan["perProduct"][slug]` when `plan_is_live(plan, now, enabled=src.kill_switch())` and `capacityMode == "auto"`, else pin; readout gains top-level `planLive: bool`); `server/tests/test_capacity_readout.py` (live plan → effectiveCap from plan for auto, pin for pinned; stale/unapplied → pin); `web/src/api/capacity.ts` (`planLive: boolean`); `web/src/features/delivery/capacity/CapacityDetail.tsx` plan card: when `plan.applied && !planLive` show a `Banner tone="warning"` "This plan is stale (older than 10 minutes) — the runner is using stored caps until the governor ticks again."; `CapacityDetail.test.tsx` case. Full `npm run test`.
- Commit `feat(capacity): readout effectiveCap follows the live plan; stale-plan banner`.

---

### Task 5: docs, gate, PR

- `docs/superpowers/specs/2026-09-19-capacity-governor-design.md` status line: "· phase 2 implemented on branch feat/capacity-governor-phase2"; `docs/product/features/capacity-governor.md` Notes: act mode now applies the plan through the three read seams; a plan older than 10 min is ignored; the deployed governor stays in `monitor` until the owner (or the next session, after a day of sensible plans) flips it to `act`.
- Full gate exactly as phase 1 Task 10 (six steps, test slot, foreground). Then push, PR (`Fixes #2031`, `Refs #2030`), `gh pr merge --squash --auto`.
