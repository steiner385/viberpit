# Automation Tab Shell — Design

**Date:** 2026-06-30
**Status:** Approved (brainstorming) → ready for implementation plan
**Roadmap:** Establishes the **Automation tab** framework; consumers are the two follow-on specs
(**PR Doctor** = CI/CD recovery, **Backlog Runner** = autonomous issue implementation).
**Scope:** First of three specs. This one builds the **framework** (+ a behavior-preserving migration
of the existing pr_autofix onto it); PR Doctor and the Backlog Runner are separate specs that plug in.

## Problem

ccswitch has exactly one "automation" today — the PR auto-fix policy
(`2026-06-25-pr-autofix-design.md`): a single `AppSettings.pr_autofix_mode` column
(`off`/`propose`/`auto`) that the orchestrator's `_on_pr_change()` consults. It works, but it is:

- **Invisible** — a lone dropdown buried in Settings; no surface shows what it's doing right now or
  what it has done.
- **Stateless beyond a flag** — no history of actions, no live status, and the loop-guard state
  (`_pr_fix_attempts`) lives in orchestrator instance dicts that vanish on restart.
- **Un-generalizable** — the next automation (a Backlog Runner) would bolt *another* column onto
  `AppSettings` and invent its own status/history/per-target-state/UI from scratch. That is the "N
  parallel implementations of one concept" trap the constitution (VI) and CLAUDE.md warn against.

We want a **home for automations**: a new "Automation" tab and a backend registry that gives every
automation a uniform anatomy — on/off + mode, a live status line, a persisted (capped) activity
feed, a config panel, **and a durable per-target state store** — plus first-class **session
traceability** for automations that spawn `claude` sessions.

## Goals

- A single **registry + contract**: automations are defined in code; the DB stores only their
  *state* (enabled, mode, config), their *activity events*, and their *per-target state*. No
  per-automation columns, no drift.
- A new **Automation tab** that renders any registered automation generically from the contract.
- **Durable per-target state** owned by the shell (retry counters, "gave up on #412", the autofix
  loop-guard) so recovery loops have one canonical home and `status()` counts are not scraped from a
  pruned feed.
- **Bidirectional session traceability** owned by the shell, with **spawn de-duplication**, so PR
  Doctor and the Backlog Runner cannot double-spawn and get traceability for free.
- **Migrate the existing pr_autofix onto the registry** as the reference automation in the same
  change — proving the contract end-to-end and retiring `AppSettings.pr_autofix_mode` so there is no
  parallel state (constitution VI).

## Non-goals

- No new automation *behavior* — PR Doctor's richer recovery (archived sessions, merge conflicts,
  retries, escalation) and the Backlog Runner are their own specs. The migrated reference automation
  keeps exactly today's pr_autofix behavior (plus emitting events + using the shared per-target store).
- No manual "run now" / trigger actions in v1 (the contract is kept forward-compatible with one —
  see Open notes — but no UI/endpoint ships).
- No per-repo *instances* — one global instance per automation, with an optional repo allow/deny
  list in config (the v1 consumer of `repo-multiselect`).
- No dynamic / user-defined automations — all automations are first-party code.
- No WebSocket push of live status — polling-on-visible (like `useSessionsViewing`) is enough; the
  persisted activity feed is the durable record, so a closed tab misses nothing.

## Decisions (from brainstorming + spec review)

| Question | Decision |
|---|---|
| Spec scoping | Three separate specs; this is the shell (built first) |
| Automation anatomy | on/off + mode, live status line, activity feed, config panel |
| Where definition lives | In code (identity, modes, config schema, behavior) |
| Where state lives | DB: `automation` row + `automation_event` rows + `automation_target_state` rows |
| Activity feed durability | Persisted, **capped at 500 rows per automation** (count-based, pruned on write) |
| Per-target state | **Shell-owned** `automation_target_state` table + `ctx` accessors (review Finding 1) |
| Scope model | Global toggle + mode, with a repo allow/deny filter in config (`repo-multiselect`) |
| Tab layout | Expandable cards (collapsed: status + toggle; expand: feed + spawned-sessions + config) |
| Session traceability | Shell-owned, bidirectional, **with spawn de-dup** (review Finding 4) |
| Reference automation | Migrate existing pr_autofix → registry automation `id: "pr-doctor"` (permanent slug) |
| pr_autofix migration ordering | The **final** implementation task; framework lands first, independently green |

