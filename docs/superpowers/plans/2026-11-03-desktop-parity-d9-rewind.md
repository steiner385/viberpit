# Desktop-parity D-9: Rewind, Edit & resend, Rewind & retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewind a Claude session to before one of your messages. The rewind restores the files Claude edited and
cuts the conversation, and it can be undone until the next prompt is sent. Edit & resend and Rewind & retry build on
it, and `/rewind` is the command form.

**Architecture:**
- **Spike.** The verified facts are in `docs/superpowers/specs/2026-11-03-d9-rewind-verification.md` (CLI 2.1.317,
  SDK 0.2.105). Every coding spawn enables file checkpointing.
- **Rewind on the idle live client.** It sends two internal control requests through one driver adapter:
  1. `rewind_files {user_message_id, dry_run?}`;
  2. `rewind_conversation {target_message_uuid, last_seen_user_message_uuid}`.
- **Undo is ViberPit's own.** Before the real rewind, ViberPit backs up the affected files and records the
  transcript's byte length. Undo truncates the JSONL, restores the files, and resumes the client.
- **Target id.** The UI sends a user turn id. The server resolves it to the CLI prompt uuid.
- **Live prompt uuids.** These come from `replay-user-messages`.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest; React + TS, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md`:
- §4: message actions, Rewind to here, Edit & resend.
- §6: error cards.
- §10: the API row `POST /sessions/{id}/rewind` + `…/rewind/undo`.
- §11: D-9.

Decisions D3, D7 and D17 are in `docs/audits/2026-11-01-claude-desktop-code-benchmark.md`. Read the verification doc
first; it is binding.

## Global Constraints

- **Engines.** Claude only. A new `AgentCapabilities.rewind` is True for Claude when the kill switch
  `VIBERPIT_REWIND_ENABLED` (default `true`) is on. Codex stays False. The UI hides every rewind affordance when the
  capability is false.
- **Checkpointing.** Every coding spawn (new, resume and fork) passes `enable_file_checkpointing=True` while the kill
  switch is on.
  - A session spawned before this ships has no checkpoints for its earlier turns. The dry run then answers "No file
    checkpoint found", and rewind is offered as **conversation only**.
- **Shared file history.** Add `"file-history"` to `SHARE_ALLOWLIST` in `src/viberpit/configdir.py`. This is ruled
  safe: it holds file backups, not credentials. It means a rotated session can still restore its files. The test must
  confirm `.credentials.json` and `.claude.json` are still never shared.
- **Target id.** The client sends `toTurnId`, the user turn's id, exactly like fork's `atTurnId`. The server resolves
  it to the CLI prompt uuid.
  - **Live:** the driver passes `extra_args["replay-user-messages"]`. The translator maps the echoed
    `UserMessage.uuid` onto the pending user turn and emits `userPromptUuid {turnId, uuid}`.
  - **Replay:** the user turn id already is the uuid.
  - **Storage:** the record keeps `prompt_uuids: dict[turnId, uuid]`, bounded to 500 like `turn_anchors`, with a
    rebuild from the transcript on a miss.
- **Mechanism, in this order, on the idle live client:**
  1. Dry run.
  2. Undo backup.
  3. `rewind_files`, unless the mode is conversation-only or there is no checkpoint.
  4. Record the JSONL length.
  5. `rewind_conversation`.
  6. Emit a `rewound` event.
  - Refuse with 409 `REWIND_BUSY` while a turn runs or a permission card is pending.
  - Every CLI `reason` (`turn_running`, `stale_target`, `target_splits_tool_call`, `target_not_found`, a non-user
    target) and `canRewind:false` maps to a 409 with a code. Codes go in one `REWIND_CODES` table in
    `server/sessions/rewind.py`.
- **Undo backup.**
  - **What:** the union of the dry-run `filesChanged` and every path tracked by the transcript's `file-history-snapshot`
    rows from the target prompt onward. The probe showed a mode-only revert is restored but not previewed. For each
    path, store the bytes and mode, or "absent".
  - **Where:** under the session host's ViberPit state dir, `rewind/<sessionId>/<rewindId>/`.
  - **Writing:** files are written through `src/viberpit/fs_beneath.py` (no symlink following).
  - **Size cap:** 64 MiB total. Above it, refuse with 409 `REWIND_TOO_LARGE` and do nothing.
  - **Retention:** a backup is deleted when its rewind stops being undoable, and on session archive.
- **Undo.**
  - **When:** valid only for the session's **latest** rewind, and only while no prompt has been sent since. Otherwise
    return 409 `REWIND_UNDO_EXPIRED`.
  - **Steps:**
    1. Stop the client.
    2. Truncate the JSONL to the recorded length.
    3. Restore the backup: write bytes and mode, delete the paths recorded as absent.
    4. Respawn with `--resume` on the same engine session id.
    5. Emit `rewindUndone`.
  - **Retries:** idempotent by `rewindId`.
- **Persistence.** A new model, `SessionRewind`, with these columns:
  - `id` (the rewindId) and `session_id`;
  - `to_prompt_uuid` and `to_turn_id`;
  - `jsonl_path` and `jsonl_len`;
  - `backup_dir`;
  - `files_restored` (json);
  - `created_at` (from `db_utc_now()`), `undone_at` and `expired_at`.

  Add migration `0094_session_rewinds.py`, chained off `0093`. Adding a model means updating `_REQUIRED_EXTRAS` in
  `server/tests/test_postgres_compat.py`, and `RECONCILE_EXEMPT` in `test_schema_reconcile_coverage.py` if needed (see
  the repo notes).
  - The next `send_prompt` on the session sets `expired_at` and deletes the backup.
- **Replay follows the leaf.** After a rewind the JSONL still holds the dropped rows, so replay must follow the active
  chain.
  - The active leaf is the last `last-prompt` row's `leafUuid`, or the file's last entry when there is none.
  - Only entries on the `parentUuid` chain from that leaf are shown.
  - A `leafUuid: null` row (a rewind to the first prompt) shows an empty conversation.
  - This also governs D-8's `transcript_anchor_facts`, so dropped turns are not fork points.
- **API.**
  - **Rewind:** `POST /api/sessions/{id}/rewind` with
    `{toTurnId, mode: "files_and_conversation" | "conversation", dryRun?: bool, requestId}`.
    - With `dryRun`, it returns 200 `{files: [worktree-relative paths], deleted: [paths], insertions, deletions, checkpointAvailable: bool}`
      and changes nothing.
    - Otherwise it returns 200 `{rewindId, prefill: string | null, filesRestored: [paths]}`.
  - **Undo:** `POST /api/sessions/{id}/rewind/undo {rewindId}` returns 200 `{ok: true}`.
  - **Errors:**
    - 404 for an unknown session;
    - 409 with a code from `REWIND_CODES`;
    - 409 `daemon_outdated` from an old daemon.
  - **Plumbing:** both go through RPC `_SIMPLE`, `RemoteOrchestrator`, fleet `_PER_SESSION` and the facade, with a
    `REWIND_RPC_TIMEOUT`. Refusals log at INFO with `error_data`, never WARNING.
- **UI.**
  - **Rewind to here.** On user turns that have a target, an item in the D-3 `MessageActions` menu opens a confirmation
    built from the dry run:
    - the file list, with deletions marked, and `+insertions −deletions`;
    - the line `Changes made by shell commands are not restored, and edits made outside Claude since that message are overwritten.`
    - With `checkpointAvailable:false` it says `Files can't be restored for this message — only the conversation will be rewound.`
      and sends `mode:"conversation"`.
  - **The rewind divider.** It replaces the cut turns, reads `Rewound — N files restored`, and has an **Undo rewind**
    button. It hides after the next prompt, or once undo is done.
  - **Edit & resend.** It is the same confirm-and-rewind flow, then the composer is filled with `prefill` (the D-8
    draft-seed mechanism, on the same session).
  - **Rewind & retry.** It sits on the `ErrorCard` of a failed turn. It confirms, rewinds to that turn's user prompt,
    then sends `prefill` unchanged through the normal send path.
  - **`/rewind`.** An app command in `catalog.py` that opens a picker of the session's recent user messages (newest
    first, at most 20). Picking one runs Rewind to here. Wire it as `/fork` was wired.
  - **Gating.** All four are disabled while busy, and hidden without the capability.
