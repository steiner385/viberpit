# 003 Phase B runbook — enabling the reconciler

## Preconditions
- `CCSWITCH_GH_PROJECT_TOKEN` in the daemon env (classic PAT, project+repo) — verified 2026-09-03.
- Boards exist and are linked (`gh_projects` rows): ccswitch #26, KinDash #27.
- Daemon on origin/master (the SHA fence refuses otherwise).

## One-time board setup that the API cannot do
- Views: create a "Board" view grouped by Status and a "Table" view sorted by Score desc, per board.

## Enable sequence (spec §8 Phase B: dry-run → journal review → enable)
1. Set `CCSWITCH_GH_RECONCILE_ENABLED=true` in `/etc/ccswitch/daemon.env` and restart at a quiet
   moment — the kill switch gates every mode, including dry run. Then Automations → GitHub
   Reconcile: mode `dry_run`, `migrate` on.
2. Wait two ticks (≤ 4 min). Review: `GET /api/pm/reconcile/journal?state=dry_run` — expect field
   bootstrap rows (Score, Stage, Status options), `add_item` rows for every open issue, `set_field`
   Status rows (`Backlog`/`Ready`/`Needs human`…), and `add_blocked_by` rows for each prose edge.
   Anything unexpected → fix before apply.
3. Mode `apply` (migrate still on). Next tick applies in batches of 20; check `?state=applied`,
   `?state=failed`, `?state=parked`. The status card shows counts; `gh_reconcile_cap` events mean the
   cap was hit — raise `migrate_cap` only if the planned set is right.
4. Verify on GitHub: fields exist, items present, Status set, blocked-by edges on the prose-dep issues.
5. Turn `migrate` off. Steady state uses the 50 cap.

## Rollback
Every applied row has old→new. To revert a batch: `?state=applied` filtered by `batchId`; set
mode `off`, then apply the inverse by hand (option ids in `gh_projects.field_cache`) or via the
spike script. Removing compiled edges: `removeBlockedBy` with the same ids.

## Failure modes you will see
- `refused: daemon SHA != origin/master` — deploy first.
- `kill switch off` — env var unset.
- `parked` rows — a mutation failed twice; fix the cause, then delete the parked journal row to re-plan.
- `gh_human_edit` events — someone edited a field on GitHub; the reconciler will not overwrite it.