## Architecture

### 1. The contract — `server/automations/base.py`

Definition lives in code; the DB stores only state. Each automation is a singleton implementing:

```python
class Automation(Protocol):
    id: str                       # stable slug PK, e.g. "pr-doctor" (permanent)
    name: str                     # display, "PR Doctor"
    description: str
    modes: tuple[str, ...]        # e.g. tuple(VALID_AUTOFIX_MODES) — never a re-typed literal
    config_schema: ConfigSchema   # declared fields → renders the config panel generically

    def status(self, ctx) -> AutomationStatus
        # live, recomputed but CHEAP: derived from per-target state (ctx), never a live GitHub call

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None
        # react to a toggle / mode / config change (start/stop watching, etc.)
```

Supporting value types (also in `base.py`):

- `AutomationStatus` — `{ text: str, level: "idle"|"active"|"warn"|"error", counts?: dict }`.
  **`counts` is derived from per-target state** (§2 `automation_target_state`), never reconstructed
  from the pruned event feed — so "gave up on N PRs" cannot silently vanish as the feed rolls.
- `ConfigSchema` / `ConfigField` — a declared-field list (`key`, `label`, `type` ∈
  `bool|int|string|enum|repo-multiselect`, `default`, `help`) the frontend renders without bespoke
  forms. An incoming config PATCH is validated against this schema. The field-type set is **closed
  for v1** — no other types ship — so the generic renderer can't quietly grow.

**Rejected alternatives** (recorded so they aren't re-litigated):
- *Config rows define behavior* (fully DB-driven automations): overkill for first-party features.
- *Keep bolting columns onto `AppSettings`*: the current state; the exact anti-pattern this spec ends.

### 2. The registry, `AutomationContext`, per-target state — `server/automations/registry.py`

- A module-level registry holds the singleton instances (a static list registered at import/startup).
  The orchestrator owns and starts them the way it owns the pollers today; the orchestrator depends on
  the registry, never the reverse.
- On startup, each registered automation's `automation` row is **upserted** (created with
  `enabled=False` + schema defaults if absent; existing state preserved). Identity (name, description,
  schema) is never written to the DB.