- **Gates.** ruff, mypy, focused pytest plus the Postgres-list tests; typecheck, lint, full vitest, budget (ceilings
  stay at 157.0/291.6 — no raise without a ruling), and e2e.

## Review Focus

1. **Rewinding a turn whose files Bash also changed.** Only Claude's Edit/Write changes revert; the Bash change
   stays, and the copy says so. Pinned in Task 4.
2. **Undo after a rotation to another account.** File history is shared, so the CLI restore and the undo both work
   under the new config dir. Pinned in Task 1 (sharing) and Task 4 (undo under a changed config dir).
3. **Reload after a rewind.** The cut turns stay hidden, the divider shows while undo is valid, and D-8 doesn't offer
   fork on dropped turns. Pinned in Task 2.
4. **A remote-host session.** Rewind and undo run on the owner daemon, where the transcript and backups live. Pinned in
   Task 4.
5. **A double click on Undo, or Undo racing a new prompt.** Exactly one wins. A prompt sent first expires the undo and
   it is refused, never half-applied. Pinned in Task 4.

---

### Task 1: Checkpointing on, shared file history, rewind capability, live prompt uuids

**Files:**
- Modify:
  - `server/sessions/engine.py`: add `rewind: bool = False` to `AgentCapabilities`, plus the kill-switch helper
    `rewind_supported()` next to `fork_supported`.
  - `server/sessions/claude_sdk_factory.py`: pass `enable_file_checkpointing` through.
  - `server/sessions/claude_driver.py`: add `replay-user-messages`, then map the echoed uuid onto the pending user turn.
  - `server/sessions/translator.py` and the events schema: add `UserPromptUuid(turn_id, uuid)`.
  - `server/sessions/types.py`: add `prompt_uuids` (bounded at 500).
  - The orchestrator: add `resolve_prompt_uuid(session_id, turn_id) -> str | None`, which rebuilds from the transcript
    on a miss, as `resolve_turn_anchor` does.
  - `src/viberpit/configdir.py`: add `"file-history"` to `SHARE_ALLOWLIST`.
  - Web: `session/events.ts`, `reducer.ts` (user turn gets `promptUuid?: string`), and the capability wiring.
