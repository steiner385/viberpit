# Native identity published before conversation durability

## Symptom

A Codex session runs normally until daemon restart, then loses its native
conversation handle and may replay its initial prompt in a new conversation.
Its in-memory record has an ID while its database row is NULL (#1493).

## Anti-pattern

The driver publishes identity during native resource allocation, before that
identity is resumable and before its application row exists. The callback's
UPDATE matches no fresh row, and the later INSERT initializes the field to NULL.
Copying the early ID into the INSERT fixes the lost pointer but persists a handle
that cannot resume if the service stops before its first native turn.

## Fix shape

Separate driver-local protocol identity from the durable resume handle published
to orchestration. Codex `thread/start` supplies the former; an accepted first
`turn/start` or `turn/started` notification establishes the latter. The real
binary rejects resuming an empty newly allocated thread after app-server restart.
A successfully resumed existing thread can publish at startup. Assistant overlays
always start a fresh thread, even when the caller supplied a resume handle.

Use the existing fenced callback at that durability boundary, after the fresh
application row exists. Serialize and deduplicate response/notification reports;
mark publication successful only after the callback succeeds, so another signal
can retry a failed write. Do not classify a database callback failure as native
turn rejection after Codex already accepted that turn. Reset publication state
when reconnect starts a new conversation.

Tests must cover both native response and notification paths, empty threads,
failed first turns, resumed threads, and owner recreation from the database.
Include fresh and preexisting queued rows. Verify initial prompts still recover
when never sent, but do not replay after a turn ran. A real-binary smoke must
restart the app-server itself, not just recreate the application owner while
leaving the native server's volatile state alive.

Verified against pinned Codex 0.153.2 with disposable SQLite state and independent
app-server processes: a completed conversation resumes the same ID and recalls
prior context; an empty session starts a fresh usable thread; a first turn
interrupted immediately after acceptance retains a resumable ID and accepts a
subsequent turn. Context recall is checked on the completed conversation because
native interruption can discard an immediately aborted prompt.

## Where to look

Search `driver.start()`, `start_thread`, `on_engine_session_id`, and
`engine_session_id` writers across `server/` and `src/`:

- `CodexAgentDriver.start`: fresh coding and assistant paths allocate only;
  successful native resume publishes an already-existing conversation.
- `_start_turn` and translated `TurnStart`: authoritative native acceptance
  signals share one publication helper. The notification covers a lost response.
- `reconnect`: drops the old native handle and publication marker before starting
  a fresh thread. The new handle is published only when its first turn exists.
- `_spawn_session`: existing INSERT/update and rollback ordering remains intact;
  new native acceptance occurs after row persistence and calls the fenced writer.
- `ensure_driver` and `_reassign_locked`: reuse the same driver/callback path.
- Claude's native session callback is already driven by an SDK session message
  during a turn; no startup-allocation publication needed to change.
- `_enqueue_session` and `_hold_for_approval`: placeholders before any driver
  starts, so NULL is intentional. `_recover_undelivered_prompts` can continue
  distinguishing unsent work from native turns without a new schema marker.
- `adopt_session`: Claude-only; inserts the explicitly adopted conversation ID.
- Codex refinement/suggestion/goal helpers: intentionally ephemeral, with no
  persistence callback or durable row.

A separate rollback-policy concern remains: `_restore_held_row` reconstructs an
approval placeholder with NULL identity even if the failure occurred after a row
was committed and a turn was accepted. That is distinct from startup publication
and is not changed here.
