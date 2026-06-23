# Generated-suggestion prefill for "Your turn" / "Ready" sessions

**Date:** 2026-06-23
**Status:** Approved (design)

## Problem

A user looking at the dashboard or a focused session expects to see a
*pre-populated next message* for any session that is waiting on them — i.e. in
`done` ("Your turn") or `idle` ("Ready") state — just like Claude Code suggests a
next prompt. Today:

- The **focused Composer** shows an empty prompt box with a green Resume/play
  button. The generated suggestion only lands in the box after an explicit click,
  and even then `play` sends it immediately rather than pre-populating an editable
  draft.
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
- **Cache + freshness.** An orchestrator-level cache keyed by `session_id →
  (signature, suggestions)`, where `signature` is derived from the transcript
  state (e.g. transcript file mtime, last-event id, or turn count — whichever is
  cheap and monotonic). `latest_suggestions()` returns:
  - the live driver's `_last_suggestions` when the session is attached (unchanged
    behavior), else
  - the detached cache **iff** its stored signature still matches the current
    transcript signature, else `[]`.
  Same transcript ⇒ same signature ⇒ no regeneration, so polling does not re-burn
  usage.
- **Trigger (throttled, deduped).** `list_sessions` (`GET /sessions`) schedules a
  fire-and-forget generation for each **`done`/`idle` detached** session whose
  cache is missing or stale. Dedup so at most one generation is in flight per
  session; bound concurrent generations with a small global semaphore. React Query
  already polls `/sessions`, so cards "fill in" within a refetch cycle. The
  endpoint returns immediately with whatever is currently cached (possibly `[]`).
- **Gating.** Generation runs only for sessions whose status is `done` or `idle`.
  Sessions that are working, waiting on permission/question, errored, complete, or
  ended are skipped.
- **Usage tradeoff (accepted by the user).** Generating for detached done/idle
  sessions spends real account usage. The signature cache limits this to once per
  transcript state, and gating to done/idle bounds the population.

### Part 2 — Frontend: Composer auto-prefill

In `web/src/components/session/Composer.tsx`:

- When the session status is `done` or `idle` and the `suggestions` prop
  transitions to non-empty, and the textarea is **empty and untouched**, set the
  text to `suggestions[0]` — focused, auto-sized, editable, and **not** auto-sent.
- Prefill at most once per suggestion batch (a ref guard). Never overwrite typed
  input. If the user clears the box after a prefill, do not re-fill. No
  `"continue"` fallback in the box (suggestion-only).
- The existing Resume/play button keeps working as-is for everything this does not
  cover.

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

**Backend (pytest, `server/tests/`):**
- A detached `done`/`idle` session: generation produces and caches suggestions;
  `/sessions` subsequently returns them in `latestSuggestions`.
- Signature change (new transcript turn) invalidates the cache and allows
  regeneration; an unchanged transcript does not regenerate.
- Non-`done`/`idle` sessions are skipped (no generation, `[]` returned).
- In-flight dedup: concurrent list calls do not launch multiple generations for
  the same session.
- The live-driver path of `latest_suggestions()` is unchanged.

**Frontend (vitest, `web/`):**
- Composer prefills `suggestions[0]` when the box is empty/untouched in `done`/
  `idle`.
- No overwrite when the user has typed.
- Box stays empty when there are no suggestions.
- No prefill outside `done`/`idle`.

## Risks / notes

- **Usage cost** is the main risk; mitigated by signature caching and strict
  state gating. If it proves too costly, a later change can switch the trigger
  from "lazy on list" to "on transition to done/idle" or add a max-age throttle.
- **In-memory cache** is lost on server restart and regenerated on demand; this is
  acceptable and avoids a schema migration.
- Generation reads the on-disk transcript; the transcript-tail extraction must
  match what the live path feeds the suggester so suggestion quality is consistent.