- Test:
  - `server/tests/test_rewind_prompt_uuid.py`;
  - `tests/test_configdir.py` (or the existing configdir test file);
  - reducer tests.

**Interfaces:**
- **Produces:**
  - `rewind_supported() -> bool`;
  - `orchestrator.resolve_prompt_uuid(session_id, turn_id) -> str | None`;
  - the event `userPromptUuid {turnId, uuid}`;
  - web `UserTurn.promptUuid`.

- [ ] **Step 1: Write failing tests**
  - **Spawn options:** a fake SDK factory sees `enable_file_checkpointing=True` and `extra_args["replay-user-messages"]`
    on new, resume and fork spawns. With the kill switch off, it sees neither, and `capabilities.rewind` is False.
  - **Live prompt uuid:**
    - A stream with a replayed `UserMessage(uuid=U)` after a send gives exactly one `UserPromptUuid(turn_id=<live user turn>, uuid=U)`.
    - The replayed echo emits no second `UserMessage`.
  - **Resolving:** `resolve_prompt_uuid` hits the record live. After a restart (empty record) it resolves a replay
    turn id from the transcript.
  - **Shared file history:** `reconcile_links` symlinks `file-history`, and still never links `.credentials.json` or
    `.claude.json`. Run with `PYTHONPATH=$PWD/src`.
  - **Reducer:** stores `promptUuid` on the matching user turn.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** focused pytest, core `tests/`, vitest for the reducer, ruff and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): file checkpointing on, live prompt uuids, rewind capability (D-9)`

### Task 2: Replay follows the active chain

**Files:**
- Modify: `server/sessions/replay.py` (`_parse_transcript`). Compute the active leaf from the last `last-prompt` row,
  walk `parentUuid` from it, and emit only the entries on that chain. D-8's anchor facts use the same chain.
- Test: `server/tests/test_replay_active_chain.py`.

**Interfaces:**
- **Consumes:** none.
- **Produces:** replay and `transcript_anchor_facts` reflect the active chain only.

- [ ] **Step 1: Write failing tests**
  - **Rewind in the middle:** a JSONL with prompts U1, U2, U3, then a `last-prompt` row with `leafUuid=<U1's last assistant entry>`,
    replays only U1's turn. U2 and U3 have no anchors.
  - **Rewind then a new prompt:** after that, a U4 whose `parentUuid` is U1's last entry replays U1 and U4.
  - **First prompt:** `leafUuid: null` replays nothing.
  - **No `last-prompt` row:** the file's last chain is used, and the output is unchanged for every existing replay test.
  - **Sidechain and compaction:** entries keep the existing behaviour. A compact boundary with `parentUuid: null`
    stays on the chain via the existing logical-parent handling. Check how the replay treats it today, and pin it.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full replay, ws, fork and anchor suites. Expected: PASS.
- [ ] **Step 5: Commit** `fix(replay): follow the active chain so rewound turns stay hidden (D-9)`

