# Moving a Codex session between fleet hosts: verification spike (D-17 follow-up, #3347)

Date: 2026-11-06. Constitution III: every claim was checked against the installed binary (`codex-cli 0.153.2`,
`/usr/bin/codex`) with a no-model probe in a throwaway `CODEX_HOME`, or in this repo's code. The findings and the
recommended design below are the spike as written; what actually landed is in [Built (2026-11-06)](#built-2026-11-06).

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

## Built (2026-11-06)

Plan: `docs/superpowers/plans/2026-11-06-codex-session-move.md` (#3347). The recommended design above landed with
the rulings listed here; the Claude saga in `server/sessions/session_move.py` is unchanged.

**What landed**

- `server/codex/thread_move.py`: the pure file logic. `find_rollout` locates the one rollout for a thread id under a
  `CODEX_HOME` (`sessions/*/*/*/rollout-*-<threadId>.jsonl`, regular file only; more than one is `ambiguous`),
  `inspect` sizes what an export would carry, `export_thread` builds a `ThreadBundle` (relPath, size, sha256, base64
  data, plus the sidecars), `import_thread` writes it on the target, `verify_thread` re-reads the written rollout to
  EOF against the bundle's size and sha256, `remove_imported` unlinks exactly what an abandoned import wrote, and
  `retire_rollout` renames the source copy. Thread ids go through `safe_thread_id()` (the `item_times` token rule
  made public) and relPaths are validated against the rollout layout before any write; writes never follow
  symlinks and go through a temp file, fsync and rename at mode 0o600.
- Orchestrator hooks on the three daemon halves of the saga: `transfer_preflight` reports `historyBytes`;
  `_transfer_export` refuses an over-cap thread before anything is touched and, after the driver closes, adds the
  bundle to the manifest as `codexThread`; `_transfer_import` writes the bundle after the worktree step and before
  the row is installed, verifies it (`_verify_moved_history`, which for Claude is still the transcript marker), and
  resumes by thread id through the normal `ensure_driver` path under the target's own `CODEX_HOME` for the same
  account (`CodexRunnerManager.resolve_home(account)`); `_transfer_finalize` retires the source rollout and reports
  `historyRetired`.
- The capability flip: `cross_host_transfer=True` for the Codex engine (`server/codex/capabilities.py`), so
  `move_supported` passes for Codex under the same `VIBERPIT_SESSION_MOVE_ENABLED` kill switch.
- Two new reason codes in `session_move.py`: `MOVE_HISTORY_TOO_LARGE` (predicate level, over the cap) and
  `MOVE_HISTORY_EXISTS` (import level, a rollout for that thread id already under the target's `CODEX_HOME`).
  `MOVE_ENGINE_UNSUPPORTED` no longer names an engine.
- The cap: `HISTORY_MAX_BYTES = 128 MiB` of raw bytes (rollout plus sidecars).
- `sessions.move_manifest` stores `storable_manifest(manifest)`: the bundle stripped to its descriptor (relPath,
  size, sha256, sidecar names and hashes) with every `data` field dropped, which is what a reconcile verifies a
  holder against.

**Rulings taken (from the plan)**

- **Transport is inline in the manifest**, base64 over the existing mTLS daemon RPC, rather than streamed or
  chunked; the cap makes that safe (the largest rollout on dobby today is 52 MB of 357 files).
- **Never overwrite:** an existing rollout on the target refuses the import and the session is restored to the
  source.
- **Finalize retires, never deletes:** the source renames its rollout to `<name>.jsonl.moved-to-<owner>` so nothing
  on the source can resume it while the bytes stay recoverable, the analogue of the worktree recovery ref.
- **Account identity:** accounts are fleet-wide (the shared account store), so the target resumes under the same
  `account` and its own `CODEX_HOME` for it, created if absent.
- **Sidecars** (`<account cfg dir>/codex-item-times/<thread>.jsonl` and `<thread>.turns.jsonl`) travel in the same
  bundle and are written on the target only when absent (on the deployed fleet that directory is the shared store,
  so they are usually already there).

**Live probe of the built module (2026-11-06, codex-cli 0.153.2, no model call, no auth)**

The smallest real rollout the deployed app-server had written (24 KB, 2026-09-16) was copied into a throwaway
`CODEX_HOME` A. `thread_move.export_thread` on A, `import_thread` into an empty home B (plus one sidecar, written
because absent), `verify_thread` on B, then `thread/resume {threadId, cwd}` on a fresh `codex app-server` over B
returned the same thread id. `retire_rollout` on A renamed the file, after which `thread/resume` on a fresh
app-server over A failed (`CodexRequestFailed`): the source can no longer resume a retired thread. One
correction to finding 1: `thread/start` alone writes no rollout yet; the file appears once the thread has an item
(the September probe's `inject_items`), so a Codex session that never ran a turn moves with no thread to carry.

**Gaps that stay open**

- Same-version rollout compatibility: whether a rollout written by one `codex` version resumes under another is
  still unprobed; the move does not check the two hosts' `codex` versions.
- A real model turn after a cross-host resume (the probe, and the move's own verification, stop at `thread/resume`
  returning the thread idle).
- Live tool state or a compaction in progress: unprobed, and the move only runs on an idle session.
- Rollout sizes on long real sessions are now bounded by the cap rather than measured; a thread over 128 MB
  refuses to move.
