---
initiative: epic-viberpit-ui-e5-quality-evidence-and-sustainable-maintenance
kind: plan
epic: steiner385/viberpit#1398
upstream: [docs/sdlc/epic-viberpit-ui-e5-quality-evidence-and-sustainable-maintenance/spec.md]
verified_against: 05a5fd9e
---

# Plan — ViberPit UI E5: Quality evidence and sustainable maintenance

Propose-only. Nothing here edits product code; Build executes it. Spec: `spec.md` (same directory).
Verified against `origin/master` `05a5fd9e` (2026-10-15), **not** the spec's `3ba8665c` — master moved
~4 weeks, and that changed the picture materially (§0).

## 0. Corrections to the spec (found while planning)

| Spec claim | Reality on `05a5fd9e` | Effect on plan |
|---|---|---|
| G13: no web-vitals collection, no bundle budget, Lighthouse enforces nothing | **Shipped and closed.** #1411 is CLOSED; PR #2319 (`3391b542`) added `web/src/obs/{vitals,WebVitalsCollector,routeTemplate}.ts(x)`, `server/obs/web_vitals.py`, `server/api/web_vitals.py` (`GET /api/web-vitals/summary`), and `web/scripts/check-bundle-budget.mjs` + `web/bundle-budget.json`, run in CI as `npm run budget` (`ci.yml:279`). It uses a dedicated ingest route, not `/api/client-logs`. | G13 drops from "build" to **verify-and-close-out** (WS-1). The spec's §4 "extend `/api/client-logs`" and P3 are moot as written; the live question is whether the shipped event shape leaks anything. |
| G16: four residual `dark:` literals (`canvas.ts`, `Button.tsx`, its test, `Pills.test.tsx`) | All four are **false positives**: `canvas.ts` has an object key `dark:`; `Button.tsx:8` is a comment; `Pills.test.tsx:32` is a *negative* assertion (`not.toMatch(/dark:/)`). There are no `dark:` Tailwind variants left. | G16 residual code work is ~zero. WS-4 is a disposition + a tightened check, not a cleanup. |
| Epic is parked; resumes after the finish line | Still `parked` on #1398 and #1412–#1415 (#1411 closed, also `parked`). No lift comment. Finish-line rows 2–8 are merged. | Gate WS-0 stays first; see §2. |
| `web/e2e` has "13 specs" | 11 spec files + `fixtures/` today. | Re-count in WS-2 baseline; do not hard-code. |

## 1. Scope decision for Build

Five leaves, **not equally buildable by an agent**:

| Leaf | Plan disposition |
|---|---|
| G13 #1411 | Already shipped. Close-out only (WS-1). |
| G14 #1412 | Buildable: cross-browser Playwright + deterministic fixtures + a11y triage (WS-2). The only large code workstream. |
| G15 #1413 | **Out of agent scope** (human-subjects research). Plan produces a study *protocol template* doc only if the owner asks; otherwise stays parked (WS-3). |
| G16 #1414 | Disposition + a small guard (WS-4). |
| G17 #1415 | Buildable only in its data half; blocked on owner input for the cost half (WS-5). |

## 2. Order

0. **WS-0 Gate (blocks everything).** The owner of the `parked` label (steiner385) must lift it on
   #1398, or comment that Plan/Build may proceed. Build MUST re-read the epic's labels/comments first
   and stop (hand back) if still parked. Spec §7 P1 stands; this plan does not override it. Also needed
   from the owner before WS-5b: loaded hourly rate (P6). Before WS-3: consent/retention owner (P2).
   Resolve P5 here too: designate `docs/audits/ui-conformance-scorecard.md` (new, WS-6) as the in-repo
   "root scorecard" or strike that criterion from the child issues.
1. **WS-1 G13 close-out** (small, independent; can go first even if the rest waits).
2. **WS-4 G16 disposition** (small, independent).
3. **WS-2 G14** in three PRs, in this order: 2a fixtures → 2b Firefox/WebKit advisory → 2c geometry/visual + a11y triage → (later, after evidence) 2d promote subset to required.
4. **WS-5a G17 data half** after WS-1 (shares the Delivery data seam); **WS-5b** cost half blocked on P6.
5. **WS-3 G15** only after P2 is resolved by a human.
6. **WS-6 scorecard doc** lands incrementally with each of the above; last PR closes it out.

