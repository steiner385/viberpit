# Decision escalation: making product decisions loud in proportion to what they block — Design

**Status:** proposed, 2026-09-30. **Builds on:** the hold ladder (`2026-09-18-hold-ladder-design.md`,
§3 holds, §6 decision proposals) and the Needs-you tiles (`2026-09-28-automations-needs-you-tiles-back-design.md`).

**Principle:** the operator should never have to discover that decisions are starving dispatch. A
pending decision is ranked by what it blocks, surfaced where the operator already looks, pushed
through the existing notification rules as one digest per product, and answerable in one step.
Nothing here resolves a decision on the operator's behalf.

## 1. Problem

On 2026-09-30 the global priority queue reported `held: 84, blocked: 56, status: 38, epic_open: 9,
assigned: 5, claimed: 2` across the fleet, with three KinDash issues dispatchable out of a 140+
issue backlog. The operator learned this by asking the assistant, and the assistant could not say
which product the holds belonged to, what each hold needed, or what any of them blocked. Verified
against the code:

| Gap | Evidence |
|---|---|
| Nothing tells the operator decisions are piling up | `propose_decision` emits no notification (`server/sessions/tools/tools.py:260-341`, no `notify_event` in `server/pm/decisions.py`); the runner logs it at info level (`backlog_runner.py:2405`), which `AutomationContext.emit` never routes (`context.py:212`). A `handed_back` session fires `needs_input` with body `"{name}: handed_back"` — the reason is dropped. |
| No per-product skip breakdown | `GET /api/pm/queue` ranks per product and then sums `skipped_p` into one fleet-wide map (`server/api/pm.py:932-933`); the runner keeps three of nine reasons per product (`orchestrator.py:6879-6885`). Skipped refs are counted, never kept (`queue.py:239-240`). |
| No way to list holds | No endpoint lists `WorkItem.hold_*` fleet-wide; `/api/pm/leaves` is per capability. No assistant tool mentions holds. |
| `resolve_decision` has no list companion | `GET /api/pm/decisions` exists, is pending-only, unscoped by product, and is already allowlisted for the assistant (`policy.py:158`) — but no tool calls it; the tool's own error says "from the decisions inbox" (`tools.py:1283`). |
| No blocking-impact model | `WorkItemDep` is read forward only (`queue_store.py:109-129`, src → open dsts). No reverse index, no transitive dependents. |
| Oversized assistant results | `get_product_deps` returns every `WorkItemDep` row for the product's repos, inactive included, plus a full drift audit (`pm.py:719-812`); `list_prs` returns every PR in every stage with `checks_detail`/`check_groups`/`node_states` (32+ keys); `list_automations` returns each `configSchema`. The only cap is 2000 chars per string (`tools.py:277-289`). |
| Quiet hours drop, not defer | `plan()` returns `deliver=False` and `notify_event` returns (`push.py:203`); no digest or deferral exists anywhere. |

Two adjacent defects found on the way, fixed in passing where the touched file is the same:
`get_priority_queue`'s description names a `'deps'` reason that does not exist (`blocked` does);
the web event-class list lacks `account_limited` (`notificationsApi.ts:63-72`).

## 2. Decisions taken

| # | Question | Decision |
|---|---|---|
| 1 | Where the "what needs a person and what does it block" model lives | One pure module, `server/pm/escalation.py`, over the queue loader's existing rows. No new table. |
| 2 | Impact metric | Transitive downstream dependents on the same board via `open_blockers`, reversed. Two numbers per hold: `downstream` (how many open issues sit behind it) and `downstreamDispatchable` (how many of those would have no other skip reason). Rank by `downstream`, then hold age. |
| 3 | Delivery | A new automation, `decision-escalation`, with one new event class `decision_needed`. One digest per product per trigger, deduped by fingerprint, repeated no more than every `repeat_hours`. No per-proposal ping. |
| 4 | Quiet hours | The digest tick defers: it skips while `in_quiet_hours` and re-evaluates from live state on the next tick. Nothing is stored and nothing is lost, because the digest is computed, not queued. |
| 5 | Dedup state | The latest `pm.escalation.digest` audit row per product carries the fingerprint and time. Survives restarts; no new table, no Postgres-suite list entries. |
| 6 | In-app surface | The Roadmap Inbox (where decisions already live) and the new automation's Needs-you tab. The Roadmap nav badge already counts pending decisions; the Sessions page is not touched. |
| 7 | One-step answering | "Accept recommendation" on the automation's actionable row and the inbox card, calling the existing accept route with `recommended_key`. A person taps; nothing auto-resolves. |
| 8 | Large results | Tool-side projection and endpoint-side filters, plus a byte cap on every assistant result with an explicit `truncated` marker. |

