# Moving a Codex session between fleet hosts: verification spike (D-17 follow-up, #3347)

Date: 2026-11-06. Constitution III: every claim was checked against the installed binary (`codex-cli 0.153.2`,
`/usr/bin/codex`) with a no-model probe in a throwaway `CODEX_HOME`, or in this repo's code. Nothing here changes
behaviour; the Codex refusal in `session_move.py` stays until the build below lands.

## Why Claude moves and Codex does not

Claude's transcript sits on a filesystem both hosts see, so the target only has to read it to EOF
(`_verify_moved_transcript`). Codex keeps thread state under a `CODEX_HOME` that `server/codex/process.py` requires
to be local and private (it refuses a network filesystem). The target host therefore cannot read it, and the move
needs a copy step. `cross_host_transfer` is `False` for the Codex engine, so `session_move.py:245` refuses it.

## Findings

1. **A thread is one JSONL file.** After `thread/start` plus one `thread/inject_items`, the whole thread is
   `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl`. Its lines are `session_meta`, `response_item`
   (the raw Responses API items), `world_state` and `turn_context`. The thread id is in the file name and in
   `session_meta.payload.id`.
2. **Copy-and-resume works.** Copying that one file into the same relative path under a different, empty
   `CODEX_HOME` and calling `thread/resume {threadId, cwd}` on a fresh `codex app-server` there returns the thread
   with `status: idle` and the same id, `createdAt` and model. No replay and no model call was needed.
3. **Import by replay is the weaker route.** `thread/inject_items` accepts raw Responses API items, but
   `thread/read` and `thread/turns/list` return `ThreadItem`/turn views, not raw items, and injected items do not
   appear as turns (`thread/turns/list` returned `data: []`). Exporting through the read API and importing through
   `inject_items` would lose turn structure, so it is not a faithful move. There is no export/import method in the
   `generate-json-schema` output (only `ExternalAgentConfigImport*`, which is an unrelated settings import).
4. **`thread/resume` takes no file path** (`ThreadResumeParams` has `threadId`, `cwd`, model and policy fields), so
   the file must be placed in the sessions tree before resuming.

## Recommended design (not built)

Mirror the Claude saga, replacing the shared-file check with a byte copy:

- **Export (source daemon):** after the driver closes, locate the rollout by thread id under the account's
  `CODEX_HOME`, and hand back its bytes with a size and last-`ordinal` marker, the analogue of
  `transcript_satisfies`.
- **Transport:** the existing mTLS daemon RPC. Rollouts can be large, so stream or chunk them and cap the size with a
  clear refusal.
- **Import (target daemon):** write to `sessions/YYYY/MM/DD/` under the target account's `CODEX_HOME` using a temp
  file and rename, verify the marker, then resume by thread id through the normal `ensure_driver` path. A thread id
  already present there must refuse rather than overwrite.
- **Account identity:** a thread may only move to a host that has the same Codex account, because `CODEX_HOME` is
  per account.
- **Sidecars:** `server/codex/item_times.py` and `turn_history.py` keep per-session files under the account root and
  need to move with the rollout, or replay timestamps change.
- Flip `cross_host_transfer` for Codex only when all of the above exist, and update the refusal text and
  `test_session_move.py`.

## Gaps (unchecked)

- Behaviour with a rollout that has live tool state or a compaction in progress (the move only runs on idle
  sessions, so it should be safe, but it was not probed).
- A same-version requirement: whether a rollout written by one `codex` version resumes under another.
- Rollout sizes on long real sessions, which set the transport design.
- A real model turn after resume on the target (the probe used no model and no auth).