Each WS is its own PR. Before each, run the in-flight-PR check from `core.md` against the files listed.

## 3. Workstreams

### WS-1 — G13 close-out (verify, don't rebuild)
**Files**
- `server/tests/test_web_vitals_api.py` (change) — add a payload-leak test (below).
- `web/src/obs/vitals.test.ts` (change) — assert no URL/query/identity in the serialized batch.
- `docs/audits/ui-conformance-scorecard.md` (add, WS-6) — record G13 evidence: budget ceiling, first p75 snapshot.
- `web/bundle-budget.json` (change, only if the measured entry chunk is below the ceiling — ceilings only go down).

**Steps**
1. Read `web/bundle-budget.json` vs a fresh `npm run build && npm run budget`; note headroom. The issue's ≤250 kB gzip main-JS was a *starting point*; record the real figure rather than the audit's stale 453 kB.
2. Query `GET /api/web-vitals/summary` on the live edge (via `ssh dobby` + `127.0.0.1:8787`) and record the p75 LCP/INP/CLS per route/device/browser, noting `MIN_SAMPLES_FOR_VERDICT` gaps.
3. Confirm segmentation covers browser, device, route, and deferred-chunk bytes (the issue's checklist) — gaps become follow-up issues, not scope creep.
4. Comment on #1411's closure with the evidence link; confirm the lab-vs-field distinction is documented in `docs/product/`.

**Tests**
- `test_web_vitals_ingest_rejects_unknown_fields — proves G13 AC-1 (no prompts/tokens/account names)`: POST a batch with extra keys (`prompt`, `token`, `account`, `url`); assert rejected or stripped, never stored.
- `test_web_vitals_stored_row_has_no_identity_columns — proves G13 AC-1`: schema allowlist of persisted columns.
- `vitals.test.ts: serialized batch contains only route shape — proves G13 AC-1`.
- Existing `npm run budget` / `bundleBudget.test.mjs` — proves G13 AC-2 (populated, CI-enforced budget).

### WS-2 — G14 cross-browser visual + a11y gates
**Files**
- `web/playwright.config.ts` (change) — add `firefox` and `webkit` projects; keep `chromium` default. Scope extra engines to a tag (`@cross-browser`) so only the representative set runs on them.
- `web/e2e/fixtures/seed.ts` (add) — deterministic seeded API fixtures (fixed clock, ids, ordering); route-level `page.route` stubs, no live backend.
- `web/e2e/cross-browser/*.spec.ts` (add) — representative flows: Sessions list→detail, Roadmap portfolio, Delivery CI scope, Pipeline workspace, phone-width touch targets. Reuse existing specs' helpers (grep `web/e2e` first; do not fork `touch-targets`/`short-viewport` logic).
- `web/e2e/support/geometry.ts` (add) — shared bounding-box assertions (no horizontal overflow, min target size, no clipped text).
- `web/package.json` (change) — `test:e2e:cross` script; `@axe-core/playwright` dev dep (VII: vetted, current). `jest-axe` stays for unit level.
- `.github/workflows/ci.yml` (change) — new **advisory** job `e2e-cross-browser` (`needs: changes`, `if web==true`), `npx playwright install --with-deps firefox webkit`, uploads traces/screenshots. A separate nightly `schedule` trigger for the broad set. Not in required checks.
- `docs/product/ui-a11y-analyzer-limitations.md` (add) — the classified analyzer findings.

**Steps**
- 2a: fixtures + one Chromium-only geometry spec that passes; seeded-defect regression (below).
- 2b: add engines advisory; collect ≥ 2 weeks of runs for a flake measure (junit already emitted for the CI/CD Doctor).
- 2c: identify the analyzer behind the audit's "invalid modern-color / native-checkbox-switch" findings (ask the owner or reproduce with axe; if unreproducible, document that and close the criterion). Classify each as fixed or documented limitation; no blanket `disableRules`.
- 2d (separate, later PR, only with evidence): promote the stable subset to required via the repo's required-check change process (admin action — hand back; an agent does not edit branch protection).

**Tests**
- `seeded_defect_fails_geometry_check — proves G14 AC-1`: a spec that mounts a deliberately broken layout (injected CSS overflow) and asserts the geometry helper **fails**, run via an inverted expectation so CI stays green while proving the gate can bite (conformance-guards-must-prove-they-can-fail). Attach trace/screenshot artifact.
- `cross-browser flows pass on chromium+firefox+webkit — proves G14 AC-2`.
- `axe scan per flow, each finding either fixed or listed in the limitations doc — proves G14 AC-3`: test fails on an unlisted rule id.
- `flake report script (scripts/e2e_flake_rate.py) over junit history < 1% — proves G14 AC-4`; unit-tested on synthetic junit fixtures. Gate for 2d, not for 2b.

### WS-3 — G15 usability baseline (human-owned)
**Files:** `docs/product/usability-study-protocol.md` (add, **only if the owner requests it**) — consent, retention, redaction rules, cohort matrix, metrics definitions (SUS/SEQ/VisAWI-S, task success, first-attempt nav).
**Steps:** none for an agent beyond the protocol draft. Recruiting, sessions and analysis are non-goals (spec §6).
**Tests:** none executable. The protocol doc is reviewed against G15 AC-1..4 by the owner; sample-size floor and cohort coverage are checklist items in the doc. Recordings never contain ViberPit session content (use a seeded demo tenant from WS-2 fixtures).

### WS-4 — G16 disposition
**Files**
- `web/eslint.config.js` (change, optional) — only if a real `dark:` Tailwind-variant ban is absent; check `viberpit/no-raw-palette` first and extend it rather than adding a rule.
- `docs/audits/ui-conformance-scorecard.md` (WS-6) — record the three spec criteria outcomes.

**Steps**
1. Re-run `grep -rnE '(^|[\s"\x27`])dark:[a-z]' web/src` (variant-shaped) to confirm zero real hits; document that the four spec hits were false positives.
2. Enumerate pages not using `EmptyState`/the shared page contract (list `web/src/features/*/` page roots vs `EmptyState` importers); migrate any trivial ones in-PR, list the rest as explicit exceptions in the scorecard.
3. Comment on #1414: recommend close as met by #993 + finish-line row 7 with the residual exceptions listed (owner decides, spec §8.6).