## 3. The read model — `server/pm/escalation.py`

Pure functions over `QueueRow`s (`server/pm/queue.py`) and pending `DecisionProposal`s. The loader
is `queue_store.load_queue_rows` unchanged — it already carries `hold_kind`, `open_blockers`,
`open_children`, `product`, and the rows for every onboarded product.

```python
@dataclass(frozen=True)
class HeldItem:
    ref: str; repo: str; number: int; product: str; title: str; url: str
    hold_kind: str; hold_reason: str; hold_by: str; hold_set_at: datetime | None
    proposal_id: str | None; options: list[dict]; recommended_key: str | None
    downstream: int; downstream_dispatchable: int; downstream_refs: tuple[str, ...]
    released_by: str   # copy: "Accept an option" | "Retry, a board move to Ready/Backlog, or an issue-body edit"

@dataclass(frozen=True)
class ProductEscalation:
    slug: str; dispatchable: int; skipped: dict[str, int]; held: tuple[HeldItem, ...]
    starving: bool; oldest_hold_hours: float | None

def reverse_blockers(rows) -> dict[str, set[str]]          # dst ref → src refs (same product)
def downstream(ref, reverse, rows_by_ref) -> tuple[str, ...]  # transitive, cycle-safe
def held_items(rows, proposals, *, now, min_status, respect_dependencies, ...) -> list[HeldItem]
def product_escalation(slug, rows, proposals, policy, *, now, starving_max) -> ProductEscalation
def rank_held(items) -> list[HeldItem]                     # downstream desc, age desc, ref
```

- **`downstream_dispatchable`** re-runs `skip_reason` on each downstream row with every ref in the
  chain removed from its `open_blockers`. It answers "if this whole chain shipped, what would the
  runner pick up?" — honest about the fact that releasing a hold makes the *held* item dispatchable
  now and its dependents only after it ships.
- **Hold kinds in scope:** `HELD_KINDS` (`owner_action`, `decision`) — the two the queue parks.
  `evidence` and `stronger_model` are queue-eligible and exit on their own; they are reported in
  `skipped` only when some other rule skips them.
- **`starving`** = `dispatchable <= starving_max` and `len(held) > 0` and the product's dispatch is
  on. A product with dispatch off is never starving.
- **Cross-product edges** are out of scope: `load_queue_rows` scopes deps to the product's repos
  (`deps_in_repos`), and a dependent on another board is that product's row. Documented, not solved.
- The pending proposal for a `decision` hold is joined by `work_item_id` (the only link that exists,
  `models.py:2208`); `proposal_id` is what the disposition dialog's deep link needs (hold-ladder §14).

## 4. API

### 4.1 `GET /api/pm/queue` — per-product breakdown (additive)

Keep the per-slug `skipped_p` the route already computes and return it beside the fleet sum:

```json
{"entries": [...], "skipped": {"held": 84, ...},
 "skippedByProduct": {"kindash": {"held": 71, "blocked": 40, ...}, "viberpit": {...}},
 "dispatchableByProduct": {"kindash": 3, "viberpit": 11},
 "generatedAt": "..."}
```

The runner's `status().products` rows gain the full per-reason map (`held`, `assigned`, `claimed`,
`live_session`, `epic_open`, `bot` join `blocked`/`untriaged`/`bounced`), and `counts` gains
`held`. The Automations detail's stat tiles cap at six (Needs-you tiles design §2), so the tile
order puts `eligible`, `held`, `blocked` first.

### 4.2 `GET /api/pm/holds` — new

`?product=<slug>&kind=owner_action|decision&limit=50&min_status=Ready&respect_dependencies=true`

```json
{"products": [{"slug": "kindash", "dispatchable": 3, "skipped": {...}, "heldCount": 71,
               "starving": true, "oldestHoldHours": 212.4}],
 "held": [{"ref": "steiner385/KinDash#412", "title": "...", "url": "...", "product": "kindash",
           "holdKind": "decision", "holdReason": "decision pending: ...", "holdBy": "session:…",
           "holdSetAt": "...", "ageHours": 31.2,
           "proposalId": "dp-…", "options": [{"key","label","detail"}], "recommendedKey": "b",
           "downstream": 7, "downstreamDispatchable": 4, "downstreamRefs": ["…#413", "…#420"],
           "releasedBy": "Accept an option"}],
 "generatedAt": "..."}
```

`held` is ranked by `rank_held` across the requested products and capped by `limit` (default 50,
max 200) with a `truncated` count. `downstreamRefs` is capped at 10 per item. Allowlisted for the
assistant as a GET; a `False` row is not needed (read-only).

