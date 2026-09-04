---
slug: cicd-pipeline-merge-queue
title: CI/CD Pipeline & Merge Queue
state: live
links:
  code:
    - server/pipeline/stage.py
    - server/pipeline/eta.py
    - server/pipeline/merge_train.py
    - server/pipeline/merge_queue_store.py
    - server/pipeline/merge_sim.py
    - server/pipeline/required.py
    - server/pipeline/timeline.py
    - server/pipeline/notifier.py
    - server/pipeline/dashboard_state.py
    - server/pipeline/deploy_status.py
    - server/pipeline/ci_graph.py
    - server/github/pr_poller.py
    - server/github/pr_status.py
    - server/github/merge_queue.py
    - server/github/actions_api.py
    - server/github/ci_issues.py
    - server/github/app_auth.py
    - server/automations/pr_doctor.py
    - server/automations/pr_doctor_engine.py
    - server/automations/cicd_doctor.py
    - server/api/pipeline.py
    - web/src/pipeline/PipelineView.tsx
    - web/src/pipeline/MetroTrack.tsx
    - web/src/pipeline/QueueTrain.tsx
  prs: [559, 572, 573, 588, 613, 623, 689, 614, 848, 472]
---

# CI/CD Pipeline & Merge Queue

**Problem** — When ccswitch runs autonomous development loops on itself, dozens of PRs
are in flight at once across several repos. A human (or an automation) cannot tell at a
glance which PRs are green, which are stuck in CI, how long until the next merge lands,
what is blocking the merge queue, or whether a merge has actually reached prod. GitHub's
own UI does not surface ETAs, merge-train progress, supersession, or a single
"is-this-shipped-yet" timeline, and polling it per-PR is expensive against the API budget.

**Target users** — The human operator watching the fleet dashboard (including on mobile and
on a read-only kiosk/wall display), and the autonomous automations (PR Doctor, backlog
runner, autopilot) that need machine-readable pipeline facts to decide what to do next.

**Desired outcome** — A live, always-current view of every tracked PR's pipeline stage
(parked / CI / ready / queued / merged, then qa / prod for deploy-configured repos), a
per-PR CI-progress ETA with confidence, a merge-queue / merge-train visualization showing
which group is building, its percent and remaining ETA, who is blocked, and p50/p90 merge
times — plus a deploy timeline that answers "has this merge reached QA / prod, or was it
superseded?" Green PRs land through the queue and reach prod without a human clicking
"merge" or "update branch." Failing PRs get diagnosed and, where safe, auto-recovered by
PR Doctor.

**Success metrics** — (proposed, no baseline recorded)
- Time-to-detect a red/stuck PR (dashboard shows it vs. it silently sitting).
- Merge-ETA accuracy: predicted p50/p90 merge time vs. actual.
- Share of stuck/out-of-date/conflicted PRs auto-recovered by PR Doctor without human action.
- GitHub API request budget stays within limit while polling the tracked (repo, branch) set.
- False deploy-status readings (e.g. replaying a merged train, mis-classified env) — target 0.

**Non-goals** — Not a replacement for GitHub Actions itself; ccswitch reads CI, it does not
run it (constitution I: never fork/patch upstream tooling). Merge-queue enrollment and the
merge itself are GitHub's; ccswitch observes the queue and arms auto-merge, it does not
re-implement the merge algorithm. CI/CD Doctor is propose-only in v1 — it files issues for
flaky-test / advisory-check findings but does not autonomously rewrite workflows.

**Notes** —
- Much of this is a faithful port of the "plotroom" pipeline replica (backend contract +
  ported core modules), landed in phases (#559 foundation, later #572/#573 sim + timeline +
  workflow-impact). `server/pipeline/*` modules are deliberately pure (no I/O) so the
  derivations are unit-testable; `server/github/*` and `server/api/pipeline.py` own the I/O.
- ETA is assembled from the required-check set (resolved by a BFS closure over the rollup
  job's `needs:` graph in the CI YAML — `required.py` + `ci_graph.py`), historical per-check
  durations, and runner-queue wait; merge-ETA is an analytic p50/p90 simulation over observed
  train durations and eject probability (`merge_sim.py`), not Monte Carlo.
- The merge-queue store writes a data-of-absence sentinel row on an empty queue so the
  dashboard drains instead of replaying a merged train for up to 24h (#585, #588).
- Deploy status handles supersession (#205) so a merge folded into a newer prod deploy is not
  double-counted as "awaiting prod"; env classification routes through `classify_env()`
  (#611) and namespaced envs were fixed in #574. Frames stream to the frontend over SSE from
  `/api/prs/stream` with per-frame sim / deploy / workflow-impact context.
- Authenticated status reads use a GitHub App (JWT + cached installation token, `app_auth.py`).
  Recent work is heavy on CI-cost/perf (folding rollups in SQL off the event loop: #791, #828,
  #835, #848) — a signal that ingest volume is a live operational concern.
- Known open gaps: #838 (cicd ingest drops whole batches on `ci_job` PK collisions — needs a
  dialect-agnostic upsert); #821/#822/#823 (serialize + cap + speed up the self-hosted server
  test suite that feeds this pipeline).

**Evidence** —
- Pipeline stages / ETA / queue: `server/pipeline/stage.py`, `server/pipeline/eta.py`,
  `server/pipeline/merge_train.py`, `server/pipeline/merge_queue_store.py`,
  `server/pipeline/merge_sim.py`, `server/pipeline/required.py`, `server/pipeline/ci_graph.py`.
- Deploy timeline / notifications / dashboard: `server/pipeline/timeline.py`,
  `server/pipeline/deploy_status.py`, `server/pipeline/notifier.py`,
  `server/pipeline/dashboard_state.py`.
- GitHub integration: `server/github/pr_poller.py`, `server/github/pr_status.py`,
  `server/github/merge_queue.py`, `server/github/actions_api.py`, `server/github/app_auth.py`,
  `server/github/ci_issues.py`.
- PR Doctor / CI Doctor: `server/automations/pr_doctor.py`,
  `server/automations/pr_doctor_engine.py`, `server/automations/cicd_doctor.py`.
- API + web: `server/api/pipeline.py`, `web/src/pipeline/PipelineView.tsx`,
  `web/src/pipeline/MetroTrack.tsx`, `web/src/pipeline/QueueTrain.tsx`.
- PRs: #559 (pipeline replica foundation), #572/#573 (mergeEtaSim + timeline + workflow
  impact), #588 (Ready/Merged split + stuck-enqueue surfacing), #613 (per-env deploy re-fetch),
  #623 (gate verdict on required checks only), #689 (PR Doctor async manual trigger),
  #614 (auto-repair stuck_enqueue via update-branch), #848 (SQL test-result rollup),
  #472 (CI/CD Doctor hardening).
- Open issues: #838, #821, #822, #823 (CI/ingest/test-suite infrastructure).
