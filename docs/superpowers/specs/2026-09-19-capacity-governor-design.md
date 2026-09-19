# Capacity governor — one capacity readout, and dials that tune themselves

**Status:** approved direction (2026-09-19, three macro decisions taken by the owner: intent knob =
priority rank + push level; existing caps become pins; phase-1 availability = Claude account quota +
host capacity) · **Date:** 2026-09-19 · **Related:** `2026-09-12-product-automation-policy-design.md`
(per-product policy, `max_in_flight`), `2026-09-04-settings-redesign-design.md` (Settings IA; D8
deferred search), `2026-09-18-hold-ladder-design.md`, #1517 (host at cap alarms), #1891 (KinDash
repo-load starvation), #2009 / PR #2020 (dead claims hold slots), #1611 (automation consolidation
audit).

## 1. Problem

Two symptoms, one cause: throughput is governed by dials the person has to find and set by hand.

**Findability.** The dials that decide how many sessions run live on four pages under three nav
sections: Settings › General › Capacity (global active-session cap), Delivery → Backlog Runner →
Config (fleet concurrency, daily budget, selection strategy), Roadmap → product → Overview →
Automation card (dispatch, in-flight cap, weight, model, ladder, risk tolerance), and Settings ›
Hosts (per-host caps, display-only). The test-slot cap is env-only and not on the About page. The
word "weight" means two unrelated things on two pages; five automations spell "daily budget" five
ways. The automation and product config forms save on every keystroke with no confirmation while
the low-risk Settings forms get a save bar. Nothing shows the ceiling stack as one thing, which is
why the 2026-09-17 stall read "capacity full" on one page while three others showed nothing running.

