# Automation Tab Shell — Design

**Date:** 2026-06-30
**Status:** Approved (brainstorming) → ready for implementation plan
**Scope:** First of three specs. This one builds the **framework**; **PR Doctor** and the
**Backlog Runner** are separate specs that plug into it.

## Problem

ccswitch has exactly one "automation" today — the PR auto-fix policy
(`2026-06-25-pr-autofix-design.md`): a single `AppSettings.pr_autofix_mode` column
(`off`/`propose`/`auto`) that the orchestrator's `_on_pr_change()` consults. It works, but it is:

- **Invisible** — a lone dropdown buried in Settings; no surface shows what it's doing right now or
  what it has done.
- **Stateless beyond a flag** — no history of actions, no live status.
- **Un-generalizable** — the next automation (a Backlog Runner) would bolt *another* column onto
  `AppSettings` and invent its own status/history/UI from scratch. That is the "N parallel
  implementations of one concept" trap the constitution (VI) and CLAUDE.md explicitly warn against.

We want a **home for automations**: a new "Automation" tab and a backend registry that gives every
automation a uniform anatomy — on/off + mode, a live status line, a persisted activity feed, and a
config panel — plus first-class **session traceability** for automations that spawn `claude`
sessions.

## Goals

- A single **registry + contract**: automations are defined in code; the DB stores only their
  *state* (enabled, mode, config) and their *activity events*. No per-automation columns, no drift.
- A new **Automation tab** that renders any registered automation generically from the contract.
- **Bidirectional session traceability** owned by the shell, so PR Doctor and the Backlog Runner
  (and anything later) get it for free: automation → sessions it spawned, and session → the
  automation that spawned it.
- **Migrate the existing pr_autofix onto the registry** as the reference automation in the same
  change — proving the contract end-to-end and retiring `AppSettings.pr_autofix_mode` so there is no
  parallel state (constitution VI).

## Non-goals

- No new automation *behavior* — PR Doctor's richer recovery (archived sessions, merge conflicts,
  retries) and the Backlog Runner are their own specs. The migrated reference automation keeps
  exactly today's pr_autofix behavior.
- No manual "run now" / trigger actions on the card (deferred).
- No per-repo *instances* — one global instance per automation, with an optional repo allow/deny
  list in config.
- No dynamic / user-defined automations — all automations are first-party code.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Spec scoping | Three separate specs; this is the shell (built first) |
| Automation anatomy | All four: on/off + mode, live status line, activity feed, config panel |
| Where definition lives | In code (identity, modes, config schema, behavior) |
| Where state lives | DB: `automation` row (enabled/mode/config) + `automation_event` rows |
| Activity feed durability | **Persisted, capped** (rolling window per automation) |
| Scope model | **Global toggle + mode**, with a repo allow/deny filter in config |
| Tab layout | **Expandable cards** (collapsed: status + toggle; expand: feed + config) |
| Session traceability | **Shell-owned, bidirectional** (event→session link + session.origin) |
| Reference automation | Migrate existing pr_autofix → registry automation `id: "pr-doctor"` |

## Architecture

### 1. The contract — `server/automations/base.py`

Definition lives in code; the DB stores only state. Each automation is a singleton implementing:

```python
class Automation(Protocol):
    id: str                       # stable slug PK, e.g. "pr-doctor"
    name: str                     # display, "PR Doctor"
    description: str
    modes: tuple[str, ...]        # ("off","propose","auto") or ("off","on")
    config_schema: ConfigSchema   # declared fields → renders the config panel generically

    def status(self) -> AutomationStatus
        # live, recomputed (never persisted): a one-line text + level + optional counts

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: AutomationContext) -> None
        # react to a toggle / mode / config change (start/stop watching, etc.)
```

Supporting value types (also in `base.py`):

- `AutomationStatus` — `{ text: str, level: "idle"|"active"|"warn"|"error", counts?: dict }`.
- `ConfigSchema` / `ConfigField` — a small declared-field list (`key`, `label`, `type` ∈
  `bool|int|string|repo-multiselect|enum`, `default`, `help`) the frontend renders without bespoke
  forms. Validation of an incoming config PATCH is driven by this schema.

**Rejected alternatives** (recorded so they aren't re-litigated):
- *Config rows define behavior* (fully DB-driven automations): overkill for first-party features and
  makes the schema, not code, the source of truth for behavior — harder to test/review.
- *Keep bolting columns onto `AppSettings`*: the current state; the exact anti-pattern this spec exists
  to end.

### 2. The registry & `AutomationContext` — `server/automations/registry.py`

- A module-level registry holds the singleton instances (a static list registered at import/startup).
  The orchestrator owns and starts them the way it owns the pollers today.
- On startup, each registered automation's `automation` row is **upserted** (created with
  `enabled=False` + schema defaults if absent; existing state preserved). Identity (name, description,
  schema) is never written to the DB.
