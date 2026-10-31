# CI/CD Doctor Systemic Red Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI/CD Doctor detects a systemic merge-queue red within minutes and corrects it: alert, incident issue, one diagnosed fix session, and a self-clearing brake that KinDash's main-red freeze honors.

**Architecture:** A pure evaluator (`server/automations/cicd/systemic.py`) turns recent merge_group and push:main runs into hits, a classification and issue text. A seam-injected `SystemicRedLoop` (`server/automations/cicd/systemic_loop.py`) ticks every 120 s under the `cicd-doctor` automation. It keeps incident state in `automation_target_state` and performs every side effect (gh issue, label, body marker, push notification, session spawn) through injected callables. The orchestrator wires real seams the same way it wires `CicdDoctorLoop` and `DeployMonitorLoop`.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy async, `gh` CLI via `gitrepos._run_gh`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-31-cicd-doctor-systemic-red-design.md` (read it first; section numbers below refer to it).

## Global Constraints

- Never mutate the merge queue (no enqueue, dequeue or merge calls). The brake is only the incident issue's `ci:systemic-red` label plus the body marker `<!-- viberpit:systemic-brake {json} -->`, and the JSON keys are exactly `job, class, incident, set_at, expires_at, by` (`by` = `"viberpit-cicd-doctor"`). Timestamps use the `YYYY-MM-DDTHH:MM:SSZ` form; the KinDash consumer (cairnea/KinDash#20319) rejects any other form and any `expires_at` more than 12 h out.
- Defaults (config knobs): `systemic_window_min=60`, `systemic_min_prs=3`, `systemic_rollup_jobs="ci,ci-gate"`, `systemic_external_feed_jobs="^security: audit$"`, `systemic_quiet_clear_min=90`, `systemic_brake_ttl_min=240`, `systemic_brake_max_min=600`, `systemic_dispatch_fix=True`, `systemic_brake=True`, `systemic_max_fix_sessions_per_day=3`, `thrash_min_failed_per_hour=6`, `aws_alarm_prefix=""`, `aws_profile=""`.
- Modes: `("off", "propose", "correct")`. `propose` = alert + issue only. `correct` adds dispatch and brake, each behind its bool.
- Brake classes: `external_feed` and `main_regression` only. `poisoner`, `infra` and `unknown` never brake, and only `external_feed` and `main_regression` dispatch a fix session.
- Fail-open everywhere: a failed GitHub read never opens or renews a brake, and a tick exception never crashes the loop.
- Repo rules: DB clock via `server/db/clock.py`; never `task.cancel()` by hand (use `server/aio.cancel_and_reap`); new model columns nullable (this plan adds none); US English copy; commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; write commit messages to a file (`-F`).
- Gate before push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile --timeout=300`, using the main checkout's venv `/home/tony/GitHub/viberpit/.venv/bin/python` with `env -u VIBERPIT_TAILNET_ALLOWLIST -u VIBERPIT_DAEMON_ROLE VIBERPIT_DEV=1`, output tee'd to `/tmp/vp-*.log`.

## Review Focus

