# 003 Phase D runbook — retiring the claim labels

Phase D made the runner's durable rows the carrier of claim and give-up state (#994): a claim is
the backlog runner's target-state row, a give-up is its `gave_up` row, and the reconciler turns
that row into board Status `Needs human`. Nothing reads `in-progress` / `needs-human` any more.

Open issues labelled *before* the deploy still wear those labels, and nothing clears them on its
own. `scripts/retire_claim_labels.py` is the ONE-TIME sweep that does — run it once per product
repo after the deploy, then delete the `in-progress` label definitions.

It is safe to re-run: a completed action leaves no label behind, so a second run plans nothing.
That is also how a partial run is finished — run it again.

## What the sweep does with each label

| Found | Becomes | Why |
| --- | --- | --- |
| `needs-human`, labeler ends `[bot]` | the runner's `gave_up` row, `needs_human_at` = the label event's timestamp | the runner's own handoff, moved to the carrier Phase D reads: the reconciler sets board Status `Needs human`, the queue applies the needs-human cooldown, and `_sweep` expires it on the normal `needs_human_retry_days` schedule |
| `needs-human`, any other labeler (or no timeline event) | a board Status edit to `Needs human` on the product's board | a human's signal. Written as a human edit, which the reconciler never overwrites — a runner row would instead re-enter the issue into the retry cycle on a decision a person made |
| `in-progress` | removed, nothing else | the label was the whole of a stale claim; no session is attached to it any more |

An issue that is on no board (no `gh_project_items` row) prints `SKIP` and **keeps its label** —
never strip a human's signal without replacing it. Fix the mirror (pm-sync must have ingested the
board) and re-run, or handle those few by hand.

Label *definitions*: `needs-human` **stays** in every repo — humans may keep using it as a plain
signal, the runner simply no longer reads it. `in-progress` is deleted after the sweep (below).

## Preconditions
- The Phase D code is deployed: the daemon and edge run a build that contains this PR, so the
  reconciler derives Status from the `gave_up` row and the queue reads the cooldown. Migrating
  rows against an older build parks issues that nothing interprets.
- **The reconciler is in `apply`** (Automations → GitHub Reconcile, mode `apply`), and pm-sync is
  enabled — the migrated rows only reach the boards through the reconciler's next tick, and its
  freshness gate refuses to write against a mirror older than the newest decision.
- Run from the edge host (dobby), where the live DB and the reconciler's token are reachable:

  ```bash
  set -a; . /etc/viberpit/viberpit.env; . /etc/viberpit/daemon.env; set +a
  echo "${CCSWITCH_DB_URL:?}" >/dev/null; echo "${CCSWITCH_GH_PROJECT_TOKEN:?}" >/dev/null
  ```

  `--apply` refuses to start without `CCSWITCH_DB_URL` when the plan writes a row or edits a board
  (it would otherwise create an empty local SQLite file and write the rows into it), and without
  `CCSWITCH_GH_PROJECT_TOKEN` when any board Status edit is planned. It checks before the first
  mutation, so a missing input never leaves labels stripped with no replacement written. A **dry
  run needs neither** — it only reads GitHub — and neither does an `--only in-progress` apply,
  which just removes labels.
- `gh` is authenticated as the ccswitch identity (the script strips ambient `GITHUB_TOKEN` /
  `GH_TOKEN` for its issue reads and edits, exactly as the rest of ccswitch shells `gh`; the
  Projects mutation is the one call that uses `CCSWITCH_GH_PROJECT_TOKEN`).

## 1. Dry run, per product repo

The five product repos today:

```bash
cd /home/tony/GitHub/viberpit   # the deployed checkout
for r in steiner385/viberpit cairnea/KinDash cairnea/web cairnea/kwgt-cairn \
         steiner385/ghostdriver-mcp; do
  .venv/bin/python scripts/retire_claim_labels.py --repo "$r" | tee "/tmp/retire-${r##*/}.log"
done
```

Each line is `DRY  <kind>  <owner/repo#N>  at=<iso-utc> actor=<login>`; the tail prints the count.
`--only in-progress` / `--only needs-human` restrict the sweep to one label (the filter is the
script's, not the planner's — the plan you review is the plan that runs).

## 2. Review before applying
- **Every `row_from_bot_label` actor is the runner's App login** (`viberpit[bot]`). Any other login
  classified as a bot means a second App has been labelling issues — stop and look.
- **`status_from_human_label` entries are issues a person parked.** Their `at` is only informational
  (a human Status edit carries no timestamp); what matters is that the issue belongs on the board.
- **Old `at` values are expected and fine.** A `gave_up` row's cooldown is measured from the label
  event, so a handoff from three weeks ago is migrated already-expired and the runner's sweep
  retries the issue on its next pass — which is the correct reading of a three-week-old handoff.
