# Fix pattern: an SDK-stream consumer reads "a frame arrived" as "a turn is running"

**Date:** 2026-09-04
**Origin:** 5-whys of "every new session sits at *Working…* with my prompt shown as *1 queued*
and *No messages yet*" (production sessions `03faa864`, `24773155`, `e0787f07` on rocky,
2026-09-03 23:08–23:11 EDT — the first three sessions launched after #1009 deployed at
23:03). One instance is fixed here; the sibling sweep below is the one `CLAUDE.md`'s
"audit recurring fix patterns" rule requires.

## Symptom

- A freshly launched session shows **Working…** immediately, the prompt typed at launch
  appears in the queued-prompt strip (`1 queued`), the conversation stays at
  **No messages yet**, and the context readout stays `—`. It never recovers.
- The DB row says `status=idle`, `claude_session_id=NULL`, `last_turn_at=NULL`: the CLI
  never received a prompt (the `init` frame only arrives with the first one).
- Nothing in the logs — the listener's `driver.autonomous_turn` INFO line is the only trace,
  and rocky's daemon does not persist INFO anywhere (its journal is WARNING+ only).

## 5 whys

1. The prompt is queued because `send_prompt` saw `_turn_active` set.
2. `_turn_active` was set on a fresh session because the idle listener (#1009) opened an
   "autonomous turn" on the first SDK message it saw.
3. A message arrived before any prompt because the CLI emits `hook_started` /
   `hook_response` / `hook_progress` frames for **SessionStart hooks** ~1.5 s after
   connect — the account config enables 23 plugins (superpowers among them), and the SDK
   loads `user`+`project` settings by default.
4. The listener treated them as a turn because it assumed *any* frame while idle is the
   `<task-notification>` continuation it was written for, and waited for a
   `ResultMessage` that bookkeeping frames never carry.
5. It shipped because the fake SDK client was **silent at connect**, the new tests only
   injected whole turns, and the real-binary check used a config without plugins — the
   pre-prompt stream shape was never modeled (Principle III: verify against the real
   binary, with the real config).

## Anti-pattern

```python
msg = await self._source.next()
if not self._turn_active:
    await self._open_autonomous_turn()     # ← any frame == a turn
```

Turn state inferred from *the existence* of a frame rather than from *which* frame. The
stream is not quiet between turns: `HookEventMessage`, `hook_progress`, and the
`task_started` / `task_progress` / `task_notification` family are all `SystemMessage`s
with no result behind them.

## Fix shape

- One source of truth for "which frames belong to a turn":
  `server/sessions/sdk_contract.py::TURN_MESSAGES` / `is_turn_message()` —
  `UserMessage`, `AssistantMessage`, `StreamEvent`, `ResultMessage`. A CLI-initiated turn
  announces itself with the user message the CLI injects; that frame opens it.
- The listener opens a turn only on `is_turn_message(msg)`; bookkeeping frames are still
  fed through `_handle_msg` (the translator ignores them) so the cursor never stalls.
- The fake SDK client now delivers `session_start_hook_frames()` on `__aenter__` by default
  and uses the same predicate for its own mid-turn tracking, so **every** driver test runs
  against the real pre-prompt stream shape. `startup_frames=[]` opts out.

## Where to look (sibling sweep, 2026-09-04)

| Consumer | Verdict |
|---|---|
| `driver._idle_listen` | **fixed here** |
| `driver._drain` (prompt pump) | safe — runs only after `query()`, segments on `ResultMessage`; bookkeeping frames pass through |
| `sdk_contract.one_turn` → `summaries.py`, `suggestions.py` | safe — same shape as the pump: always after a `query()` |
| `FakeSDKClient._turn_open` | had the same assumption (any non-result frame opens a turn) — fixed here |

Watch for the shape in anything new that awaits `receive_messages()` / `_source.next()`
while no prompt is in flight, or that derives status from "the stream produced something".

## Reference

- Real-binary capture: `ClaudeSDKClient` with the production factory options, a copy of
  `~/.claude-accounts/cfg/tonystein85_gmail.com` (settings + plugins), cwd = a ccswitch
  checkout, **no `query()`**: `connected +0.55s`, then at `+1.52s` four `hook_started`,
  four `hook_response`, one `hook_progress` (`SessionStart:startup`). A probe with the
  store-root `settings.json` (no plugins) is silent — which is why the first two probes
  missed it.
- Operator workaround before the fix deploys: **Stop** the session (clears the phantom
  turn *and* the queue), then re-send the prompt from the composer.
