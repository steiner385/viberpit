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
  weekly-usage reserve on Claude accounts. A higher reserve makes fewer accounts count as available,
  so the derived fleet concurrency shrinks; `max` spends quota closer to the ceiling.
- **Priority rank** is the one per-product knob (`Project.priority_rank`, nullable, 1 = highest,
  unranked products sort after ranked ones). The planner splits available fleet slots in rank order:
  pinned products take their pin first, then each remaining `auto` product gets slots round-robin by
  rank, never below its current in-flight count and never above its eligible work.
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
  plan application. The readout keeps working either way, since it is built from the same
  `decide_implementation` counts the runner already uses, not from the plan.
- **Phase 2 (shipped)** applies the plan through those read seams. A plan is only "live" — and so only
  applied — when it is fresh (computed within the last 10 minutes), was written in `act` mode
  (`applied=True`), and the kill switch was on when it was written; a stale plan, one written in
  `monitor` mode, or one written while the kill switch was off is ignored and every seam falls back
  to the human-owned stored value. The deployed governor stays in `monitor` until the owner (or the
  next session, after a day of sensible plans reading on the Capacity page) flips it to `act`. The
  Capacity page's runner row shows the enforced cap (`effective`) next to the stored ceiling, so a
  plan-derived cap is visibly distinct from what the operator set.

**Evidence** — Spec: `docs/superpowers/specs/2026-09-19-capacity-governor-design.md`. Plan:
`docs/superpowers/plans/2026-09-19-capacity-governor-phase1.md`.
