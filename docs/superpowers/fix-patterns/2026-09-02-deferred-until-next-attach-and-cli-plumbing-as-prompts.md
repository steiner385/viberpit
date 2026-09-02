# Fix pattern: "deliver on the next attach" when the attach already happened, and CLI bookkeeping read as a human prompt

**Date:** 2026-09-02
**Origin:** RCCA of "dispatching a session with model=Fable wipes the prompt and renames the
session" (production sessions `9d32d33f` 21:06 UTC, `655ea334` 16:36 UTC, `01873ce9` 18:22 UTC,
2026-09-02). The user dispatched with Fable, the launch-time approval hold appeared, they clicked
Approve from the open session view — and the session sat idle with an empty transcript. Whatever
they typed next ("continue") ran instead, and the session was auto-titled **"Set model to Fable"**
/ **"Continue work with Fable model"**. Two independent defects; both recur elsewhere.

## Symptom

- After approving a launch-time model hold (or a queued session getting its slot) *while its view
  is open*, nothing happens: no turn, the header says Ready, the seeded prompt bubble vanishes on
  the next replay. `ws.attach.decision … detached=true` precedes the approval in the journal and no
  attach follows it.
- The transcript's first `type:"user"` entries are the CLI's own `/model` journal —
  `<local-command-caveat>`, `<command-name>/model</command-name>`,
  `<local-command-stdout>Set model to fable (claude-fable-5)</local-command-stdout>` — and the
  fallback titler summarises them. On reload, replay renders them as raw user bubbles.

## Anti-pattern

1. **Deferred delivery keyed to an event that may already be in the past.** `_spawn_session`
   stages a human session's initial prompt in `_pending_initial_prompt` "until the first WS
   attach", and only `_handle_attach`'s LIVE path pops it. But a `pending_approval` / `queued`
   session can be viewed *before* it is spawned: that attach takes the read-only detached path
   (no driver → no pop), the spawn lands later, and no client re-attaches. Same slot, same shape
   for the unarchive seed (`_pending_initial_prompt.setdefault(seed)` on a detached, idle session
   — "lazy delivery on the NEXT WS attach", which was again the detached path) and for
   `_recover_undelivered_prompts` after a daemon restart.
2. **A transcript reader that assumes every `type:"user"` entry is a person.** The CLI journals
   `set_model` (any client-side slash command), the interrupt marker, task notifications and the
   compaction summary as `type:"user"` records; only some carry `isMeta`. Replay had grown three
   ad-hoc exclusions; the titler had none. Two readers, two definitions of "a prompt".

## Fix shape

- **Deliver on every live-stream open, not on "the first attach".** The bridge now sends the
  deferred prompt from one helper (`_send_deferred_initial_prompt`) in BOTH places it opens a live
  stream: the live attach path and the lazy `_ensure_live` a typed prompt triggers — so the
  original prompt always queues ahead of what the user typed. The pop keeps it at-most-once.
- **The view re-attaches when the record leaves a pre-spawn state.** `useSessionRuntime` watches
  `sessionRecord.status`; on `pending_approval|queued → anything else` it re-sends `attach`, which
  takes the live path. `PRE_SPAWN_STATUSES` / `isPreSpawnStatus` in `web/src/api/types.ts` is the
  one definition (Dashboard's inline pair now routes through it).
- **One predicate for "is this user entry a human prompt".** `server/sessions/transcript_entries.py`
  (`is_harness_user_entry`) is shared by `replay.py` and `titles.read_user_prompts_from_file`.

## Where to look

- Anything else staged "for the next attach" / "until a client is watching": grep
  `_pending_initial`, `pop_pending_`, `next attach`, `first attach`. Every consumer must run on
  *every* live-stream open, and every producer must consider that a socket may already be attached
  read-only.
- Other readers of transcript `type:"user"` entries that still use raw text: `search.py`
  (indexes plumbing text — harmless, but should route through the predicate),
  `background_tasks.py` (intentionally targets `<task-notification>`). `export.py` consumes
  replay events and inherits the fix.

## Reference

- Tests: `server/tests/test_ws_deferred_prompt_after_approval.py`,
  `server/tests/test_titles.py::test_user_prompts_skip_cli_local_command_records`,
  `server/tests/test_replay.py::test_replay_skips_cli_local_command_records`,
  `web/src/session/__tests__/useSessionRuntime.reattach.test.ts`.
