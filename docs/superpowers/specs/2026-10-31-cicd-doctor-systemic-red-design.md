# CI/CD Doctor — Systemic Red (v2: Sense, Alert, Correct, Brake)

Date: 2026-10-31
Status: owner-approved direction (2026-10-31): **alert + dispatch a diagnosed fix + brake**.
Supersedes the v1 boundary in `2026-07-03-cicd-doctor-design.md` ("propose-only; never
touches an individual PR or CI") **for the systemic-red path only**. Every v1 analyzer
(flake, graduation, waste) stays propose-only.

## 1. Why — the 2026-10-28 → 10-31 incident

KinDash's merge queue runs on self-hosted ARC runners on AWS EKS. On 2026-10-30 at
~23:00 UTC npm published four GHSAs; `static-checks / security: audit` (it reads npm's
LIVE advisory feed) went 6→10 with no code change. From 00:34 UTC on 10-31 every
merge_group candidate failed it: **41 of 48** failed candidates between 20:00 UTC 10-30
and 11:30 UTC 10-31 carry `security: audit`, and 39/39 runs failed after midnight. Each
doomed candidate re-ran the full static + integration + e2e fan-out. EC2+EKS ran
$167 / $271 / $235 per day on 10-28..30 against a ~$40 baseline; October closed at
$2,849 against the $1,800 budget. Nothing caught it except AWS Cost Anomaly email,
which lags ~a day. The same class recurred on 09-08, 09-29, 09-30, 10-29, 10-31.

Two facts found during the investigation shape this design:

1. **KinDash's own brake stayed off.** `main-red-gate.sh` (#17473) logged
   `main_red=false reason=main-vouched` all night (verified in ops-auto-merge run
   41198346469, 11:48 UTC). Clause (B) lets the *latest completed push:main run*
   vouch for a job it ran green "whatever its age" — the #17541 deadlock fix. That
   push:main ran at 21:53 UTC 10-30, an hour before the advisories, and nothing can
   merge while every candidate is red, so no newer push:main ever arrives. For a
   job that reads a live external feed the old green is not evidence; the vouch
   holds the brake off indefinitely. The gate's header names this mirror case and
   accepts it as "bounded thrash"; for a live feed it is unbounded.
2. **ViberPit already has the data path but not the question.** `group_history`
   records merge-group failures per check (`group_failures`) for the ETA sim, and
   CI/CD Doctor ingests completed runs every 900 s. Neither asks "is the same job
   failing across unrelated candidates right now?", and both wait for runs to
   complete — a 40-min e2e tail hides a static-check failure that landed at minute 5.

## 2. Goals, non-goals, anti-goals

Goals
- Detect a systemic gate failure **within minutes** of the 3rd distinct candidate
  failing the same job (replay of 10-31: ~01:01 UTC vs. Cost Anomaly ~a day later).
- Diagnose it: the failing job, a normalized log signature, example runs, and a
  classification (external feed · poisoned front entry · infra/runner · main
  regression · unknown).
- Correct it: push-alert the owner immediately, open one incident issue, dispatch
  ONE fix session carrying the diagnosis.
- Stop the bleeding: brake KinDash's own re-arm / force-enqueue automation while
  the incident is live — via a signal KinDash's existing freeze already honors.
- A waste signal (failed merge_group runs/hour vs baseline) so the Doctor flags
  thrash before AWS does; and a bridge from AWS CloudWatch spend alarms (infra PR)
  into the same alert path.
- Close the PR Doctor residual blind spot: a real failure hidden behind a sibling
  pending/cancelled check (see §7).

Non-goals
- No merge-queue mutation of any kind: no enqueue, dequeue, jump, or merge.
  KinDash rule: queue mutations are incident-only and lock-gated; ViberPit's
  `merge_hold_guard` blocks them for sessions. The Doctor *recommends* the exact
  owner command for a dequeue; it never runs one.
- No muting, skipping, or baseline-raising of the failing check by the Doctor
  itself. The fix session may propose a baseline change in a PR, reviewed like any PR.
- No per-PR work (PR Doctor's job) except the §7 classification fix.
- Not a cost dashboard. The waste signal is one finding + one number in the alert.

Anti-goals (would make this a failure even if it "works")
- A brake that deadlocks the queue (the #17541 shape): any brake must self-clear
  without a human and must never fire on a poisoned front entry.
- A brake that outlives ViberPit: if the daemon dies mid-incident the brake must
  lapse on its own (fail-open), never freeze merging until someone notices.
- Alert spam: one push per incident open, one per clear; recurrences comment on
  the existing issue.
- A fix-session storm: one live fix session per incident, hard daily cap.

## 3. Detection

### 3a. Signal (new, fast)

A new loop `SystemicRedLoop` (in `server/automations/cicd/systemic_loop.py`, pure
evaluator in `server/automations/cicd/systemic.py`) ticks every **120 s** under the
`cicd-doctor` automation, per enabled repo. Per tick it reads, via the Actions REST API
(`gh api`), with an explicit `created>=` bound (the KinDash lesson: the runs API
filtered by event without `created` serves week-stale results):

- merge_group runs (any workflow; no per-repo config, since KinDash's only merge_group
  workflow is `CI`) created in the last `systemic_window_min` (default 60),
  **including in-progress runs**;
- for each run, its jobs, keeping those already `completed` with `conclusion`
  `failure`/`timed_out` (a static check that failed at minute 5 counts at minute 5,
  not when the e2e tail finishes) and the jobs that passed;
- the latest completed push:main run of each workflow seen above (same
  `workflow_id`), and its job outcomes.

Jobs fetched per run are cached by `(run_id, attempt)` with their completed outcomes,
so a run already seen fully completed is never re-fetched. Cost: one list call plus
one jobs call per new/in-progress run per tick — tens of calls per 120 s at peak.

The PR a candidate belongs to is parsed from `head_branch`
`gh-readonly-queue/<base>/pr-<N>-<sha>`; the `<sha>` is the candidate's BASE (main or
the entry ahead), not the PR head (verified 2026-10-02 in KinDash).

Job names are normalized to the segment after the last ` / ` (reusable-workflow
prefix). Aggregate rollup jobs are excluded (config `systemic_rollup_jobs`, default
`ci`, `ci-gate`): they fail as a consequence, never as a signature.

### 3b. Rule

A job J is **systemic** in repo R when, within the window:

- (C) J failed on candidates of **≥ `systemic_min_prs` distinct PRs** (default 3), and
  no candidate *created after the earliest of those failures* ran J green; **or**
- (M) J failed on the latest completed push:main run and on ≥ 1 candidate created
  after it.

"Distinct PRs", not distinct candidates: one PR rebuilt three times is the rebuild
cap's domain, not a systemic red.

Replay of 10-31 (fixtures from the real runs in the plan's test data): (C) trips on
the third distinct-PR `security: audit` failure ≈ 01:01 UTC; the incident stays open
through 11:30 UTC; no other job reaches the threshold except `test: unit (3/4)`,
which the classifier must evaluate separately (it is its own incident if it trips).

### 3c. Clearing

An incident for (R, J) clears when any candidate or push:main run **created after the
incident opened** runs J green, or when J has not failed for
`systemic_quiet_clear_min` (default 90) while **at least one** candidate completed J in
that span. Zero candidates means "not yet cleared", never "quiet". Clearing lifts the
brake (§5c); the incident issue gets a recovery comment and closes.

The brake also has a hard ceiling that doesn't depend on clearing:
`systemic_brake_max_min` (default 600, under the KinDash consumer's 12 h marker cap).
Past it the brake is lifted and not re-applied for that incident, and the owner gets one
push: "brake lifted after 10 h; incident still live". This bounds the case the brake
creates itself: few new candidates while braked, so no green run to clear on.

## 4. Diagnosis

### 4a. Log signature

For the newest failing job of J, fetch its log once (`/actions/jobs/{id}/logs`),
take the first `##[error]` line, else the last 40 lines' first line matching
`error|failed|✗|FAIL`, and normalize it (strip timestamps, hex shas ≥ 7, numbers →
`N`, paths after `node_modules/`). The normalized line + J is the incident
fingerprint; the raw excerpt (≤ 40 lines) goes into the issue and the fix prompt.

### 4b. Classification (first match wins, in this order)

| Class | Rule | Brake? | Fix session? |
|---|---|---|---|
| `external_feed` | J matches `systemic_external_feed_jobs` (default `^security: audit$`), or the signature carries an advisory id (`GHSA-`, `CVE-`) | yes | yes: "the external feed moved; restore the gate (overrides or bumps, or the repo's documented baseline procedure); never mute the check" |
| `infra` | a failing job concluded `startup_failure`; or the signature matches the runner list (`received a shutdown signal`, `lost communication with the server`, `No space left on device`, `exit code 137`, `The operation was canceled`); or ≥ 3 different jobs fail with one infra signature | no | no: alert plus infra runbook pointer |
| `poisoner` | all four hold: J is not an external-feed job; every failing candidate's stack contains the same unmerged PR P; J ran green on the latest push:main; and no candidate whose stack lacks P failed J | **never** (#17541) | no: PR Doctor already owns P's failing CI; the issue names P and the owner's lock-gated dequeue command |
| `main_regression` | (M) tripped, or the failing stacks share no common PR | yes | yes: "fix forward or revert the merge that broke J on main" |
| `unknown` | none of the above | no | no: alert only |

External feed is checked first on purpose. In a serial queue every candidate behind the
front entry shares that entry in its stack, so on 2026-10-31 the poisoner test alone
would also have matched (push:main had run the audit green). A live-feed job's result
doesn't depend on what's in the stack, so a shared stack proves nothing for it.

The stack chain is rebuilt from base shas: a candidate whose base sha equals another
candidate's group head sha sits behind it. From head-branch data alone the chain may be
partial. When the classifier can't decide between `poisoner` and `main_regression` it
returns `unknown` (no brake), never a brake class.

## 5. Correction

### 5a. Alert

`notify_event("ci_systemic_red", …, fanout_when_unconfigured=True)` is the same path
`deploy_failed` uses, so it reaches the owner's phone with no rule configured. One push
on open (title `KinDash: security: audit red on 4 PRs (external feed, queue braked)`)
and one on clear. Plus a feed event and an actionable card (Mute · Clear brake ·
Dismiss), following `DeployMonitorActionProvider`.

### 5b. Incident issue

One GitHub issue per incident fingerprint, labeled `ci-doctor` + `ci-systemic`
(created if missing), through `issue_filing.decide/apply`: comment on recurrence,
recovery comment on clear, regression link when a closed one recurs.

The body holds: class, job, signature excerpt, the distinct PRs and run links,
first/last failure times (UTC and America/New_York), failed runs/hour, the brake state,
and the recommended owner action. For `poisoner` that includes the exact lock-gated
dequeue command for the owner to run; the Doctor does not run it.

### 5c. Brake

The brake rides on the incident issue itself. For a brake class the Doctor adds the
label `ci:systemic-red` and a body marker:

```
<!-- viberpit:systemic-brake {"job":"security: audit","class":"external_feed",
 "incident":"<fingerprint>","set_at":"2026-10-31T01:01:00Z",
 "expires_at":"2026-10-31T05:01:00Z","by":"viberpit-cicd-doctor"} -->
```

The first draft used a repo Actions variable instead. That doesn't work: the `viberpit`
GitHub App on cairnea has no Actions-variables permission (verified 2026-10-31 via
`orgs/cairnea/installations`), so the write would fail silently. Issues need no new
scope, show up on the owner's phone, and closing the issue is a kill switch anyone can
reach.

- **TTL** `systemic_brake_ttl_min` (default 240). Each tick while the incident is live,
  the Doctor edits the issue body to push `expires_at` forward. If ViberPit stops, the
  brake lapses by itself (fail-open).
- **Lifted** (label removed, marker deleted) when the incident clears (§3c), on the
  card's Clear brake action, at `systemic_brake_max_min`, or when the class is
  re-evaluated to a non-brake class. The issue closes only on clear.
- **Verified.** After each brake write the Doctor reads the issue back. A failed write
  shows in the alert as "brake NOT set" and is retried next tick. It is never reported
  as set.
- **Brake classes:** only `external_feed` and `main_regression`.

The KinDash consumer is cairnea/KinDash#20319. `main-red-check.sh` lists open
`ci:systemic-red` issues **before** the gate runs. A marker that names a job and
expires in the future, at most 12 h out, emits
`main_red=true reason=systemic-red jobs=<job>`. Anything else is ignored (fail-open).

The effect is exactly today's freeze: ops-auto-merge stops arming and force-enqueuing,
the retry listeners stand down, and the rebuild cap keeps running. A PR that fixes the
red job still lands once its author arms auto-merge, and its green candidate clears the
incident. The freeze also stops `kindash-automerge[bot]` from arming PRs, so a fix
written by a human gets armed by hand. That is how main-red already behaves.

Why it can't deadlock:
- The brake never fires for `poisoner`, the shape that deadlocked #17541.
- It doesn't touch entries already in the queue.
- A manually armed fix still enqueues.
- TTL, the green-candidate clear and the hard ceiling each lift it without a human.

### 5d. Fix session

`ctx.spawn_or_join_session(origin_ref=<repo>#systemic:<fingerprint>, dedupe_key=…)`
in the affected repo. The seed carries the diagnosis: class, job, signature excerpt,
example run URLs, the class-specific instruction from §4b, and "never mute or skip the
check; never mutate the merge queue; open a PR and arm auto-merge".

- **One session per incident, for its whole lifetime.** The incident state records the
  session id, and a finished session does not free a slot for a second one. A recurrence
  after a clear is a new incident.
- **Daily cap:** `systemic_max_fix_sessions_per_day` (default 3) per repo.
- **Existing work first:** if a non-Doctor PR that touches the failing job's inputs is
  already open (e.g. KinDash #20310/#20311 today), the seed lists it and tells the
  session to finish or review that PR rather than open a parallel one.

## 6. Waste signal and the AWS bridge

### 6a. Queue thrash finding

Per repo per tick: failed merge_group runs in the last 60 min vs the baseline = the
median hourly failed count over the last 14 days (from `ci_run`, already ingested).
Finding `queue_thrash` (alert-only, attached to any live incident) when failed/hour
≥ max(`thrash_min_failed_per_hour` = 6, 3 × baseline) **and** the failure ratio over
the window is ≥ 80% across ≥ 8 runs. The alert states the failed runs/hour and the
runner-minutes those runs consumed (sum of job durations) — the same unit AWS bills.

### 6b. AWS spend alarm bridge

Config `aws_alarm_prefix` (default empty = off) and `aws_profile`. When set, every
5 min the loop runs `aws cloudwatch describe-alarms --alarm-name-prefix <prefix>
--state-value ALARM` (read-only). An alarm entering ALARM → push `ci_spend_alarm`
once + feed event, attached to the live incident if any; leaving ALARM → feed event.
The alarms themselves (rolling-24h CI vCPU-hours, daily budget) are defined in the
infra repo (cairnea/infra, `terraform/ops`). ViberPit has no public ingress, so it
polls instead of subscribing SNS.

## 7. PR Doctor residual blind spot

#3159 made never-finishing checks `stuck_checks` after 120 min. Still open: a PR
whose required check concluded `failure` while a *sibling* check is `pending` or
`cancelled` reads `ci_red_is_unproven` → no verdict until the sibling resolves or
120 min pass. Change: a `failure` or `timed_out` on a check that is not an
aggregate counts as evidence on its own. The aggregates are `ROLLUP_CHECKS` in
`pr_doctor_engine.py`: `ci` and `ci-gate` (KinDash), plus `lint-and-test` (ViberPit's own
`if: always()` aggregator, the #2213 shape). With such a check present,
`ci_red_is_unproven` returns False. Rollup-only failures
keep today's behavior (the #2218 case: a rollup red because its inputs were
cancelled).

## 8. Modes and config

`CicdDoctor.modes` becomes `("off", "propose", "correct")`.
- `propose`: v1 analyzers + systemic detection with alert + incident issue only.
- `correct`: adds fix-session dispatch and the brake, each behind its own bool
  (`systemic_dispatch_fix`, `systemic_brake`, both default true) so either can be
  switched off without leaving `correct`.

New config fields (all with defaults above): `systemic_window_min`,
`systemic_min_prs`, `systemic_rollup_jobs`, `systemic_external_feed_jobs`,
`systemic_quiet_clear_min`, `systemic_brake_ttl_min`, `systemic_brake_max_min`,
`systemic_dispatch_fix`,
`systemic_brake`, `systemic_max_fix_sessions_per_day`, `thrash_min_failed_per_hour`,
`aws_alarm_prefix`, `aws_profile`. The engine policy stays `CLAUDE_ONLY`.

Incident state lives in `automation_target_state` under the `cicd-doctor` context
(key `systemic:<repo>:<fingerprint>`), like Deploy Monitor — no new table.

## 9. Testing

- Pure evaluator: table tests for rule (C)/(M), distinct-PR counting, rollup
  exclusion, the clear rules, and every classification row — including the three
  shapes the KinDash memory demands: red main, poisoned front entry (must classify
  `poisoner` and never brake), breaking merge whose push:main was cancelled.
- A replay fixture built from the real 10-30/31 KinDash runs (job names, PR
  branches, timestamps): asserts the open time ≈ 01:01 UTC, class `external_feed`,
  brake set, and clear on the first green candidate.
- Loop: seam-injected (fetch_runs, fetch_jobs, fetch_log, issue read/write/label,
  notify, spawn, clock) — dedupe of alerts and sessions, TTL renewal,
  fail-open on API errors (no brake set on a failed read), brake lifted on clear,
  daily fix-session cap.
- PR Doctor: classify tests for failure-plus-pending sibling and rollup-only red.
- KinDash companion (#20319): bats cases in `main-red-wiring.bats` for a valid,
  expired, over-12 h and malformed marker, no issue, and the query scope.

## 10. Calls made by the designer (not owner rulings)

- Thresholds: 3 distinct PRs in 60 min, 240-min TTL, 600-min brake ceiling, 90-min
  quiet clear, 3 fix sessions per day, thrash at ≥ 6/h and 3× baseline.
- The 120 s cadence.
- Carrying the brake on an issue label, replacing a repo variable the App can't write.
- No fix session for `poisoner`.
- The classification table and its order.
- Polling CloudWatch rather than an SNS webhook.
- Folding the work into `cicd-doctor` rather than a new automation.

## 11. Delivery

1. ViberPit PR: this spec + plan + implementation, TDD.
2. KinDash companion PR cairnea/KinDash#20319: `main-red-check.sh` honors the
   `ci:systemic-red` issue. Ships before the Doctor is switched to `correct`.
3. Infra PR cairnea/infra#78:
   - CloudWatch alarm `cairnea-ci-vcpu-hours-24h` with SNS topic `cairnea-ci-spend-alarms`
   - NodePool CPU limits (320 / 448)
   - `cairnea-ci-daily` $120 budget

   Applied only on the owner's explicit approval.
4. Enabling `correct` mode on the live ViberPit is the owner's toggle.
