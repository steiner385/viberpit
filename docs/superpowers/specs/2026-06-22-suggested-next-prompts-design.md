# Suggested next prompts — design

**Date:** 2026-06-22
**Feature branch:** `feat/suggested-next-prompts`

## Goal

After an assistant turn finishes, show **2–4 tappable "next prompt" chips** above the prompt
input, derived from the conversation context. Tapping a chip **pre-fills the editable input**
(does not auto-send). This mirrors Claude Code's context-aware prompt suggestions — optimized for
mobile, where typing is the friction point.

## What this is NOT (piggy-back is unavailable)

ccswitch does not run the interactive `claude` TUI in a PTY. It drives `claude-agent-sdk`
programmatically (`server/sessions/sdk_factory.py`) and ships structured events
(`translator.py` → `events.py` → `bridge/ws.py`). The TUI's suggestion UI is a **client-side
feature of that app** and is never present in the SDK protocol stream — so there is nothing to
inherit. We **generate** suggestions ourselves.

## Constitution compliance

- **Principle II (account = `CLAUDE_CONFIG_DIR`, per-process isolation):** generation runs as a
  *separate, throwaway* `ClaudeSDKClient` bound to one account's config dir. **No paid API** — Max
  subscription only (explicit user constraint).
- **Principle III (verify, don't assume):** ⚠️ verification item — confirm a Max-OAuth SDK query
  permits selecting a cheaper model (Haiku). If not, fall back to the session's own model.
- **Principle VI (single source of truth):** the suggestion context is built from the *existing*
  server-side transcript state, not a parallel store.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Generation source | **Max account via SDK** — never the paid API |
| Which account | **The same account that owns the session** (honest usage attribution; rotation already tracks that account's load) |
| Trigger | **Automatic on every `TurnEnd`** |
| Model | Cheapest Max-exposed model (Haiku); fall back to session model if SDK/Max disallows override (⚠️ verify) |
| Context window fed | Last 1–2 turns (user text + assistant text/result), truncated |
| Count | 2–4 short strings |
| Tap behaviour | **Pre-fill the editable input** (not auto-send) |
| Failure mode | Best-effort: any failure emits nothing, logs, never blocks/delays/fails a real turn |

## Architecture / data flow

A new server-side **`SuggestionService`**. On `TurnEnd` the orchestrator fires a
fire-and-forget task:

1. Build a compact context blob from the session's existing transcript state (last 1–2 turns,
   truncated).
2. Run a **one-shot throwaway SDK query** — a *separate* `ClaudeSDKClient`, **no `resume`**, bound
   to the session's account config dir — with a system prompt instructing JSON-only output:
   `[ "short prompt 1", "short prompt 2", ... ]`.
3. Parse + validate JSON into 2–4 short strings (drop on malformed/empty).
4. Emit a new `Suggestions` event into the driver's event queue → batched over the existing WS.

## Wire change

New session event (**must** be added to `SESSION_EVENT_TYPES` so the WS fan-out routes it):

```
suggestions { type: 'suggestions'; sessionId: string; turnId: string; items: string[] }
```

- Emitted by `SuggestionService` directly into the driver queue — **not** via `translator.py`
  (these do not originate from the session's SDK stream).
- Not replayed on re-attach (ephemeral; regenerated on next turn).

## Components & modules (each independently testable)

**Backend**
- `sessions/suggestions.py` — `SuggestionService`: builds context, runs the throwaway query via an
  injected SDK factory, parses JSON, returns `list[str]`. Pure of transport concerns.
- `sessions/events.py` — `Suggestions` dataclass + `to_wire()`.
- `sessions/driver.py` / orchestrator — on `TurnEnd`: skip if a permission request or
  `AskUserQuestion` is pending; cancel any in-flight suggestion task; launch one; on completion
  enqueue `Suggestions`. At most **one in flight per session**.
- `bridge/ws.py` — add `suggestions` to `SESSION_EVENT_TYPES`.
- `tests/fakes/fake_sdk.py` — throwaway-query path returns canned JSON.

**Frontend**
- `session/events.ts` — `EvSuggestions`, union member, `SESSION_EVENT_TYPES` entry.
- `session/reducer.ts` — state gains `latestSuggestions: string[]`; set on `suggestions`,
  **cleared** on `prompt` send and on `turnStart`.
- `session/SuggestionChips.tsx` (or fold into the prompt bar) — renders chips; tap → pre-fill the
  prompt input (via existing input-controlled state), focus it, do not send.

## Lifecycle / edge cases

- **Stale cancellation:** user sends a prompt before suggestions arrive → cancel in-flight task,
  discard result.
- **Skip when redundant:** no generation while a permission request or `AskUserQuestion` is
  pending — actionable options are already shown.
- **One in flight per session.**
- **Quota safety:** suggestion query uses tight `max_tokens`; on rate-limit/error, silently no-op.

## Testing (TDD)

- `SuggestionService`: valid JSON → list; garbage → empty; SDK raises → empty; respects truncation.
- Orchestrator: `TurnEnd` launches generation; permission/question pending → skipped; new prompt
  cancels in-flight; only one in flight at a time.
- Reducer: `suggestions` populates chips; `prompt`/`turnStart` clears them.
- WS routing: `suggestions` present in `SESSION_EVENT_TYPES`.

## Out of scope (YAGNI)

- Input autocomplete / ghost text.
- Heuristic state-driven chips (could layer later; not in v1).
- Persisting/replaying suggestions across re-attach.
