---
initiative: epic-viberpit-ui-e2-accessible-and-consistent-interaction
kind: plan
epic: steiner385/viberpit#1395
upstream: [docs/sdlc/epic-viberpit-ui-e2-accessible-and-consistent-interaction/spec.md]
verified_against: 1a66f955
---

# Plan — ViberPit UI E2: Accessible and consistent interaction

| | |
|---|---|
| **Stage** | Plan (propose-only) |
| **Spec** | `spec.md` — read-only input. At planning time it lives on the unmerged branch `sdlc/spec-epic-viberpit-ui-e2-accessible-and-consistent-interaction`, not on `master`. Build must not start until the spec PR has merged. |
| **Planned against** | `1a66f955` (master) |
| **Epic / children** | #1395 · G03 #1401 · G04 #1402 · G05 #1403 · G18 #1416 |

The spec's `FB-`/`AC-` IDs are used below. The spec numbers acceptance criteria 1–24 under headings AC-0…AC-5; this plan cites them as `AC-n.k` where `k` is the spec's running number (e.g. `AC-1.6`).

## 0. Drift between the spec and master

Re-verified on `1a66f955`:

| Spec claim | Reality on master | Consequence |
|---|---|---|
| "no automated check measures rendered geometry at all" (§1) | `web/e2e/touch-targets.spec.ts` exists and measures `boundingBox()` under `hasTouch`/`isMobile` (393×852) for the Sessions list-controls strip. Its header already cites **WCAG 2.2 AA 2.5.8**. | FB-0 **generalizes this spec**; it does not start from nothing. Reuse its API-mock helper rather than re-inventing it. |
| FB-3 names `useOverlayState` as living beside `useFocusTrap` | Hook is `web/src/shell/useOverlayState.ts`; `useFocusTrap` is `web/src/ui/useFocusTrap.ts`. | Use the real paths. |
| `LegendPanel`, Help are overlays | `web/src/pipeline/LegendPanel.tsx`; Help is `web/src/shell/regions/HelpSheet.tsx`; Pipeline page is `web/src/features/pipeline/PipelinePage.tsx`. | Overlay set for AC-3.15 = `Dialog`, `Sheet`, `DrawerHost`, `HelpSheet`, `LegendPanel`. |
| `AboutSection` has a pointer-only scroll region | `AboutSection.tsx` renders a `dl` in `SettingsSection` and has no `overflow`/`tabIndex` of its own. The scroll container may be the settings chassis or page host. | Task 3.5 begins with a **measurement** (does it scroll at all, and in which ancestor?) before adding anything. If nothing scrolls, AC-3.17 is satisfied by a regression test only and the item is recorded as "no defect". |
| Constitution says 2.1 AA; `docs/ACCESSIBILITY.md` titled 2.1 AA | Confirmed. But `touch-targets.spec.ts` and other repo docs already say 2.2. | The repo is internally inconsistent today; PC-1 resolves it. |
| `docs/ACCESSIBILITY.md` cites #953 for the `Button` base-class change | Confirmed (~line 83). `Button.tsx` still has `min-h-target` on `lg` only; `sm`=`h-9`, `md`=`h-10`. | Task 1.2 resolves it. |
| `contrast.test.ts` threshold `AA_NORMAL_TEXT = 4.5` and token parsing | Both are **module-private** to the test file. | Task 0.1 extracts them. The harness cannot import from a `*.test.ts`. |

Open PRs touching `web/src/ui/`, `web/src/index.css`, `web/src/shell/regions/`, `docs/ACCESSIBILITY.md`, `web/e2e/` or `ci.yml` at planning time: **none found**. Re-run the core.md GraphQL query immediately before each task starts.

## 1. Plan-stage resolutions of the spec's open questions

These are recommendations; the human gate may overrule them. Where the spec says "needs a human decision", the plan sequences work so that decision blocks only the tasks that depend on it.