1. **GitHub returns in-progress runs whose jobs list is partial or empty.** Count only jobs whose `status == "completed"`; a run with no completed failing job adds nothing. Test in Task 1 (`test_in_progress_run_counts_only_completed_failed_jobs`).
2. **A candidate rebuilt several times for ONE PR.** It must not reach the 3-PR threshold by itself (this is the rebuild cap's domain). Test in Task 1 (`test_same_pr_rebuilt_three_times_is_not_systemic`).
3. **ViberPit restarts mid-incident.** State reloads from `automation_target_state`; no second push alert, no second issue, no second fix session. Test in Task 3 (`test_restart_resumes_incident_without_duplicate_side_effects`).
4. **The issue-body edit that renews `expires_at` fails.** Report "brake NOT set" in the feed event, never claim the brake is set, and retry next tick. Test in Task 3 (`test_failed_brake_write_is_not_reported_as_set`).
5. **The owner closes the incident issue by hand while the incident is live.** Treat it as a manual brake lift: don't re-label or reopen it for this incident, and keep alerting only on clear. Test in Task 3 (`test_owner_closed_issue_is_not_reopened_or_rebraked`).

---

### Task 1: Pure systemic evaluator

**Files:**
- Create: `server/automations/cicd/systemic.py`
- Create: `server/tests/test_cicd_systemic.py`
- Create: `server/tests/fixtures/cicd_systemic_2026_10_31.json` (replay data; build it from the real KinDash runs with the command in Step 1)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class JobObs: name: str; conclusion: str; status: str; completed_ts: float | None; job_id: int`
  - `@dataclass(frozen=True) class RunObs: run_id: int; workflow_id: int; event: str; head_branch: str; head_sha: str; created_ts: float; status: str; conclusion: str; jobs: tuple[JobObs, ...]`
  - `def normalize_job(name: str) -> str` (segment after the last `" / "`)
  - `def pr_of_branch(head_branch: str) -> tuple[int, str] | None` (parses `gh-readonly-queue/<base>/pr-<N>-<sha>` → `(N, base_sha)`)
  - `@dataclass(frozen=True) class Hit: job: str; rule: str  # "C" | "M"; prs: tuple[int, ...]; first_failed_ts: float; last_failed_ts: float; failing_runs: tuple[int, ...]; failing_job_ids: tuple[int, ...]`
  - `def detect(candidates: list[RunObs], mains: dict[int, RunObs], *, now: float, window_s: float, min_prs: int, rollups: frozenset[str]) -> list[Hit]` (mains keyed by workflow_id)
  - `def is_cleared(job: str, opened_ts: float, candidates: list[RunObs], mains: dict[int, RunObs], *, now: float, quiet_s: float, rollups: frozenset[str]) -> bool`
  - `def signature(log_text: str) -> tuple[str, str]`, returning `(normalized_line, raw_excerpt≤40 lines)`
  - `def fingerprint(repo: str, job: str, normalized_sig: str) -> str` (stable sha1 hex[:12])
  - `def classify(hit: Hit, candidates: list[RunObs], mains: dict[int, RunObs], *, normalized_sig: str, external_feed_re: str) -> str` (one of `external_feed|infra|poisoner|main_regression|unknown`)
  - `BRAKE_CLASSES = frozenset({"external_feed", "main_regression"})`
  - `def brake_marker(*, job: str, klass: str, incident: str, set_at: float, expires_at: float) -> str`
  - `def strip_brake_marker(body: str) -> str`
  - `def thrash(failed_last_hour: int, hourly_failed_14d: list[int], ratio: float, runs_in_window: int, *, min_failed: int) -> bool` (rule §6a: failed ≥ max(min_failed, 3×median) and ratio ≥ 0.8 over ≥ 8 runs)

- [ ] **Step 1: Build the replay fixture** from the real incident. Write a throwaway script outside the repo that calls `gh api repos/cairnea/KinDash/actions/runs?event=merge_group&created=2026-10-30T20:00:00Z..2026-10-31T11:30:00Z&per_page=100` (paginate), then fetches each run's jobs, and saves `[{run_id, workflow_id, event, head_branch, head_sha, created_at, status, conclusion, jobs:[{id,name,status,conclusion,completed_at}]}]`. Add the push:main run from 2026-10-30T21:53Z with its jobs, under key `"main"`. Keep only the fields above.

- [ ] **Step 2: Write the failing tests** in `server/tests/test_cicd_systemic.py`:
  - `test_normalize_job_strips_reusable_prefix`: `"static-checks / security: audit"` → `"security: audit"`.
  - `test_pr_of_branch_parses_queue_branch_and_rejects_others`: `"gh-readonly-queue/main/pr-20310-3fb396c0"` → `(20310, "3fb396c0")`; `"main"` → `None`.
  - `test_three_distinct_prs_failing_one_job_is_rule_c` (synthetic: PRs 1, 2, 3 each fail `security: audit` within 60 min → one `Hit(job="security: audit", rule="C", prs=(1,2,3))`).
  - `test_same_pr_rebuilt_three_times_is_not_systemic` (Review Focus 2).
  - `test_rollup_jobs_never_hit` (`ci`, `ci-gate` failing on 5 PRs → `[]`).
  - `test_later_green_candidate_for_the_job_prevents_hit`.
  - `test_in_progress_run_counts_only_completed_failed_jobs` (Review Focus 1).
  - `test_main_failure_plus_one_later_candidate_is_rule_m`.
  - `test_replay_2026_10_31_opens_on_security_audit_by_0110_utc`: load the fixture, step `now` over the window in 120 s increments, assert the first tick that yields a `security: audit` hit is at or before `2026-10-31T01:10:00Z`, and `classify(...) == "external_feed"`.
  - `test_classify_external_feed_wins_over_shared_stack` (every candidate stacked on PR 9, job `security: audit`, main green → `external_feed`).
  - `test_classify_poisoner_when_only_stacks_with_p_fail_and_main_green` (job `docs: conformance`, every failing stack contains PR 9, a candidate without PR 9 ran it green → `poisoner`).
  - `test_classify_infra_on_startup_failure_or_runner_signature` (sig `"The runner has received a shutdown signal"` → `infra`).
  - `test_classify_main_regression_on_rule_m`.
  - `test_classify_ambiguous_stack_is_unknown_not_a_brake_class`.
  - `test_signature_prefers_first_error_line_and_normalizes` (input with `##[error]Found 10 vulnerabilities (GHSA-abcd-1234-wxyz) at 2026-10-31T01:00:00Z sha 3fb396c0aa` → normalized line has `N`, no timestamp, no sha; GHSA id kept).
  - `test_fingerprint_is_stable_and_repo_scoped`.
  - `test_brake_marker_round_trip_matches_kindash_contract`: marker matches regex `<!-- viberpit:systemic-brake (\{.*?\}) -->`, its JSON has exactly the six keys, and both timestamps match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`; `strip_brake_marker` removes it and leaves the rest of the body unchanged.
  - `test_cleared_by_later_green_run_of_the_job` and `test_not_cleared_by_quiet_with_zero_candidates` and `test_cleared_by_quiet_when_a_candidate_completed_the_job` (§3c).
  - `test_thrash_rule` (6 failed at baseline median 1, ratio 0.9 over 10 runs → True; ratio 0.5 → False; 5 failed → False).

- [ ] **Step 3: Run** `pytest server/tests/test_cicd_systemic.py -v`. Expected: FAIL (module missing).

- [ ] **Step 4: Implement `server/automations/cicd/systemic.py`.** The classifier follows the §4b order exactly (external_feed → infra → poisoner → main_regression → unknown). Rebuild the stack chain by mapping each candidate's `base_sha` to the candidate whose `head_sha` equals it; a candidate's stack is the PR set reached by walking that chain. If the walk breaks (an unknown base that isn't main's head), mark the stack partial. When partial stacks leave `poisoner` versus `main_regression` undecided, return `unknown`. Infra signature list per §4b. No I/O.

- [ ] **Step 5: Run** the tests. Expected: PASS. Also run `ruff check server/automations/cicd/systemic.py` and `mypy server/automations/cicd/systemic.py --ignore-missing-imports`: clean.

- [ ] **Step 6: Commit** `feat(cicd-doctor): pure systemic-red evaluator and 2026-10-31 replay`.

### Task 2: GitHub and AWS read/write seams

**Files:**
- Modify: `server/github/actions_api.py` (add functions)
- Modify: `server/github/ci_issues.py` (add functions)
- Create: `server/github/aws_alarms.py`
- Test: `server/tests/test_cicd_actions_api.py` (extend), `server/tests/test_ci_issues_systemic.py`, `server/tests/test_aws_alarms.py`

**Interfaces:**
- Consumes: `RunObs`, `JobObs` from Task 1.
- Produces:
  - `actions_api.list_recent_runs(repo: str, *, event: str, since_ts: float, branch: str | None = None, run=None) -> list[RunObs]`. Any status, `created>=` bound always present, up to 3 pages of 100. Jobs come from `/runs/{id}/jobs?per_page=100` with `status`/`conclusion`/`completed_at`. An `in_progress` run's jobs are always re-fetched. A completed run's jobs are fetched once and cached in a module-level `dict[(repo, run_id, run_attempt), tuple[JobObs, ...]]` bounded to 2,000 entries (evict oldest).
  - `actions_api.job_log_text(repo: str, job_id: int, *, run=None) -> str` (`/actions/jobs/{id}/logs`; returns `""` on any failure; caps at the last 2 MB).
  - `ci_issues.edit_issue_body(repo: str, number: int, body: str, *, run=gitrepos._run_gh) -> bool`
  - `ci_issues.add_label(repo: str, number: int, name: str, *, run=gitrepos._run_gh) -> bool` (calls `_ensure_label` first)
  - `ci_issues.issue_snapshot(repo: str, number: int, *, run=gitrepos._run_gh) -> dict | None` (`{"state", "body", "labels": [..]}`; None on failure)
  - `ci_issues.reopen_issue` if it does not already exist (check first; reuse it if it does)
  - `aws_alarms.alarms_in_alarm(prefix: str, profile: str, *, run=subprocess.run) -> list[dict] | None`. Runs `aws cloudwatch describe-alarms --alarm-name-prefix <prefix> --state-value ALARM --output json [--profile p] --region us-east-1` and returns `[{name, reason, updated}]`. Returns None when the CLI is missing or fails, and returns `[]` without running anything when the prefix is empty.

- [ ] **Step 1: Write failing tests** with a fake `run` that records argv and returns canned `CompletedProcess`:
  - `test_list_recent_runs_always_bounds_created_and_includes_in_progress`: argv contains `created=%3E%3D<iso>` (the URL-encoded `created>=` bound) and no `status=completed`.
  - `test_completed_run_jobs_fetched_once_in_progress_refetched`
  - `test_job_log_text_returns_empty_on_failure`
  - `test_edit_issue_body_and_add_label_argv` (`api -X PATCH repos/<r>/issues/<n> -f body=…`; `api repos/<r>/issues/<n>/labels -f labels[]=ci:systemic-red`)
  - `test_issue_snapshot_parses_state_body_labels`
  - `test_alarms_in_alarm_empty_prefix_runs_nothing`, `test_alarms_in_alarm_parses_metric_alarms`, `test_alarms_in_alarm_cli_missing_returns_none`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Follow the existing `_run_gh_for(repo)` App-token pattern for Actions reads, and the ambient `gitrepos._run_gh` default for issue writes (same as `create_issue`).
- [ ] **Step 4: Run** them. Expected: PASS. Then ruff + mypy on the touched files.
- [ ] **Step 5: Commit** `feat(cicd-doctor): run/job/log/issue/alarm seams for systemic red`.

### Task 3: `SystemicRedLoop`, the incident state machine

**Files:**
- Create: `server/automations/cicd/systemic_loop.py`
- Test: `server/tests/test_cicd_systemic_loop.py`

**Interfaces:**
- Consumes: Task 1 evaluator, and Task 2 functions (injected, never imported directly by the loop).
- Produces: `class SystemicRedLoop` with
  ```python
  def __init__(self, ctx, *, read_config, list_repos,
               fetch_runs: Callable[[str, str, float, str | None], Awaitable[list[RunObs]]],  # (repo, event, since_ts, branch)
               fetch_log: Callable[[str, int], Awaitable[str]],
               file_issue: Callable[[str, str, str, list[str]], Awaitable[int | None]],
               comment_issue: Callable[[str, int, str], Awaitable[bool]],
               edit_issue_body: Callable[[str, int, str], Awaitable[bool]],
               add_label: Callable[[str, int, str], Awaitable[bool]],
               remove_label: Callable[[str, int, str], Awaitable[bool]],
               close_issue: Callable[[str, int], Awaitable[bool]],
               issue_snapshot: Callable[[str, int], Awaitable[dict | None]],
               notify: Callable[..., Awaitable[None]],          # notify(event_class, title, body, *, repo, url)
               spawn_fix: Callable[[str, str, str], Awaitable[str | None]],  # (repo, origin_ref, prompt) -> session id
               failed_hourly_counts: Callable[[str], Awaitable[tuple[int, list[int], float, int]]],
               alarms_in_alarm: Callable[[dict], Awaitable[list[dict] | None]],
               clock: Callable[[], float]) -> None
  async def tick(self) -> None   # lock-guarded, never raises
  ```
  It also sets `ctx.cicd_systemic_counts = {"incidents": int, "braked": int, "thrash": bool, "alarms": int}`.
  State key: `systemic:<repo>:<fingerprint>`. Value: `{job, class, opened_ts, first_failed_ts, prs, runs, signature, excerpt, issue_number, brake_set_ts, brake_expires_ts, brake_state: "none"|"set"|"failed"|"lifted"|"ceiling", session_id, alerted_open: bool, owner_closed: bool}`. Daily dispatch counter key: `systemic-dispatch:<repo>:<YYYY-MM-DD>`.

Per tick, per repo (mode != off):
1. `fetch_runs(repo, "merge_group", now - window_s, None)` and `fetch_runs(repo, "push", now - 24h, "main")`. Use the newest completed push run per workflow_id as `mains`. Any exception → skip the repo this tick and change nothing.
2. Live incidents: if `is_cleared(...)`, then: remove the label, strip the marker, post a recovery comment, close the issue, notify `ci_systemic_red` "cleared", and delete the state. Otherwise, if `issue_snapshot` shows `state == "closed"`, set `owner_closed`, lift the brake in state only, and do nothing more. Otherwise renew the brake (below).
3. New hits from `detect(...)` not matching a live incident's job: `fetch_log` for the newest failing job, then `signature`, `fingerprint`, `classify`. Open the incident: `file_issue` with labels `["ci-doctor", "ci-systemic"]` and the body from `issue_body(...)` (Task 1 may host the pure body builder; put it in `systemic.py` as `issue_body(state: dict, *, repo: str) -> str` with UTC + America/New_York times, the PR list, run links, class, recommended action, and for poisoner the owner's dequeue command as a quoted code block). Then notify once (`alerted_open`).
4. Correct mode only:
   - **Brake** for `BRAKE_CLASSES` when `systemic_brake`: `add_label("ci:systemic-red")` + `edit_issue_body(strip_brake_marker(body) + brake_marker(expires_at = now + ttl))`, then `issue_snapshot` to verify. Verified → `brake_state="set"`; else `"failed"`, and the feed event says "brake NOT set". Each tick the live incident renews `expires_at` until `now - brake_set_ts >= max_s`. At that point lift (remove the label, strip the marker), set `brake_state="ceiling"`, notify once "brake lifted after 10 h; incident still live", and never re-brake.
   - **Dispatch** for `BRAKE_CLASSES` when `systemic_dispatch_fix`, `session_id` is unset, and the daily counter < cap: `spawn_fix(repo, f"{repo}#systemic:{fp}", prompt)` using `fix_prompt(state, *, repo) -> str` (pure, in `systemic.py`, with the §4b class instruction and the never-mute / never-mutate-queue lines). It also tells the session to first search open PRs that already touch the failing job's inputs, and to finish or review such a PR instead of opening a parallel one (§5d). Record `session_id` and bump the counter.
5. Thrash: `failed_hourly_counts(repo)` → `thrash(...)`. The first time it's true → `ctx.emit("warn", ...)` + notify `ci_systemic_red` with the failed runs/hour and runner-minutes in the body. Gate it by a state key `systemic-thrash:<repo>` that resets once thrash goes false.
6. AWS: at most every 300 s, `alarms_in_alarm(config)`; a newly seen alarm name → notify `ci_spend_alarm` once, keyed in state `systemic-alarm:<name>`. Delete the key when it leaves ALARM.

- [ ] **Step 1: Write failing tests** with in-memory fakes for every seam and a fake ctx (copy the shape used in `server/tests/test_deploy_monitor_*.py`):
  - `test_propose_mode_alerts_and_files_once_but_never_brakes_or_dispatches`
  - `test_correct_mode_external_feed_sets_verified_brake_and_dispatches_once`
  - `test_poisoner_never_brakes_and_never_dispatches`
  - `test_brake_ttl_renewed_each_tick_and_lifted_at_ceiling_without_rebrake`
  - `test_clear_lifts_brake_comments_closes_and_notifies_once`
  - `test_restart_resumes_incident_without_duplicate_side_effects` (Review Focus 3: a new loop instance on the same ctx state)
  - `test_failed_brake_write_is_not_reported_as_set` (Review Focus 4)
  - `test_owner_closed_issue_is_not_reopened_or_rebraked` (Review Focus 5)
  - `test_daily_dispatch_cap`
  - `test_fetch_failure_changes_nothing`
  - `test_thrash_alerts_once_until_it_resets`
  - `test_new_aws_alarm_notifies_once`
  - `test_off_mode_does_nothing`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the loop. All side effects happen after state is computed (same two-pass discipline as `CicdDoctorLoop._reconcile`). Write state with `ctx.set_target_state` after every successful side effect, so a crash never replays one.
- [ ] **Step 4: Run** them. Expected: PASS. Then ruff + mypy.
- [ ] **Step 5: Commit** `feat(cicd-doctor): systemic-red loop (alert, incident issue, brake, fix dispatch)`.

### Task 4: Wire it in: modes, config, events, orchestrator, card status

**Files:**
- Modify: `server/automations/cicd_doctor.py` (modes, `config_schema` fields from Global Constraints, `status()` appends "N systemic incident(s), braked" from `ctx.cicd_systemic_counts`; update the module docstring to name the v2 boundary)
- Modify: `server/sessions/notify_rules.py` (`EVENT_CLASSES` += `"ci_systemic_red"`, `"ci_spend_alarm"`), plus any web list of event classes (`grep -rn "deploy_failed" web/src`) so the rules UI can target them
- Modify: `server/sessions/orchestrator.py`: construct `SystemicRedLoop` next to `_cicd_loop`, sharing `self._cicd_ctx` and `self._cicd_read_config` and `self._cicd_list_repos`. Add `_cicd_systemic_task` to the task-name tuples at the two places `_cicd_task` appears (around lines 944 and 3676) and start it at `interval=120.0` beside `cicd-doctor-loop`. Real seams: Task 2 functions via `asyncio.to_thread`; `notify` → `server.sessions.push.notify_event(..., fanout_when_unconfigured=True)`; `spawn_fix` → `self._cicd_ctx.spawn_or_join_session(origin_ref=..., dedupe_key=origin_ref, repo=repo, prompt=prompt)`, returning `rec.id` or None (check `spawn_or_join_session`'s `create_kwargs` names against an existing caller such as the pm_onboard spawn near line 12065); `failed_hourly_counts` → a query over `CiRun` (event `merge_group`, last 14 days, hourly buckets of `conclusion == "failure"`, plus the last-hour ratio and count); `alarms_in_alarm` → `aws_alarms.alarms_in_alarm(config["aws_alarm_prefix"], config["aws_profile"])`.
- Modify: `CicdDoctorActionProvider` (orchestrator line ~1328 registers it; find its class with `grep -rn "class CicdDoctorActionProvider" server`). Add actionable items for live systemic incidents and the kinds `clear_brake` (lift the brake now and set `brake_state="lifted"` so it is never re-applied for this incident) and `dismiss` (lift the brake, close the issue with a dismiss comment, delete the state). Add `SystemicRedLoop.action_result(key: str, kind: str) -> str` for the provider to call. Tests: `test_clear_brake_action_lifts_and_never_rebrakes`, `test_dismiss_action_closes_and_forgets`.
- Modify: `_cicd_read_config`, which currently resolves an enabled row with NULL mode to the first non-off mode (`propose`). Keep that: `correct` must be chosen explicitly.
- Test: `server/tests/test_cicd_doctor_automation.py` (modes, schema keys, status text), `server/tests/test_cicd_systemic_wiring.py` (orchestrator builds the loop with the shared ctx; task name `cicd-systemic-loop`; teardown reaps it), and `notify_rules` tests for the new classes.

- [ ] **Step 1: Write failing tests:**
  - `test_modes_include_correct`
  - `test_systemic_config_fields_and_defaults` (every key and default from Global Constraints)
  - `test_status_reports_systemic_incidents`
  - `test_event_classes_include_ci_systemic_red_and_ci_spend_alarm`
  - `test_orchestrator_wires_systemic_loop_on_cicd_ctx`
  - `test_systemic_task_is_reaped_on_shutdown`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the wiring.
- [ ] **Step 4: Run** them, plus the existing `server/tests/test_cicd_*.py` and `test_automations_api.py`. Expected: PASS. Run the web `npm run typecheck && npm run lint && npm run test` in `web/` only if a web file changed (symlink `web/node_modules` from `/home/tony/GitHub/viberpit/web/node_modules`).
- [ ] **Step 5: Commit** `feat(cicd-doctor): correct mode wiring for systemic red`.

### Task 5: PR Doctor: a real failure is not hidden by a pending sibling (§7)

**Files:**
- Modify: `server/automations/pr_doctor_engine.py` (`ci_red_is_unproven`)
- Test: `server/tests/test_pr_doctor_engine.py`

**Interfaces:**
- Produces: `ROLLUP_CHECKS = frozenset({"ci", "ci-gate"})` in `pr_doctor_engine.py`. `ci_red_is_unproven(info)` returns False when any `checks_detail` entry has conclusion `failure` or `timed_out` and a name, normalized with `systemic.normalize_job`, that is not in `ROLLUP_CHECKS`.

- [ ] **Step 1: Write failing tests:**
  - `test_required_failure_with_pending_sibling_is_failing_ci` (classify → `"failing_ci"`)
  - `test_rollup_only_failure_with_cancelled_inputs_stays_unproven` (the #2218 shape is unchanged)
- [ ] **Step 2: Run** them. Expected: the first FAILS, the second PASSES.
- [ ] **Step 3: Implement.** Check whether `PullRequestInfo.checks_detail` entries can carry `timed_out`, or whether conclusions are already normalized to `failure`. Read `server/github/pr_status.py` and match it.
- [ ] **Step 4: Run** `pytest server/tests/test_pr_doctor_engine.py server/tests/test_pr_doctor_step.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `fix(pr-doctor): a real check failure is evidence even with a pending sibling`.

### Task 6: Docs, full gate, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-07-03-cicd-doctor-design.md`: one line under Status, "v2 systemic-red path supersedes the propose-only boundary for systemic incidents: see 2026-10-31-cicd-doctor-systemic-red-design.md".
- Modify: `deploy/README.md` (or the automations runbook if one exists; grep "cicd-doctor"): how to switch to `correct`, how to lift a brake (close the issue or use the card's Clear brake action), and `aws_alarm_prefix=cairnea-ci-` with `aws_profile` for the infra alarms.

- [ ] **Step 1:** Write the doc edits.
- [ ] **Step 2:** Run the full gate from Global Constraints and confirm all four steps are green (logs in `/tmp/vp-ruff.log`, `/tmp/vp-mypy.log`, `/tmp/vp-core.log`, `/tmp/vp-server.log`).
- [ ] **Step 3:** Commit, push, open the PR to `master` (body from a file, ending with the Claude Code trailer), mark it ready, and arm `--squash --auto`.
