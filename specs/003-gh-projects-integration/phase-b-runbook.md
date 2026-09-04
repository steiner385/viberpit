# 003 Phase B runbook — enabling the reconciler

## Preconditions
- `CCSWITCH_GH_PROJECT_TOKEN` in the daemon env (classic PAT, project+repo) — verified 2026-09-03.
- Boards exist and are linked (`gh_projects` rows): ccswitch #26, KinDash #27.
- **Mirror keyed on GitHub's canonical `nameWithOwner`** (issue #999). `ProjectRepo.repo` was
  entered by hand as `steiner385/cc-account-switcher` / `cairnea/kindash`; GitHub answers queries
  for those aliases, so every mirrored key followed the alias while GitHub-sourced refs stayed
  canonical — no parent linked, no native edge resolved, and the migrate run would re-issue
  `addBlockedBy` for edges that already exist. pm-sync now renames the mirror itself on the first
  pass whose payload disagrees (`repo_renamed` event with per-table counts); confirm before the
  dry run that `GET /api/pm/projects` shows `steiner385/ccswitch` / `cairnea/KinDash` and that
  `GET /api/pm/projects/<slug>/deps` has an empty `unresolvedTargets`.
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
- **Rename the built-in Status options to the spec vocabulary by hand, before the first apply.**
  GitHub's defaults are `Todo` / `In Progress` / `Done`; the spec's are `Backlog` / `Ready` /
  `In progress` / `Blocked` / `Needs human` / `Done`. `_ensure_fields` appends missing options
  by **exact name**, so `In Progress` and `In progress` are two different columns to it — leave
  the defaults in place and the board comes out of the migration with both, plus a dead `Todo`.
  Renaming in the UI first leaves the union with nothing to append.

## Board schema safety (verified live on a scratch board, #987)
`python scripts/spike_gh_projects_v2.py optionids` creates a throwaway board, runs the migrate
union update both ways and deletes it (findings §"Option-id probe"). Findings, and what the code
does about them:

- **`updateProjectV2Field` regenerates option ids unless you echo them back.** An input entry in
  `singleSelectOptions` with no `id` is a *new* option, even when the name matches. Omitting ids,
  the scratch board's `Todo` went `f75ad846` → `ac49461c`, and the issue parked on it read back
  `Status: null` — silent data loss on a human's column, no error. `ProjectV2SingleSelectFieldOptionInput.id`
  is optional-but-accepted, so `build_update_field_options` sends each preserved option's existing
  id from `field_cache`; probed that way, ids stayed stable and the item's assignment survived. So
  the union `_ensure_fields` sends preserves a human's extra columns *and* their contents.
  **Caveat for anyone hand-rolling a repair from the spike script or the rollback section below:
  pass the ids**, or every Status on the board is blanked — there is no undo, and the journal's
  `old_value` for a `set_field_options` row is the option *name list*, not the per-item values.
  Colors and descriptions are also replaced by whatever the input carries — the reconciler sends
  `GRAY`/`""` for the spec options it appends, and the probe confirmed preserved entries keep the
  color/description they were sent.
- **The spec vocabulary is case-sensitive against GitHub's defaults** (hence the rename step
  above). Spec `In progress` does not match the built-in `In Progress`, so a stock board that was
  not renamed comes out of the migrate run with *both* columns. Cosmetic — the reconciler writes
  only to its own `In progress` — but a case-insensitive union is the wrong fix: `_field_gap` and
  `build_batch` both look options up by the exact spec name, so folding case would park every
  differently-cased board. `Done` matches the default exactly and is reused; `Todo` has no spec
  equivalent and simply survives as an extra human column.
- Steady state never edits board schema; the union runs only while `migrate` is on.

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
   **Expected on a board that has not been bootstrapped yet:** `would park batch … field 'Score'
   not in cache`. A dry run writes nothing, so it does not create Score/Stage, so the batch builder
   genuinely cannot render a `set_field` for them — the row is still journaled and reviewable. This
   clears after the first apply; it is not a reason to hold off. (Only a real apply gates on the
   post-bootstrap field gap; the dry run is exempt so the preview survives.)
3. Mode `apply` (migrate still on). Next tick applies in batches of 20; check `?state=applied`,
   `?state=failed`, `?state=parked`. The status card shows counts; `gh_reconcile_cap` events mean the
   cap was hit — raise `migrate_cap` only if the planned set is right.
4. Verify on GitHub: fields exist, items present, Status set, blocked-by edges on the prose-dep issues.
5. **Leave `migrate` on until the status card shows `0 planned` on two consecutive ticks**, then turn
   it off. Status/Score for *newly added* items land on the **second** apply, and the reason is the
   mirror, not the field cache: the field cache is updated in the same cycle that creates the fields
   (`_ensure_fields` returns the new ids and the batch builder uses them immediately), so items the
   mirror already knows get their Status/Score on the **first** apply. An item that this cycle's
   `add_item` just put on the board is not in the mirror yet, and the diff engine only plans field
   mutations for mirrored items — so nothing can be planned for it until pm-sync re-ingests the
   board. Hence: apply 1 = fields + membership + fields-on-known-items, pm-sync, apply 2 = fields on
   the newly added items.
   Turning `migrate` off before that leaves the board half-written — and with it off, a board still
   missing a field or Status option is no longer repaired: the product emits `gh_reconcile_parked`
   ("board fields incomplete") and skips its item mutations until migrate is turned back on. That
   emit is **latched per product**: you get it once, not once per tick, and again only if the gap
   changes or recurs after clearing. The `parked` count on the status card keeps ticking either way
   — trust the count, not the notification volume, for "is this still broken".
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
  Score/Stage or a Status option. Turn `migrate` on for one apply cycle, then off again. Emitted
  once per product per distinct gap, not once per tick; the status card's `parked` count is the
  live signal.
- `board field bootstrap did not complete` (`gh_reconcile_parked`) — `migrate` IS on but a field
  mutation failed or is parked, so the board still has a gap. The cycle stops there deliberately:
  every `set_field` for the missing field would raise UnknownOption and park a whole 20-row batch,
  burying the one real failure. Fix the failed `create_field`/`set_field_options` row (see
  `?state=failed` / `?state=parked`), delete the parked journal row, and the next tick retries.
  Latched like the migrate-off form: one push per distinct gap, not one per tick.
- `gh_reconcile_cap` — the planned set exceeds the cap; nothing was applied. Raise `migrate_cap`
  only once you have confirmed the planned set is right. Latched per product on the
  `(planned, cap)` overshoot: the same overshoot pushes once, a *different* one (another issue
  arrived) pushes again, and a product that fits and later overshoots again is loud again. The
  `halted_cap` count ticks every cycle regardless.
- `parked` rows — a mutation failed twice; fix the cause, then delete the parked journal row to re-plan.
- `would park batch … has no option id` in **dry run** — board vocabulary drift. Emitted once per
  product per distinct error, not once per tick: a dry run parks nothing, so it re-plans the same
  drift forever and an unlatched emit would never stop. Fix the drift and the latch clears, so a
  recurrence is reported again. As always, the `parked` count is the live signal.
- `gh_human_edit` events — someone edited a field on GitHub; the reconciler will not overwrite it.