### 4.3 `GET /api/pm/decisions?projectSlug=` — filter (hold-ladder §14 follow-up)

Server-side scope, same parameter name as `/pm/triage`. Each row additionally carries
`downstream` and `downstreamDispatchable` from the read model so the Inbox can rank without a
second fetch. Unfiltered behaviour is unchanged.

### 4.4 `POST /api/pm/decisions/{id}/accept` — unchanged

One-step answering is a client convenience over the existing route with `chosen_key =
recommendedKey`. No new mutation.

## 5. The `decision-escalation` automation

Registered in `server/automations/registry.py` like `DeployMonitor`, `work_class = "maintenance"`,
`engine_policy = CLAUDE_ONLY` (never spawns), oversight `Responsibility.EXECUTION`, relationships
`uses backlog-runner` (reads the same queue) and `shares_work_with pm-sync`.

- **Modes:** `off` | `notify`. There is no mode that resolves anything.
- **Config:** `tick_min` (30), `starving_max_dispatchable` (1), `hold_age_hours` (48),
  `downstream_threshold` (3), `repeat_hours` (24), `products` (empty = every product with dispatch on).
- **Loop:** primary-only, registered in `_PRIMARY_ONLY_LOOP_ATTRS`, `__init__` and `stop()` per
  the deploy-monitor wiring test pattern; `_schedule_allows_tick` gate. Every tick:
  1. If `in_quiet_hours(settings)` — defer: return without evaluating (decision 4).
  2. Load rows + proposals + policies once; `product_escalation` per product.
  3. A product **escalates** when any of: `starving`; `oldest_hold_hours >= hold_age_hours`; any
     held item with `downstream >= downstream_threshold`.
  4. Fingerprint = sha of `(slug, starving, sorted(held refs), max(downstream))`. Read the latest
     `pm.escalation.digest` audit row for the slug. Send when the fingerprint differs OR the row is
     older than `repeat_hours`. Otherwise silent.
  5. Send one `notify_event("decision_needed", ...)` per escalating product, `fanout_when_unconfigured=True`
     (this is the class an operator with no rules must still hear), `automation="decision-escalation"`,
     `repo=` the product's primary repo, `url="/roadmap?scope=inbox&product=<slug>"`.
     Title: `"KinDash: 12 decisions waiting, 3 dispatchable"`. Body: the top three held items as
     `"#412 Retry policy for offline sync — blocks 7 (recommended: b)"` lines, then
     `"9 more · oldest 8d"`.
  6. Write the `pm.escalation.digest` audit row (`slug`, `fingerprint`, `heldCount`,
     `dispatchable`, `starving`, `topRefs`) — this is both the dedup state and the record.
- **`status()`:** `counts = {held, decisions, starving_products, dispatchable}`, `products=[{slug,
  dispatchable, held, starving, oldestHoldHours}]`, level `warn` when any product is starving,
  `active` when anything is held, else `idle`. Text: `"3 products · 84 held · 2 starving"`.
- **Actionable items:** the ranked held list (limit 50). A `decision` row carries one
  `ActionSpec("accept_recommended", "Accept recommendation")` whose trigger posts to
  `/api/pm/decisions/{id}/accept` with the recommended key — the existing route, the existing
  `may_resolve` gate (a person or the assistant). An `owner_action` row carries `"Retry"` via
  `/hold/release`. Either action is one tap by a human; decision 7.
- **Watches:** `CONDITION_MAP["product"]["needs_decision"] → ("decision_needed", "automation")`
  so `watch(kind="product", condition="needs_decision")` works.
- **Web:** `decision_needed` labelled "Decision needed" in `notificationsApi.ts` (adding the
  missing `account_limited` in the same edit).

**Why an automation and not a bare loop.** The thresholds want config with help text, the digests
want an events tab, the ranked list wants the Needs-you tab and the Delivery nav badge
(`actionableCount`), and the operator wants an off switch. `DeployMonitor` already paid for all of
that shape; a bare orchestrator loop would rebuild each piece.

## 6. Assistant tools

Four changes in `server/assistant/tools.py`, each one tool → one endpoint:

