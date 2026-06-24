# Generated-suggestion prefill for "Your turn" / "Ready" sessions

**Date:** 2026-06-23
**Status:** Approved (design)

## Problem

A user looking at the dashboard or a focused session expects to see a
*pre-populated next message* for any session that is waiting on them — i.e. in
`done` ("Your turn") or `idle` ("Ready") state — just like Claude Code suggests a
next prompt. Today:

- The **focused Composer** already renders the generated suggestion as ghost text
  in the empty box (Claude-Code-style, Tab/tap to accept) — but only *after* the
  session is opened, because that is when generation runs (`kick_idle_suggestions`
  on WS attach). It works; it just has no data for never-opened sessions.
- **Session cards** for sessions the user has not opened show **no** suggestion at
  all: `orchestrator.latest_suggestions()` returns `[]` for any session without a
  live driver, and suggestions are generated only *after* attach
  (`kick_idle_suggestions`) or during a live turn. They are never persisted.

The generation engine already exists (`server/sessions/suggestions.py`:
`SuggestionService.generate()` asks a fresh SDK client for "2-4 short next
prompts" from the transcript tail). What is missing is (a) generating for
*detached* done/idle sessions and (b) surfacing the result into the prompt box and
the card Resume control.

Commit #178 (`6d73e80`) removed the on-demand generation trigger because it was a
silent no-op: a read-only attach has no live driver and no event stream to deliver
a generated suggestion back. This design deliberately avoids that dead path by
generating **without resuming the driver** and delivering over the existing
`/sessions` REST response instead of a WebSocket event.

## Scope

In scope: sessions in `done` or `idle` state only (for now).

Out of scope (for now): `DispatchBar` prefill; states beyond `done`/`idle`
(`complete`, `ended`, etc.); persisting suggestions to the database (in-memory
cache only); any `SessionCard` layout change.

## Key enabler

`SuggestionService.generate(cwd, config_dir, context)` builds its **own** one-shot
SDK client via `self._factory(cwd, config_dir)`. It does **not** require the
session's live `SessionDriver`. Therefore suggestions for a detached done/idle
session can be generated straight from its on-disk transcript, without resuming
the session — sidestepping the #178 dead path entirely.

## Design

### Part 1 — Backend: suggestions for detached done/idle sessions

The crux. Today `orchestrator.latest_suggestions(session_id)` returns `[]` when no
live driver exists.

- **Standalone generation.** Add an orchestrator path that, given a
  `SessionRecord`, reads the persisted transcript tail (reusing the existing
  `last_turn_context` helper), builds the same `User: …\n\nAssistant: …` context,
  and calls `SuggestionService.generate(cwd, config_dir, context)`. No driver
  resume.
- **Mechanism: fold the trigger into `latest_suggestions` (no new cross-layer
  wiring).** `latest_suggestions` already exists in all three layers — the core
  `SessionOrchestrator`, the in-process facade (`InProcessOrchestratorAdapter`,
  which `_maybe_await`s every call), and the daemon RPC dispatcher
  (`orchestrator_rpc._make_simple`, also `_maybe_await`). Making the **core** method
  `async` and folding the detached-generation logic into it is therefore
  transparent across the facade, the `RemoteOrchestrator` RPC client, and the
  daemon dispatcher — **zero** new facade/remote/RPC entries, which sidesteps the
  remote-shim asymmetry risk. Generation runs daemon-side (where the suggester,
  records, and transcripts live), exactly where it must.

  New async `SessionOrchestrator.latest_suggestions(session_id)`:
  1. Live driver present → return `driver._last_suggestions` (unchanged path).
  2. Else look up `rec = self._records.get(session_id)`; return `[]` if missing,
     if `self._suggester is None`, or if `rec.status not in ("done", "idle")`.
  3. Resolve the transcript path via the existing async `_resolve_transcript_path`;
     `[]` if none. Compute `signature = os.path.getmtime(path)`.
  4. If the cache has a fresh entry (`cache[sid][0] == signature`) → return its
     items (possibly `[]`).
  5. Otherwise, if no generation is already in flight for this session, schedule a
     deduped fire-and-forget `_generate_detached_suggestions(sid, rec, path,
     signature)` task and return the stale cached items (or `[]`) immediately.

- **Cache + freshness.** Orchestrator-level dict `self._detached_suggestions:
  dict[str, tuple[float, list[str]]]` (`session_id → (mtime_signature, items)`) plus
  an in-flight set `self._suggest_inflight: set[str]` for dedup. The generator
  **always** writes `(signature, items)` on completion — *including an empty
  `items`* — so a transcript that yields no suggestions (or a transient failure) is
  not re-attempted on every poll for the same transcript state. A genuinely new
  turn changes the mtime and re-triggers. Same transcript ⇒ same signature ⇒ no
  regeneration ⇒ usage is not re-burned per poll.

- **Generator** `_generate_detached_suggestions(sid, rec, path, signature)`: replay
  events from `path` (`replay.replay_events`), derive context with
  `last_turn_context(events)` → `f"User: {user}\n\nAssistant: {assistant}"`, call
  `self._suggester.generate(rec.cwd, str(compose(rec.account)), context)`, store
  `self._detached_suggestions[sid] = (signature, items)` in a `finally`-guarded
  block that also discards `sid` from `_suggest_inflight`.

- **Trigger surface.** No `list_sessions` change is needed: the endpoint already
  calls `orch.latest_suggestions(r.id)` per record, so folding the trigger into
  that method means React-Query polling of `/sessions` drives generation, and cards
  "fill in" within a refetch cycle.

- **Gating.** Generation runs only for `done`/`idle` detached sessions. Working,
  waiting (permission/question), errored, `complete`, and `ended` sessions are
  skipped.

- **Usage tradeoff (accepted by the user).** Generating for detached done/idle
  sessions spends real account usage. The mtime-signature cache (which caches even
  empty results) limits this to once per transcript state, and gating to done/idle
  bounds the population.

- **Existing test update.** `test_suggestion_cache.py::
  test_orchestrator_latest_suggestions_reads_driver` calls `latest_suggestions`
  synchronously; making the core method async requires updating that test to
  `asyncio.run(orch.latest_suggestions(...))`. It is the only synchronous caller —
  all production callers already `_maybe_await`.

### Part 2 — Frontend: Composer (no change)

The focused Composer **already** pre-populates the empty box with the generated
suggestion as Claude-Code-style **ghost text**: `Composer.tsx:63` computes `ghost =
!isWorking && text === '' && suggestions[0]` and renders it as a dimmed overlay
(`:289–313`), accepted via Tab/→ or a tap, and `SessionComposer.tsx:27` already
feeds it `latestSuggestions`. For a `done`/`idle` session, `isWorking` is false, so
the ghost shows whenever `suggestions` is non-empty.

The user confirmed ghost text is the desired "pre-populated message." Writing the
suggestion into the textarea as real committed text would be a **parallel
implementation** of the existing ghost-text path (violating the project's
eliminating-parallel-implementations rule), so **no Composer change is made**. Once
Part 1 supplies suggestions for detached sessions, the existing ghost text lights up
automatically the moment the session is opened.

### Part 3 — Cards

No new UI. With Part 1 feeding `session.latestSuggestions`, the existing card
controls render automatically:

- The Resume button uses `suggestions[0]` as its action.
- The Chevron dropdown (shown when `suggestions.length >= 2`) lists
  `suggestions.slice(1)`.

The Resume button keeps `"continue"` as its **click-action** fallback when no
suggestion exists — the button must still do something. This is the button's
behavior, not the prefilled text box, so it does not conflict with the
suggestion-only rule for the Composer.

## Testing (TDD)

Backend-only (no frontend test changes — the Composer is unchanged and ghost text
is already covered by existing Composer tests).

**Backend (pytest, `server/tests/`):**
- A detached `done`/`idle` session with a transcript: the first
  `latest_suggestions` await returns `[]` (or stale) and schedules generation; once
  the generation task completes, a subsequent await returns the cached items.
- Caching: a second await with the same transcript mtime does **not** call the
  suggester again (the cache, including an empty result, is reused).
- Signature change: bumping the transcript mtime re-triggers generation.
- Gating: a detached session whose status is not `done`/`idle` (e.g. `in_progress`,
  `complete`) returns `[]` and never calls the suggester.
- In-flight dedup: concurrent awaits for the same session launch at most one
  generation task.
- The live-driver path is unchanged: with a driver present, `latest_suggestions`
  returns `driver._last_suggestions` and never touches the detached path.
- `_suggester is None` (e.g. `CCSWITCH_SUGGESTIONS=0`) → `[]`, no generation.
- Update `test_orchestrator_latest_suggestions_reads_driver` to await the now-async
  method.

Generation in tests uses an injected fake suggester (`AsyncMock`) and a temp
transcript file — no live SDK call — mirroring `test_suggestion_cache.py` and
`test_suggestions.py`.

## Risks / notes

- **Usage cost** is the main risk; mitigated by mtime-signature caching (empty
  results cached too) and strict `done`/`idle` gating. If it proves too costly, a
  later change can move the trigger to "on transition to done/idle" or add a
  max-age throttle.
- **Caching empty results** means a transient generation failure is not retried
  until the transcript changes. Accepted to avoid per-poll usage burn; a new real
  turn (new mtime) recovers it.
- **In-memory cache** is lost on server restart and regenerated on demand; this is
  acceptable and avoids a schema migration.
- Generation reads the on-disk transcript; `last_turn_context` is the same helper
  the live idle-seed path uses, so suggestion quality stays consistent.
