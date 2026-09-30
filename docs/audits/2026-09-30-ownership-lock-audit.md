# Ownership / claim locking audit — why the runner looped on KinDash#16774 (2026-09-30)

Investigation of the operation/ownership locking and claim mechanism for defects that stop forward
progress. Read-only against the prod DB on dobby (`automation_operations`, `automation_owned_targets`,
`automation_target_state`, `automation_event`, `audit_events`, `work_items`, `sessions`) plus GitHub
GraphQL. All times UTC unless marked local (America/New_York, UTC−4). Nothing was modified.

## 1. The locking model as implemented

| Primitive | Where | Acquired by | Released by | Lifetime |
|---|---|---|---|---|
| **Target lock** — `automation_owned_targets(key=github:<node_id>, operation_token)` | `server/automations/ownership.py:_lock_targets` | `acquire()` — atomic, all-or-nothing over the sorted key set; any key with a token ⇒ `OwnershipConflict("target already has an operation")` | only `OwnershipTransaction.release()` / `transfer()`; token set to NULL / successor | **Permanent, "Never TTL-expired"** (`models.py:718`) |
| **Operation** — `automation_operations` (token, owner, source, phase, `reserved_session_id`, `session_id`, `execution_generation`/`stopped_generation`, `execution_sessions`, provenance) | `ownership.py` | `acquire()` writes `claimed`; `reserve_session()`→`launching`; `bind_session()`/`start_action()`→`active`; `complete_action()`→`completed`; `mark_uncertain()`→`uncertain` | `release(proof)` needs `NOT_STARTED` (phase still `claimed`) or `WORK_STOPPED` (`stopped_generation == execution_generation`, the whole managed session tree attested stopped). Retained forever for provenance. | "Unknown outcomes retain ownership forever" (module docstring) — by design |
| **Automation row state** — `automation_target_state(automation_id, target).state` JSON: `ownership_token`, `claimed`, `session_id`, `gave_up`, `held`, `handed_off`, `closeout_started`, `proposed`, `waiting_account` | `context.py:claim_target_state / compare_target_state` | claim path | fenced CAS through `owned(token)`; deleted on release; token-less legacy rows only via `release_legacy_target_state` | per row |
| **Backlog claim** = a runner row that is `claimed and not gave_up` | `server/pm/queue_store.py:runner_claims` | `dispatch_batch` | the sweep (`_sweep_one`) or `_release_give_ups` | — |
| **Group consumption** — `backlog_groups.consumed_at` | `backlog_grouping.mark_consumed` | `select_batch` | never (UI only; "correctness never depends on it") | per plan generation |
| **Issue hold** — `work_items.hold_kind` (`owner_action`/`decision`/`stronger_model`/`evidence`) + `hold_fingerprint` | `server/pm/holds.py` | runner give-up / ladder / agents / people | board edit, **body edit (fingerprint mismatch)**, Retry, dispatch, decision | until a release signal |

What happens to a lock when its owner goes away:

* **Session ends / completes / is archived** — nothing releases automatically. The next sweep tick proves
  "work stopped" through the generation counters (`_automation_session_work_stopped`,
  `attest_terminal_session_stopped`) and then the workflow decides: merged → close-out → release; no PR →
  ladder climb (release) or **give-up (retains the lock, see D1)**; PR Doctor → `_pr_doctor_clear_observed`.
* **Session row permanently deleted** — `_release_orphaned_claim` (runner) and the `purged` branch of
  `_automation_session_work_stopped` (PR Doctor) treat absence as the stop proof. (Four earlier #16774
  operations on 2026-09-24/25 were released exactly this way: "session row deleted while the claim was open".)
* **Daemon restart** — `_reconcile_dangling` rebinds a reserved session; generation proofs survive in the DB.
* **Spawn fails between claim and session start** — `dispatch_batch` releases with `NOT_STARTED` only if the
  phase is still `claimed`; anything later is `uncertain` and retained.
* **API call times out mid-operation** — the operation is unaffected (the work continues server-side; see D8).
* **TTL / heartbeat / reaper** — none for locks. There is no operator inspection or release surface (API, UI
  or assistant tool); the assistant recorded the gap itself at 11:54:47 UTC
  (`audit_events` type `assistant.gap`, capability `clear-operation-lock`).

