# Capacity settings — one structure for throughput, hosts, and limits

**Status:** proposed; D1, D2, D3, D12 and D13 confirmed by the owner in the 2026-09-20 walkthrough
(§11), D6 and D7 still open (§10) · **Date:** 2026-09-20 · **Parents:**
`2026-09-19-capacity-governor-design.md` (the five-layer ceiling stack and the Delivery → Capacity
readout), `2026-09-04-settings-redesign-design.md` (Settings IA: five groups, `settingsDefs`,
chassis and save contract), `2026-09-12-product-automation-policy-design.md` (per-product policy),
`2026-07-21-max-active-sessions-queue-design.md` (the global cap) · **Evidence:** the audit in §3
was taken from the checkout at `b98ad848` on 2026-09-20; every finding cites `file:line`.

## 1. Problem

The capacity governor (2026-09-19) gave ViberPit one *readout* of its ceiling stack, but left the
*dials* where they had accreted. Today the numbers that decide how many sessions run are set on
five pages under three nav sections, and the readout links to them one way only:

| Layer of the stack | Where its dial lives | Nav section |
|---|---|---|
| Global cap (`maxActiveSessions`) | Settings › General › *Capacity* group | Workspace |
| Hosts (`maxSessions`, `weight`, `enabled`) | Settings › Hosts | Accounts |
| Accounts (`enabled`, `weeklyReservePct`) | Settings › Accounts | Accounts |
| Runner ceiling (`max_concurrent`) | Delivery → Automations → Backlog Runner → Config | Delivery |
| Products (`priority_rank`, `capacity_mode`, `max_in_flight`) | Delivery → Capacity **and** Roadmap → product → Automation card | Delivery, Roadmap |
| Push level, governor mode, `sessions_per_account` | Delivery → Capacity (first two); the governor's generic config page (third) | Delivery |

Three things make this confusing rather than merely spread out:

1. **The same word means different things on different pages.** "Global cap" is
   `AppSettings.max_active_sessions` on the Capacity page but the runner's `max_concurrent` inside
   admission (`server/automations/product_admission.py:147`). "Max sessions" is a per-host column,
   a global setting, and a runner field. "Weight" is a host score multiplier (1–1000, default 100)
   and a product slot-share divisor (≥0, default 1). "Weekly reserve %" is a per-account ledger value
   on the Accounts page and the governor's push-level percentage on the Capacity page.
2. **Related limits are not shown together.** The global cap sits in General next to archive
   retention and the AI assistant; the per-host caps it should relate to are two groups away. Nothing
   says how `maxActiveSessions = 10` relates to `dobby 4 + rocky 6 = 10`; they are independent
   numbers that happen to match.
3. **One number stands in for four constraints.** Throughput is limited by runners (hosts), by
   Claude account quota, by how many PRs a repo's CI and merge queue can absorb at once, and by how
   much parallel work the owner wants to supervise. The global cap is a single hand-typed integer
   that silently conflates all four, and the one that bites most on KinDash — CI and merge
   throughput — has no cap at all, only an ordering hint (§3.3, C23).

## 2. Goals and non-goals

**Goals**

1. Two pages, two questions. **Settings › Capacity** answers "what can the fleet run?" (supply you
   provision, changes rarely). **Delivery → Capacity** answers "how is automation spending it, and
   why is nothing running?" (allocation you steer weekly, plus the live readout).
2. Every capacity number has exactly one page where it is edited, and every page that shows it
   links there.
3. Each of the four constraints has its own dial at its own layer, and the page says so: the
   global cap is the lower of a *physical* ceiling (derived from hosts) and an *attention* ceiling
   (typed); quota caps the *runner* ceiling; CI and merge throughput caps each product's *pipeline*.
4. One vocabulary. One meaning per word across Settings, Delivery, Roadmap, and the API.

**Non-goals**

- No change to the governor's planner (`plan_capacity`) beyond feeding it the pipeline cap (D13),
  and none to the queue semantics of the global cap.
