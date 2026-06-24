# Archived List: Grouping, Sorting & Searching

**Date:** 2026-06-24
**Status:** Approved (design)
**Related:** `2026-06-23-session-list-ordering-design.md`, `2026-06-23-session-list-search-design.md`

## Problem

The live "Sessions" list has grouping (None / Repo / Status), sorting (Status tier /
Last active / Age / Name), and debounced backend full-text search. The archived list
has none of these — it is a flat, uncontrolled list, and is implemented twice (mobile
inline in `Dashboard.tsx`, desktop in `ArchivedManager.tsx`).

We want the archived list to have the same *capability class* (grouping, sorting,
searching), adapted to the data archived sessions actually carry.

## Constraints & key facts

- **Different data shape.** Live `Session` (`web/src/api/types.ts`) has
  `accountName`, `repo`, `status`, `updatedAt`. `ArchivedSession` has `account`,
  `cwd`, `createdAt`, `archivedAt` — **no `repo`, no `status`, no `updatedAt`**. So
  the live axes do not map 1:1: status grouping/sorting and last-active sorting are
  meaningless for archived sessions.
- **Same DB table.** Archived sessions are rows in the single `Session` table with
  `status='archived'` (`server/db/models.py`); they are removed from the in-memory
  `_records`. There is no separate `ArchivedSession` model.
- **Transcripts stay on disk.** Archiving moves the `cwd` to `archive_dir()/<id>/` but
  **never moves the transcript**. The transcript remains at
  `<account_config_dir>/projects/*/<claude_session_id>.jsonl` and is reachable via
  `orchestrator._resolve_transcript_path()` (the DB-aware resolver), not
  `transcript_path()` (active-only).
- **Pure search helpers exist.** `server/sessions/search.py` exposes `transcript_text`,
  `parse_terms`, `session_matches` — none of them know "live vs archived." Only the
  endpoint's choice of *population* and *path resolver* is live-specific.
- **Governance.** Constitution Principle VI (single source of truth / no dead code) and
  the `eliminating-parallel-implementations` working norm forbid a second, parallel
  ordering engine. The chosen approach (C) reuses one engine for both lists and pays
  down the existing archived dual-implementation debt.

## Decided dimensions (per user)

- **Grouping (archived):** None / Account / Repo
- **Sorting (archived):** Archived date / Created date / Name
- **Searching (archived):** backend full-text, mirroring live (matches transcript body
  text, not just metadata).

## Approach C (chosen): generalize the ordering/search primitives, reuse for both lists

Rejected alternatives:
- **A — adapt archived into the `Session` shape** (fake statuses, map fields): least
  code but semantically muddy; controls would surface irrelevant axes; violates the
  single-source-of-truth norm.
- **B — parallel `useArchivedOrdering` + archived controls:** clean separation but is
  exactly the parallel-implementation anti-pattern the constitution warns against.

## Design

### 1. Backend — extend search by scope, do not duplicate

Add a `scope` query param to the existing endpoint instead of a new route:

- `GET /api/sessions/search?q=…&scope=live` — default, current behavior.
- `GET /api/sessions/search?q=…&scope=archived` — new.

Refactor: extract the match loop out of `search_sessions()`
(`server/api/sessions.py`) into a helper, e.g.
`_search_population(records, resolve_path, terms) -> list[str]`, that reuses the pure
`search.py` helpers. Per-scope wiring:

| scope | population | transcript resolver |
|---|---|---|
| live (default) | `orch.list_sessions()` | `orch.transcript_path()` |
| archived | `orch.list_archived()` | `orch._resolve_transcript_path()` |

Blank/whitespace query returns all IDs **in that scope** (mirrors live's
"blank returns all active"). Unknown `scope` → 422 (or default to live; resolve in
planning — prefer explicit 422 for a bad enum). No new storage, no re-indexing.

### 2. Frontend — generalize the ordering engine

Split each ordering piece into a **shared engine** + **per-list config**:

- `web/src/session/ordering.ts`: `orderSessions(items, { groupBy, sortBy }, config)`.
  `config` supplies:
  - `groupings`: `Record<groupByKey, { label, keyOf(item) -> { key, label } }>`
  - `sorts`: `Record<sortByKey, (a, b) => number>`
  - The grouping, stable-sort, and `buildNavItems` mechanics stay in the engine and
    remain item-shape-agnostic.
- `web/src/session/useSessionOrdering.ts`: make generic over item type; signature
  `useSessionOrdering(items, config, storageNamespace)`. The namespace
  (`'live' | 'archived'`) keys localStorage prefs so the two lists don't collide.
- Two thin config modules:
  - **live config** — group: none/repo/status · sort: status/lastActive/age/name.
    This is today's behavior, relocated out of the engine into a config (behavior
    must be unchanged — covered by existing live ordering tests).
  - **archived config** — group: none/account/repo · sort: archivedDate/createdDate/name.

### 3. Frontend — share controls + list shell, inject the row renderer

- `web/src/components/SessionListControls.tsx`: render groupBy/sortBy options **from
  the config's option lists** instead of hardcoding None/Repo/Status etc. Each list
  shows only its own axes plus the search box.
- Extract the grouping / collapse / keyboard-nav **list shell** from `SessionList.tsx`
  so it is row-agnostic, with the row renderer **injected**: live injects
  `SessionCard`; archived injects its existing Review / Unarchive / Delete row.
- `web/src/session/useSessionSearch.ts`: accept a `scope` and call
  `…&scope=archived` for the archived list.

Routing the archived data through these shared pieces (hook + controls + shell)
**collapses the archived dual implementation** — mobile (`Dashboard.tsx`) and desktop
(`ArchivedManager.tsx`) both use one path, exactly as the live list already does
across both layouts.

### 4. Data — deriving `repo` for archived

`account` is already present, so group-by-account is free. For group-by-repo, derive a
repo label from `cwd` via a **single shared helper**. Resolve the source of truth in
planning: if live's repo-label formatting already operates on a path
(`session/ordering.ts` repo formatting), route the archived derivation through it;
otherwise add one helper used by both. Do **not** add a second, archived-only repo
formatter.

### 5. Testing

- **Backend** (`server/tests/test_session_search_api.py`): add `scope=archived`
  cases — archived session matched by name; archived session matched by transcript
  body; archived rows excluded from default/`scope=live`; live rows excluded from
  `scope=archived`; blank query returns all in scope. Reuse `test_archive.py` fixtures
  for creating archived rows with transcripts on disk.
- **Frontend:**
  - Unit-test both ordering configs through the shared `orderSessions` engine (group
    keys, sort order, stability), including archived account/repo grouping and
    archived-date/created-date/name sorting.
  - Component test: archived list renders the controls strip and correctly
    filters (search) / groups / sorts. Both mobile and desktop are covered because
    they now share one path.
  - Live ordering tests must remain green unchanged (regression guard for the
    engine extraction).

## Out of scope (YAGNI)

- Status grouping/sorting for archived (no live status to order by).
- Saved searches / search history.
- Server-side pagination of archived search results.
- Backfilling `repo`/`updatedAt` onto archived rows in the DB.

## Risks

- **Engine extraction regressing the live list.** Mitigated by keeping the live config
  behavior-identical and relying on the existing live ordering test suite as a guard.
- **`cwd`→repo derivation accuracy.** Archived `cwd` is the original worktree path;
  the derived repo label may be approximate. Acceptable for a grouping label; refine
  only if it proves confusing.
- **localStorage namespace collision.** Existing live prefs use fixed keys; the
  namespace parameter must preserve the live keys exactly to avoid resetting users'
  current live grouping/sort prefs.
