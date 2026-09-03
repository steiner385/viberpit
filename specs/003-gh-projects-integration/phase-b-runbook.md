# 003 Phase B runbook — enabling the reconciler

## Preconditions
- `CCSWITCH_GH_PROJECT_TOKEN` in the daemon env (classic PAT, project+repo) — verified 2026-09-03.
- Boards exist and are linked (`gh_projects` rows): ccswitch #26, KinDash #27.
- Daemon on origin/master (the SHA fence refuses otherwise).
- **pm-sync automation enabled (mode on).** The reconciler's freshness gate refuses to write
  against a mirror older than the newest decision, and pm-sync is what refreshes it — with pm-sync
  off, every product logs `skipped_stale` forever and nothing is ever applied.
- **Journal index created on the live DB.** `_reconcile_columns` ALTER-ADDs missing columns on
  every dialect but never adds an index to an existing table, so migration
  `0020_journal_mutation_id_index` does not reach production by itself. Run once:

  ```sql
  CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gh_write_journal_mutation_id
      ON gh_write_journal (mutation_id);
  ```

## One-time board setup that the API cannot do
- Views: create a "Board" view grouped by Status and a "Table" view sorted by Score desc, per board.

## Enable sequence (spec §8 Phase B: dry-run → journal review → enable)
1. Set `CCSWITCH_GH_RECONCILE_ENABLED=true` in `/etc/ccswitch/daemon.env` and restart at a quiet
   moment — the kill switch gates every mode, including dry run. It is read from the daemon's
   process environment, so **closing the kill switch again also requires a daemon restart**; to
   stop the reconciler immediately without one, set the automation's mode to `off`. Then
   Automations → GitHub Reconcile: mode `dry_run`, `migrate` on.
2. Wait two ticks (≤ 4 min). Review: `GET /api/pm/reconcile/journal?state=dry_run` — expect field
   bootstrap rows (Score, Stage, Status options), `add_item` rows for every open issue, `set_field`
   Status rows (`Backlog`/`Ready`/`Needs human`…), and `add_blocked_by` rows for each prose edge
   **whose blocker is still open** — a prose dep pointing at a CLOSED issue produces no
   `add_blocked_by` row, because a closed blocker doesn't block. Dry-run rows are deduped within
   24h and pruned after it, so leaving dry run on does not grow the journal per tick; a second
   review pass shows the same rows, not new copies. Anything unexpected → fix before apply.
3. Mode `apply` (migrate still on). Next tick applies in batches of 20; check `?state=applied`,
   `?state=failed`, `?state=parked`. The status card shows counts; `gh_reconcile_cap` events mean the
   cap was hit — raise `migrate_cap` only if the planned set is right.
4. Verify on GitHub: fields exist, items present, Status set, blocked-by edges on the prose-dep issues.
5. **Leave `migrate` on until the status card shows `0 planned` on two consecutive ticks**, then turn
   it off. Status/Score land on the *second* apply: the first apply creates Score/Stage and extends
   the Status options, and the item mutations that use those new option ids can only be planned once
   pm-sync has re-ingested the items against the updated field cache. Turning `migrate` off before
   that leaves the board half-written — and with it off, a board still missing a field or Status
   option is no longer repaired: the product emits `gh_reconcile_parked` ("board fields incomplete")
   and skips its item mutations until migrate is turned back on.
6. Steady state uses the 50 cap and never edits board schema.

## Rollback
Every applied row has old→new. To revert a batch: `?state=applied` filtered by `batchId`; set
mode `off`, then apply the inverse by hand (option ids in `gh_projects.field_cache`) or via the
spike script. Removing compiled edges: `removeBlockedBy` with the same ids.

## Failure modes you will see
- `refused: daemon SHA != origin/master` — deploy first.
- `kill switch off` — env var unset (a daemon restart is needed either way; use mode `off` to stop
  the loop without one).
- `board fields incomplete` (`gh_reconcile_parked`) — `migrate` is off and the board is missing
  Score/Stage or a Status option. Turn `migrate` on for one apply cycle, then off again.
- `parked` rows — a mutation failed twice; fix the cause, then delete the parked journal row to re-plan.
- `gh_human_edit` events — someone edited a field on GitHub; the reconciler will not overwrite it.
