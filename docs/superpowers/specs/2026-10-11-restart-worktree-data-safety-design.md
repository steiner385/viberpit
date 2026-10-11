# Restart worktree data safety (#2913, #2914; part of #2910): design

**Date:** 2026-10-11 · **Status:** approved for implementation · **Parent design:** `2026-10-11-restart-survivable-session-work-design.md`

## Root cause

The 2026-09-07 audit trail shows that both reports came from sessions whose worktree lived on dobby being resumed on rocky after the deploy that cut over host naming (#1424). In each case `ensure_driver` ran `Path(rec.cwd).mkdir(parents=True, exist_ok=True)` and created an empty directory at the same path, and the session took that to mean its work was gone.

The cross-host routing bugs have since been fixed (#1389, #1825, #1833), but the unconditional `mkdir` is still there, in `ensure_driver`, `_reassign` and adopt. A primary still owns rows whose host stamp matches no enabled host (`_owns_record_by_stamp`), so any future stamp mismatch will produce the same symptom.

Separately, three destructive paths take no snapshot of uncommitted or unpushed work first:

- `permanent_delete` runs `rmtree` with no dirty check.
- `remove_worktree(force=True)` (janitor force-evict, transfer finalize) discards local changes.
- A clean close runs `branch -D` on a session branch even when it has unpushed commits.

## Rules

1. **A restart or re-attach never deletes or recreates a worktree path that exists.**
2. **A missing managed worktree is never silently turned into an empty directory.**
   - If the session's branch exists in the local clone, recreate the worktree from that branch with `git worktree add`, after `clear_stale_registration`. Say so loudly: log a WARNING `worktree.recreated`, write an audit event, and send a preamble on the session's next prompt naming the branch and any recovery ref for that session.
   - If the branch is not available, refuse the driver build. `ensure_driver` returns None, logs a WARNING `worktree.missing`, and the session stays visible behind the existing resume-failure surface (`_resume_recovery`, gave-up flag). No directory is created.
   - Scratch sessions (`worktrees.is_scratch_path`) and unmanaged cwds keep today's `mkdir`.
3. **Snapshot before anything destructive, and at every drain.** The new `worktrees.snapshot_recovery_ref(worktree, session_id, *, reason) -> str | None` builds a commit of the full working tree without touching the user's index or files:
   - copy the index to a temp `GIT_INDEX_FILE`;
   - `git add -A`;
   - `git write-tree`;
   - `git commit-tree <tree> -p HEAD -m "viberpit recovery: <reason>"`;
   - `git update-ref refs/viberpit/recovery/<session_id>/<utc-ts> <commit>`.

   It returns None when the tree is clean (the tree equals `HEAD^{tree}`). It keeps the newest 5 refs per session, never raises, and has a timeout.

   It is called from:
   - **`begin_drain`**, which covers both the deploy RPC drain and the SIGTERM drain. It runs for every session with a live driver and a managed worktree on this host, through `asyncio.to_thread`, and is bounded in total.
   - **`permanent_delete`**, before the `rmtree`.
   - **`remove_worktree(force=True)`**, before the removal.
   - **The clean-close `branch -D`**, but only when the branch has commits that aren't on its upstream. In that case it records the branch tip as `refs/viberpit/recovery/<sid>/<ts>-branch`.

   The refs live in the clone's common git dir, so they survive the worktree's deletion.
4. **Restore is manual and explicit.** The preamble and the log both give the command:

   ```
   git restore --source=<ref> --worktree --staged -- . && git reset
   ```

   or, more simply, `git checkout <ref> -- .`.

## Tests

- `ensure_driver` on a missing managed worktree whose branch exists recreates it as a real git worktree on that branch, warns, and queues the preamble.
- A missing managed worktree whose branch is absent: no directory is created and the driver build is refused.
- An existing directory is never removed or recreated.
- A scratch path is still created.
- `snapshot_recovery_ref` covers:
  - a dirty tree, including untracked, staged and unstaged changes, producing a ref whose tree contains all of them;
  - the user's index and working tree being byte-identical afterwards;
  - a clean tree returning None;
  - pruning to 5 refs.
- `begin_drain` snapshots a dirty live worktree.
- `permanent_delete` of a dirty archived worktree leaves a recovery ref.
- `remove_worktree(force=True)` leaves a recovery ref.
- A clean close with unpushed commits leaves a branch-tip ref.
