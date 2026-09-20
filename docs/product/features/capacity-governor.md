---
slug: capacity-governor
parent: planning-and-automation
title: Capacity Governor
state: live
links:
  code:
    - server/automations/capacity_plan.py
    - server/automations/capacity_readout.py
    - server/automations/capacity_wiring.py
    - server/automations/capacity_governor.py
    - server/automations/capacity_store.py
    - server/api/capacity.py
    - web/src/features/delivery/capacity/
---

# Capacity Governor

Show the whole ceiling stack as one readout, then let two human knobs — priority rank and push
level — drive the caps that used to be set once and never revisited.

**Problem** — Throughput was governed by dials the person had to find and set by hand, scattered
across four pages under three nav sections (Settings › General capacity, Delivery → Backlog Runner
config, each product's Automation card, Settings › Hosts), with no single place showing the ceiling
stack as one thing. Meanwhile the real constraint moved hour by hour — account weekly windows drain
and reset, hosts fill with human sessions, one product's backlog empties while another's grows — but
every product's `max_in_flight` and the runner's `max_concurrent` were set once and never revisited.
The person was the only feedback loop.

**Target users** — Anyone deciding how hard the fleet should push: the owner tuning push level and
per-product priority when quota or host headroom shifts, and anyone diagnosing "why is nothing
running" via the Delivery → Capacity page instead of cross-referencing four screens.

**Desired outcome** — One readout (`GET /api/capacity`) names the binding constraint across global
cap, hosts, accounts, runner ceiling, and products, and a `capacity-governor` automation computes a
plan every tick from real headroom and priority order. In phase 1 (this doc) the governor only
computes and shows the plan (`mode: monitor`); phase 2 applies it to the runner's caps (`mode: act`).

**Success metrics** — (unknown — needs owner input). Grounded candidates: time from "nothing is
running" to identifying the binding constraint; how often a product's `max_in_flight` still needs a
manual edit once `capacity_mode: auto` plans are trusted; whether the 2026-09-17-style stall (a card
and a ledger disagreeing about capacity) recurs.

**Non-goals** — Applying the plan to the runner's caps (phase 2); Codex/Copilot quota in the
availability budget (phase 3, gated on reliable quota readers); a queue-theoretic scheduler; changing
`selection_strategy` semantics or per-product daily budgets.

**Notes** —
- **Push level** is the one fleet-wide knob: `conserve` / `steady` / `max` map to a 40% / 20% / 5%
  weekly-usage reserve on Claude accounts (the share KEPT BACK; a per-account override replaces it, spec 2026-09-20 D4). A higher reserve makes fewer accounts count as available,
  so the derived fleet concurrency shrinks; `max` spends quota closer to the ceiling.
- **Priority rank** is the one per-product knob (`Project.priority_rank`, nullable, 1 = highest,
  unranked products sort after ranked ones). The planner splits available fleet slots in rank order:
  pinned products take their pin first, then each remaining `auto` product gets slots round-robin by
  rank, never below its current in-flight count and never above its eligible work.
- **Global layer** — `AppSettings.max_active_sessions` is the fleet-wide active-session ceiling.
  Since 2026-09-20 (PR 1 of the capacity-settings spec) that column is the **attention ceiling**;
  the effective global cap is `min(Σ enabled host caps, attention ceiling)` with a floor of 5 when
  both are unbounded, read only through `server/sessions/global_cap.py`. It is shown on
  Settings › Capacity › Limits and on the readout's Global row (`physical`, `attention`, `binding`).
- **Runner form and settings surface (PR 3)** — Since spec PR 3 (2026-09-20) the runner ceiling (`max_concurrent`), the daily budget
  (`max_spawns_per_day`) and `sessions_per_account` are edited on Delivery → Capacity in the same
  form as the product pins; the automations' Config tabs show them read-only (`ConfigField.surface =
  "capacity"`). A runner ceiling above the effective global cap is refused (422). The runner-layer
  admission code is `runner_ceiling_full` (was `global_capacity_full`); the readout's `accounts`
  block carries `sessionsPerAccount` and `accountCap` so the Runner row can say when quota binds.
  `PATCH /api/automations/{id}` merges a partial `config` over the stored row.
- **One owner for product dials (PR 4)** — Since spec PR 4 (2026-09-20) a product's rank, cap mode
  (`capacity_mode`) and pin (`max_in_flight`) are edited on Delivery → Capacity only: both policy
  fields carry `surface = "capacity"` with the D8 labels **Cap mode** / **Pin**, and the Roadmap
  card's *Capacity* legend became *Dispatch* (dispatch, reach, eligibility) with one read-only
  line built from the readout ("Capacity: rank 2 · auto (effective cap 4) · 1 in flight.") linking
  there. The product `weight` is deprecated (`ConfigField.deprecated`, removal #2103): nothing
  reads it, both cards hide it, a stored row still validates. The `weighted` selection strategy
  orders like `priority`, and `priority` now serves products in **rank order** (`priority_rank`,
  then product age) — the same key `capacity_plan.product_order` uses.
- **One reserve (PR 5)** — Since spec PR 5 (2026-09-20, D4) a per-account `weeklyReservePct` is an
  OVERRIDE of the push level's reserve with the same meaning (percent of the weekly allowance kept
  back; ceiling `100 − reserve`), and the plan honours it: `account_headroom(push_level=…)` judges
  each account by `effective_reserve_pct` (override, else `RESERVE_PCT[push level]`) and every
  readout row carries the `reservePct` it was judged by. A row's `reason` is `at_ceiling` when any
  window has reached the hard ceiling (the selector's exclusion rule) and `over_reserve` when its
  reserve window has reached the effective reserve's ceiling, so the governor and the selector agree
  on both. `GET /api/accounts` publishes `effectiveReservePct` and `pushLevelReservePct`; Settings
  › Capacity › Accounts labels the field **Weekly reserve override** (placeholder "push level's N%"),
  and its over-reserve chip and usage marker sit at the effective ceiling. The CLI selector demotes
  only explicit overrides (a human's own pick is never steered by the push level, D12).
- **Pins vs auto** — `capacity_mode` on the product policy is `auto` (the governor sets
  `max_in_flight` from the plan) or `pinned` (the stored value is a hard cap the governor respects
  and allocates around). The runner's `max_concurrent` and the global active-session cap stay ceilings
  the governor never raises, only ever caps below.
- **Monitor vs act** — `monitor` computes and stores the plan (`capacity_plans` row, `applied=False`)
  purely for display; `act` sets `applied=True` and the plan is merged into the runner's caps at its
  existing read seams (`SessionOrchestrator._backlog_read_config` for `max_concurrent`,
  `policies_by_slug` for each auto product's `max_in_flight`). Nothing else in the runner changes —
  `decide_implementation` still reads the same merged values it always did.
- **Kill switch** — `VIBERPIT_CAPACITY_GOVERNOR_ENABLED` (default true); setting it to `false` stops
  plan application. It is re-read on every seam read (`capacity_store.live_plan` /
  `live_plan_sync`), not baked in at write time: turning it off stops an already-written,
  already-`applied=True` plan from governing on the very next read, with no need to wait for the
  loop to write a new row. The readout keeps working either way, since it is built from the same
  `decide_implementation` counts the runner already uses, not from the plan.
- **Phase 2 (shipped)** applies the plan through those read seams. A plan is only "live" — and so only
  applied — when it was written in `act` mode (`applied=True`), the kill switch reads on *now* (not
  at write time), and it is fresh: computed within the last 10 minutes and not in the future (a small
  60s tolerance absorbs ordinary clock skew between the writer and a reader). A stale plan, one
  written in `monitor` mode, one computed in the future, or a currently-disabled kill switch is
  ignored and every seam falls back to the human-owned stored value. The deployed governor stays in
  `monitor` until the owner (or the next session, after a day of sensible plans reading on the
  Capacity page) flips it to `act`. The Capacity page's runner row shows the enforced cap
  (`effective`) next to the stored ceiling, so a plan-derived cap is visibly distinct from what the
  operator set.
- **An occupied claim is never refused on capacity** (D8: "admission refuses new claims; it never
  kills a session"). `product_policy.decide_implementation`'s capacity check skips the ceiling
  entirely for a claim that already holds its slot (`existing_slot=True` with a matching ownership
  token) — a cap that moved under it (a plan re-tick, a pause to 0, a product falling out of the
  plan) can never turn an in-flight claim's next revalidation into a refusal. A brand-new claim is
  still refused by a 0 cap exactly as before.

**Evidence** — Spec: `docs/superpowers/specs/2026-09-19-capacity-governor-design.md`. Plan:
`docs/superpowers/plans/2026-09-19-capacity-governor-phase1.md`.