- Every automation is handed an **`AutomationContext`** — the only way it touches the outside world,
  so the cross-cutting concerns (activity log, per-target state, session spawning + provenance + de-dup)
  cannot be re-implemented per automation:

  ```python
  class AutomationContext:
      async def emit(self, level, summary, *, kind=None, target=None,
                     session_id=None, detail=None) -> None
          # append one row to the activity feed (best-effort; NEVER propagates into the
          # automation's control flow), then prune that automation's feed to the 500-row cap.

      async def get_target_state(self, target) -> dict        # {} if none
      async def set_target_state(self, target, state) -> None # upsert (automation_id, target)
          # the canonical per-target store: retry counts, last-failure-class, last-action ts,
          # terminal "gave up" flags. The migrated autofix loop-guard moves here from _pr_fix_attempts.

      async def spawn_session(self, *, origin_ref, dedupe_key=None, **create_kwargs) -> SessionRecord
          # find-or-create, ATOMIC: if a NON-terminal session with the same (origin, dedupe_key)
          # (dedupe_key defaults to origin_ref) already exists, return it and emit a "skipped
          # duplicate" event instead of creating a second. On create: stamp Session.origin = self.id
          # and Session.origin_ref = origin_ref, then auto-emit a linked "spawned session" event.
  ```

  `spawn_session`/`get_target_state` are unused by the migrated pr_autofix's *fix injection* path
  (which keeps today's owning-session behavior), but the per-target store IS used to host the
  migrated loop-guard, and both seams are what PR Doctor and the Backlog Runner build on. They live in
  the shell so provenance, de-dup, and per-target state are guaranteed, not left to each automation.

### 3. Data model — `server/db/models.py`

Three new tables + two new nullable `Session` columns.

- **`automation`** — `id` (str PK = slug), `enabled` (bool, default False), `mode` (str, nullable),
  `config` (JSON, nullable; NULL → schema defaults), `last_active_at` (datetime, nullable — heartbeat
  the loop bumps; lets the UI show "stalled"), `updated_at`. One row per registered automation.
- **`automation_event`** — `id` (PK), `automation_id` (FK → `automation.id`, indexed),
  `ts` (datetime, default utcnow, indexed), `level` (str: info/warn/error), `kind` (str, nullable —
  a queryable event code: `detected`/`attempted`/`recovered`/`escalated`/`gave_up`/`spawned`/
  `skipped_duplicate`/…), `summary` (str), `target` (str, nullable — display-only, e.g. `"kindash#412"`),
  `session_id` (str, nullable FK → `sessions.id`, **`ON DELETE SET NULL`**), `detail` (JSON, nullable).
  **Capped at 500 rows per `automation_id`**, pruned opportunistically on insert in the same
  transaction (best-effort: a prune failure never blocks the emit).
- **`automation_target_state`** — composite PK `(automation_id, target)`, `state` (JSON),
  `updated_at`. The durable per-target store; rows are deleted by the automation when a target reaches
  terminal rest (e.g. PR merged) so it doesn't grow unbounded.
- **`Session.origin`** — str, nullable. The automation `id` that spawned this session (NULL = human).
- **`Session.origin_ref`** — str, nullable. What it was spawned *for* (e.g. `"kindash#412"`).

**Schema-creation paths:** the two new nullable `Session` columns rely on the dev-SQLite `init_db`
backfill (`create_all` never ALTERs — see CLAUDE.md). The three new **tables** are created by
`create_all` on dev SQLite. **Prod/Postgres gets a real Alembic migration** for the three tables +
two columns (per CLAUDE.md, new tables / NOT-NULL need a migration outside dev SQLite).

### 4. API — `server/api/automations.py`

Reads hit our own DB, so the GraphQL-for-reads norm (about the GitHub API) does not apply.

- `GET /api/automations`
  → `[{ id, name, description, enabled, mode, modes, status, configSchema, config, lastActiveAt }]`.
  `status` is computed live via the cheap `status(ctx)`; `configSchema` from code, `config` from the row.
- `GET /api/automations/{id}/events?limit=&target=&kind=` → activity feed, newest first, each row
  `{ ts, level, kind?, summary, target?, sessionId? }`. `target`/`kind` filters let the UI (and tests)
  reconstruct one PR's full recovery history without scanning a capped feed client-side.
- `GET /api/automations/{id}/sessions` → sessions where `Session.origin == id` (live + recent).
- `PATCH /api/automations/{id}` → `{ enabled?, mode?, config? }`. Validates `mode ∈ modes` and
  `config` against `config_schema` (invalid → 422), persists, then awaits `on_state_change`.

Auth: same authenticated path as the other `/api/*` routes (Tailscale-identity allowlist).

### 5. Frontend — `web/src/components/AutomationView.tsx` (+ tab wiring)

- `TabId` gains `'automation'`; route `/automation` in `App.tsx`; the tab is added to `TAB_ORDER` and
  the tab bar in `Dashboard.tsx`, following the existing `pipeline` tab exactly (keyboard nav via
  `tabRefs`/`handleTabKeyDown`).
- `AutomationView` renders a vertical list of **expandable cards**:
  - **Collapsed:** name · live **status line** (with a "stalled" hint if `lastActiveAt` is stale) ·
    the **toggle** (+ a mode `<select>` when `modes.length > 2`).
  - **Expanded (inline):** the **activity feed** (from `/events`, rows linking to `/session/:id` when
    `sessionId` is set), the **"Spawned sessions"** section (from `/sessions`), and the **config
    panel** rendered generically from `configSchema` — including the `repo-multiselect` widget, whose
    repo source reuses the existing repos API (`web/src/api`).
- `web/src/api/automations.ts` client + `useAutomations`/`useAutomationEvents` hooks, mirroring the
  existing `web/src/api` + `web/src/hooks` patterns. Live-status refresh routes through the existing
  visible-tab refresh hook (`useSessionsViewing`) — named explicitly in the plan, not re-implemented.
- **Session provenance badge:** when `Session.origin` is set, the session card shows a small
  "Created by {automation name}" badge linking back to `/automation`.

### 6. Migrate pr_autofix onto the registry (final task, behavior-preserving)

The existing pr_autofix becomes the **reference automation** `id: "pr-doctor"` (the permanent slug —
spec #2 "PR Doctor" only *adds behavior* under this id, it is not a claim PR Doctor is complete),
`modes: tuple(VALID_AUTOFIX_MODES)`, default mode `propose`:

- Its mode moves from `AppSettings.pr_autofix_mode` to the `automation` row;
  `_pr_autofix_mode()` reads the `automation` row for `"pr-doctor"` (NULL/unknown → `propose`).
- Its loop-guard moves from the in-memory `_pr_fix_attempts` dict to `ctx.get/set_target_state` keyed
  by `target = f"{repo}#{pr.number}"` — now durable across restart. The **invariants are preserved
  and explicitly tested** (see Testing): one attempt per failure event; re-arm **only** on
  `passing`/`merged`, never on transient `pending`/`none`; the mark-before-await guard re-arms (does
  not consume the attempt) when the driver is draining/no-live-driver.
- `_on_pr_change` additionally calls `ctx.emit(...)` (`kind` = `detected`/`attempted`/`recovered`)
  so the activity feed is populated. The fix path itself routes through the existing
  `request_pr_fix` / `build_pr_fix_prompt` units **unchanged**.
- pr-doctor's `config` carries the v1 repo allow/deny filter (`repo-multiselect`) — the concrete
  consumer that exercises the field type; default = all repos.
- `AppSettings.pr_autofix_mode`, its Settings dropdown, and the `prAutofixMode` API field are
  **removed in this same task** — no half-dead parallel state (CLAUDE.md "Purge replaced code").

## Data flow

```
startup → registry registers automations → upsert automation rows → start them

GET /api/automations            → [contract + row state + cheap status(ctx)]      → tab renders cards
PATCH /api/automations/{id}     → validate vs modes/config_schema → persist → on_state_change()
automation acts (e.g. _on_pr_change for pr-doctor):
    st = await ctx.get_target_state("kindash#412")     → loop-guard / retry state (durable)
    ctx.emit("info", "detected CI fail", kind="detected", target="kindash#412")  → event (pruned 500)
    (PR Doctor #2) ctx.spawn_session(origin_ref="kindash#412", dedupe_key=...)    → find-or-create
    await ctx.set_target_state("kindash#412", st)
GET /events?target=kindash#412  → that PR's full recovery history (kind-filterable)
GET /sessions                   → sessions where origin == id  ("Spawned sessions")
```

## Error handling

- **Unknown automation id** (GET/PATCH) → 404.
- **Invalid mode / config** (PATCH) → 422; nothing persisted, `on_state_change` not called.
- **`on_state_change` raises** → state already persisted; logged + surfaced as a `level:"error"`
  event, not a 500 (the toggle took, the reaction failed — visible in the feed).
- **`emit` fails** → swallowed and logged (mirrors `pr_poller` L129); never propagates into the
  automation's control flow.
- **`spawn_session`:** de-dup is atomic — a concurrent second call for the same `(origin, dedupe_key)`
  returns the existing non-terminal session (+ `skipped_duplicate` event); `origin`/`origin_ref` are
  set only on a session actually created. Underlying create failure → exception to the automation +
  an `error` event; no partial provenance.
- **Event prune** is best-effort within the insert transaction; a prune failure never blocks the emit.

## Acceptance criteria (verifiable "done")

1. `grep -r pr_autofix_mode server/ web/` → **0 hits** (the constitution-VI "no parallel state"
   promise, made mechanical).
2. The existing pr_autofix behavior tests pass **unmodified** against the registry path.
3. Fresh-DB startup upserts the `pr-doctor` row; a restart **preserves** a toggled enabled/mode (upsert
   idempotency).
4. Toggling pr-doctor's mode in the new tab changes live `_on_pr_change` behavior end-to-end.
5. Loop-guard invariants hold on the durable store (no second attempt within a failure event; no
   re-arm on `pending`; re-arm on `passing`/`merged`).
6. `spawn_session` never creates two non-terminal sessions for the same `(origin, dedupe_key)`.
7. Server `pytest`, web `vitest`/`tsc`/`eslint` all green (constitution V quality gates).

## Testing (TDD)

**Backend (`server/tests/`):**
- Registry: registering upserts the row (defaults first run, state preserved on restart); duplicate id
  rejected.
- Contract validation: PATCH bad mode / bad config → 422, no persistence; valid → persisted +
  `on_state_change` awaited.
- Activity feed: `emit` appends with `kind`; prune holds the 500 cap (newest kept); `GET /events`
  honors `target=`/`kind=` filters; an `emit` that raises does not break the caller.
- Per-target state: `get/set_target_state` round-trips per `(automation_id, target)`; `counts` derive
  from it (not from the feed).
- Traceability + de-dup: `spawn_session` stamps `origin`/`origin_ref`, auto-emits one linked event;
  a second call for the same `(origin, dedupe_key)` returns the existing session + `skipped_duplicate`;
  `GET /sessions` returns exactly the origin-matched sessions; FK `ON DELETE SET NULL` verified.
- pr_autofix migration regressions: `_pr_autofix_mode` reads the row; **one attempt per failure
  event**; **no re-arm on `pending`/`none`**, re-arm on `passing`/`merged`; **draining/no-driver
  re-arms (does not consume the attempt)**; `modes` sourced from `VALID_AUTOFIX_MODES`;
  `AppSettings.pr_autofix_mode` gone.

**Frontend (`web/`, vitest + tsc + eslint):**
- `AutomationView` renders a card per automation; toggle + mode select PATCH and reflect new state.
- Expanded card renders feed (with session links), spawned-sessions, and a schema-driven config panel
  including `repo-multiselect`.
- Tab + route wiring: `/automation` selects the tab; keyboard nav parity with existing tabs.
- Session provenance badge renders only when `origin` is set and links to the tab.

## Out of scope / YAGNI

- Manual trigger / "run now" actions (contract kept forward-compatible; no UI/endpoint in v1).
- Per-repo automation instances.
- Cross-automation orchestration.
- Dynamic / user-defined automations.
- WS-push of live status (polling-on-visible is enough for v1).

## Open implementation notes

- Keep pure pieces pure and independently testable: the config-schema validator, the event-prune
  helper, and `AutomationStatus`/`ConfigSchema` value types carry no I/O.
- **Reserve the poller seam for spec #2:** PR Doctor will need to act on PRs whose owning session is
  archived, but `PrStatusPoller`'s pairs-provider (`pr_poller.py` `set_pairs_provider`) is driven by
  *live* sessions' branches — so an archived session's PR drops out of the poll set and becomes
  invisible to detection. The shell does **not** fix this, but the plan should keep the pairs-provider
  extendable (an automation can register extra `(repo, branch)` pairs) so spec #2 isn't blocked by a
  structurally-blind poller.
- **Forward-compat for "run now":** ensure the `Automation` contract + PATCH/endpoint shape can grow a
  trigger action later without a schema break (no UI now).
- `server/automations/` is the new package boundary (base + registry + the pr-doctor reference
  automation); route the migrated fix through the existing `request_pr_fix`/`build_pr_fix_prompt`
  units — the migration changes *where mode/loop-state live* and *emits events*, not the fix path.