**Tuning.** Every product carries `max_in_flight: 1` and the runner `max_concurrent: 6`, set once
and never revisited, while the real constraint moves hour by hour: account weekly windows drain and
reset, hosts fill with human sessions, one product's backlog empties while another's grows. The
person is the only feedback loop. The owner's stated involvement budget is "priority and macro
direction", nothing else.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **One feature, two phases: a readout first, the loop second.** A Capacity scope under Delivery renders the whole ceiling stack and names the binding constraint; a `capacity-governor` automation computes a plan every tick, in `monitor` mode only shows it, in `act` mode applies it. | The readout is what makes the loop reviewable; shipping it first makes phase 2 a mode flip, not a leap. |
| D2 | **The person's knobs are exactly two: per-product `priority_rank` and one fleet-wide `push_level`.** | Decision 1 of the owner. Everything else derives. |
| D3 | **Existing caps become pins.** A product's `max_in_flight` and the runner's `max_concurrent` stay as fields; a new per-product `capacity_mode` (`auto` default, `pinned`) says whether the governor owns the product's cap. `max_concurrent` and the global `maxActiveSessions` are ceilings the governor never raises. | Decision 2. Blank means "the machine decides"; a set value is respected and badged. Ceilings protect shared capacity exactly as the 2026-09-12 spec intended. |
| D4 | **Phase-1 availability = Claude account quota + host capacity.** Codex and Copilot quota join once their readers are reliable (rocky's Codex framing latch, #1444, still blanks). Until then a Codex/Copilot account contributes zero to the budget and is listed as "not metered". | Decision 3. Unreliable signals would make the governor flap. |
| D5 | **The plan is written to its own row, never into the human's policy.** `capacity_plans` holds the derived values; the runner's read seams merge plan over policy. `Project.automation_policy` and the runner config row stay human-owned. | The governor can never clobber an edit; the UI can always show "yours" vs "derived". Same separation the hold ladder keeps between a person's decision and a bounce. |
| D6 | **The readout and admission share one function.** `GET /api/capacity` is built from `decide_implementation` and the runner's own counts; the binding constraint IS the first refused constraint code. | Two readers of the same rows disagreeing is the bug shape this repo names first; the 2026-09-17 stall was exactly a card and a ledger disagreeing. |
| D7 | **Defects are not capacity.** A product whose in-flight count exceeds its live runner sessions is reported as a `capacity_defect`, not as "full". Self-heal is PR #2020's job; the governor names the row and links the session. | Last night's three dead claims were invisible because they looked like legitimate load. |
| D8 | **Hysteresis over precision.** A derived cap changes only when the new value has held for two consecutive ticks, and a cap is never lowered below the product's current in-flight count. | Admission refuses new claims; it never kills a session. Flapping caps would only churn the plan and the event feed. |
| D9 | **Findability fixes ride along, cheaply.** Cross-links from every capacity dial to the Capacity page; the product policy card groups its fields under Capacity / Models / Gates legends; the runner card links "See capacity" when its waiting reason is capacity; `CCSWITCH_TEST_SLOT_CAP` joins the About page allowlist. Settings search (2026-09-04 D8) is phase 3. | The page that shows the stack is where the person will look; every other page should point there. |
| D10 | **Kill switch `VIBERPIT_CAPACITY_GOVERNOR_ENABLED`** (default true) stops plan application; the readout keeps working. New settings take the `VIBERPIT_` prefix. | Same shape as `VIBERPIT_GATE_AUTORESOLVE_ENABLED`. |

## 3. Use cases

1. **Why is nothing running?** Delivery → Capacity shows the stack; the binding row is highlighted
   ("viberpit: 1/1 in flight, but 0 live sessions — capacity defect, claim steiner385/viberpit#1010
   from an archived session"). One click opens the session; the defect is an event PR #2020 acts on.
2. **Push harder this week.** The owner sets push level `max`. The governor drops the weekly reserve
   to 5%, recomputes: 4 available accounts × 2 = 8, hosts 6 + 5 free, runner ceiling 6 →
   fleet 6; split by rank: KinDash 3, viberpit 2, ghostdriver 1. Next tick the runner claims
   accordingly. The plan row says why.
3. **Quota is draining.** Two accounts cross the reserve line; available drops to 2 → fleet 4. The
   governor lowers KinDash from 3 to 2 after two ticks; running sessions finish; no new claim until
   in-flight drops below 2. The Capacity page shows "accounts: 2 of 4 available, next reset 14:30".
4. **Pin one product.** The owner sets viberpit `capacity_mode: pinned`, `max_in_flight: 1`. The
   governor allocates around it; the product row shows a "pinned" badge.
5. **Everything is at ceiling.** Available accounts 0 → fleet 0. The runner idles with a plain
   sentence ("waiting on quota: earliest reset 08:00 Wed") instead of spawning sessions that stall.

## 4. Data model

| Change | Detail |
|---|---|
| `Project.priority_rank` | `Integer`, nullable. 1 = highest. Unranked products sort after ranked ones by `created_at`. Nullable, so `_reconcile_columns` adds it on both dialects; migration `00NN_project_priority_rank` recorded anyway (chain `down_revision` off the newest at merge time; re-check the number just before merging). |
| `PRODUCT_POLICY_SCHEMA` | new `ConfigField(key="capacity_mode", label="In-flight cap", type="enum", choices=("auto", "pinned"), default="auto", help="auto: the capacity governor sets this product's in-flight cap from the fleet's real headroom and your priority order. pinned: the value below is a hard cap the governor respects.")`. `max_in_flight` label becomes "Max in flight (pin)"; help gains "Ignored while In-flight cap is auto, except as the fallback when no plan exists." |
| `capacity_plans` | one row, `id = "current"`: `computed_at`, `push_level`, `applied` (bool: act mode and kill switch on), `fleet_concurrency` (int), `per_product` (JSON `{slug: cap}`), `budget` (JSON: the inputs, §6.2), `binding` (JSON `{layer, code, text}`), `reasons` (JSON list of one-sentence explanations), `defects` (JSON list). Non-nullable columns get an entry in `_REQUIRED_EXTRAS` (`test_postgres_compat.py`) and, where no default synthesises, `RECONCILE_EXEMPT` (`test_schema_reconcile_coverage.py`). |
| Governor config (`ConfigField`s) | `push_level` enum `conserve / steady / max` default `steady` (weekly reserve 40 / 20 / 5 %); `sessions_per_account` int default 2 (concurrent runner sessions one account can carry — the selector's "sharing live load" rule); `hysteresis_ticks` int default 2. |
| Runner config | unchanged. `max_concurrent` keeps its meaning as a ceiling. |

## 5. The readout — `GET /api/capacity`

One function, `server/automations/capacity_readout.py:build_readout()`, called by the route and by
the governor loop (so the page and the plan never diverge). Shape:

```
{
  computedAt, pushLevel, killSwitch: bool,
  global:   { cap, active, free },                       # AppSettings.max_active_sessions vs orchestrator count
  hosts:    [{ name, enabled, healthy, active, max, free }],
  accounts: { available, total, reservePct, nextResetAt,
              rows: [{ name, engine, metered: bool, available: bool, reason, windows: [...] }] },
  runner:   { ceiling: max_concurrent, inFlight, derived: fleet_concurrency | null },
  products: [{ slug, name, rank, dispatch, capacityMode, pin, effectiveCap, inFlight, eligible,
               liveSessions, waitingOn: text | null, constraint: code | null }],
  binding:  { layer: "global"|"host"|"account"|"runner"|"product"|"defect"|null, text },
  defects:  [{ product, target, sessionId, sessionStatus, text }],
  plan:     { applied, fleetConcurrency, perProduct, reasons } | null
}
```

`products[].constraint` is the first non-permitted code from `decide_implementation` for that
product's canonical repo (`product_capacity_full`, `global_capacity_full`, `daily_budget_full`,
`dispatch_off`, `approval_required`, …), and `waitingOn` is that constraint's own `text` — the
readout adds no second vocabulary. `binding` is the constraint that refused the highest-ranked
product with eligible work, or the defect when one exists (D7). `accounts.rows[].available` uses
`selector.at_ceiling` with `ceiling = 100 - reservePct` so the governor and the account selector
agree on what "usable" means.

## 6. The governor — `server/automations/capacity_governor.py`

Two objects, mirroring `control_band_monitor.py`: `CapacityGovernor` (the `Automation` surface:
id `capacity-governor`, name "Capacity Governor", modes `off / monitor / act`, `work_class =
"maintenance"`, oversight responsibility "Execute and finish work", relationships `uses pm-sync`
(eligible counts) and `shares_work_with backlog-runner` (it sets what the runner may claim), with a
mode description per mode as `validate_oversight` requires) and `CapacityGovernorLoop` (supervised
loop, every I/O injected: `read_config`, `read_readout`, `write_plan`, `emit`).

### 6.1 Tick

Every 60 s (the runner's own cadence): `readout = build_readout()`; `plan = plan_capacity(readout,
config, previous_plan)` (pure); `write_plan(plan)`; emit events for every changed cap
(`cap_changed`, target = product slug, summary "kindash in-flight cap 1 → 3: 4 accounts available,
rank 1 of 3") and for every defect (`capacity_defect`, warn, once per target until it clears).
`monitor` computes and writes the plan with `applied = False`; `act` writes `applied = True` when
the kill switch is on. The card's `AutomationStatus.text` is the binding sentence.

### 6.2 `plan_capacity()` — pure, table-tested

```
reserve      = {conserve: 40, steady: 20, max: 5}[push_level]
available    = accounts that are enabled, healthy, metered, and not at_ceiling(100 - reserve)
account_cap  = len(available) * sessions_per_account
host_free    = Σ max(0, max - active) over enabled healthy hosts with a max; unbounded hosts add ∞
global_free  = global.cap - global.active
fleet        = min(runner.ceiling, account_cap, host_free + runner.inFlight, global_free + runner.inFlight)
```

Per-product split, over products with `dispatch != off` and `eligible > 0`, ordered by
`priority_rank` (nulls last, then `created_at`):

1. Pinned products take `min(pin, eligible)` first; `fleet -= Σ pins`.
2. Each remaining auto product gets 1 while `fleet > 0`, in rank order.
3. Leftover slots go to the highest-ranked product with `cap < eligible`, one at a time, wrapping.
4. A cap never drops below the product's current `inFlight` (D8), and never exceeds
   `eligible`.

Hysteresis: a change is applied only if `plan_capacity` produced the same new value on the previous
tick too (`hysteresis_ticks`); otherwise the previous cap is carried and `reasons` says "kindash:
2 → 3 pending confirmation next tick". `reasons` always lists, per product, the one sentence that
explains its cap.

### 6.3 Applying the plan

One pure merge, `server/automations/capacity_plan.py:apply_plan(policies, runner_config, plan)`,
called in the two read seams the runner already has:

- `SessionOrchestrator._backlog_read_config`: `config["max_concurrent"] = min(config["max_concurrent"],
  plan.fleet_concurrency)` when `plan.applied`.
- `policies_by_slug` consumers (`ProductAdmission` coverage refresh): for each product with
  `capacity_mode == "auto"` and a plan entry, `policy["max_in_flight"] = plan.per_product[slug]`.
  Pinned products and products absent from the plan keep their stored value (today's behaviour).

Nothing else changes in the runner: `decide_implementation` still reads `effective["max_in_flight"]`
and `config["max_concurrent"]`; it just receives merged values. The `_refill` per-tick attempt count
follows `max_concurrent` as today.

### 6.4 Failure modes

| Failure | Behaviour |
|---|---|
| Account usage unreadable (`availability != "available"`) | that account counts as unavailable; `reasons` says so; a fleet of zero available accounts yields `fleet = 0` and the runner idles with the reset time in the card, not an error. |
| Host probe stale (`healthy is None`) | host excluded; noted in `reasons`. |
| Readout raises | loop logs, keeps the previous plan, emits `plan_stale` once; runner keeps reading the previous plan (bounded by its own ceilings, so a stale plan can only under-spawn). |
| Plan write fails | same as above; next tick retries. |
| Kill switch off / mode monitor | plan written with `applied = False`; seams ignore it. |
| Governor disabled entirely | no plan row is refreshed; seams treat a plan older than 10 minutes as absent (a stale plan never governs). |

## 7. UI

### 7.1 Delivery → Capacity (phase 1)

Third Delivery scope, `capacity` (`deliveryNavState.ts` `DELIVERY_SCOPES`), route
`/delivery?scope=capacity`. One page, `web/src/features/delivery/capacity/CapacityPage.tsx`,
reading `GET /api/capacity` (React Query hook `useCapacity`, 30 s refetch):

- **Push level** row at the top (`RadioGroup` conserve / steady / max, immediate save via
  `PATCH /api/automations/capacity-governor`), with the governor's mode switch beside it (off /
  monitor / act) and its one-sentence mode description from oversight.
- **The stack**: five rows top-to-bottom (Global cap → Hosts → Accounts → Runner ceiling →
  Products), each a labelled bar `used / cap` with a "free" figure; the binding row carries a
  `Banner tone="warning"` with the binding text. Each row links to the page that owns its dial
  (Settings › General, Settings › Hosts, Settings › Accounts, Delivery → Backlog Runner → Config).
- **Products table**: rank (▲ ▼ buttons, immediate save via `PATCH /api/pm/projects/{slug}`
  `{priorityRank}`), name, dispatch, cap mode (`Switch` auto/pinned, immediate), pin (number input,
  form with `SaveBar`, enabled only when pinned), effective cap, in flight, eligible, waiting on.
  A defect row renders `Badge tone="danger"` "defect" with the session link.
- **Plan card**: "Governor would set…" (monitor) / "Governor set…" (act) with `reasons` as a list,
  `computedAt`, and the last five `cap_changed` events.

Save contract follows the 2026-09-04 spec §7: `Switch`/`RadioGroup`/rank buttons are immediate
rows with `role="status"` feedback; the pin input is the only form and has the only `SaveBar`.

### 7.2 Findability changes (phase 1, D9)

- `AutomationPolicyCard`: fields grouped under legends **Capacity** (`dispatch`, `capacity_mode`,
  `max_in_flight`, `weight`, `queue_min_status`, `respect_dependencies`), **Models**
  (`default_model`, `ladder_top`, `unattended_model_floor`), **Gates** (`risk_tolerance`,
  `triage_auto_accept`, `give_up_after_bounces`, `max_initiatives_in_flight`); the Capacity legend
  links to Delivery → Capacity. `weight` label becomes "Weight (contested slot share)"; Hosts'
  becomes "Weight (dispatch share)".
- Backlog Runner status card: when `waitingReason == "capacity"` or the text carries
  `product_capacity_full`, an inline link "See capacity".
- Settings › Hosts: `maxSessions` becomes editable (the wire and `HostPatch` already accept it; the
  2026-09-04 spec §8 promised it).
- `server/api/settings.py:_MANAGED` gains `CCSWITCH_TEST_SLOT_CAP` and
  `VIBERPIT_CAPACITY_GOVERNOR_ENABLED` so About shows them.

### 7.3 Settings search (phase 3)

A "Find a setting" box on `SettingsIndexPage` over `settingsDefs` (label, description, keywords),
every automation's `configSchema` (label, help) from `GET /api/automations`, and
`GET /api/pm/policy-schema`, each hit linking to its page. Deferred to phase 3 so phases 1–2 are not
gated on it.

## 8. API summary

| Route | Change |
|---|---|
| `GET /api/capacity` | new, §5 |
| `PATCH /api/pm/projects/{slug}` | accepts `priorityRank: int | null`; audited as `pm.project.priority_rank` |
| `GET /api/pm/projects` | rows gain `priorityRank` |
| `PATCH /api/automations/capacity-governor` | existing generic route; config keys §4 |
| `GET /api/automations` | the governor appears with `status`, `oversight`, `configSchema` like every other automation |
| `PUT /api/fleet/hosts/{name}` | unchanged; UI starts sending `maxSessions` |

## 9. Testing

| Test | Asserts |
|---|---|
| `test_capacity_plan.py` (pure) | table-driven: reserve per push level; fleet = min of the four ceilings; pins first; rank-ordered round robin; never below in-flight; never above eligible; hysteresis carries the previous value once and applies on the second tick; zero available accounts → fleet 0 with the earliest reset in `reasons`. |
| `test_capacity_readout.py` | `binding.code` equals the first refused constraint `decide_implementation` returns for the top-ranked product with work (D6 parity); a claim row whose session is `archived`/`ended` with no live runner session lands in `defects`, not in `inFlight`-as-capacity; unmetered engines report `metered: false` and count zero. |
| `test_capacity_governor_loop.py` | injected seams: monitor writes `applied=False`; act + kill switch writes `applied=True`; readout raise keeps the previous plan and emits `plan_stale` once; `cap_changed` emitted only on a confirmed change. |
| `test_capacity_plan_apply.py` | `apply_plan` lowers `max_concurrent`, never raises it; auto products take the plan value, pinned keep theirs; a plan older than 10 min is ignored. |
| runner integration (`test_backlog_runner_*`) | with an applied plan, `product_capacity_full` fires at the derived cap; with `capacity_mode: pinned` at the pin. |
| Postgres suite | new table registered in `_REQUIRED_EXTRAS` / `RECONCILE_EXEMPT`; `CCSWITCH_TEST_PG_URL` run before merge. |
| web | `CapacityPage.test.tsx` (phone/desktop): stack renders, binding banner, rank buttons call the PATCH, pin form has one SaveBar, defect badge links the session; `deliveryNavState.test.ts` gains the `capacity` scope; the full `npm run test` (mocked `@/api/hooks` files). |
| brand ratchet | new env var uses `VIBERPIT_`. |

## 10. Phasing

1. **Readout + knobs, governor in monitor.** `priority_rank`, `capacity_mode`, `capacity_plans`,
   `capacity_readout.py`, `GET /api/capacity`, the governor automation with `plan_capacity` and the
   loop in `monitor` (registry default mode `monitor`, enabled), the Capacity scope and page, the
   D9 findability changes, tests. Behaviour-preserving: no plan is applied.
2. **Act mode.** `apply_plan` in the two read seams, kill switch, hysteresis tests, runner
   integration tests; flip the deployed governor to `act` after one day of monitor-mode plans
   reading sensibly on the Capacity page.
3. **Settings search; Codex/Copilot quota** once `ProcessQuotaRefresher` readings are reliable on
   rocky; a `capacity_defect` self-heal call into PR #2020's `attest_terminal_session_stopped` if
   the defect count stays non-zero after #2020 lands.

Not in this spec: changing `selection_strategy` semantics (#1891 fixes the repo-load input the
`balanced` strategy reads; the governor sets caps, not order within a product), per-product daily
budgets, host `weight` semantics, a queue-theoretic scheduler.

## 11. Decisions of record (2026-09-19)

Owner answered "go with your recommendations" to: (1) intent knob = per-product priority rank +
fleet push level; (2) existing caps become optional pins, global cap stays a hard human ceiling;
(3) phase-1 availability = Claude account quota + host capacity. The 2026-09-17 → 2026-09-18 stall
(#2009) and the KinDash starvation (#1891) are the motivating incidents.