- Every automation is handed an **`AutomationContext`** — the only way it touches the outside world,
  so the two cross-cutting concerns (activity log, session spawning + provenance) cannot be
  re-implemented per automation:

  ```python
  class AutomationContext:
      async def emit(self, level, summary, *, target=None, session_id=None) -> None
          # append one row to this automation's activity feed (and prune to the cap)

      async def spawn_session(self, *, origin_ref: str | None = None, **create_kwargs) -> SessionRecord
          # wraps orchestrator.create_session, stamps Session.origin = self.id and
          # Session.origin_ref = origin_ref, then auto-emits a linked "spawned session" event
  ```

  `spawn_session` is unused by the migrated pr_autofix (which keeps today's owning-session behavior)
  but is the seam PR Doctor and the Backlog Runner build on. It lives in the shell so provenance is
  guaranteed, not left to each automation to remember.

### 3. Data model — `server/db/models.py`

Two new tables + two new nullable `Session` columns. All additions nullable per the dev-SQLite rule
(`init_db` backfills; `create_all` never ALTERs — see CLAUDE.md "Dev / testing").

- **`automation`** — `id` (str PK = slug), `enabled` (bool, default False), `mode` (str, nullable),
  `config` (JSON, nullable; NULL → schema defaults), `updated_at`. One row per registered automation.
- **`automation_event`** — `id` (PK), `automation_id` (FK → `automation.id`, indexed),
  `ts` (datetime, default utcnow), `level` (str: info/warn/error), `summary` (str),
  `target` (str, nullable — e.g. `"kindash#412"`), `session_id` (str, nullable FK → `sessions.id`),
  `detail` (JSON, nullable). **Capped**: after insert, prune rows for that `automation_id` beyond the
  rolling window (last N, e.g. 500, and/or older than 30 days). Prune is opportunistic on write, in
  the same transaction — no separate job.
- **`Session.origin`** — str, nullable. The automation `id` that spawned this session (NULL = created
  by a human).
- **`Session.origin_ref`** — str, nullable. What it was spawned *for* (e.g. `"kindash#412"`).

### 4. API — `server/api/automations.py`

Reads hit our own DB, so the GraphQL-for-reads norm (which is about the GitHub API) does not apply.

- `GET /api/automations`
  → `[{ id, name, description, enabled, mode, modes, status, configSchema, config }]`.
  `status` is computed live via `status()`; `configSchema` comes from code, `config` from the row.
- `GET /api/automations/{id}/events?limit=` → activity feed, newest first, each row
  `{ ts, level, summary, target?, sessionId? }`.
- `GET /api/automations/{id}/sessions` → sessions where `Session.origin == id` (live + recent), for
  the "Spawned sessions" section.
- `PATCH /api/automations/{id}` → `{ enabled?, mode?, config? }`. Validates `mode ∈ modes` and
  `config` against `config_schema` (invalid → 422), persists, then awaits `on_state_change`.

Auth: same authenticated path as the other `/api/*` routes (Tailscale-identity allowlist).

### 5. Frontend — `web/src/components/AutomationView.tsx` (+ tab wiring)

- `TabId` gains `'automation'`; route `/automation` added in `App.tsx`; the tab is added to
  `TAB_ORDER` and the tab bar in `Dashboard.tsx`, following the existing `pipeline` tab exactly
  (keyboard nav via `tabRefs`/`handleTabKeyDown` included).
- `AutomationView` renders a vertical list of **expandable cards**:
  - **Collapsed:** name · live **status line** · the **toggle** (+ a mode `<select>` when
    `modes.length > 2`).
  - **Expanded (inline):** the **activity feed** (from `/events`, with rows linking to
    `/session/:id` when `sessionId` is set), the **"Spawned sessions"** section (from `/sessions`),
    and the **config panel** rendered generically from `configSchema`.
- `web/src/api/automations.ts` client + a `useAutomations` hook (and `useAutomationEvents`), mirroring
  existing `web/src/api` + `web/src/hooks` patterns. Live status refresh reuses the existing
  visible-tab refresh idiom (like `useSessionsViewing`); a WS push is a later optimization, not
  required for v1.
- **Session provenance badge:** when `Session.origin` is set, the session card shows a small
  "Created by {automation name}" badge linking back to `/automation`. (Cheap; included. The one piece
  safe to defer if we want the shell truly minimal.)

### 6. Migrate pr_autofix onto the registry (same change)

The existing pr_autofix becomes the **reference automation** `id: "pr-doctor"`, `modes:
("off","propose","auto")`, default mode `propose`:

- Its state moves from `AppSettings.pr_autofix_mode` to the `automation` row. The orchestrator's
  `_pr_autofix_mode()` reads the `automation` row for `"pr-doctor"` instead of `AppSettings`.
- `AppSettings.pr_autofix_mode` (and its Settings dropdown / `prAutofixMode` API field) are **removed
  in the same PR** — no half-dead parallel state (CLAUDE.md "Purge replaced code"). The behavior in
  `_on_pr_change` is otherwise unchanged; it additionally calls `ctx.emit(...)` on detect / inject /
  recover so the new activity feed is populated.
- This is a behavior-preserving migration: PR Doctor's *new* capabilities are spec #2.

## Data flow

```
startup → registry registers automations → upsert automation rows (enabled/mode/config) → start them

GET /api/automations            → [contract + row state + live status()]   → tab renders cards
PATCH /api/automations/{id}     → validate vs modes/config_schema → persist → on_state_change()
automation acts (e.g. _on_pr_change for pr-doctor):
    ctx.emit("info", "detected CI fail", target="kindash#412")            → automation_event row (pruned)
    ctx.spawn_session(origin_ref="kindash#412", ...)                       → Session.origin set + linked event
GET /api/automations/{id}/events   → feed (rows deep-link to /session/:id when sessionId set)
GET /api/automations/{id}/sessions → sessions where origin == id ("Spawned sessions")
```

## Error handling

- **Unknown automation id** (GET/PATCH) → 404.
- **Invalid mode / config** (PATCH) → 422; nothing persisted, `on_state_change` not called.
- **`on_state_change` raises** → the state is already persisted; the error is logged and surfaced as
  a `level: "error"` activity event, not a 500 (the toggle "took", the reaction failed — visible in
  the feed). Mirrors the orchestrator's best-effort idioms.
- **`spawn_session` fails** → the automation gets the exception to handle; the shell emits an `error`
  event. No partial provenance: `origin` is set only on a session that was actually created.
- **Event prune** is best-effort within the insert transaction; a prune failure never blocks the emit.

## Testing (TDD)

**Backend (`server/tests/`):**
- Registry: registering an automation upserts its row (defaults on first run, state preserved on
  restart); duplicate id rejected.
- Contract validation: PATCH with a bad mode / bad config field → 422, no persistence; valid →
  persisted + `on_state_change` awaited.
- Activity feed: `ctx.emit` appends; prune holds the cap (insert N+overflow → only the window
  remains, newest kept); `GET /events` returns newest-first with `sessionId`/`target` passthrough.
- Traceability: `ctx.spawn_session` stamps `Session.origin`/`origin_ref`, auto-emits one linked
  event; `GET /sessions` returns exactly the origin-matched sessions.
- pr_autofix migration: a fake automation fixture proves the contract; `_pr_autofix_mode` now reads
  the `automation` row; existing pr_autofix behavior tests still pass against the migrated path;
  `AppSettings.pr_autofix_mode` is gone (no references remain).

**Frontend (`web/`, vitest + tsc + eslint):**
- `AutomationView` renders a card per automation; toggle + mode select PATCH and reflect new state.
- Expanded card renders feed (with session links), spawned-sessions, and a schema-driven config panel.
- Tab + route wiring: `/automation` selects the tab; keyboard nav parity with existing tabs.
- Session provenance badge renders only when `origin` is set and links to the tab.

## Out of scope / YAGNI

- Manual trigger / "run now" actions on cards.
- Per-repo automation instances.
- Cross-automation orchestration.
- Dynamic / user-defined automations.
- WS-push of live status (polling-on-visible is enough for v1).

## Open implementation notes

- Keep the pure pieces pure and independently testable: the config-schema validator, the event-prune
  helper, and `AutomationStatus`/`ConfigSchema` value types carry no I/O.
- Route the migrated pr_autofix through the *existing* `request_pr_fix` / `build_pr_fix_prompt` units
  unchanged — the migration is about *where the mode is stored* and *emitting events*, not about
  re-implementing the fix path.
- `server/automations/` is the new package boundary (base + registry + the pr-doctor reference
  automation); the orchestrator depends on the registry, not the reverse.