**Tests**
- `no_dark_variant_in_tsx — proves G16 AC-1`: vitest/node test scanning `web/src` for variant-shaped `dark:` (allowlist `index.css`), with a fixture proving it flags a real one.
- `emptystate_exceptions_listed — proves G16 AC-2`: test asserting every page root either imports `EmptyState` or is in a named exception list.

### WS-5 — G17 maintenance-cost and delivery-stability scorecard
**Files**
- `server/pm/dora.py` (add) — pure functions: deployment frequency, lead time, change-failure rate, time-to-restore, from existing PR/CI/deploy records. Grep `server/` (`pipeline/`, `deploy/`, `github/`) for existing timestamps first; extend those sources, no new ingestion pipeline.
- `server/api/delivery_scorecard.py` (add) + registration in the routers — `GET /api/delivery/scorecard?weeks=4`, read-only.
- `server/tests/test_dora.py`, `server/tests/test_delivery_scorecard_api.py` (add).
- `web/src/features/delivery/ci/ScorecardPanel.tsx` (+ test, + `web/src/api/` hook) (add) — rendered in the existing CI scope; reuse `CheckTable` primitives and the Delivery list/detail chassis.
- 5b (blocked on P6): `server/pm/ui_cost.py` — repair-hours × `VIBERPIT_UI_LOADED_HOURLY_RATE`, **unset ⇒ labor cost not computed** (never a default); CI minutes; dependency-patch lag; routine-change spread.

