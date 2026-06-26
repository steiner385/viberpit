# Session-list live summaries — design

**Date:** 2026-06-25
**Status:** Approved (brainstorming → spec)

## Problem

The sessions list shows each session's name, status pill, and "active X ago", but
nothing tells the user *what is actually happening* in a session right now. The user
wants a short, human-readable summary per session so they can glance at the list and
understand each session's current activity — **without** burning Claude Max quota when
nobody is looking.

## Decisions (from brainstorming)

- **Summary source:** LLM-generated status — a fresh one-shot Haiku summary of the latest
  turn, e.g. "Refactoring the auth middleware and running the test suite." (Not the
  existing static 3–6 word *name/title*, which is a different, one-time concept.)
- **Viewing signal:** regenerate only when the client is *actually looking* — i.e.
  `activeTab === 'sessions'` AND the browser tab is visible (Page Visibility API).
- **Throttle:** at most once per 60s per session.
- **Scope:** only recently-active sessions — naturally enforced by keying the cache on the
  transcript's mtime; a dormant session's transcript doesn't change, so it is never
  re-summarized.
- **Display:** one truncated, muted line directly beneath the session name (mobile +
  desktop), hidden when empty.

## Architecture

Mirror the existing `latest_suggestions` machinery, which already implements lazy
background generation, an mtime-keyed cache, an in-flight guard, and a non-blocking list
GET. Summaries add two gates on top: a **60s throttle** and a **viewer-present** check.

Like suggestions, summaries are **not** stored on `SessionRecord` and **not** persisted to
the DB. They live in a side cache and are injected into the HTTP list wire format only.
This avoids a DB migration and avoids touching the RPC `to_dict` path. (Summaries are
ephemeral "what's happening now" — regenerating after a restart, when next viewed, is
acceptable.)

### Components

1. **`server/sessions/summaries.py` — `SummaryService`**
   A near-copy of `SuggestionService`. Builds a one-shot, resume-less SDK client bound to
   the session's own account `CLAUDE_CONFIG_DIR` via the existing suggestion/summary
   factory, queries Haiku with a status-summary prompt fed `last_turn_context()` output,
   and returns a single short sentence (clamped to a max length, e.g. ≤100 chars) or `""`.
   Best-effort: returns `""` on empty context or any exception; never raises.

2. **Orchestrator cache + `latest_summary`**
   - `self._summaries: dict[str, tuple[float, float, str]]` — keyed by session_id,
     value `(transcript_signature, generated_at_epoch, text)`.
   - `self._summary_inflight: set[str]` — in-flight guard, mirroring `_suggest_inflight`.
   - `async def latest_summary(self, session_id, *, viewing: bool) -> str`:
     - Resolve the transcript path + mtime signature (best-effort; `""` on failure).
     - Serve the cached text on a signature hit.
     - **Schedule background regeneration only when the regenerate predicate is true**
       (see below). On a miss/stale/throttled state, return the cached text if any, else
       `""` — never block.
   - `async def _generate_summary(self, session_id, rec, path, signature)`: replay events,
     build `last_turn_context`, call `SummaryService.generate(...)`, cache
     `(signature, now, text)` even on empty, always clear the in-flight flag — exactly the
     `_generate_detached_suggestions` shape.

   **Regenerate predicate** (the one piece of genuinely new business logic — implemented as
   a small, separately-tested helper):
   ```
   should_regenerate(viewing, cached, signature, now, status) -> bool
   ```
   Returns true iff: `viewing` is true AND status is not a terminal/ended state AND
   (no cache OR cached signature != current signature) AND
   (no cache OR now - cached_generated_at >= 60).
   When `viewing` is false, it always returns false → zero quota spent when nobody looks.

