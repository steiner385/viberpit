# CI/CD Doctor — Design (v1: Sense + Advise)

Date: 2026-07-03
Status: draft — pending user sign-off on graduation-gate defaults and prerequisite-PR sequencing.

## Purpose

A new registry automation (`cicd-doctor`) responsible for continuous improvement of all
CI/CD aspects NOT covered by PR Doctor: system-level health of the checks themselves —
flake rates, durations, cost/waste, and the lifecycle of advisory (non-blocking) checks
such as Playwright e2e, Lighthouse, and future visual regression. v1 is **propose-only**:
it senses and advises; it never mutates CI config or branch protection.

**Boundary vs PR Doctor:** PR Doctor is reactive and per-PR ("restore this failing PR
now"). CI/CD Doctor is proactive and per-system ("the e2e check has been 1.4% flaky over
30 days — graduate it to blocking"). The CI/CD Doctor never touches an individual PR.

## Part 1 — CI check inventory (researched 2026-07-03)

### 1a. Blocking checks (in CI today)

Branch protection on `master` requires the `lint-and-test` and `frontend` status checks
(`.github/workflows/ci.yml`), so everything inside those jobs blocks merges:

| # | Check | Job | JUnit today? |
|---|-------|-----|--------------|
| 1 | ruff (`ruff check src/ server/`) | lint-and-test | n/a (lint) |
| 2 | mypy (`mypy server/ --ignore-missing-imports`) | lint-and-test | n/a (types) |
| 3 | Core unit tests (`unittest discover -s tests`) | lint-and-test | NO |
| 4 | Server tests (`pytest server/tests/ -q`) | lint-and-test | NO |
| 5 | typecheck (`tsc --noEmit`) | frontend | n/a |
| 6 | eslint (`--max-warnings 0`) | frontend | n/a |
| 7 | vitest unit suite — **includes jest-axe a11y/aria tests** (`web/src/components/__tests__/a11y.test.tsx`) | frontend | NO |
| 8 | vite production build | frontend | n/a |

Research corrections to initial assumptions:
- **a11y/aria is already blocking** — jest-axe runs inside `vitest run`, not as a
  separate advisory check.
- **No suite emits machine-readable results** — test-level sensing is blind until JUnit
  emission is added (see prerequisite PR ①).

### 1b. Advisory checks — exist locally, absent from CI (onboard in v1)

- **Playwright e2e** — 3 specs in `web/e2e/` (`dashboard`, `transcript-virtualization`,
  `rotation-reconnect`). `web/playwright.config.ts` is already CI-aware (`retries: 2`,
  `workers: 1`, `forbidOnly` under `CI`); the `webServer` block is written but commented
  out. All 3 specs fully mock the backend via `page.route` interception — no real server
  needed. Onboarding cost: one new non-required `e2e` job (`npm ci`,
  `npx playwright install chromium --with-deps`, build, enable `webServer`, JUnit
  reporter, artifact upload).

### 1c. Aspirational checks — no tooling exists (Doctor proposes; NOT onboarded in v1)

- **Lighthouse** (perf/a11y audit of the built dashboard) — nothing installed. Once
  alive, the Doctor files a proposal issue to wire it as advisory.
- **Storybook / visual regression** — zero stories, zero config. Adopting Storybook is a
  product decision, not CI wiring; out of v1 scope entirely.

## Part 2 — Check state machine

Each `(repo, check)` pair occupies one of four states. **Ground truth for "blocking" is
read live from the GitHub branch-protection required-checks API** — never stored as a
parallel copy — so a manual flip in the GitHub UI is observed, not fought.

```
 absent ──onboard──▶ advisory ──graduate──▶ blocking
                        ▲  │                    │
                        │  └───retire──▶ gone   │
                        └───────demote──────────┘
```

| Transition | Meaning | Trigger |
|------------|---------|---------|
| onboard (absent→advisory) | Add job to CI but NOT to required checks. Red is visible but never blocks — `gh pr merge --auto` completes on required-checks-green, so a flaky advisory check cannot stall the pipeline. | Tooling exists locally but isn't in CI (e2e today). |
| graduate (advisory→blocking) | Add check to the branch-protection required list. | All graduation gates pass (Part 3). |
| demote (blocking→advisory) | Remove from required list. | Mirror gates breached, with hysteresis. |
| retire (advisory→gone) | Remove the job entirely — dead weight burning runner minutes. | Advisory too long without ever nearing the graduation bar. |

In v1 (propose-only), **every transition is a filed, evidence-bearing GitHub issue
labeled `ci-doctor`** — never an executed mutation. Execution arrives in Phase C; until
then the backlog runner (which consumes labeled issues as its work queue) provides a
de facto remediation path.

## Part 3 — Graduation gates (defaults; every threshold is a config knob)

A check graduates advisory→blocking only when ALL hold over the stability window:

| Gate | Default | Rationale |
|------|---------|-----------|
| Window AND volume | ≥ 21 days AND ≥ 30 completed runs | Either alone is gameable by a quiet week. |
| True failures on default branch | 0 unexplained | A check failing on `master` isn't measuring the PR. |
| Flake rate (fail→pass, same SHA) | < 2% | Above this it stalls auto-merge. |
| p95 duration | ≤ 10 min | Blocking checks sit on the merge critical path. |
| Trend | Flake rate flat or falling | Never graduate into a decaying check. |

- **Demotion** (blocking→advisory): flake ≥ 5% sustained 7 days — deliberately far above
  the 2% graduation bar (hysteresis) so borderline checks don't oscillate.
- **Retirement** (advisory→gone): ≥ 60 days advisory without ever clearing the
  graduation bar; proposed for removal or ownership reassignment.

## Part 4 — Automation design

### 4a. Identity and shape

- `CicdDoctor` in `server/automations/cicd_doctor.py` — id `cicd-doctor`, modes
  `("off", "propose")`; `auto` reserved for Phase C. Registered in `registry.py` beside
  PR Doctor / Backlog Runner / Autopilot. Automation-card status via `status()`
  (e.g. "3 open findings · 1 graduation candidate").
- `CicdDoctorLoop` — restart-safe, non-reentrant (lock around `tick()`), every I/O
  dependency injected as a seam (`fetch_runs`, `fetch_jobs`, `fetch_artifact`,
  `fetch_branch_protection`, `file_issue`, `close_issue`, `clock`), matching the
  AutopilotLoop pattern so the whole pipeline is testable with zero network.
- Ticked from the orchestrator at ~15 min (CI history moves slowly; respects API rate
  limits; reads via GraphQL per project convention).

### 4b. Monitored inputs (per enabled repo, per tick, cursor-based ingest-once)

1. **Workflow runs + jobs** (Actions API): conclusions, durations, re-runs of same SHA,
   queue times.
2. **Test-level results** from JUnit artifacts: per-test pass/fail/skip/duration →
   per-test flake rates and duration trends (known flakes like
   `test_batch_unarchive_partial_failure` become measured rates, not folklore). Repos
   without JUnit artifacts degrade gracefully to job-level sensing and receive a filed
   "emit JUnit XML" proposal.
3. **Check-tier state** (branch-protection API) — feeds the state machine.
4. **Waste signals**: the `push`+`pull_request` double-run in `ci.yml` (every PR-branch
   push runs the full matrix twice today), missing `concurrency` cancellation, missing
   pip cache in `lint-and-test`, runner-minute totals per repo/week.

### 4c. Pipeline per tick: Ingest → Analyze → Report

- **Ingest**: new tables `ci_run`, `ci_job`, `ci_test_result`, `ci_check_state`,
  `ci_finding` (new columns nullable, per the dev-SQLite backfill rule). One cursor per
  repo so history is pulled exactly once.
- **Analyze**: pure functions `(stored history, config) → findings` — flake computation,
  duration regression, waste detection, state-machine verdicts. No I/O inside analyzers;
  fully unit-testable (constitution V).
- **Report**: upsert findings fingerprint-deduped with lifecycle
  `open → updated → resolved`; actionable findings become `ci-doctor` GitHub issues,
  auto-closed when the underlying finding resolves; counts feed `status()`.

### 4d. Key decisions

| Decision | Choice |
|----------|--------|
| Transport | Poll, not webhooks — Tailscale-only deployment has no public ingress until P4; the ingest seam lets webhooks swap in later without touching analyzers. |
| Findings sink | DB rows + GitHub issues — issues supply dedupe/close semantics and feed the backlog runner. |
| Tier truth | Branch-protection API, read live — single source of truth. |
| Repo scope | `enabled_repos` repo-multiselect; empty = all ccswitch-managed repos (matches PR Doctor/Autopilot). |
| Granularity | Test-level from the start (user decision) — requires JUnit emission (prerequisite PR ①). |
| Modes | off / propose in v1; auto (Phase C) executes transitions and fixes. |
| Config schema | `enabled_repos`, five graduation-gate thresholds, demotion + retirement thresholds, tick interval, `file_issues: bool` (off = DB findings + feed only). |

### 4e. Prerequisite CI wiring (small companion PRs; Doctor is blind without ①)

1. **JUnit emission everywhere** — `pytest --junitxml=…`; fold the `unittest discover`
   core suite into pytest so it emits too; vitest `--reporter=junit` alongside the
   default reporter; Playwright junit reporter; all uploaded as stably-named artifacts.
2. **Advisory `e2e` job** — chromium install, build, enable the commented `webServer`
   block; NOT added to required checks. Becomes the Doctor's first live graduation
   candidate.
3. **Trigger dedupe + cancellation** — `on: push: branches: [master]` +
   `pull_request`, plus `concurrency: cancel-in-progress` — roughly halves CI spend
   immediately.

## Out of scope for v1 (later phases)

- Phase C (remediation): executing transitions (branch-protection mutations, CI-file
  PRs, flake quarantine) behind an `auto` mode.
- Phase D (tuning): continuous cost/quality/throughput balancing of frequencies,
  parallelism, and assignments.
- Lighthouse/Storybook wiring (Doctor proposes; separate decisions/PRs).
- Webhook ingestion (post-P4 public hosting).

## Testing

- Analyzers: pure-function unit tests (flake math, gate evaluation, hysteresis,
  fingerprint stability) — no network, no DB.
- Loop: seam-injected tests per AutopilotLoop precedent (`test_autopilot_*` shape):
  cursor advance, dedupe, issue file/close, degraded (no-JUnit) mode, non-reentrancy.
- Ingest: JUnit XML parsing fixtures (pytest, vitest, Playwright dialects).
- API/UI: automation card wiring follows the existing automations API tests
  (`test_automations_api.py`) — new hooks must be added to the vi.mock lists per the
  SessionCard/SessionList gotcha memory.

## Open items for sign-off

1. Graduation-gate defaults (Part 3) — encode the cost/quality/throughput balance.
2. Land prerequisite CI wiring (4e) as PRs now vs. as Doctor proposals later
   (recommendation: now, since ① is the Doctor's eyes).