### Task 3: Driver rewind adapter + binary contract test

**Files:**
- Create: `server/sessions/rewind.py`, holding `REWIND_CODES`, `RewindError(code, message)`, and the result
  dataclasses `RewindPreview(files, insertions, deletions, checkpoint_available)` and
  `ConversationRewind(prefill, preceding_uuid)`.
- Modify: `server/sessions/claude_driver.py`. Add `async rewind_files(user_message_id, *, dry_run) -> dict` and
  `async rewind_conversation(target_uuid, *, last_seen) -> dict`. Both send the raw control request through the SDK
  client's control channel, because the SDK's `rewind_files()` has no `dry_run` and drops the response.
- Test:
  - `server/tests/test_rewind_driver.py`, with a fake control channel;
  - `server/tests/test_rewind_cli_contract.py`, a no-model test against the installed binary, marked `slow` and skipped
    when the binary is absent. A bogus target gives `target_not_found`; a dry run with a bogus id gives
    "No file checkpoint found".

**Interfaces:**
- **Produces:** the driver methods above, plus `rewind.map_cli_error(resp) -> RewindError`.

- [ ] **Step 1: Write failing tests**
  - **Request shapes:** the exact JSON sent for each call, for example
    `{"subtype":"rewind_files","user_message_id":U,"dry_run":true}`.
  - **Errors:**
    - every CLI `reason` maps to its code;
    - `canRewind:false` with "No file checkpoint" maps to `REWIND_NO_CHECKPOINT`;
    - an unknown error maps to `REWIND_FAILED`.
  - **The contract test** is as described in Files.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests, ruff and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): driver adapter for CLI file and conversation rewind (D-9)`

### Task 4: Orchestration, undo, persistence, API

**Files:**
- Modify:
  - the orchestrator: add `rewind_session(session_id, *, to_turn_id, mode, dry_run, request_id)` and
    `undo_rewind(session_id, rewind_id)`;
  - `server/sessions/rewind.py`: the backup store, with `take_backup(...)`, `restore_backup(...)` and `drop_backup(...)`;
  - `server/db/models.py`: `SessionRewind`;
  - migration `0094`;
  - the expiry hook in `send_prompt`;
  - the RPC, remote, fleet and facade layers;
  - `server/api/sessions.py`: both routes;
  - the Postgres lists.
- Test: `server/tests/test_session_rewind.py`, with a real temp worktree, a fake driver for the control requests, a
  real JSONL, and the DB.

**Interfaces:**
- **Consumes:**
  - from Task 1: `resolve_prompt_uuid` and `rewind_supported`;
  - from Task 3: the driver methods and `RewindError`.
- **Produces:**
  - both routes, with the contract in Global Constraints → API;
  - the events `rewound {rewindId, toTurnId, filesRestored}` and `rewindUndone {rewindId}`.

- [ ] **Step 1: Write failing tests**
  - **Dry run:** it returns worktree-relative files plus `deleted`, and touches no file, the JSONL or the DB.
  - **Real rewind:**
    - the call order is dry run, then backup, then `rewind_files`, then the length recorded, then
      `rewind_conversation`;
    - a `SessionRewind` row is written;
    - the `rewound` event is emitted;
    - `prefill` is returned.
  - **Conversation-only mode** skips `rewind_files`.
  - **No checkpoint:** a files mode with no checkpoint gives 409 `REWIND_NO_CHECKPOINT`.
  - **Busy:** a running turn or a pending permission gives 409 `REWIND_BUSY`.
  - **Undo:**
    - the JSONL is truncated to exactly `jsonl_len`;
    - the files are restored, including a mode-only file and a file that was absent before and is now deleted;
    - the client is respawned with `resume`;
    - `rewindUndone` is emitted;
    - the backup dir is removed.
  - **Undo under another account:** a changed `CLAUDE_CONFIG_DIR` still works.
  - **Expiry:**
    - a `send_prompt` after a rewind sets `expired_at` and drops the backup, and undo then gives 409 `REWIND_UNDO_EXPIRED`;
    - only the latest rewind is undoable.
  - **Idempotency:** a second request with the same `requestId` returns the same `rewindId`. Two concurrent undos give
    one apply and one 200 no-op. Undo racing a prompt gives either a clean undo or `REWIND_UNDO_EXPIRED`.
  - **Size cap:** a backup over 64 MiB gives 409 `REWIND_TOO_LARGE`, with nothing changed.
  - **Symlinks:** a symlink in the backup set is never followed (`fs_beneath`).
  - **Remote host:** over a real UDS RPC round trip the call runs on the owner daemon, the codes survive the hop, there
    is no WARNING, and `daemon_outdated` is mapped.
  - **Codex or kill switch off** gives 409 `REWIND_UNAVAILABLE`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse the orchestrator's existing respawn-on-resume path (grep how a model switch or a
  rotation restarts a client) and the Cancel rule helpers.
- [ ] **Step 4: Run** focused pytest, the RPC parity tests, the schema reconcile and postgres-compat tests, the
  migration tests, ruff and mypy. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): rewind to a message with file restore and undo (D-9)`