- No sixth top-level nav entry. The five-entry nav is a recorded finish-line decision.
- No settings search (still the governor spec's phase 3).
- Codex / Copilot quota in the availability budget (still gated on #1444).
- No CI-minutes accounting. The pipeline cap counts PRs, not runner minutes.

## 3. Audit findings

Severity: **H** misleads or blocks · **M** costs time or trust · **L** polish.

### 3.1 Placement and navigation

| # | Sev | Finding | Evidence |
|---|---|---|---|
| C1 | H | The global cap is filed under Workspace › General beside paths, archive retention and the AI assistant. It is the top of the ceiling stack and the only layer in that section. | `web/src/features/settings/sections/GeneralSection.tsx:280-297`; `settingsDefs.ts:68` |
| C2 | H | Hosts (the supply side of the stack) are filed under the **Accounts** group. The section is labelled "Hosts" by the registry and "Fleet" by its own `h3`, two names on one page. | `settingsDefs.ts:86`; `FleetSection.tsx:151-157` |
| C3 | M | The Capacity page's *list* column renders no links: `CapacityScopeBody` drops `row.href`. Only the detail pane links, and the Products row has `href: null` there too. | `CapacityScopeBody.tsx:33-45`; `capacityStack.ts:76` |
| C4 | M | Links from the stack to Settings are hard-coded strings (`/settings/general`, `/settings/fleet`, `/settings/accounts`) rather than `settingsSectionPath()`; a renamed section id breaks them silently. | `capacityStack.ts:54,58,65`; `navDefs.ts:150-153` |
| C5 | M | Linking is one-directional. Settings › General, › Hosts and › Accounts never mention the live readout. A person editing "Max sessions" on Hosts cannot see what it does. | all three sections |
| C6 | M | A queued session ("Queued (3)") or one showing "Waiting for capacity" offers no path to the cap it is waiting on. The only "See capacity" affordances are the runner header and the Roadmap card. | `QueuedSection.tsx:43`; `statusTier.ts:111`; `DeliveryPage.tsx:92-95` |
| C7 | L | The runner ceiling row links to `?tab=config`, which `AutomationDetail` silently drops back to `status` when `configSchema` is empty. | `capacityStack.ts:69`; `AutomationDetail.tsx:43-44` |
| C8 | L | A binding capacity constraint feeds the scope-tab badge only; the Delivery nav badge counts actionable items, so "nothing can run" is invisible until you are already on Delivery. | `DeliveryListColumn.tsx:47`; `useNavBadges.ts:51-61` |

### 3.2 Two owners, or none

| # | Sev | Finding | Evidence |
|---|---|---|---|
| C9 | H | Product `max_in_flight`, `capacity_mode` and rank are editable on Delivery → Capacity (batched `SaveBar`, hand-managed `expectedUpdatedAt`) **and** on the Roadmap product card (per-change PATCH, `automationPolicyUpdatedAt`). Two optimistic-concurrency implementations over one field, two validation rules. | `CapacityDetail.tsx:88-151,214-269`; `AutomationPolicyCard.tsx:118-121,162-169` |
| C10 | M | The runner ceiling (`max_concurrent`, labelled "Max occupied slots") is shown on Capacity with a "stored ceiling N" hint but editable only on the runner's config tab, which saves on every change with no confirmation. | `capacityStack.ts:67-72`; `ConfigPanel.tsx:15-57`; `backlog_runner.py:272-274` |
| C11 | M | Product `weight` decides contested-slot share but appears only on the Roadmap card, not on the page named Capacity. It overlaps `priority_rank`, which the governor uses; `weight` feeds only the `weighted` selection strategy. | `AutomationPolicyCard.tsx:162-169`; `backlog_selection.py:107-111`; governor spec §4 |
| C12 | M | The governor's third knob, `sessions_per_account`, is editable only on the governor automation's generic config form, not on the Capacity page that shows the two knobs beside it. | `capacity_governor.py:30-34`; `CapacityDetail.tsx:286-316` |
| C13 | M | Per-repo "weights / concurrency caps" on the runner config tab are a third caps surface, keyed by repo while `max_in_flight` is keyed by product; the `concurrency-cap` strategy receives no caps for products. | `ConfigInput.tsx:55-95`; `orchestrator.py:6099-6146` |

### 3.3 Relationships that exist in code but not on screen

| # | Sev | Finding | Evidence |
|---|---|---|---|
| C14 | H | The global cap and the sum of host caps are independent numbers. On the primary the global gate counts every host's records (the primary holds all rows), so it *is* fleet-wide, yet nothing reconciles it with `Σ Host.max_sessions`; the readout shows both rows as if commensurate. | `orchestrator.py:2983-3022`; `capacity_plan.py:72-80` |
| C15 | H | Two reserves. The plan uses the push level's `RESERVE_PCT` (`account_headroom(reserve_pct=…)`); the per-account `weeklyReservePct` is used only for display ranking. An operator who sets a per-account reserve changes nothing in the plan. | `capacity_wiring.py:104`; `accounts.py:617-634`; `capacity_plan.py:6-7` |
| C16 | M | Two "global cap" vocabularies: `global_capacity_full` (runner ceiling) is mapped to layer `runner` while a separate `global_cap_full` code is invented for the real global cap. | `capacity_readout.py:31-42,186-201`; `product_admission.py:147` |
| C17 | M | Three `max_active_sessions` clamps and a duplicated reader; two `active session` counters with a documented outage when the wrong one was published. | `orchestrator.py:3022`; `api/sessions.py:484-499`; `api/settings.py:227`; `daemon.py:463-487` |
| C18 | M | Three ways for the plan to silently stop governing: `mode`, `VIBERPIT_CAPACITY_GOVERNOR_ENABLED`, and `PLAN_MAX_AGE_S` staleness. The readout returns `killSwitch` and `governorMode`; the UI reads neither (the select reads `useAutomations()` instead). | `capacity_governor.py:93`; `capacity_apply.py:26`; `CapacityDetail.tsx:172,304-311` |
| C19 | M | Ceilings outside the five-layer stack: `max_spawns_per_day` (daily budget), `CCSWITCH_TEST_SLOT_CAP` (not even in `_MANAGED`), `CCSWITCH_CHILD_SESSION_CAP`, `CCSWITCH_STT_MAX_CONCURRENT`. None appear on either capacity page. | `backlog_runner.py:275`; `config.py:278-291`; `settings.py:42-56` |
| C23 | H | **The product pin does not bound PRs in CI.** A runner slot frees when the PR *merges* — unless PR Doctor covers the repo, in which case it frees as soon as the PR is open with auto-merge armed. A product at "max in flight 4" can therefore have four sessions working plus any number of PRs stacked in CI, each rebase cycle re-exposing them to flaky checks. `_backlog_repo_load` already counts handed-off work until merge, but only to *order* repos fairly; nothing refuses a claim on it. | `backlog_selection.py:51-60`; `orchestrator.py:6335-6360`; `backlog_runner.py:2037-2038` |

### 3.4 Data freshness

| # | Sev | Finding | Evidence |
|---|---|---|---|
| C20 | M | `useUpdateSettings`, `useFleetHostPatch` and the account mutations do not invalidate `QK.capacity`; after editing a cap in Settings, the Capacity page shows the old stack for up to 30 s. | `hooks.ts:372-378,1158-1164` |
| C21 | M | `GET /api/capacity` returns per-account rows (`reason`, `nextResetAt`), per-host rows, `runner.derived`, `plan.budget`, `plan.perProduct` — none rendered. When the binding constraint is "no account headroom", the UI cannot say which account or when it resets, though both are on the wire. | `api/capacity.ts:9-124`; `CapacityDetail.tsx` |
| C22 | L | `PUSH_LEVEL_RESERVE_PCT` is mirrored client-side while `accounts.reservePct` arrives on the wire unread (already in #2032). | `CapacityDetail.tsx:51-57` |

## 4. Decisions (with alternatives)

Decisions marked **✔** were confirmed by the owner on 2026-09-20 (§11).

| # | Question | Decision | Alternatives considered |
|---|---|---|---|
| D1 ✔ | Where do capacity dials live? | **Two pages by question, not one page by topic.** *Settings › Capacity* holds **supply**: what the fleet can run (limits, hosts, accounts). *Delivery → Capacity* holds **allocation and readout**: how automation spends it (push level, governor, runner ceiling, product rank / pin / mode / pipeline cap) and the live stack. Each page carries a read-only summary of the other and links to it. The placement rule for any future knob: does it change what exists, or how it is spent? | **(a) Everything on Delivery → Capacity**, retiring Settings › Hosts and the General field: host lifecycle (add, endpoint, upgrade, remove) and account auth flows are provisioning, not delivery steering; Delivery's list/detail chassis would host entity CRUD it was not built for, and Settings' scope badges / confirmation tiers would be lost. **(b) A top-level Capacity nav entry** holding all three: breaks the five-entry nav decision. **(c) Leave placement, add cross-links only**: this is what the governor spec's D9 already did; the screenshots show it is not enough. |
| D2 ✔ | Settings group | **Rename the `accounts` group to "Capacity"** (id `capacity`) with three sections in this order: **Limits** ★ (`capacity`, new), **Hosts** (`fleet`), **Accounts** (`accounts`). Section ids unchanged so every existing link resolves. Group description: "What the fleet can run: limits, machines, and the accounts that carry sessions." Settings › Capacity and Delivery → Capacity share the word on purpose: same stack, two questions; each page's description line and the two-way links disambiguate. | **"Fleet"** as the group name: "fleet" already means hosts specifically (`/settings/fleet`, `FleetSection`, `fleet_orchestrator`) and accounts are not fleet; a person looking for the global cap would not try "Fleet" before "Capacity". **Keep "Accounts" and add Limits under Workspace**: leaves C1 in place. |
| D3 ✔ | What is the global cap? | **The lower of two ceilings, one derived and one typed.** *Physical ceiling* = Σ `max_sessions` over enabled hosts; if any enabled host is uncapped the physical ceiling is unbounded. *Attention ceiling* = `AppSettings.max_active_sessions`, repurposed: nullable, blank = none, meaning "never more than N sessions at once, whatever the hardware could take". Effective global cap = `min(physical, attention)`; when both are unbounded, `DEFAULT_MAX_ACTIVE_SESSIONS` (5) applies as the last-resort floor and the Limits section says so ("no host cap set — using the default of 5; set a cap on Hosts"). The Limits section shows all three lines and names which one binds; the readout's Global row gains `physical`, `attention`, `binding`. Nothing changes on the live fleet: the row's 10 becomes an attention ceiling equal to the physical one. | **Blank derives from hosts, set value overrides** (the first draft): still one number carrying two meanings; an "override" that is *higher* than the physical ceiling is meaningless and one that is lower is really an attention budget — say so. **Fold quota in** (global cap falls as accounts drain): rejected, see D12. **Remove the global cap** and rely on host caps: host caps refuse dispatch, the global cap *queues*; the single-host, no-cap case needs the floor. |
| D12 ✔ | Where does account quota bind? | **At the runner layer only, never the global cap.** The global cap gates every session, including ones the owner starts by hand. The weekly reserve exists precisely to keep quota back for the owner's own use, so a global cap that fell as accounts drained would queue the owner's own sessions to protect quota for the owner. The governor already applies quota where it belongs: `fleet_concurrency = min(runner_ceiling, account_cap, host_free + in_flight, global_free + in_flight)`. Unchanged; recorded so nobody "fixes" it. | Apply quota globally: see above. |
| D13 ✔ | CI and merge throughput | **A per-product pipeline cap.** New `PRODUCT_POLICY_SCHEMA` field `max_pipeline_prs` (label **Pipeline cap**, int ≥ 0, nullable, blank = unlimited; help: "Most automation PRs this product may have open and unmerged at once, counting PRs handed off to PR Doctor. A full pipeline refuses new claims so a fast-claiming product cannot stack CI."). Counted from `_backlog_repo_load` summed over the product's repos (it already counts handed-off work until merge and excludes `gave_up`). Enforced in `decide_implementation` as constraint `product_pipeline_full` (layer `product`), refusing new claims only; an in-flight session is never touched. The governor treats a pipeline-full product as `eligible = 0` in `_split`, so its slots go to the next product by rank rather than sitting idle. The readout's `products[]` gain `pipeline` and `pipelineCap`; the Capacity table gains a **Pipeline** column (`n / cap`, `∞` when blank). This is a new mechanism, not a relabel: its own PR (§8, PR 6). | **Reuse the pin as the CI cap** (today's proxy): C23 shows it does not bound CI once PR Doctor covers the repo. **Global pipeline cap**: CI capacity is per repo (KinDash's checks are not ViberPit's). **Count CI minutes**: needs GitHub Actions billing reads and a model of check duration; a PR count is the number the owner already reasons in. **Make it a runner-config per-repo map** (like the existing `RepoNumberMap`): C13 shows per-repo maps on the runner tab are the third caps surface nobody finds; the product policy is where the other product dials live. |
| D4 | One reserve | **Per-account `weeklyReservePct` becomes an override of the push level's reserve**, and the plan honours it: `account_headroom` takes `reserve_pct` per account = `account.weeklyReservePct ?? RESERVE_PCT[push_level]`. Accounts page label: "Weekly reserve override (blank = push level's N%)", with N live from `GET /api/capacity`. | **Delete the per-account reserve**: an account shared with a human's own daily use legitimately needs a higher floor than the fleet default. **Delete the push level's reserve and use per-account only**: the push level is one of the owner's two intent knobs (governor D2). |
| D5 | Product dials: one owner | **Delivery → Capacity owns rank, cap mode, pin and pipeline cap.** The Roadmap card's *Capacity* fieldset keeps `dispatch` (a product-level on/off/propose decision) and `queue_min_status` / `respect_dependencies` (eligibility, not capacity), renames the legend to **Dispatch**, and replaces `capacity_mode` / `max_in_flight` / `weight` with a read-only line "Rank 2 · auto (cap 4) · pipeline 3/6 — set on Delivery → Capacity". | **Roadmap card owns them, Capacity is read-only**: the Capacity table is where the person compares products; editing one row while looking at the others is the use case. **Both stay editable with one shared component**: still two doors to one room (2026-09-04 D3's rejected shape). |
| D6 | Product `weight` | **Deprecate.** `priority_rank` is the governor's ordering; `weight` feeds only the `weighted` selection strategy. Mark the `ConfigField` `deprecated`, hide it from both cards, keep the key readable for one release, and file the removal issue. `selection_strategy` `weighted` falls back to rank order. **Open (§10 Q1).** | **Keep both and explain the difference**: the difference (rank = who gets the next fleet slot; weight = share of a *contested freed* slot under one strategy) is real but too fine for a product owner's involvement budget of "priority and macro direction". |
| D7 | Runner ceiling | **Editable on Delivery → Capacity**, in the same `SaveBar` form as the pins, labelled "Runner ceiling" with the description "Most sessions the Backlog Runner may hold at once; a share of the global cap. Quota (push level) and the governor lower it, never raise it." The runner's config tab shows the field read-only with a link ("Set on Capacity"). Mechanism: `ConfigField` gains `surface: "capacity" \| "config"` (default `config`); `ConfigPanel` renders `capacity`-surfaced fields as read-only links. `max_spawns_per_day` and `sessions_per_account` take the same surface so all governor-adjacent numbers are on one page. **Open (§10 Q2).** | **Move `max_concurrent` into `AppSettings`**: it is the runner's, and the plan overlay (`_backlog_read_config`) reads it there. **Leave it on the config tab with a link** (today): C10. |
| D8 | Vocabulary | One term per concept, used on every surface and in every `ConfigField` label: **Global cap** (effective), **Physical ceiling** (Σ host caps), **Attention ceiling** (`maxActiveSessions`), **Host cap** (`Host.max_sessions`, column header "Cap"), **Runner ceiling** (`max_concurrent`), **Pin** (`max_in_flight`), **Pipeline cap** (`max_pipeline_prs`), **Rank**, **Push level**, **Reserve**. "Max active sessions", "Max occupied slots", "Max sessions", "In-flight cap" and "Max in flight (pin)" are retired as labels. The two `global_cap*` constraint codes are collapsed: `global_capacity_full` → `runner_ceiling_full`, layer `runner`; `global_cap_full` keeps layer `global`. | Keep the wire names and only fix labels: the constraint-code confusion (C16) lives in the API and its tests, and every future reader trips on it. |
| D9 | Outside-the-stack ceilings | **Shown, not restructured.** The Limits section gets a *Managed by env* group listing `CCSWITCH_TEST_SLOT_CAP`, `CCSWITCH_CHILD_SESSION_CAP`, `CCSWITCH_STT_MAX_CONCURRENT`, `VIBERPIT_CAPACITY_GOVERNOR_ENABLED` read-only (the `_MANAGED` allowlist gains the first three). `max_spawns_per_day` moves to the Capacity page per D7. | Model them as stack layers: the test-slot and child caps are per-host / per-session, not fleet ceilings; the readout would mislead. |
| D10 | Freshness and links | Every mutation that touches a dial invalidates `QK.capacity`. Links go through `settingsSectionPath()` and `deliveryPath('capacity')`; the list column renders row links; Settings › Capacity sections carry a **Live readout** card (stack summary + binding text) linking to Delivery → Capacity; queued and "Waiting for capacity" sessions link there too; the Accounts row expands to the per-account rows already on the wire (which account is at ceiling, when it resets). | Fix only the invalidation: C3–C6 and C21 stay. |
| D11 | Code consolidation | One reader for the effective global cap (`server/capacity/limits.py:effective_global_cap()` returning physical, attention, effective and which binds; used by the orchestrator, `api/sessions.py`, `api/settings.py`, and the readout), one clamp; `api/sessions.py:_max_active_sessions_setting` deleted. The double plan overlay (`_backlog_read_config` and `gather_implementation_snapshot`) collapses to one seam that returns the overlaid config. Principle VI. | Leave as is: three clamps and two overlays are the shape of the 2026-09-17 stall. |

## 5. Information architecture

### 5.1 Settings › Capacity (supply)

Group id `capacity`, label "Capacity" (renamed from `accounts`; group ids appear in no URL, so
only `SETTINGS_GROUPS`, the three defs' `group` field and `settingsDefs.test.ts` change).

| Section (id) | Scope | Contents |
|---|---|---|
| **Limits** ★ (`capacity`) | Server | *Global cap* group, three rows: **Physical ceiling** (read-only: "10 — dobby 4 · rocky 6", or "unbounded — rocky has no cap" linking to Hosts); **Attention ceiling** (number input, blank = none, description "Never run more than this many sessions at once, whatever the hardware could take. Blank leaves the physical ceiling in charge."); **Effective global cap** (read-only: the value and which ceiling binds, or "default 5 — no ceiling set" when neither exists). *Live readout* card: the five stack rows read-only with used / cap, binding text, link "Open Delivery → Capacity". *Managed by env* group (D9). `SaveBar` form. |
| **Hosts** (`fleet`) | Server | As today, with: the inner `h3 "Fleet"` removed (C2); column "Max sessions" → **Cap**; "Weight (dispatch share)" keeps its label and gains the description "Tiebreaker when several hosts have headroom; not a cap."; a footer line "Σ caps = 10 → physical ceiling" linking to Limits. Weight and cap move into the per-host form with `SaveBar` (2026-09-04 §8, still pending). |
| **Accounts** (`accounts`) | Server | As today, with: "Weekly reserve %" → **Weekly reserve override** (blank = push level's N%, N live); the "over reserve" chip uses the same effective reserve the plan uses (D4); a row-level "not metered" badge for Codex/Copilot so the Accounts stack row's `total` is explicable. |

### 5.2 Delivery → Capacity (allocation and readout)

One page, unchanged chassis. Top to bottom:

1. **Intent** row: Push level (`RadioGroup`, immediate) · Governor mode (`Select`, immediate; reads
   `readout.governorMode`, and shows a `Banner` when `killSwitch` is off or the plan is stale so the
   three silent stops in C18 become one visible state).
2. **The stack**: five rows, each linking to its owner page. Global shows "physical 10 · attention
   10" and which binds; Accounts expands to per-account rows (`reason`, `nextResetAt`); Runner shows
   stored vs effective and, when quota binds, "quota: N accounts × M"; Products links to the table
   below (`href: '#products'`).
3. **Runner form** (`SaveBar`, shared with the pins): Runner ceiling · Daily budget
   (`max_spawns_per_day`) · Sessions per account. One form, one save, one confirmation when a
   ceiling is *raised* (lowering never kills a session; raising spends quota).
4. **Products table**: Rank ▲▼ · Product · Dispatch (read-only, links to the Roadmap card) · Cap
   mode · Pin · **Pipeline** (`n / cap`, input in the same form; `∞` when blank) · Effective cap ·
   In flight · Eligible · Waiting on. A pipeline-full product's *Waiting on* reads "pipeline full:
   6 PRs open (cap 6)" and links to the Pipeline page filtered to that repo.
5. **Plan card**: unchanged, except `reasons` names pipeline-full products ("KinDash: pipeline
   full, slots given to ViberPit").

### 5.3 Roadmap → product → Automation card

Legend **Dispatch** (was *Capacity*): `dispatch`, `queue_min_status`, `respect_dependencies`, then
a read-only line "Capacity: rank 2 · auto (effective cap 4) · 1 in flight · pipeline 3/6 — adjust
on Delivery → Capacity". Legends *Models* and *Gates* unchanged.

## 6. Relationships, stated

| Relationship | Where it is shown | How it is computed |
|---|---|---|
| Global cap = min(physical, attention), floor 5 when both unbounded | Limits (three rows), Hosts (footer), stack Global row | D3; `effective_global_cap()` |
| Physical ceiling = Σ enabled host caps, unbounded if any is uncapped | Limits, Hosts footer | D3 |
| Runner ceiling ≤ global cap | Capacity page runner form (description; validation refuses a ceiling above the effective global cap with "the global cap is N") | client + `PATCH /automations/backlog-runner` 422 |
| Runner effective ≤ quota (available accounts × sessions per account) | stack Runner row ("quota: N × M") | D12; unchanged `_fleet` |
| Effective reserve = account override ?? push level | Accounts (label), stack Accounts row expanded (`reservePct` per row) | D4; `account_headroom` |
| Effective product cap = pin if pinned else plan ?? pin | Products table (already), Roadmap card read-only line | unchanged |
| New claims refused while product pipeline ≥ pipeline cap; governor gives the slots away | Products table *Pipeline* + *Waiting on*, plan card reasons | D13; `decide_implementation`, `_split` |
| Plan live = mode act ∧ kill switch ∧ fresh | Capacity page intent row (`Banner`), About (`_MANAGED`) | unchanged, surfaced |

## 7. API changes (additive)

| Route | Change |
|---|---|
| `GET /api/settings` | `effectiveDefaults.maxActiveSessions` is replaced by `effectiveDefaults.globalCap: { physical: int \| null, attention: int \| null, effective: int, binding: "physical" \| "attention" \| "default", hosts: [{ name, cap }] }`. `managed` gains `CCSWITCH_TEST_SLOT_CAP`, `CCSWITCH_CHILD_SESSION_CAP`, `CCSWITCH_STT_MAX_CONCURRENT`. `PUT /api/settings` `maxActiveSessions` semantics: attention ceiling, `null` = none, a set value must be ≥ 1 (422 otherwise, replacing today's silent clamp; the default-5 floor lives in `effective_global_cap()`). |
| `GET /api/capacity` | `global` gains `physical`, `attention`, `binding`; `products[]` gain `pipeline: int` and `pipelineCap: int \| null`; `accounts.rows[].reservePct` (effective per account); new constraint code `product_pipeline_full` (layer `product`); rename per D8 (old code accepted by the web for one release). |
| `GET /api/pm/policy-schema` · `PATCH /api/pm/projects/{slug}/policy` | new field `max_pipeline_prs`; `weight` accepted but ignored once deprecated (D6). |
| `GET /api/accounts` | `weeklyReservePct` unchanged; new `effectiveReservePct`. |
| `GET /api/automations` | `configSchema[].surface` and `configSchema[].deprecated`. |
| `PATCH /api/automations/backlog-runner` | 422 when `max_concurrent` exceeds the effective global cap. |

### 7.1 Data model

| Change | Detail |
|---|---|
| `AppSettings.max_active_sessions` | Column unchanged (nullable int). Meaning changes to *attention ceiling*; docstring updated; `DEFAULT_MAX_ACTIVE_SESSIONS` becomes the last-resort floor inside `effective_global_cap()` only. No migration. |
| `PRODUCT_POLICY_SCHEMA` | `ConfigField(key="max_pipeline_prs", label="Pipeline cap", type="int", default=None, min=0, nullable=True, surface="capacity", help=…)` (D13). JSON policy column; no migration. |
| `capacity_plans.budget` | gains `pipeline: {slug: {open, cap}}` so the plan card can explain a pipeline-full allocation. JSON column; no migration. |

## 8. Sequence

Six PRs, each green in a day, each citing the C-numbers and D-numbers it closes. PR 1 first; PRs
2–6 are independent of each other once PR 1 has landed.

| PR | Delivers | Deletes |
|---|---|---|
| **1 — Limits + the two ceilings** | `server/capacity/limits.py:effective_global_cap()` and the single clamp (D11 first half); D3 physical / attention / effective; `Limits` section; group rename (D2); General loses its *Capacity* group; `_MANAGED` additions (D9); invalidation (C20); typed links (C4). | `api/sessions.py:_max_active_sessions_setting`; the General *Capacity* group; the three clamps |
| **2 — Readout depth + links** | List-column links; Accounts row expansion (C21); Global row's three numbers; intent-row `Banner` for C18; Live-readout card on the three Settings sections; queued / waiting-for-capacity links (C6); nav badge for a binding constraint (C8). | `PUSH_LEVEL_RESERVE_PCT` mirror |
| **3 — Runner form** | `ConfigField.surface`; runner ceiling, daily budget, sessions-per-account on the Capacity page with one `SaveBar`; read-only link rows on the config tab; ceiling ≤ global cap validation; one plan overlay seam (D11 second half); constraint-code rename (D8). Blocked on §10 Q2. | the second overlay in `gather_implementation_snapshot` |
| **4 — One owner for product dials** | Roadmap card *Dispatch* legend + read-only capacity line (D5); `weight` deprecated (D6) with its issue filed; labels per D8 across schemas. Blocked on §10 Q1. | the card's `capacity_mode` / `max_in_flight` / `weight` controls; `weighted` strategy's weight read |
| **5 — One reserve** | D4: per-account override honoured by the plan; Accounts label + effective chip; `effectiveReservePct`. | the display-only reserve ranking path |
| **6 — Pipeline cap** | D13: `max_pipeline_prs` field; per-product pipeline count from `_backlog_repo_load`; `product_pipeline_full` constraint in `decide_implementation`; `_split` treats pipeline-full as `eligible = 0`; `pipeline` / `pipelineCap` on the readout; *Pipeline* column and input on the Capacity table; plan-card reason; feature doc. | — |

PR 1 is independent of the governor follow-ups (#2032, #2040); PR 3 subsumes #2032's
`RESERVE_PCT`-mirror item and #2040's `limiting`/`floor` display fix.

## 9. Tests

| Test | Asserts |
|---|---|
| `test_capacity_limits.py` (pure) | physical = Σ enabled host caps; any uncapped enabled host ⇒ physical unbounded; effective = min(physical, attention); both unbounded ⇒ 5 with `binding: "default"`; `binding` names the lower one; the orchestrator gate, `api/sessions`, `api/settings` and the readout all call it (a grep-style test that no other `max_active_sessions` read exists). |
| `test_capacity_readout.py` | `global.{physical,attention,binding}`; `products[].pipeline` counts handed-off rows and excludes `gave_up` and merged; per-account `reservePct` = override ?? push level; renamed constraint code and its layer. |
| `test_product_admission.py` | pipeline ≥ cap ⇒ `product_pipeline_full` refused for a *new* claim; an `existing_slot` re-validation is never refused on it (D8 of the governor spec, "never kills a session"); blank cap ⇒ never refused; PR Doctor coverage makes no difference to the pipeline count (C23). |
| `test_capacity_plan.py` | a pipeline-full product gets cap = its in-flight count and its remaining slots go to the next rank; `reasons` names it. |
| `test_account_headroom.py` | an account with a 60% override is unavailable at 41% used while the push level is `steady`. |
| `test_backlog_runner_config.py` | `max_concurrent` > effective global cap ⇒ 422 with the cap in the detail. |
| `settingsDefs.test.ts` | the `capacity` section registered under group `capacity`; order Limits · Hosts · Accounts. |
| `LimitsSection.test.tsx` | three rows render from `effectiveDefaults.globalCap`; blank attention shows the physical value as placeholder; "unbounded" links to Hosts. |
| `CapacityDetail.test.tsx` | runner form has the only `SaveBar` together with the pins and pipeline caps; raising the ceiling confirms; intent banner renders for kill-switch-off and stale plan; *Pipeline* column shows `∞` when blank and the waiting-on text when full. |
| `AutomationPolicyCard.test.tsx` | no `capacity_mode` / `max_in_flight` / `weight` controls; read-only line links to Delivery → Capacity. |
| brand ratchet, mypy, full web suite | as always. |

## 10. Open questions for the reviewer

1. **D6 weight deprecation.** Is anyone relying on `selection_strategy: weighted`? If yes, keep
   `weight` under an *Advanced* legend on the Roadmap card and only drop it from Capacity.
2. **D7 surface flag.** Rendering `surface: capacity` fields read-only on the generic config tab is
   the smallest change; the alternative is to remove them from the runner's `configSchema` entirely
   and have the Capacity page PATCH raw keys, which loses schema validation on that path.

## 11. Decisions of record (2026-09-20)

Walked through one at a time with the owner:

1. **D1** — "go with the first option": two pages split by question, supply in Settings, spend on
   Delivery.
2. **D2** — "Go with Capacity": the Settings group is named Capacity, deliberately pairing with the
   Delivery tab.
3. **D3 / D12 / D13** — the first draft's "blank derives from hosts" was reframed after the owner
   named the real factors: "we're limited by runners, claude account capacity, and in some cases
   (like kindash) want to be thoughtful about how many PRs we're pushing through CI at a time".
   Confirmed as: global cap = physical ceiling (hosts) and an attention ceiling (typed) only;
   quota binds at the runner layer, never globally, because the reserve exists to protect the
   owner's own sessions; CI and merge throughput becomes a per-product pipeline cap counted from
   repo load, which the existing pin does not bound once PR Doctor covers the repo (C23).