- Spot-check a couple of refs on GitHub against `GET /api/pm/queue` — an issue already at Status
  `Needs human` for another reason needs no migration and should not be in the plan.

## 3. Apply

```bash
for r in steiner385/viberpit cairnea/KinDash cairnea/web cairnea/kwgt-cairn \
         steiner385/ghostdriver-mcp; do
  .venv/bin/python scripts/retire_claim_labels.py --repo "$r" --apply \
    | tee -a /tmp/retire-apply.log
done
```

One line per action: `DONE`, `SKIP  (issue is on no board)` or `FAIL  (<reason>)`. A failure does
not abandon the rest of the sweep; the exit status is non-zero if anything failed. Re-run to retry
the failures — the successful actions are already invisible to the next plan.

## 4. Verification
- `GET /api/pm/queue` — `skipped` is a reason→count map, not a list of refs, so compare counts:
  `skipped.needs_human_cooldown` should have risen by the number of bot-migrated rows still inside
  the cooldown window, and none of those refs should appear in `entries`. (Bot handoffs whose label
  event is older than `needs_human_retry_days` are expired on arrival, as above — they count
  towards neither.)
- **Human-migrated items are on the board already.** The script performs those Status edits itself,
  the moment `--apply` prints `DONE`, and deliberately does NOT journal them — an unjournaled field
  change is exactly what the reconciler reads as a human edit and will not overwrite. So they never
  appear in `GET /api/pm/reconcile/journal`; verify them on the board (or expect a `gh_human_edit`
  event from the next pm-sync pass that ingests them).
- **Bot-migrated rows reach the board on the reconciler's next apply tick** (≤ 2 min): the row is
  the decision, the reconciler derives Status `Needs human` from it, and
  `GET /api/pm/reconcile/journal?state=applied` shows those `set_field` rows. If they do not
  appear, check that the reconciler is in `apply` and that pm-sync has refreshed the mirror (its
  freshness gate refuses to write against a mirror older than the decision).
- No open issue wears a claim label in any product repo:

  ```bash
  for r in steiner385/viberpit cairnea/KinDash cairnea/web cairnea/kwgt-cairn \
           steiner385/ghostdriver-mcp; do
    echo "== $r"; gh issue list --repo "$r" --label in-progress --state open
    gh issue list --repo "$r" --label needs-human --state open   # only SKIPped ones may remain
  done
  ```

- A second dry run plans `0 action(s)` (bar any SKIPs you chose to leave).

## 5. Delete the `in-progress` label definitions

Only after the sweep verifies clean — deleting the definition first would strip the labels without
recording anything, and the classification evidence (who labelled it, when) goes with them.

```bash
for r in steiner385/viberpit cairnea/KinDash cairnea/web cairnea/kwgt-cairn \
         steiner385/ghostdriver-mcp; do
  gh label delete in-progress --repo "$r" --yes
done
```

`needs-human` definitions are **kept** deliberately: a human may still label an issue to say "this
needs me", and after Phase D that is a plain human signal with no machine behaviour attached to it.

## Failure modes
- `error: CCSWITCH_DB_URL must be set (the live database) to apply` / `error:
  CCSWITCH_GH_PROJECT_TOKEN must be set to edit board Status` — the env was not sourced. Nothing was
  mutated.
- `SKIP  (issue is on no board)` — the issue has no mirrored board item. Its label was left alone.
  Usually a repo that is onboarded but not yet ingested by pm-sync, or an issue opened since the
  last sync; re-run after a sync.
- `SKIP  (the runner holds a live claim on this issue)` — the stale label outlived the deploy and
  the runner has since re-claimed the issue. Writing the migrated row would REPLACE that live
  claim with a weeks-old give-up, so the sweep refuses: look at the session, and drop the label by
  hand (`gh issue edit N --remove-label needs-human`) once you agree the old handoff is spent.
- `FAIL  (… has no option id)` — the board's Status field has no `Needs human` option (vocabulary
  drift). Fix the board (Phase B runbook, § board schema), then re-run.
- `FAIL (gh issue edit …)` — the label was already gone, or a permissions/rate-limit blip. The
  durable replacement is written BEFORE the label is removed, so a failure here is a re-runnable
  duplicate, never a lost handoff. Re-run.

## Rollback
The sweep is a data migration, not a mode: there is no switch to flip back. Undoing an individual
action means deleting the runner row (`DELETE FROM automation_target_state WHERE automation_id =
'backlog-runner' AND target = '<ref>'`) or setting the board item's Status back by hand, and
re-adding the label with `gh issue edit N --add-label …`. Which is why step 2 is a real review.