**Steps:** 5a pure DORA over existing data with explicit denominators; 5b cost half after the rate exists; schedule 30/60/90-day reviews as dated entries in the scorecard doc (no retroactive edits — append-only section).

**Tests**
- `test_change_failure_rate_uses_explicit_denominator — proves G17 AC-1`.
- `test_lead_time_excludes_unmerged_prs — proves G17 AC-1`.
- `test_labor_cost_absent_without_rate_env — proves G17 AC-2`.
- `test_scorecard_api_requires_auth_and_is_read_only — proves constitution IV`.
- `ScorecardPanel.test.tsx` renders empty/partial data states with `EmptyState`.
- Gate: `ruff`, `mypy server/`, `pytest tests/`, `pytest server/tests/`; web typecheck/lint/test/build. New model ⇒ none planned (compute on read); if one is added, update `_REQUIRED_EXTRAS` and `RECONCILE_EXEMPT` and run the Postgres suite.

### WS-6 — Conformance scorecard (resolves spec P5)
**Files:** `docs/audits/ui-conformance-scorecard.md` (add) — one table per leaf (G13–G17): criterion, status, evidence link (PR, CI artifact, issue comment), sanitized. Replaces the unreachable `~/Documents/ViberPit-UI-Review-2026-09-07/report.html` as the thing the acceptance lines say to "update". Updated in each WS PR; the last PR links it from `docs/product/` index if one exists.
**Tests:** `scripts`-level link check only if the repo already has a docs link checker; otherwise none.

## 4. Cross-cutting requirements
- All evidence (screenshots, traces, RUM, recordings) sanitized: no transcript text, prompts, tokens, account identifiers. WS-2 fixtures use synthetic names; add a trace-scrub step note to the scorecard.
- New CI jobs are **advisory** at first and state added runtime in the PR body (spec P7). Required-check promotion is a human/admin action.
- Web PRs run the **full** `npm run test`, plus a grep of `web/e2e` for changed strings and the feature spec (e2e is not a required check).
- Each PR body uses `Refs #N` for partial leaves, `Fixes #N` only where the leaf is fully done (WS-4 → #1414 only if the owner agrees).

## 5. Risks

| Risk | Mitigation |
|---|---|
| Building against a parked epic | WS-0 gate; Build re-reads labels first and hands back if still parked. |
| Spec is stale again by Build time (it already was by Plan) | Every WS opens with a re-verification step; findings go in the PR body, not silently assumed. |
| Duplicating shipped G13 work | WS-1 is verify-only; any "new" RUM/budget code is a red flag to stop and re-read #2319. |
| WebKit/Firefox flakiness in CI (retries=2, workers=1 already) | Advisory-first, tagged representative subset, flake script gates promotion; geometry assertions use tolerances, not pixel-exact diffs across engines. Visual-diff baselines are per-engine. |
| CI cost growth (browser installs ~+2–4 min/PR) | Separate advisory job with `changes` gating; broad set nightly only; measure and report in PR. |
| Visual-diff baselines churn and become noise | Few, stable, seeded screens; baselines updated only by explicit script, reviewed in PR. |
| Trace/screenshot artifacts leaking real data | Synthetic fixtures only; no live backend in cross-browser specs. |
| G17 cost invented without the owner's rate | Env-gated, absent by default, test enforces it. |
| G17 duplicating CI data | Start from `ciHealth.ts` and existing server PR/deploy records; new code only for the aggregation. |
| G15 pressured into automation | Explicitly out of scope; protocol doc only on request. |
| Branch protection edits by an agent | Never; promotion is handed back to the owner. |
| Duplicate migration/model collisions (G17) | Prefer compute-on-read; if a model is added follow the two-list + Postgres-suite checklist. |

## 6. Done criteria for this epic
G13 closed with evidence recorded; G14 advisory cross-browser job live with a measured flake rate and a demonstrated-failing seeded defect; G16 dispositioned; G17 DORA half live and cost half either live (rate supplied) or explicitly deferred; G15 either owner-run or explicitly left parked; scorecard doc current.
