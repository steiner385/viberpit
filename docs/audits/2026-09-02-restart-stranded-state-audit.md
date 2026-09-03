# Restart-stranded state audit — 2026-09-02

Captured while fixing "queued / just-started sessions come back from a restart with no
initial prompt" (dead orphan cards labelled *Ready* over an empty conversation).

## Symptom

A session appears in the list, its pill reads a calm resting state, and its conversation
is empty forever. Nothing reaps it and nothing retries it. In the live Postgres, 70 rows
had `initial_prompt IS NOT NULL AND claude_session_id IS NULL` — created, never ran a
single turn (34 `backlog-runner`, 31 `pr-doctor`, 5 human).

## Anti-pattern

**Work-critical state held only in process memory, or durable state deleted before the
work it enables commits**, across a boundary (the daemon) that restarts routinely on
every deploy.

Two concrete instances, both in `server/sessions/orchestrator.py`:

1. `_maybe_start_queued` deleted the `session_queue` entry *before* calling
   `_spawn_session`. The entry is the only place the spawn params
   (`cols/rows/identity/attachment_ids`) are persisted, so a daemon killed in that
   window left a `queued` row nothing could replay. `_reconcile_orphan_queued` could
   then only mark it `ended` — and its docstring justified that as inherent, encoding
   the upstream bug as a permanent constraint.
2. `_pending_initial_prompt` is an in-memory dict seeded at spawn and popped on the
   first WS attach. `rehydrate()` never re-staged it, so a session spawned but not yet
   attached lost its prompt permanently.

A third, presentational instance made both invisible: the status vocabulary lived in two
independent lists (`SessionStatus` in `web/src/api/types.ts` and the `PRESENTATION` keys
in `web/src/session/statusTier.ts`, typed `Record<string, …>`). A status missing from the
table does not fail loudly — it silently renders the `FALLBACK`, labelled **"Ready"**.

## Fix shape

- **Commit-then-delete.** Hold the durable record until the operation it enables has
  committed; every exit path drops it exactly once. A crash becomes a *retry*, not a
  discard.
- **Recover from row-backed truth on boot.** Prefer state already on the row over a new
  column: `initial_prompt` is the prompt, and `claude_session_id IS NULL` is proof no
  turn ever ran, which is what makes redelivery idempotent in effect.
- **Bind duplicated vocabularies with a type**, so drift is a compile error rather than
  a silent fallback (`Record<SessionStatus | 'working', …>`).

## Where to look

Checked during this audit:

| State | Verdict |
|---|---|
| `_pending_initial_prompt` | **Fixed** — `_recover_undelivered_prompts` re-stages / re-sends on rehydrate. |
| `_pending_initial_attachment_ids` | **Unrecoverable, documented** — never persisted on the row; a recovered turn runs text-only. Same limitation `_rebuild_hold_kwargs` already accepts. |
| `_pending_approvals` | Covered — `_rebuild_hold_kwargs` rebuilds it from the `pending_approval` row. |
| `_last_prompt` | Covered for a *graceful* deploy by `snapshot_deploy` / `restore_deploy` (#864). Note it only snapshots sessions with a **live driver**, so it can never cover a queued session or an undelivered first prompt — hence the rehydrate-based path above, which also covers ungraceful restarts (crash, OOM, systemd kill) where no snapshot exists. |
| `_worktrees` | Covered — `close_session` re-derives from `cwd` via `worktrees.is_managed_path`. |
| `pending_rearm` / `pending_prompt` | Correct ordering already — cleared only after `_rearm_session` succeeds. |
| Caches/throttles (`_summaries`, `_suggest_inflight`, `_recompose_attempts`, …) | Regenerable; loss is harmless. |

**Interaction worth remembering:** `restore_deploy` replays a snapshotted `_last_prompt`
for mid-turn sessions, and `rehydrate` normalizes a pre-crash `in_progress` → `idle` —
which looks identical to an undelivered-prompt orphan. Recovery therefore skips any row
flagged `interrupted_by_restart` (#434): a running turn *proves* its prompt was
delivered, so acting there too would send the same prompt twice on a graceful deploy.

## Reference

- Fix: `_maybe_start_queued`, `_recover_undelivered_prompts`, `_deliver_initial_prompt`
  in `server/sessions/orchestrator.py`; `PRESENTATION` in `web/src/session/statusTier.ts`.
- Tests: `server/tests/test_orphan_initial_prompt.py`,
  `web/src/session/__tests__/statusTier.test.ts`.
- Prior art on the same boundary: #434 (interrupted-by-restart flag), #864 (deploy
  snapshot/restore).
