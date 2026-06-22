# AI-Generated Session Titles — Design

**Date:** 2026-06-21
**Feature:** Replace the broken passive transcript-scraping for session titles with
active, AI-generated titles for auto-named sessions.

## Problem

Every auto-named session displays "New Session" forever. The current
`_maybe_apply_auto_name` hook scrapes a title out of Claude's transcript JSONL by looking
for `ai-title`/`summary`/`customTitle` keys — but Claude does not reliably write those keys,
so a title is essentially never found and the name never updates. Manually-renamed sessions
are unaffected (they pin `name_is_auto = False`).

## Goal

When an **auto-named** session finishes a turn, generate a short, human-readable title by
asking a small Claude model (Haiku) to summarize the conversation's user prompts, and adopt
it as the session `name`. Refresh on every subsequent turn while the session remains
auto-named. Never touch manually-renamed sessions.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Title source | **Active LLM generation** via the `claude-agent-sdk` one-shot `query()` (not API keys) |
| Credential / account | The **session's own account** (`CLAUDE_CONFIG_DIR` of `rec.account`) |
| Trigger | After the first turn, **refresh on every later turn** while `name_is_auto == True` |
| Generator input | **All user prompts so far**, concatenated (highest signal-to-token) |
| Model | **Haiku** (`model="haiku"`), one-shot, no tools |
| Failure handling | **Silent degrade + retry next turn**; **skip** generation when the account is `limited` |

## Architecture

Three focused units, each independently testable:

### 1. `server/sessions/title_generator.py` (new) — generation only
```python
async def generate_title(
    user_prompts: list[str],
    *,
    config_dir: str,
    cwd: str,
    model: str = "haiku",
    query_fn=None,            # injectable; defaults to claude_agent_sdk.query
) -> str | None
```
- Builds `ClaudeAgentOptions(model=model, env={**os.environ, CLAUDE_CONFIG_DIR=config_dir},
  cwd=cwd, system_prompt=<title instruction>, allowed_tools=[], max_turns=1,
  setting_sources=[])` and runs the one-shot `query()`.
- System prompt: *"Generate a concise 3–6 word title summarizing this work session.
  Output only the title — no quotes, no punctuation, no preamble."*
- User prompt: the concatenated prompts (each on its own line, capped so the request stays
  small).
- Collects text from `AssistantMessage` content blocks, sanitizes (strip quotes/newlines,
  collapse whitespace, clamp to 60 chars), returns the result or `None` on empty/error.
- Owns generation only — no transcript reading, no DB. Fully unit-testable by passing a fake
  async-generator `query_fn`.

### 2. `server/sessions/titles.py` (repurposed) — prompt extraction
Replace the unused `read_ai_title*` scrapers with:
```python
def read_user_prompts_from_file(path: str | Path) -> list[str]
```
- Reads the transcript JSONL, collects the text of `type == "user"` messages (normalizing the
  string-or-block `content` shape exactly as `replay.py` does), skipping tool-result echoes
  (they carry no text). Best-effort; returns `[]` on missing/unparseable file. Never raises.
- The old `read_ai_title` / `read_ai_title_from_file` and their tests are removed (no longer
  referenced anywhere → dead code per Principle VI).

### 3. `server/sessions/orchestrator.py` — wiring + policy
- Constructor gains an injectable `title_fn(prompts, *, account, cwd)` (default:
  `_default_title_fn`, which resolves `config_dir = compose(account)` and calls
  `generate_title`) so orchestrator tests never spawn a real CLI or touch `compose()`.
  The orchestrator deals in **accounts** (its domain); config-dir resolution is the
  default generator's concern.
- `_maybe_apply_auto_name(session_id)` becomes:
  1. Guard: `rec is not None and rec.name_is_auto` **and** `not _account_limited(rec.account)`
     (skip otherwise).
  2. Locate transcript → `read_user_prompts_from_file()`. If empty, return.
  3. `title = await self._title_fn(prompts, account=rec.account, cwd=rec.cwd)`.
  4. On a non-empty title that differs from the current name: persist to `name` (in-memory
     record + DB row). On `None`/empty/unchanged: no-op (next turn retries).
- Still fires from `_persist_status` on `status == "done"`, unchanged.

### Account-`limited` check
`UsagePoller.cached(account)` (new, read-only — no network fetch) returns the last-known
`Usage`. `_account_limited(account)` treats `session_pct >= _ROTATION_CEILING` (100) as limited;
an unknown account (no cached usage) is *not limited* (best-effort — a doomed query just degrades
silently, matching the failure policy).

## Data flow
```
turn done → _persist_status("done") → _maybe_apply_auto_name
   guard: name_is_auto && account not limited
   → read_user_prompts_from_file(transcript) → [p1, p2, ...]   (skip if [])
   → title_fn(prompts, config_dir, cwd) ──Haiku one-shot──▶ "Fix auth redirect bug"
   → persist name (record + DB)   |   None/empty/unchanged → no-op, retry next turn
```
**Concurrency / UX:** runs only in the idle window after a turn, under the session's own
account; titles change only at turn boundaries (no mid-work flicker); failures self-heal next
turn. The dashboard's existing 5 s record poll surfaces the new name (parity with the prior
auto-name path — no new broadcast needed).

## Frontend
No change. `SessionCard` already renders `session.name` and shows the "Auto-named by Claude"
hint when `nameIsAuto`. Titles simply start appearing/updating.

## Testing (TDD)
- **`title_generator`**: fake `query_fn` yielding canned assistant text → assert sanitization
  (quotes/newlines stripped, ≤60 chars); empty stream / raising `query_fn` → `None`;
  asserts `model` passed through and `CLAUDE_CONFIG_DIR` set in `env`.
- **`read_user_prompts_from_file`**: fixture JSONL (string and block content; tool-result
  user entries) → correct ordered prompt list excluding tool results; missing/garbage → `[]`.
- **`_maybe_apply_auto_name`** (fake `title_fn`): manual name (`name_is_auto=False`) → never
  calls `title_fn`; `limited` account → skipped; empty prompts → skipped; success → `name`
  updated in record + DB; `None` → name unchanged.

## Out of scope (YAGNI)
- Honoring a user-set `customTitle` from inside the Claude TUI (separate concern).
- On-demand "regenerate title" endpoint/button.
- Configurable model / length (hardcoded Haiku, 60-char clamp; easy to lift later).