### Task 5: UI — Rewind to here, divider + Undo, Edit & resend, /rewind

**Files:**
- Create:
  - `web/src/features/sessions/rewind/` with `RewindConfirmDialog.tsx`, `RewindDivider.tsx`, `useRewind.ts` and
    `rewindCopy.ts`. The copy is one table of messages, one per code.
  - the `RewindPicker.tsx` component for `/rewind`.
- Modify:
  - `MessageActions.tsx`: add Rewind to here and Edit & resend on user turns that have a `promptUuid` or a replay id,
    when the capability is on and the session is idle.
  - `web/src/api/hooks.ts`: add `useRewindSession` and `useUndoRewind`, and update the explicit hook mocks.
  - the reducer: on `rewound`, drop the turns from `toTurnId` onward and insert the divider; on `rewindUndone`, refetch
    or replay.
  - `catalog.py` / the dispatcher: add `/rewind`.
- Test:
  - component tests;
  - hook tests, including that the `requestId` stays the same across retries;
  - reducer tests;
  - e2e `web/e2e/rewind.spec.ts` (route-mocked).

**Interfaces:**
- **Consumes:** the Task 4 API and events, and the Task 1 `promptUuid`.
- **Produces:** `useRewind(sessionId).start(turn, {edit: boolean})`, which Task 6 reuses.

- [ ] **Step 1: Write failing tests**
  - **Confirmation:**
    - the dialog lists the dry-run files, marks deletions, and shows the shell-commands line;
    - `checkpointAvailable:false` shows the conversation-only line and sends `mode:"conversation"`.
  - **After confirm:** the turns are cut and the divider shows `Rewound — 2 files restored`. Undo posts with
    `rewindId`, and the divider hides after `rewindUndone` or after the next send.
  - **Edit & resend** fills the composer with `prefill`.
  - **Busy or no capability:** the items are disabled while busy and hidden without the capability.
  - **Errors:** a 409 shows the mapped copy.
  - **`/rewind`:** opens the picker, and picking a message runs the same flow.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Lazy-load the dialog and picker.
- [ ] **Step 4: Run** the full web gates, the budget and e2e. Expected: PASS. Report the budget.
- [ ] **Step 5: Commit** `feat(web): Rewind to here with undo, Edit & resend, and /rewind (D-9)`

### Task 6: Rewind & retry on error cards

**Files:**
- Modify: `web/src/features/sessions/transcript/ErrorCard.tsx`. Add a **Rewind & retry** action when the capability is
  on and the failed turn's user prompt has a target. It runs `useRewind.start(turn, {edit: false})`, then sends
  `prefill` through the normal send path.
- Test: `ErrorCard.test.tsx`, plus an e2e case in `rewind.spec.ts`.

- [ ] **Step 1: Write failing tests**
  - The action appears only with the capability and a target.
  - Confirm, then rewind, then exactly one send carrying the prefill text.
  - A rewind refusal sends nothing and shows the copy.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the web gates. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): Rewind & retry on error cards (D-9)`

### Task 7: Benchmark and docs

- In `scripts/desktop_parity_benchmark.py`:
  - `Rewind to message (with file restore, undo)` becomes `Y`, with the note
    `Claude; Edit/Write only — shell changes not restored`;
  - `Edit & resend a previous message` becomes `Y`;
  - `Error recovery actions` gets `rewind & retry` added to its note;
  - the app-command row gets `/rewind` added to its note.
- Regenerate audit §3.
- Product docs: rewind, undo rules, limits (Bash, outside edits, 100 checkpoints, pre-D-9 sessions are
  conversation-only), and the kill switch.
- Spec §4/§10: record the verified mechanism and the undo design.
- [ ] Run the script. Expected: Target completion rises above the D-8 value of 80% (79.8% before rounding). Report it.
- [ ] Commit `docs: D-9 benchmark rerun and rewind docs`.