| Tool | Endpoint | Params | Notes |
|---|---|---|---|
| `list_decisions` (READ, new) | `GET /api/pm/decisions?projectSlug=` | `product?`, `limit?` (1-50) | Already allowlisted. Rows projected to `id, ref, product, title, options, recommendedKey, rationale, downstream, createdAt`. `resolve_decision`'s error text now points here. |
| `list_holds` (READ, new) | `GET /api/pm/holds` | `product?`, `kind?`, `limit?` (1-50) | Projected to the §4.2 shape minus `downstreamRefs` beyond three. |
| `get_priority_queue` | unchanged | `product?` | Passes `skippedByProduct`/`dispatchableByProduct` through; `product` narrows entries and the map; `'deps'` wording fixed to `blocked`. |
| `get_product_deps` | `GET …/deps?active=true&openOnly=true&summary=true` | `summary?` (default true) | See §7. |
| `list_prs` | unchanged route | `repo?`, `state?` (default `open`), `limit?` (default 30) | `_trim_pr` projection: `repo, number, title, stage, state, ci, mergeable, reviewDecision, url, session, updatedAt`. `verbose=true` returns the full row for at most 5 PRs. |
| `list_automations` | unchanged route | `verbose?` | Default projection: `id, name, enabled, mode, status.{level,text,counts}, actionableCount, lastActiveAt`. `verbose=true` adds `config`/`configSchema`. Description corrected (no `lastError` key exists). |

Per the tool-addition recipe: `ToolSpec` rows, `base_tools` entries, `ARGS` in
`test_assistant_conformance.py`, an eval case per new tool in `cases.jsonl` (+ the pinned hash and
row count in `test_eval_assistant_fixtures.py`), the doc's tool-count table, `appActionToolKeys.json`
and `APP_ACTION_TITLES`, `_ALL_OPTIONAL_TOOLS` for the all-optional ones, and an `EvalFixture`
handler for `/api/pm/decisions` and `/api/pm/holds` so evals see real shapes.

**Result byte cap.** `wrap_untrusted` (the one envelope every handler's return passes through,
`tools.py:301-313`) gains a `_MAX_RESULT_BYTES = 48_000` guard: a result over the cap has its longest
top-level list (or the longest list one level down) halved until it fits, and the envelope gains
`"truncated": {"path": "edges", "kept": 40, "dropped": 212}`. The assistant then knows to narrow,
rather than receiving a 300 KB block the CLI may reject. Unit-tested on a synthetic payload.

## 7. `GET /api/pm/projects/{slug}/deps` — filters and summary

Additive query parameters, defaults preserving today's response for the web:

- `active=true` — only `WorkItemDep.active` rows.
- `openOnly=true` — only edges whose `src` is an open mirrored issue.
- `summary=true` — replaces `edges` and the drift lists with counts and the ten highest-degree
  blockers: `{"edges": {"total", "active", "bySource": {...}}, "drift": {"proseOnly": n,
  "unresolvedTargets": n, "missingFromProject": n, "checklistEpics": n}, "topBlockers":
  [{"ref", "blocks", "state"}]}`. `topBlockers` uses `escalation.reverse_blockers`, the one
  reverse index.

## 8. Web

- **Inbox (`features/roadmap/inbox/`):** the Decisions bucket orders by `downstream` desc then age;
  `ProposalCard`'s `DecisionRow` gets a chip `"Blocks 7"` (from the row) and `ProposalDetail`
  gets a primary **Accept recommendation** button beside the option group (the group stays; the
  button is the recommended option pre-applied — one tap, decision 7). Copy from
  `server/pm/explain.py:decision_guidance` as today.