Census at 11:56 UTC: **76 target locks held by 74 operations**. 60 belong to Backlog Runner rows that are
`gave_up` (parked on a hold, no live session; 45 of them have `session_id` rows that no longer exist);
8 are runner `terminal-closeout … completed` operations still holding issues (the #2203 shape); 9 are PR
Doctor operations stuck in phase `uncertain` since 2026-09-16…22 ("PR effect outcome unknown;
reconciliation required" — nothing reconciles them); 2 are genuinely live.

## 2. Defects

### D1 — A parked bundle keeps the lock on every member, but a member's hold is released independently (root cause of observation 1) — **HIGH**

**What holds `cairnea/KinDash#16774` right now**

```
automation_owned_targets.key = github:I_kwDONe8AYs8AAAABSxUpSg  →  operation_token 5a3c6bc7-0291-43e7-93b1-da257d5c82d4
automation_operations 5a3c6bc7: owner=backlog-runner source=automatic phase=active
    origin_ref=cairnea/KinDash#16980  targets={#16980,#16982,#16984,#16985,#16774}
    session_id=12b8dd36-…  (sessions row DELETED)  execution_generation=3 stopped_generation=3  created 2026-09-25 12:36:43Z
automation_target_state(backlog-runner, cairnea/KinDash#16980).state:
    gave_up=true held=true claimed=false reason="bounced at opus; fable needs approval"
    issues=[#16980,#16982,#16984,#16985,#16774] ownership_token=5a3c6bc7
```

How it got there (audit trail, `audit_events`):

| when (UTC) | event |
|---|---|
| 2026-09-25 12:36:40 | runner claims the 5-issue bundle (primary #16980, `stronger_model` rung → opus) |
| 2026-09-25 12:39:13 | session 12b8dd36 calls `mark_no_pr` |
| 2026-09-25 12:39:41 | sweep: bounced at the top of the ladder → `_give_up` → `pm.hold.set owner_action "bounced at opus; fable needs approval"` on all 5 issues. **`_give_up` deliberately retains the operation** ("Retain attempt/session provenance and durable exclusion while the human decides", `backlog_runner.py:1870`) |
| 2026-09-26 14:09:03 | `pm.hold.release signal=body_edit` on **#16774 only** (`holds.py:339`): the KinDash `red-watchdog` bot rewrote the issue body ("4+ consecutive non-success trunk runs"), the fingerprint no longer matched, the reconciler released the hold. The other four holds are untouched. |
| since then | #16774 has no hold → `queue.skip_reason` does not skip it → it is in the candidate pool (`runner_claims` excludes only `claimed and not gave_up` rows, `queue_store.py:48`) → the grouper plans it → `dispatch_batch` → `claim_target_state` → `acquire` → conflict with 5a3c6bc7. |
| 2026-09-30 08:43 → 11:54 | 75 `ownership_wait "target already has an operation"` events on #16774 (earlier days it was masked by `skipped_product_cap 4/4` and `skipped_pr_doctor_priority`). |

`_release_give_ups` (`backlog_runner.py:1713-1718`) is explicit: "A bundle is released as a UNIT: while ANY
ref of the row is still held the whole row stays". So the lock will not move until a human releases the
`owner_action` hold on #16980/#16982/#16984/#16985 (approve fable, Retry, board edit, or a body edit on
those). Two ledgers with two release rules: the **hold** is per-issue and releasable by anyone (including a
bot), the **lock** is per-bundle and releasable only as a unit. Any bundle that contains a bot-maintained
issue (red-watchdog, nightly, [flaky]) deadlocks that issue: `pm.hold.release signal=body_edit` fired 16
times in the last 14 days.

A second way to build the same state: `_backlog_write_runner_hold` skips issues a person already holds
(`orchestrator.py:7325`), so a bundle can be *born* with an unheld-by-runner member whose person-hold is
later released.

Explains observations 1 and 2 (manual dispatch of all three refs bounced on #16774; #17275 + #17222 alone
spawned).

### D2 — All-or-nothing claim, no split around a locked member, a wait loop that never escalates — **HIGH**

* `plan_to_batch` (`backlog_grouping.py:527`) keeps every ref of the group that is still in the pool; the
  claim (`acquire`) is atomic across them. One locked member ⇒ the whole group bounces.
* On `busy`, `_refill` neither excludes the member nor cools the group down; it just returns. The next
  tick's `_select_batch` pops the same group (`consumed_at` is UI-only) → same bounce, every 60–75 s.
* `refill_stalled "…none is claimable — refreshing the plan"` sets `_group_nudge`; the grouper re-runs over
  the **same** pool and produces the same group. The message promises a fix that cannot happen.
* The runner's `ownership_wait` says only "target already has an operation" — PR Doctor's equivalent was
  taught to name the holder in #2203 (`foreign_holder_message`); the runner was not.
* No counter, no threshold, INFO level: nothing ever tells the operator. (The runner's other wait,
  `handoff_wait "PR recovery prerequisites are not satisfied"`, has the same shape — 26 events on #17275
  in 30 min today.)

### D3 — PR Doctor waits ~8 h on its own repair session that is parked on `hand_back`, and reserves the product's backlog capacity while it waits (observation 3, #17314) — **MEDIUM**

* PR Doctor spawned session 310d2ab1 for #17314 at 2026-09-30 02:22 (op `1b90330e`, phase `active`).
* 03:09:13 UTC the session called **`hand_back`** ("A twin session (worktree d6d41d87) is actively pushing to
  PR #17314 and just merged my commit … tell me whether to keep working here"). It then sat until
  `mark_complete` at 10:40:17 / 10:41:30 UTC; the operation was released at 10:42 ("observed stopped
  standalone PR work"); `recovered` at 10:41.
* Meanwhile the twin kept pushing (head SHAs 40b0…, 724c…, 11cb…, 7b13…, e027…, 08e7…, b091…, 2a9b…, 5337…).
  Each push re-armed the target (`apply_rearm`), `_pr_doctor_step` planned a `SessionFix`, `_pr_doctor_claim`
  found its own token with phase `active`, `_automation_session_work_stopped(310d2ab1)` said no (a
  handed-back session is neither complete/ended/archived nor gone) → `OwnershipConflict("prior PR execution
  has not stopped")` — **79 refusals, one every ~90 s, across 9 head SHAs**.
* `_pr_doctor_claim_unclaimable` rule 2 (`orchestrator.py:15488`): a refusal under PR Doctor's *own* token
  "is its own recovery lifecycle, which clears itself and does legitimately reserve capacity". So
  `_pr_doctor_pending_products` kept gating KinDash on #17314 (runner `skipped_pr_doctor_priority` naming
  #17314 at 08:46–08:57 UTC).

Not the same defect as D1 (the owner was alive), but the same failure shape: an unbounded wait that reserves
capacity and never escalates. A session that has handed back *is* waiting on a human — the operation is
"in progress" only in the sense that nobody has told it to stop. The refusal is also a symptom of two
actors (PR Doctor's session and an unrelated "twin" session) working one PR, which the ownership store does
not model for non-automation sessions.

### D4 — `stuck_enqueue` fires on a 3-minute-old PR (observation 3, #17382) — **MEDIUM**

GitHub timeline for cairnea/KinDash#17382: opened 11:30:52; `ci-gate` (the only required context under
the `main` protection rule) SUCCESS at 11:32:20; rollup SUCCESS; auto-merge enabled by the sweep at
11:31:35/11:31:57; **no check was pending at 11:34:02**, when PR Doctor ran `update_branch` with
`last_failure=stuck_enqueue`. The author converted it to draft at 11:34:27 (after the action — at action
time it was not a draft, and no ViberPit session owns that branch, so no attached-session check could apply).

`classify` (`pr_doctor_engine.py:129`) treats "auto-merge armed + `mergeStateStatus=BLOCKED` + not in queue +
no pending checks" as "GitHub silently refused to enqueue". #2324 added the pending-check guard; this is the
next shape — GitHub reports BLOCKED for a short window while it recomputes merge state after the arm, and a
single snapshot cannot tell that from a genuinely shadowed required check. The mechanical update kicked off a
fresh CI run (`enable-auto-merge-*` restarted at 11:34:32) and gated the product (D6).

### D5 — A merge-conflict fix session spawned 95 s after the PR opened, racing the author's own rebase (observation 3, #17383) — **MEDIUM**

#17383 opened 11:32:23 (DIRTY against `main`); `auto_resolve_conflicts` is **ON** in prod config (schema
default `False`, labelled "Risky"); PR Doctor spawned f67d3c07 at 11:33:58; the author force-pushed a
rebase at 11:34:43; the fix session found nothing to do and called `mark_complete` at 11:34:54 (77 s of
life). There is no grace period after PR creation / last push before a SessionFix, and nothing recognises a
non-ViberPit author actively working the branch.

### D6 — `_pr_doctor_pending_products` ignores the per-head one-shot guard, and the two "stuck" readouts disagree by construction (observation 3) — **LOW, clearly fixable**

`_pr_doctor_step` (`orchestrator.py:13813`) will not act again at a head it already acted on
(`last_action_sha == info.head_sha and attempts > 0`). `_pr_doctor_pending_products` (`:15533`) runs
`classify` + `plan_recovery` only, so a PR PR Doctor has *already fixed* at this head keeps reserving the
product until the poller sees the PR change. Today: both actions were done by 11:34:02, yet the runner was
still refused at 11:34:57 naming both PRs. The message "PR Doctor has N **stuck** pull requests pending a
fix" is also used for BEHIND/DIRTY/enqueue PRs. `PrDoctor.status` ("all PRs green") counts `actionable()`
(classify only, computed at the end of the sweep) — a different filter at a different time.

### D7 — Locks outlive their owners with no reaper, no alarm, no inspection/release surface — **MEDIUM (systemic)**

By design the store never expires a lock and "unknown outcomes retain ownership forever". That is the right
safety default for *live* work, but 74 of 76 held locks belong to rows with no live session (D1 bundles,
completed close-outs, `uncertain` PR Doctor operations from two weeks ago). Nothing surfaces the age of a
lock, and the only release paths are the workflows' own sweeps. When those sweeps are the thing that is
stuck, the operator has no lever — the assistant logged exactly that gap today.

### D8 — Manual dispatch is synchronous and holds the runner's tick lock (observation 2, the ReadTimeout) — **LOW**

`POST /api/automations/backlog-runner/dispatch` → `_backlog_dispatch_batch` runs resolve-targets, claim,
clone, model assessment and spawn inline under `loop._lock` (`orchestrator.py:8388`). Today: `claimed`
11:53:57, `spawned` 11:54:21 (24 s); the assistant's HTTP client times out at 20 s
(`server/assistant/client.py:42`). The claim had already succeeded, so the caller saw a timeout for a
dispatch that worked. Same shape #668 fixed for PR Doctor with a queued job. The lock also stalls the tick
for the duration.

### D9 — `groups_refresh_failed: GroupingModelError: kind=other Control request timeout: initialize` (observation 4) — **LOW, contributing**

The grouping SDK client failed to initialise; the loop backs off `GROUP_REFRESH_BACKOFF_S` (120 s) and
keeps the previous plan generation — which is the same unclaimable group. Contributing, not root.

## 3. Other places a wait can repeat indefinitely without escalating

| Loop | Evidence | Bound? |
|---|---|---|
| runner `ownership_wait` (D1/D2) | 75/day on one target | no |
| runner `handoff_wait` | 26 in 30 min on #17275 today | no |
| PR Doctor `ownership_wait` own-token (D3) | 79 on #17314 | no |
| PR Doctor `ownership_wait` foreign holder | 198 on #17006 in 5 h on 2026-09-27 (the "runner close-out" shape, partially addressed by #2203's adoption path) | no |
| PR Doctor `uncertain` operations | 9 held since 2026-09-16…22, re-observed every sweep by `_pr_doctor_observe_mechanical` | no |
| runner `refill_stalled no_batch` → `_group_nudge` | regenerates the same plan | no |

## 4. Proposed fixes, ranked

1. **Split a parked bundle when a member's hold is released (D1) — shipped in PR #2547.**
   `_release_give_ups` → `_split_parked_bundle`: when a `gave_up` row has some refs held and some not,
   release *only the unheld refs* from the operation (`OwnershipTransaction.release_targets(subset,
   WORK_STOPPED, …)`, same proof bar as a full release; `_work_stopped` repairs it first for a gone session),
   shrink `state["issues"]`/`titles`, emit `bundle_split`. A freed PRIMARY re-keys the row (and the
   operation's `origin_ref`) to the first still-held sibling. The split runs before `_refill` in the same
   tick, so the freed member is claimed without a single bounce. Not done: a queue skip reason for the
   one-tick window between the reconciler's hold release and the runner's next tick (no bounce is possible
   there — the split precedes the claim). Regression: `test_a_parked_bundle_member_whose_hold_was_released_is_dispatchable`,
   `test_a_parked_bundle_whose_primary_was_released_is_rekeyed_to_a_held_sibling`, and the `release_targets`
   unit tests on both dialects.
2. **Name the holder and bound the wait (D2).** Runner `ownership_wait` names the holding operation and the
   parked row's reason (this audit's PR); count consecutive waits per target and after N emit one WARN
   `ownership_wait_escalated` + cool the target down so the rest of the plan is served; stop `refill_stalled`
   from claiming "refreshing the plan" when the plan is unchanged.
3. **`_pr_doctor_pending_products` honours the per-head guard (D6)** — shipped with this audit.
4. **A handed-back repair session does not reserve capacity and is not re-claimed every tick (D3).**
   `_pr_doctor_claim_unclaimable` rule 2 should exempt an own-token operation whose session is parked
   (hand_back / needs-human), and `_pr_doctor_step` should try the transfer once per head, not once per sweep.
   Longer term: an operator-visible "PR Doctor is waiting on you: #17314" hold, the same way the runner parks.
5. **Dwell time before `stuck_enqueue`, grace period before a SessionFix (D4, D5).** Require the
   armed+BLOCKED+green state to be observed on two consecutive polls (or ≥ N min since
   `autoMergeRequest.enabledAt`, which the poller can fetch) before update-branch; no merge-conflict /
   failing-CI session inside the first ~10 min after PR creation or the last push. Both need a design decision
   on the thresholds — see the `hand_back`.
6. **Lock lifecycle surface (D7).** `GET /api/automations/operations` (held targets → operation, phase,
   session + status, age, owning row state), `POST /api/automations/operations/{token}/release` bound by the
   same proof rules (`NOT_STARTED`/`WORK_STOPPED`, session absent), an assistant tool over that endpoint, and a
   staleness WARN once a lock has been held > 24 h with no live session. No TTL on the lock itself.
7. **Reconcile `uncertain` PR Doctor operations (D7)**: an operation `uncertain` for > N hours with no session
   and a terminal/absent PR is released; otherwise escalated.
8. **Async manual dispatch (D8)**: return the claim immediately, spawn in a job (#668 pattern); don't hold
   `loop._lock` across the clone.

Adjacent in-flight work (local worktree branches, no PR yet at the time of writing):
`fix/capacity-defect-self-heal-orphaned-claims` (ownership.py: self-heal claims wedged by an unattested
predecessor execution — the #2202 generation-proof shape, not D1) and `fix/handed-off-rows-blind-to-merge`
(PR Doctor's finished handoff holding a batch's issues — the D7 close-out shape). Fix 1 should land after
the former to avoid a three-way merge in `ownership.py`.

## 5. Releasing #16774 now (not done — awaiting confirmation)

* **Designed path (recommended):** release the `owner_action` hold on the four siblings — approve the
  fable retry or "Retry" on #16980 from the roadmap/queue (`release_via_api`). `_release_give_ups` then drops
  the row and the operation, freeing all five keys; the bundle re-enters the queue.
* **Surgical path** (if the fable approval is not wanted): one transaction that (a) removes
  `github:I_kwDONe8AYs8AAAABSxUpSg` from `automation_operations.targets` and `display_refs` of
  `5a3c6bc7-…`, (b) sets `automation_owned_targets.operation_token = NULL` for that key, and (c) removes
  `cairnea/KinDash#16774` from `automation_target_state(backlog-runner, cairnea/KinDash#16980).state.issues`.
  **Do not** null the key alone: `owned(5a3c6bc7)` would then raise `StaleOwnership` ("operation no longer
  owns its targets") and the #16980 row could never be released.