- **PC-1 (WCAG version) — blocks Tasks 1.x wording and 4.x only.** Tasks 0.x, 2.x and 3.x are version-independent, so they proceed. The harness takes the 24×24 allowance as a named constant sourced once; if 2.2 is declined, 2.5.8-dependent exemptions are re-expressed as findings. The constitution amendment is **a separate PR by a human maintainer** and is not part of this plan's implementation PRs. No agent edits the constitution file.
- **OQ-1 (gate placement) — option (c).** AC-3 checks (headings, landmark names, overlay focus, route-change focus, keyboard scroll) are jsdom/vitest tests in the blocking `frontend` job. Geometry and composited contrast run in `e2e`, which is advisory today. This plan does **not** graduate `e2e` to required. Consequence stated plainly: a touch-target or contrast regression is **not merge-blocking** after this epic; it is caught by the advisory job and by the committed baseline diff.
- **OQ-2 (matrix location) — sibling file** `docs/accessibility/conformance.md`, linked from `docs/ACCESSIBILITY.md` as the single pointer.
- **OQ-3 (exception expiry) — yes, a `reviewBy` date per entry**, enforced softly: the harness reports expired entries as `warning`, not `fail`, so churn cannot red a build.
- **OQ-4 (critical journeys) — adopt the spec's proposal**: launch a session, monitor a running session, respond to a permission card, review a PR/Delivery item, change a setting.
- **PC-4 (dependency) — default to no new dependency.** Contrast is computed from Playwright `page.evaluate` using the extracted luminance formula. If the gate approves `@axe-core/playwright`, it is used only for non-contrast rules (landmark/region/name) as a cross-check, never as a second contrast implementation (AC-0.4).
- **PC-2 (evidence) — synthetic fixtures only.** All harness runs use `page.route` mocks (the pattern `touch-targets.spec.ts` already uses). The scrubber in Task 0.4 is a test, not a convention.
- **PC-3 (claim audience) — default to an internal engineering record.** The matrix opens with a scope/version/level/technologies statement and a "this is not a published accessibility statement" line. Publishing is a separate human decision.

## 2. Files

### Add

| File | Purpose |
|---|---|
| `web/src/lib/a11yThresholds.ts` | **The one** home for `TOUCH_TARGET_PX` (asserted equal to `--target` in `index.css`), `WCAG_AA_NORMAL_TEXT = 4.5`, `WCAG_AA_LARGE_TEXT = 3`, `WCAG_MIN_TARGET_PX = 24`, plus `contrastRatio()` / `relativeLuminance()`. Imported by `contrast.test.ts`, the harness, and the vitest assertions. |
| `web/e2e/a11y/harness.ts` | Shared Playwright harness core: route/viewport/theme/state matrix runner, report writer. |
| `web/e2e/a11y/fixtures.ts` | Extracted `interceptApi` / mock sessions (moved out of `touch-targets.spec.ts`, which re-imports them). |
| `web/e2e/a11y/touch-pass.ts` | Touch-target measurement: rect, ancestor clip, nearest-neighbour spacing, verdict. |
| `web/e2e/a11y/contrast-pass.ts` | Composited-colour resolution (opacity + stacking walk), font size/weight, large-text classification, `indeterminate` on non-finite. |
| `web/e2e/a11y/routes.ts` | The enumerated route list (Sessions list+detail, Pipeline, Delivery, Roadmap/Initiative, Settings index+sections, Review, Sub-agent Reader, Help, About), derived from `shell/routes.tsx` path constants, not retyped literals. |
| `web/e2e/a11y/touch-targets.a11y.spec.ts` | Matrix spec: 393×852, 390×844, both landscapes, one tablet; coarse pointer. Asserts zero `fail`. |
| `web/e2e/a11y/contrast.a11y.spec.ts` | Matrix spec: light, dark-by-media, dark-by-attr × default/hover/focus-visible/selected/active. Asserts zero `fail`. |
| `web/e2e/a11y/exceptions.json` | Exception register: `{ selector, route, criterion, allowance, rationale, addedAt, reviewBy }`. |
| `web/e2e/a11y/baseline.json` | Committed, scrubbed report baseline. |
| `web/e2e/a11y/harness.unit.test.ts` | Vitest unit tests for the pure verdict logic; runs in the blocking `frontend` job. |
| `web/src/__tests__/pageSemantics.test.tsx` | jsdom: one `h1`, no skipped levels, per primary route (AC-3.13). |
| `web/src/__tests__/landmarkNames.test.tsx` | jsdom: repeated landmark roles carry distinct accessible names (AC-3.14). |
| `web/src/__tests__/overlayFocus.test.tsx` | jsdom: one case per overlay — Tab cycle, Escape, focus return (AC-3.15). |
| `web/src/__tests__/routeFocus.test.tsx` | jsdom: focus is not on `<body>` after navigation, per primary route (AC-3.16). |
| `docs/accessibility/conformance.md` | Criterion-level conformance matrix (FB-4), AT pass records, device checklist. |