3. **`GET /api/sessions` — viewing flag**
   - Parse `?viewing=1` (truthy → `viewing=True`).
   - Pass `viewing` into `latest_summary(session_id, viewing=...)`, resolved in parallel
     alongside repos/suggestions/prs (same pattern as the existing parallel resolution).
   - Thread the result into `_record_to_dict(record, ..., summary=summary)` as a new wire
     field `summary` (string, omitted/empty when none).

4. **Frontend — viewing detection**
   - `usePageVisible()` hook: subscribes to `document` `visibilitychange`, returns the
     current `document.visibilityState === 'visible'`.
   - `useSessions(viewing: boolean)`: when `viewing` is true, the query key + fetch include
     `?viewing=1`; otherwise the param is omitted. The 5s `refetchInterval` continues
     regardless (status pills stay live); only the flag toggles, so summaries cost quota
     strictly while the list is on-screen. `viewing` is computed by the Dashboard as
     `activeTab === 'sessions' && usePageVisible()`.

5. **Frontend — `SessionCard`**
   - Render `session.summary` as a single muted, `truncate` line directly beneath the name
     row. Shown on mobile + desktop. When `summary` is empty/absent, render nothing (no
     reserved blank space).

### Data flow

```
SessionCard mounted on 'sessions' tab + tab visible
  → useSessions(viewing=true) polls GET /api/sessions?viewing=1 every 5s
    → handler: latest_summary(id, viewing=true) per session (parallel)
      → should_regenerate? (viewer + mtime changed + >60s + active)
          yes → schedule bg _generate_summary (Haiku under session account); return cached/""
          no  → return cached text (or "")
    → _record_to_dict(..., summary=text) → wire field `summary`
  → next poll (≤5s later) serves the freshly-cached summary → card renders the line
```

## Error handling

- `SummaryService.generate` catches all exceptions (incl. `AuthError`/quota), logs a
  warning, returns `""`. Consistent with `SuggestionService`.
- `_generate_summary` caches `(signature, now, "")` on failure so the same transcript
  state is not retried until it changes or 60s passes; always clears the in-flight flag.
- `latest_summary` never raises and never blocks the list GET; on any resolution error it
  returns the cached value or `""`.
- Frontend: empty/absent `summary` → no line rendered.

## Cost / quota note (constitution III, IV)

Summaries spend each *viewed, active* account's Max quota — up to N Haiku calls/min for N
active sessions while the sessions tab is open and visible. The three-way gate
(viewer-present + mtime-changed + 60s throttle) is the mitigation, and dormant sessions
cost nothing. A settings toggle to disable summaries entirely is deferred as YAGNI.

## Testing

**Backend**
- `should_regenerate`: false when `viewing=False`; false when signature unchanged; false
  when <60s since last gen; false for terminal/ended status; true when viewer+changed+>60s.
- `SummaryService`: empty context → `""`; happy path parses/clamps a sentence; exception →
  `""`.
- Orchestrator `latest_summary`: cache hit serves text without scheduling; miss with
  `viewing=True` schedules generation; `viewing=False` never schedules; in-flight guard
  prevents double-scheduling.
- `_record_to_dict` includes `summary`; `GET /api/sessions?viewing=1` threads the flag
  (assert on a real endpoint response, not just a unit of `_record_to_dict`).

**Frontend**
- `usePageVisible` reflects `visibilitychange`.
- `useSessions` includes `?viewing=1` only when `viewing` true; omits otherwise.
- `SessionCard` renders the summary line when present, truncates long text, omits the line
  when empty.
- Gates: full `vitest run`, `tsc -b --force`, `eslint`.

## Out of scope / deferred

- Persisting summaries to the DB (ephemeral by design).
- A settings toggle to disable summaries (YAGNI).
- Summaries for the RPC `to_dict` path (HTTP list only, matching suggestions).
- Backfilling summaries for archived sessions.

## Learning-mode contribution

During implementation, the `should_regenerate` predicate (and optionally the Haiku summary
prompt wording) will be handed to the user to write — it is the decision with real
trade-offs (freshness vs. quota) and where domain judgment matters most.