- **Inbox scope header:** when the selected product (or any product, unfiltered) is `starving`, one
  notice line above the list: `"KinDash: 3 dispatchable, 12 held — the runner is waiting on you"`,
  from `GET /api/pm/holds` (`products[]`), reusing the hygiene-notice one-line shape (#2540).
- **`useDecisions(product?)`** passes `projectSlug` server-side; `useRoadmapWorkspace` drops its
  client-side narrowing (hold-ladder §14).
- **Disposition dialog:** a `decision` hold links to `?scope=inbox&proposal=<id>` using
  `proposalId` from `/api/pm/holds` filtered by ref (hold-ladder §14 deep-link follow-up).
- **Automations detail:** nothing new to build — the `decision-escalation` automation renders
  through the existing Overview / Needs you / Config / Events tabs; `countTiles` shows its counts.
- **Sessions page:** untouched (decision 6).

## 9. Testing

Every rule is pure and table-tested; wiring is pinned the way the deploy monitor's is.

- **`escalation.py`:** reverse index over a fixture graph (chain, diamond, cycle); `downstream`
  transitive and cycle-safe; `downstream_dispatchable` respects other skip reasons (a dependent
  that is also `assigned` does not count); `starving` truth table across dispatch on/off,
  `dispatchable` 0/1/2, held 0/1; `rank_held` ordering; `released_by` copy per kind; a `decision`
  hold with no pending proposal (un-mirrored) still lists with `proposalId: null`.
- **Queue API:** `skippedByProduct` sums equal `skipped`; runner `status().products` carries every
  reason key; the existing text assertions unchanged.
- **Holds API:** product/kind filters, limit + `truncated`, ordering, join to the proposal; a
  `False` row is not required (GET) but the policy test gains the allow row.
- **Decisions API:** `projectSlug` filter; unfiltered unchanged; `downstream` present.
- **Automation:** tick truth table (starving / aged / downstream threshold / none); fingerprint
  change sends, unchanged within `repeat_hours` is silent, unchanged past `repeat_hours` repeats;
  quiet hours defer (no audit row, no send) and the next tick outside the window sends; one event
  per product; the audit row shape; `status()` levels; actionable rows and both triggers hit the
  existing routes with the existing actor gate (`may_resolve` refuses a `session:` actor).
  Wiring test modelled on `test_deploy_monitor_wiring.py`: class in `EVENT_CLASSES`,
  `CONDITION_MAP` entry, web label, loop attr, `stop()` source, `start()` task name; the literal
  `notify_event("decision_needed"` within 200 chars for `_emitted_event_classes`.
- **Assistant:** conformance picks the new tools up; eval cases "what is blocking KinDash?" →
  `list_holds`, "list pending decisions" → `list_decisions`, "accept the recommended option on
  the top decision" → `list_decisions` then `resolve_decision`; byte-cap unit test; projection
  tests for `_trim_pr` / automations; `get_product_deps` summary shape.
- **Web:** inbox ordering and chip; Accept-recommendation calls `useAcceptDecision` with the
  recommended key; starvation notice renders from `products[]`; deep link; full vitest, typecheck,
  `lint --max-warnings 0`, `web/e2e/roadmap.spec.ts` run locally (e2e is not a required check).
- Injected clocks everywhere; no absolute date literals.

## 10. PR series

Each PR is independently reviewable and green on the four `lint-and-test` steps plus the web gates.

1. **`feat(pm): per-product skip breakdown`** — §4.1; `get_priority_queue` passthrough + `'deps'`
   fix; runner `status().products` full map. Files: `server/api/pm.py`, `orchestrator.py`
   (`_backlog_query_queue`), `backlog_runner.py` (`status`), `assistant/tools.py`, tests,
   `web/src/api/roadmap.ts` types.
2. **`feat(pm): escalation read model + GET /api/pm/holds + decisions?projectSlug`** — §3, §4.2,
   §4.3. New `server/pm/escalation.py`; policy allow row; tests.
3. **`feat(assistant): list_decisions and list_holds`** — §6 rows 1-2; eval cases; doc counts;
   `appActions`. Depends on 2.
4. **`feat(automations): decision-escalation digest + decision_needed event class`** — §5; web
   label list (+ `account_limited`); wiring tests. Depends on 2.
5. **`feat(web): impact-ranked decisions, starvation notice, one-tap accept, proposal deep link`**
   — §8. Depends on 2.
6. **`feat(assistant): readable large results`** — §6 rows 4-6, §7, the byte cap. Independent.

PRs 1 and 6 can open immediately; 3, 4, 5 fan out after 2 merges. Each PR body says `Refs` this
spec; the last one to land flips the spec status to "shipped".

## 11. Non-goals

- Auto-resolving a decision, a veto window, or acting on the recommendation without a tap.
- Deferring or digesting the *other* event classes through quiet hours (the tick-defer trick only
  works for a computed digest; a stored deferral for `needs_input` etc. is a separate design).
- Fixing the quiet-hours UTC-vs-local comparison (`push.py:197`) — filed as its own issue; this
  design uses `in_quiet_hours` as it is so the two surfaces agree.
- Cross-product dependency impact (§3).
- Surfacing decisions on the Sessions page.
- Carrying `hand_back`'s reason into the `needs_input` notification body — a one-line change worth
  doing, but it belongs to the hand-back surfacing spec (`2026-09-22`), not here.

## 12. Open questions for the operator

1. **Sessions page (decision 6).** Recommendation: leave it alone — the Roadmap badge already
   counts pending decisions, and the failure on 2026-09-30 was silence, not location. The
   alternative is a one-line "N decisions are starving KinDash" notice above the sessions list.
2. **Default thresholds (§5).** `starving_max_dispatchable=1`, `hold_age_hours=48`,
   `downstream_threshold=3`, `repeat_hours=24`, `tick_min=30`. All are config fields; the defaults
   only decide what the first digest looks like.
3. **Ship order.** Recommendation: 1 and 6 first (small, independent, immediately useful to the
   assistant), then 2, then 3/4/5 in parallel.