### Change

| File | Purpose |
|---|---|
| `web/src/__tests__/contrast.test.ts` | Import thresholds/luminance from `a11yThresholds.ts`; extend with any token pair the composited pass finds failing. |
| `web/src/ui/Button.tsx` | Decision under FB-1: move `min-h-target` into the base class (recommended), collapse `sm`/`md` heights, delete the "`lg` is the only size" comment. |
| `web/src/index.css` | Fix failing `--t-*` token values **in place**. No new tokens. |
| `web/src/ui/PageHeader.tsx`, page components that lack it | Widen adoption so every primary route has exactly one `h1` (only where Task 3.1's test fails). |
| `web/src/features/pipeline/PipelinePage.tsx` (+ `web/src/pipeline/HealthHeader.tsx`) | `aria-label`/`aria-labelledby` on repeated landmarks (the G05 Pipeline case). |
| `web/src/shell/regions/PageHost.tsx` (or the router outlet host) | Route-change focus management, only if Task 3.4 shows focus landing on `<body>`. Extends the existing mechanism; no second one. |
| `web/src/features/settings/sections/AboutSection.tsx` (or its chassis) | Keyboard-operable scroll container **iff** Task 3.5's measurement finds a pointer-only one. |
| `web/e2e/touch-targets.spec.ts` | Import extracted fixtures from `web/e2e/a11y/fixtures.ts` (no behavior change). |
| `docs/ACCESSIBILITY.md` | Replace the #953 touch-target paragraph to match shipped `Button`; link the matrix; re-anchor the terminal partial-conformance note to the PC-1 version. |
| `.github/workflows/ci.yml` | `e2e` job: upload the harness report as an artifact. **No new job, no required-check change.** |
| `web/package.json` | Only if PC-4 approves a package. Otherwise untouched. |

### Delete (in the same PR as the thing that replaces it)

- Module-private threshold and luminance helpers inside `contrast.test.ts` (superseded by `a11yThresholds.ts`).
- The "`size="lg"` is the only size with `min-h-target`" comment in `Button.tsx` and the matching paragraph in `docs/ACCESSIBILITY.md` (if option (a) is taken).
- The duplicate fixture block inside `touch-targets.spec.ts`.

## 3. Order

Dependency graph: `0.1 → 0.2 → 0.3 → 0.4 → (1.x ‖ 2.x)`; `3.x` is independent of the harness and may start in parallel with 0.x; `4.x` needs 1–3 to have produced evidence; `5` last. Each lettered group is one PR.

1. **PR-A — Single source of truth for thresholds (Task 0.1).** Extract `a11yThresholds.ts`; rewire `contrast.test.ts`. No behavior change; proves the extraction is lossless. Lands first because everything else imports it.
2. **PR-B — Harness (Tasks 0.2–0.4).** Extract fixtures; build touch pass, contrast pass, register, report schema, scrubber; commit `baseline.json`. The harness lands **reporting**, with its `fail`-asserting specs compared against an allowlist of currently-known failures in the baseline, so the PR is green while making today's debt visible. Remediation tasks then shrink the allowlist to zero.
3. **PR-C — Touch targets, G03 (Tasks 1.1–1.3).** Depends on PR-B. Wording of exemptions depends on PC-1.
4. **PR-D — Contrast, G04 (Tasks 2.1–2.2).** Depends on PR-B. May run parallel to PR-C (only `index.css` could overlap; rebase before opening).
5. **PR-E — Semantics & keyboard, G05 (Tasks 3.1–3.5).** Independent of PR-B. Ships its vitest tests first (red), then the fixes, in one PR.
6. **PR-F — Conformance matrix, G18 (Tasks 4.1–4.3).** Docs plus recorded human evidence. AT passes are **human work** (spec non-goal: automating AT); this PR can merge with "not yet verified" rows and be amended as passes complete. Depends on PC-1/PC-3.
7. **PR-G — Close-out (Task 5.1).** Drives the allowlist to empty, adds the CI artifact upload, records the sibling-pattern sweep, final gates.

Gating note: G04 and G05 are **Blocked** and G18 **Backlog** in the project view (OQ-5). Task 0.0 reads the native blocked-by edges before PR-D/PR-E are scheduled. PR-E is likely safe to unblock early because it has no harness dependency.

## 4. Tasks and tests

All tests are written first and must fail on `1a66f955` for the right reason before the fix lands.

### Task 0.0 — Pre-flight (no code)
Read the native blocked-by edges on #1401/#1402/#1403/#1416; run the in-flight PR query for `web/src/ui/`, `web/src/index.css`, `web/src/shell/regions/`, `web/e2e/`, `ci.yml`. Record results in PR-A's body. Resolves OQ-5.

### Task 0.1 — Extract thresholds
- `a11yThresholds.test.ts::test_relative_luminance_matches_wcag_reference_values` — black/white = 21:1, known mid-greys. *proves AC-0.4*
- `a11yThresholds.test.ts::test_target_constant_equals_css_token` — parses `--target` from `index.css` and asserts `TOUCH_TARGET_PX` equals it, so the TS constant cannot drift from the CSS token. *proves AC-0.4, FB-0 "reads the 44 floor"*
- `contrast.test.ts` — existing assertions pass unchanged against the imported helpers. *proves AC-2.12*
- `test_only_one_threshold_constant` — repo-scan vitest: the contrast `4.5` and target `44` literals appear only in `a11yThresholds.ts` (allowlisting `index.css`). Proven able to fail by temporarily adding a duplicate. *proves AC-0.4*

### Task 0.2 — Fixtures extraction
- Existing `touch-targets.spec.ts` passes unchanged after importing from `fixtures.ts`. *proves no regression of the existing geometry spec*

### Task 0.3 — Touch & contrast passes
Pure verdict logic is split from browser collection so it is unit-testable in vitest (blocking job):
- `harness.unit.test.ts::test_touch_verdict_pass_at_44`, `fail_at_43`, `exempt_when_spacing_allowance_and_24`, `fail_below_24_without_register_entry`. *proves AC-1.6, AC-1.7*
- `test_touch_verdict_accounts_for_ancestor_clip` — a 44px box clipped to 30px by `overflow:hidden` measures 30. *proves AC-1.7*
- `test_disabled_and_nonhittable_excluded`. *proves FB-1*
- `test_contrast_composites_through_opacity` — 50% opacity text over a known background yields the composited ratio, not the token ratio. *proves FB-0 contrast pass, AC-2.9*
- `test_contrast_nan_is_indeterminate_not_fail` — non-finite ratio → `indeterminate` with a reason. *proves AC-2.10*
- `test_large_text_threshold` — 24px normal and 18.66px bold use 3:1; 18.65px bold uses 4.5:1. *proves FB-2*
- `test_inactive_component_is_exempt_with_name`. *proves AC-2.11*
- `test_verdict_layer_is_deterministic` — same input twice, identical verdicts. *proves AC-0.3*
- `test_fail_without_register_entry_fails_harness` and `test_register_entry_requires_all_fields`. *proves the §4 exception-register contract*
- `*.a11y.spec.ts` (e2e) smoke: harness runs against the built bundle on one route and emits a schema-valid JSON report. *proves AC-0.1, AC-0.2*
- e2e determinism: run the matrix twice, diff verdicts only. *proves AC-0.3*

### Task 0.4 — Scrubber and baseline
- `harness.unit.test.ts::test_report_scrubber_removes_identifying_fields` — a seeded report containing an account name, session id, repo name and tailnet hostname comes out with none of them. *proves the PC-2 constraint*
- `test_baseline_contains_no_identity_outside_synthetic_allowlist`. *proves PC-2*

### Task 1.1 — Measure
Run the touch pass on the enumerated routes; commit the failing set to `baseline.json`. Evidence only; no product change. *proves the FB-0 → FB-1 handoff*

### Task 1.2 — Resolve the `Button` convention (FB-1 decision point)
Recommendation **(a)**: `min-h-target` in the base class, because `docs/ACCESSIBILITY.md` already treats this as the intended end state and the `lg`-only rule is the root of the sibling pattern.
- `Button.test.tsx::test_every_size_has_min_height_target_class` (iterates `buttonVariants` sizes). *proves FB-1, AC-1.8*
- e2e touch pass: every `Button` on the enumerated routes ≥ 44 high. *proves AC-1.6*
- Audit: `git grep -n 'size="sm"\|size="md"'` — intentionally dense sites are classified (moved to `IconButton` / inline allowance with a register entry, or accepted at 44). List them in the PR body. *proves AC-5.24*

### Task 1.3 — Remaining control fixes and register
Fix each remaining `fail` via the `min-h-target`/`min-w-target` tokens, or register an exemption with a named 2.5.8 allowance.
- `touch-targets.a11y.spec.ts` at all viewports, zero `fail`. *proves AC-1.5, AC-1.6*
- `test_accessibility_doc_cites_no_closed_issue` (doc `#NNN` references checked against a checked-in snapshot; skipped offline). *proves AC-1.8*

### Task 2.1 — Measure, then fix tokens
- Baseline contrast set committed. Each failing **token pair** gets a row added to `contrast.test.ts` first (red), then the `index.css` value is corrected in place (green). *proves AC-2.12*
- Literal-hex / opacity-modifier failures are rerouted to tokens; the `no-raw-palette` lint keeps passing. *proves FB-2 "routing through the token set"*
- `test_no_second_corrected_color` — no new `--t-*` name appears in the diff. *proves FB-2*

### Task 2.2 — State and theme coverage
- `contrast.a11y.spec.ts` matrix: light, dark-media, dark-attr × default/hover/focus-visible/selected/active, zero `fail`. *proves AC-2.9*
- Report lists every `indeterminate` with a reason and every `exempt` with a named exemption. *proves AC-2.10, AC-2.11*

### Task 3.1 — Headings
- `pageSemantics.test.tsx::test_<route>_has_exactly_one_h1` per primary route, parametrized from the route constants so the lists cannot drift. *proves AC-3.13*
- `test_heading_levels_do_not_skip` per route. *proves AC-3.13*
- `test_document_title_synced_via_documentTitle_stack`. *proves FB-3*
- `test_topbar_renders_no_h1` (existing contract kept). *proves FB-3*
- Self-check: a deliberately broken fixture page (two `h1`s) makes the assertion fail. *proves AC-3.13*

### Task 3.2 — Landmark names
- `landmarkNames.test.tsx::test_repeated_landmark_roles_have_distinct_names` over each primary route; expected red on Pipeline before the fix. *proves AC-3.14*
- Self-check with a duplicated unnamed `nav` fixture. *proves AC-3.14*

### Task 3.3 — Overlay focus coverage
One test per overlay, each: Tab cycles within, Escape closes, focus returns to the opener.
- `test_dialog_focus_contract`, `test_sheet_focus_contract`, `test_drawerhost_focus_contract`, `test_helpsheet_focus_contract`, `test_legendpanel_focus_contract`. *proves AC-3.15*
- Reuses `useFocusTrap`; an overlay found not using it is fixed to use it, not given a second mechanism. *proves FB-3 "coverage and proof, not a new mechanism"*

### Task 3.4 — Route-change focus
- `routeFocus.test.tsx::test_focus_not_on_body_after_navigation` per primary route. *proves AC-3.16*
- Fix only if red: extend the existing page-host mechanism. Regression test keeps the `PageContent` busy announcer intact.

### Task 3.5 — Keyboard-operable scroll
- A Playwright probe (`scrollHeight > clientHeight` at 393×852 and under a text-zoom emulation) establishes whether a scroll container exists and which element owns it, because jsdom cannot measure scroll.
- If one exists: `AboutSection.test.tsx::test_scroll_container_is_focusable_with_accessible_name` (`tabIndex=0`, `role="region"`, label). *proves AC-3.17*
- If none: record "no defect on `1a66f955`" in the PR and keep the probe as a regression test. *proves AC-3.17*

### Task 4.1 — Matrix skeleton
- `docs/accessibility/conformance.md` with one row per applicable criterion for the PC-1 version. Tests: `conformance.test.ts::test_every_row_has_method_and_date_or_not_yet_verified` and `test_no_row_marked_conforming_without_evidence_ref`. *proves AC-4.18*
- `test_matrix_and_accessibility_doc_state_same_wcag_version`. Enforces 2.1 on both initially; PC-1 approval flips the expected constant in the same PR as the (human-authored) amendment. *proves AC-4.21*
- `test_terminal_note_preserved` (string-anchored). *proves FB-4*

### Task 4.2 — Assistive-technology passes (human)
Execute VoiceOver/Safari (iOS + macOS), NVDA/Firefox-or-Chrome, TalkBack/Chrome over the OQ-4 journeys; record date, platform version, SR version, journeys, findings. Each finding becomes an issue. *proves AC-4.19* (recorded artifact; no automated test by design.)

### Task 4.3 — Device checklist (human)
Rotation, keyboard occlusion, safe-area, 200% text, 400% reflow, reduced-motion, forced-colors, live regions, focus, error recovery — per device class. *proves AC-4.20*

### Task 5.1 — Close-out
- Baseline allowlist reduced to empty. *proves AC-1.6, AC-2.9*
- `ci.yml`: harness report uploaded as an artifact from `e2e`. Validate with `actionlint`; confirm no literal `${{ }}` text lands in action/step metadata descriptions.
- Sibling-pattern sweep recorded in PR bodies. *proves AC-5.24*
- Final gates (AC-5.22): `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/` (expected no-op passes; the epic does not touch Python), and in `web/`: `npm run typecheck`, `npm run lint` (`--max-warnings 0`), the full `npm run test`, `npm run build`, plus `npx playwright test`. *proves AC-5.22*
- `git grep` for removed identifiers returns nothing. *proves AC-5.23*

## 5. Risks

| Risk | Mitigation |
|---|---|
| **Moving `min-h-target` into `Button`'s base class changes density app-wide** and could break layouts or the ~47 hook-mocking test files. | Land as its own PR; run the **full** `npm run test` and existing e2e specs; list changed call sites in the PR; any visual comparison uses synthetic fixtures only. Fallback is option (b): keep the convention and re-document it. |
| **Composited-contrast resolution is hard** (gradients, images, `backdrop-filter`, pseudo-elements, blend modes); wrong answers become false failures. | Anything the walker cannot resolve to a single opaque colour is `indeterminate` with a reason — never `fail`, never `pass` (AC-2.10). Unit tests cover the walker's decisions; the report counts indeterminates so growth is visible. |
| **Flaky verdicts in CI** (animation, font load, mid-transition measurement) erode trust in an advisory job. | Wait on `document.fonts.ready`; use `reducedMotion: 'reduce'` and disabled animations; verdicts are asserted, raw pixel values are tolerance-compared (AC-0.3); run twice and diff. |
| **Advisory-only gating**: geometry/contrast regressions can merge. | Stated openly (OQ-1 resolution). Mitigations: committed baseline diff is reviewable; the register fails the harness; the CI/CD Doctor can graduate `e2e` under #453 separately. The AC-3 checks, which jsdom can test, **do** block. |
| **PC-1 undecided**: the 24×24 allowance and matrix version depend on it. | Version is a single constant; 1.x wording and 4.x sequence after the decision; 0.x/2.x/3.x do not depend on it. No agent edits the constitution (governed path, `amendment` required check). |
| **Private audit evidence leaks** via screenshots, JSON, traces or JUnit artifacts. | Synthetic `page.route` fixtures only; the scrubber is a unit-tested gate (Task 0.4); the a11y specs disable screenshots/traces. |
| **jsdom cannot see layout** (focus-visible styling, scroll geometry, real stacking). | jsdom asserts structure only (headings, names, focus order); anything geometric stays in Playwright. Task 3.5 explicitly uses a browser probe. |
| **Spec drift continues** (four cited paths already vanished; the spec's own file references have drifted again). | Plan re-verified at `1a66f955`; each task starts with a path check; routes derive from `routes.tsx` constants so renamed pages cannot silently leave the matrix. |
| **A guard that cannot fail.** | Each AC-3 test has a deliberately-broken fixture proving it fails; AC-0.4's repo-scan is verified by temporarily adding a duplicate constant. |
| **Parallel PRs conflict** on `index.css`, `ACCESSIBILITY.md`, `Button.tsx`. | PR-C and PR-D own disjoint token sets; rebase before opening; re-run the in-flight query per task; `ACCESSIBILITY.md` edits are small and ordered (C → F). |
| **Human AT passes may not happen on schedule**; the matrix stalls. | PR-F merges with honest "not yet verified" rows (the spec requires this); AC-4.19/4.20 are tracked as a follow-up checklist rather than blocking PR-A…E. |
| **New dependency** (PC-4) adds supply-chain weight. | Default is zero new dependencies; any addition only after explicit approval, version-pinned. |
| **Shared-component changes** break unrelated hook-mocked tests. | Full `npm run test`, never changed-files. |

---

*Propose-only Plan artifact. It plans; it does not implement, amend governance, or arm auto-merge.*
