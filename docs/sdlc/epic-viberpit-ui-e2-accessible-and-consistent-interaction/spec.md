# Spec — ViberPit UI E2: Accessible and consistent interaction

**Initiative:** `epic-viberpit-ui-e2-accessible-and-consistent-interaction`
**Stage:** Design (propose-only) · **Repo:** `steiner385/viberpit`
**Upstream intent:** epic [#1395](https://github.com/steiner385/viberpit/issues/1395), part of
review initiative [#1393](https://github.com/steiner385/viberpit/issues/1393).
**Children:** G03 [#1401](https://github.com/steiner385/viberpit/issues/1401) ·
G04 [#1402](https://github.com/steiner385/viberpit/issues/1402) ·
G05 [#1403](https://github.com/steiner385/viberpit/issues/1403) ·
G18 [#1416](https://github.com/steiner385/viberpit/issues/1416)

---

## 0. Provenance note (read this before the acceptance criteria)

This initiative's `origin` is `epic-adoption`, not the SDLC Intake stage, so **no
`docs/sdlc/<slug>/intent.md` exists** and none was produced by this stage (requirement 5: upstream
artifacts are read-only; a Design session must not author an Intake artifact). The upstream intent
for this spec is epic #1395 and the four child issues above, read from GitHub. This is a prompt/
origin mismatch in the synthesizer, flagged as **PC-5** below.

**The audit evidence is stale and must not be used as an acceptance threshold.** Every child issue
pins its evidence to commit `07c02a99` (2026-09-07). Master has since advanced through the
finish-line work (rows 6, 7a, 7b — #1563, #1565, #1566). Of the thirteen file paths the children
cite as scope references, **four no longer exist**:

| Cited path | Status on master @ `95eaeeeb` |
| --- | --- |
| `web/src/components/InsightsView.tsx` | **gone** |
| `web/src/pipeline/plotroom.css` | **gone** (cited by both G03 and G04) |
| `web/src/initiatives/InitiativesTab.tsx` | **gone** |
| `web/src/components/SessionListControls.tsx` | present |
| `web/src/ui/Button.tsx`, `web/src/projects/DependencyPanel.tsx`, `web/src/__tests__/contrast.test.ts` | present |
| `web/src/shell/regions/TopBar.tsx`, `web/src/ui/PageHeader.tsx`, `web/src/shell/regions/DrawerHost.tsx`, `web/src/ui/useFocusTrap.ts`, `web/src/features/settings/sections/AboutSection.tsx` | present |
| `docs/ACCESSIBILITY.md` | present |

Consequently this spec inherits the audit's **criteria** but not its **counts**. Numbers such as
"29 enabled hit-testable Sessions controls", "17 Pipeline controls", "4.06:1", "4.38:1" are treated
as historical observations, not as targets. Acceptance is defined against a re-run of a
*measurement harness this work must build* (FB-0), executed on the then-current master.

---

## 1. Problem

ViberPit's design system already encodes the right accessibility contracts, but adoption of those
contracts is incomplete and, critically, **unverifiable at merge time**:

- The 44px touch floor exists as a real token (`--target` / `--spacing-target` in
  `web/src/index.css`, surfaced as Tailwind `min-h-target` / `min-w-target`). `IconButton` carries
  both in its base class. `Button` carries `min-h-target` on `size="lg"` **only** — `sm` is 36px and
  `md` (the `defaultVariants` size) is 40px. Nothing prevents a touch-context control from shipping
  at 36px, and no automated check measures rendered geometry at all.
- Contrast is enforced only as **token arithmetic**. `web/src/__tests__/contrast.test.ts` parses
  `index.css` and applies the WCAG relative-luminance formula to authored `--t-*` pairs. It cannot
  see *composited* colour — opacity modifiers, overlay stacking, selected/hover states, or literal
  hex values written outside the token set. The file's own header documents why the jsdom `axe`
  assertions cannot cover contrast either: with no stylesheet applied, axe reports
  `color-contrast` as `incomplete`, and `toHaveNoViolations()` only inspects `violations`.
- Page semantics are contractually owned (`PageHeader` is documented as "the only h1 on the page,
  synced to document.title (WCAG 2.4.2)"; `TopBar` "deliberately renders NO h1"), but adoption is
  per-page and unenforced, and repeated landmarks are not required to carry unique accessible names.
- The repository claims **WCAG 2.1 AA** (constitution § Development Workflow & Quality Gates;
  `docs/ACCESSIBILITY.md`). All existing verification is emulated — jsdom, or headless chromium.
  No conformance has been established against real assistive technology or real mobile hardware, and
  there is no criterion-level record of what has actually been tested.

The result is a surface that is *designed* accessible and *asserted* accessible, with a verification
gap in the middle where regressions land silently.

---

## 2. Functional behavior

### FB-0 — A measurement harness is the first deliverable (prerequisite for FB-1…FB-3)

Before any remediation, the work must produce a repeatable harness that measures the two geometric/
visual criteria **in a real browser against the built bundle**, and emits a machine-readable report.

- It runs Playwright/chromium against the production build, reusing the existing `e2e` job's
  build-and-serve pattern (`npm run build` → serve → drive), not a new parallel runner.
- **Touch-target pass:** for every enabled, hit-testable interactive element on the enumerated
  routes, record `getBoundingClientRect()` width and height, effective clipping, and nearest-
  neighbour spacing, at the coarse-pointer viewports in AC-1.
- **Contrast pass:** for every text node on the enumerated routes, record the *composited*
  foreground and background (resolved through opacity and stacking), the computed font size and
  weight, and the resulting ratio. Non-finite results (`NaN`) are recorded as **`indeterminate`**
  and are never reported as failures — per G04, invalid analyzer output is not a defect.
- Output is a JSON report plus a human-readable summary, written to `web/test-results/`, with a
  baseline file checked into the repo so the delta between runs is reviewable.
- The harness reuses the existing sources of truth: it reads the 44 floor from `--target` and
  the thresholds from the same constants `contrast.test.ts` uses. It **must not** introduce a second
  touch-target token, a second threshold constant, or a second contrast implementation
  (constitution VI; the repository's "find the existing source of truth" norm).

### FB-1 — Touch targets (G03)

- Every **primary touch control** on the enumerated routes measures ≥ 44 × 44 CSS px in both
  dimensions as rendered, unclipped, at coarse-pointer viewports.
- Controls that do not meet 44 × 44 must independently satisfy WCAG 2.5.8 (Target Size — Minimum,
  24 × 24) **or** carry a recorded exception under one of the criterion's own allowances (spacing,
  inline, equivalent control, user-agent control, essential). Exceptions are data, not comments —
  see the exception register in § 4.
- Remediation routes through the existing token. The `Button` size convention is the live decision
  point: `docs/ACCESSIBILITY.md` currently says moving the 44 floor into `Button`'s base class "is
  tracked in issue #953" — **#953 is closed**, so that documentation points at a dead tracker. The
  work must either (a) move the floor into the base class and delete the `size="lg"` convention from
  the doc, or (b) keep the convention and re-document it against a live decision. Either way the doc
  and the code agree afterwards, and the replaced path is deleted in the same change (no half-dead
  code).
- Disabled and non-hit-testable elements are out of the measured population, matching the audit's
  own framing ("enabled hit-testable").

### FB-2 — Composited text contrast (G04)

- Normal text measures ≥ 4.5:1 against its composited background; text qualifying as large under
  WCAG 1.4.3 (≥ 18.66px bold or ≥ 24px) and applicable essential graphics measure ≥ 3:1.
- Measurement covers **both themes** (light, and dark via both `prefers-color-scheme` and
  `[data-theme="dark"]` — the three authored blocks `contrast.test.ts` already distinguishes) and
  **stateful renderings**: hover, focus-visible, selected, and active.
- Inactive/disabled controls are classified as exempt under WCAG 1.4.3's inactive-component
  allowance and recorded as exemptions, not silently skipped.
- Any failure found in a literal colour value or an opacity modifier is fixed by routing through the
  token set; where a token pair itself is the failure, the token is fixed in place and
  `contrast.test.ts` is extended to cover the pair. Adding a second "corrected" colour alongside the
  failing one is explicitly disallowed.

### FB-3 — Page semantics and keyboard navigation (G05)

- Every route rendering a primary page exposes exactly one `h1`, owned by `PageHeader`, with
  `document.title` synced through the existing `shell/documentTitle` stack. `TopBar` continues to
  render none.
- Heading levels descend without skipping within a page.
- **Repeated landmarks carry unique accessible names.** Where two or more landmarks of the same role
  appear on one page (the Pipeline case named by G05), each gets a distinguishing `aria-label` or
  `aria-labelledby`.
- Focus is predictable: after route navigation focus moves to the page's heading or main region
  rather than being lost to `<body>`; closing an overlay returns focus to the element that opened it.
  This is the existing `useFocusTrap` / `useOverlayState` / `DrawerHost` behavior — the deliverable
  is **coverage and proof across every overlay**, not a new mechanism.
- Scroll-only content reachable only by pointer (the `AboutSection` long-content case) becomes
  keyboard-operable — a focusable scroll container, or an equivalent keyboard affordance.

### FB-4 — Verified conformance and the conformance matrix (G18)

- The repository gains a **criterion-level conformance matrix**: one row per applicable WCAG success
  criterion, each recording level, applicability, verification method (automated / manual /
  assistive technology / not yet verified), the evidence reference, and the date verified.
- Manual verification passes are executed and recorded against: VoiceOver + Safari (iOS and macOS),
  NVDA + Firefox or Chrome (Windows), TalkBack + Chrome (Android). Each pass covers the epic's
  critical journeys end to end.
- Physical-device verification covers rotation, on-screen keyboards occluding content, safe-area
  insets, 200% text sizing, 400% zoom with reflow, `prefers-reduced-motion`, forced-colors mode,
  live-region announcements, focus behavior, and error recovery.
- The matrix records **tested reality**, including "not yet verified" rows. A criterion with no
  evidence is never marked conforming.
- The terminal surface's existing documented partial conformance (WCAG "essential" exceptions for
  1.1.1 and 2.2.2, with the `aria-live` recent-output region, the always-visible "Back to sessions"
  link, and the helper-key bar as mitigations) is **carried forward verbatim**, re-anchored to
  whichever WCAG version the gate approves under PC-1.

---

## 3. Acceptance criteria

Each criterion is verifiable by a command or by a recorded artifact. "Enumerated routes" means the
route list the harness is configured with, which at minimum covers Sessions (list and detail),
Pipeline, Delivery, Roadmap/Initiative, Settings (index and sections), the archived Review and
Sub-agent Reader pages, and the Help and About overlays.

**AC-0 — Harness**
1. `npx playwright test` includes the new harness specs, and they pass against the built bundle.
2. The harness produces a JSON report enumerating every measured element with its measurement and a
   verdict of `pass` / `fail` / `exempt` / `indeterminate`.
3. Running the harness twice on an unchanged tree produces identical verdicts (no flake in the
   verdict layer; raw pixel values may differ within a stated tolerance).
4. `grep` confirms exactly one touch-target floor constant and one contrast-threshold constant are
   in use across the repository; the harness imports them rather than redeclaring them.

**AC-1 — Touch targets**
5. The harness runs at, at minimum, 393×852 and 390×844 portrait, their landscape rotations, and one
   tablet viewport, with a coarse pointer emulated.
6. On the enumerated routes, the touch-target pass reports **zero `fail` verdicts**. Every non-44
   control resolves as `exempt` with a named WCAG 2.5.8 allowance recorded in the exception register.
7. Measurement includes both dimensions, clipping by ancestors/overflow, and nearest-neighbour
   spacing (spacing is what makes the 24×24 allowance legitimate — it must be measured, not assumed).
8. `docs/ACCESSIBILITY.md`'s touch-target section matches the shipped code and cites no closed issue.

**AC-2 — Contrast**
9. On the enumerated routes, in light and both dark selectors, and in default/hover/focus-visible/
   selected/active states, the contrast pass reports **zero `fail` verdicts**.
10. Every `indeterminate` result is listed in the report with the reason it could not be resolved,
    and none is counted as a pass or a failure.
11. Every `exempt` result names the exemption (inactive component, decorative, essential graphic).
12. `web/src/__tests__/contrast.test.ts` still passes and has been extended to cover any token pair
    the composited pass found failing. No second contrast implementation exists.

**AC-3 — Semantics and keyboard**
13. An automated assertion — runnable in the existing `frontend` (vitest) job where jsdom suffices —
    fails if a primary route renders zero or more than one `h1`, or skips a heading level.
14. An automated assertion fails if any page renders two landmarks of the same role without distinct
    accessible names.
15. For every overlay (Dialog, Sheet, Drawer, Help, LegendPanel): Tab cycles within the overlay,
    Escape closes it, and focus returns to the opening element — asserted in tests, one per overlay.
16. After a route change, focus is verifiably not on `<body>`; asserted for each primary route.
17. The `AboutSection` long-content region is scrollable by keyboard alone, asserted in a test.

**AC-4 — Conformance matrix and manual verification**
18. A conformance matrix exists in the repository, is criterion-level, and every row has a
    verification method and a date or an explicit "not yet verified".
19. The three assistive-technology passes (VoiceOver/Safari, NVDA/Firefox-or-Chrome, TalkBack/Chrome)
    are each recorded with date, platform version, screen-reader version, journeys covered, and
    findings. Any finding becomes a tracked issue.
20. The physical-device checklist in FB-4 is recorded as executed, per device class.
21. `docs/ACCESSIBILITY.md` and the constitution state the **same** WCAG version, and the terminal
    partial-conformance claim is re-anchored to it. (Gated on PC-1.)

**AC-5 — Repository gates**
22. `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`,
    `pytest server/tests/` all pass (the four-step `lint-and-test` gate), and in `web/`:
    `npm run typecheck`, `npm run lint`, the **full** `npm run test`, `npm run build`.
23. No replaced path survives the change: any file, helper, token, or convention superseded by this
    work is deleted in the same PR that supersedes it.
24. Sibling occurrences of each fixed pattern are audited, not just the cited instance — the audit
    sweep is recorded in the PR body.

---

## 4. Data and API surface

This epic is frontend-and-verification work. **No HTTP endpoint, database model, column, migration,
WebSocket event, or session-engine contract is added or changed.** The daemon, orchestrator, and
edge are untouched, so none of the DB-clock, schema-reconcile, or Postgres-model rules apply.

The surface that *does* change:

| Surface | Kind | Nature of change |
| --- | --- | --- |
| `web/src/index.css` — `--target`, `--spacing-target`, `--t-*` colour tokens | Design tokens | Token values may be corrected in place. **No new token is added** for either concern. |
| `web/src/ui/Button.tsx` size variants | Component API | Possible: `min-h-target` moves from `lg` into the base class. If so, `size` keeps its current values and no call site changes meaning — a widening, not a breaking change. Decided under FB-1. |
| `web/src/ui/PageHeader.tsx` | Component API | No new props expected; adoption widens to routes that lack it. |
| Per-page landmark labelling | Markup | `aria-label` / `aria-labelledby` added to repeated landmarks. |
| Touch-target / contrast report | **New artifact** | JSON + summary in `web/test-results/` (git-ignored) plus a committed baseline. Schema: `{ route, viewport, theme, state, selector, measurement, verdict, exemption? }`. |
| Exception register | **New artifact** | Committed, machine-readable record of every `exempt` verdict: `{ selector, route, criterion, allowance, rationale, addedAt }`. The harness fails if a `fail` verdict has no matching register entry, so exceptions are reviewable in diff rather than buried in code comments. |
| Conformance matrix | **New artifact** | Criterion-level table. Location to be settled at the gate — see OQ-2. |
| `docs/ACCESSIBILITY.md` | Docs | Updated to match shipped code; terminal partial-conformance claim preserved and re-anchored. |
| `.github/workflows/ci.yml` | CI | New checks placed into an existing job. See OQ-1 — this determines whether the gate can block a merge. |
| `web/package.json` | Dependency | A real-browser axe integration (e.g. `@axe-core/playwright`) is likely required; `jest-axe` is present but jsdom-bound. Any new dependency must satisfy constitution VII (modern, vetted, maintained). |

**No product-code change is expected in `src/ccswitch/` or `server/`.** If one becomes necessary,
note that the shared `.venv` resolves `ccswitch.*` from the deploy worktree, so such a change must be
run with `PYTHONPATH=<checkout>/src`.

---

## 5. Non-goals

- **Not a redesign.** No visual language, layout, navigation, or information-architecture change
  beyond what a named criterion requires. Where a fix has a visual consequence, the minimal one wins.
- **Not WCAG AAA.** AA only, at the version resolved by PC-1.
- **Not the terminal's full conformance.** The xterm.js PTY surface keeps its documented partial
  conformance under the WCAG "essential" exceptions. Building an accessible terminal alternative is
  out of scope; the existing mitigations are preserved, not extended.
- **Not the living-docs rebrand.** `docs/ACCESSIBILITY.md`'s historical terminal-era framing is
  owned by [#1377](https://github.com/steiner385/viberpit/issues/1377) (ViberPit Phase 6: living
  docs). This epic changes only the accessibility *facts* in that file and does not take the rebrand.
- **Not a constitution amendment.** This spec cannot amend governance. It *requests* a decision
  (PC-1); the amendment, if approved, is a separate PR through the documented process.
- **Not the sibling E-epics.** Other children of #1393 outside G03/G04/G05/G18 are out of scope.
- **Not performance work.** The `lighthouse` job's performance category is untouched.
- **Not backfilling the audit's original numbers.** Per § 0, the historical counts are not targets
  and will not be reconciled against.
- **Not automating assistive-technology verification.** Screen-reader passes are human work; the
  deliverable is the recorded evidence, not a CI job pretending to do it.

---

## 6. Policy concerns — flagged for resolution at the human gate

> These are called out per the Design-stage requirement. Each needs a human decision before Build;
> none should be resolved by an implementing agent.

**PC-1 — Governance: WCAG 2.2 AA is a constitutional amendment, not a spec decision.**
G18 proposes WCAG **2.2** AA as the benchmark. The constitution states **2.1 AA**
(`.specify/memory/constitution.md`, § Development Workflow & Quality Gates), and `docs/ACCESSIBILITY.md`
is titled "Accessibility (WCAG 2.1 AA)". Raising the bar redefines a quality gate, so under the
constitution's Governance section it requires a PR that edits `.specify/memory/constitution.md`,
states the rationale, updates the version and Sync Impact Report, and — because it changes a
principle's practical meaning — carries explicit acknowledgement in the PR description. `amendment`
is a **required** status check on `master`, enforced by `scripts/check_constitution_amendment.py`,
so this is a hard gate, not a convention. G18's own acceptance text already says "any governance
amendment follows owner approval."
*Decision needed:* approve the 2.2 AA amendment (and sequence it **before** Build, since it changes
what AC-1's 24×24 allowance and AC-4's matrix are measured against), or keep 2.1 AA and descope
2.2-only criteria (2.5.8 Target Size (Minimum), 2.4.11 Focus Not Obscured, 3.2.6 Consistent Help,
3.3.7 Redundant Entry) from this epic.
*Note:* tooling also caps the ceiling — governed paths are write-blocked; a `grep` whose command
text merely names `.specify/memory/constitution.md` is denied by the managed governance hook. The
amendment must go through a human maintainer.

**PC-2 — Data handling: the source evidence is private and must not reach GitHub.**
Every child issue cites `~/Documents/ViberPit-UI-Review-2026-09-07/report.html` and states that
"private screenshots and account/session details are not attached to GitHub." The report, its
screenshots, and any capture containing account names, session ids, repository names, tokens, or
tailnet identities **must not** be committed, attached to a PR, or included in a test artifact.
*Constraint on Build:* before/after evidence is **sanitized** — synthetic fixture data only, from the
existing `web/src/test/factories` and `web/e2e/fixtures`. The harness's JSON report must be scrubbed
of user-identifying content before it is uploaded as a CI artifact or committed as a baseline. A
reviewer should treat an unsanitized screenshot in a PR as a blocking finding.
*Decision needed:* confirm that sanitized synthetic evidence is acceptable as the conformance record,
given that the original audit's evidence cannot be republished.

**PC-3 — Governance: shipping a conformance claim is a public statement.**
A criterion-level matrix asserting AA conformance is the kind of claim users and procurement rely on.
Under WCAG Conformance Claim guidance, a claim must state the version, level, scope, and the
technologies relied upon, and must not overstate. The existing terminal partial-conformance note is
the model. The constitution also requires compliance review "before any public exposure."
*Decision needed:* confirm the intended audience and wording of the matrix — internal engineering
record versus a published accessibility statement. These carry materially different review
obligations, and the answer changes how conservatively "not yet verified" rows must be worded.

**PC-4 — Supply chain: a new real-browser a11y dependency.**
Closing the composited-contrast gap most likely adds `@axe-core/playwright` or equivalent to
`web/package.json`. Constitution VII requires modern, vetted dependencies.
*Decision needed:* approve the specific package and version at review, or direct that the harness be
written against Playwright primitives with no new dependency. Note that the advisory `lighthouse`
job already runs an axe-derived accessibility category against the built bundle — extending it may
be cheaper than adding a runner, at the cost of route coverage (it audits a single URL).

**PC-5 — Process: this initiative has no `intent.md`, and the Design prompt assumed one.**
The synthesizer's Design prompt inlines `docs/sdlc/<slug>/intent.md` from `origin/master`. For an
`epic-adoption` initiative that file never exists, so the prompt degraded to "read that file from the
repository," which is unsatisfiable. This session recovered by reading epic #1395 and its children.
*No decision needed for this epic* — recorded so the loop can be fixed (the Design prompt builder
should inline the adopted epic body when `origin == "epic-adoption"`). Filed as ViberPit-harness
feedback, not repository work.

**Security:** no new authentication, authorization, network, credential, or data-egress surface is
introduced. No security concern identified.
**ToS:** no third-party service, model provider, or vendor surface is involved. No ToS concern
identified.

---

## 7. Open questions for the gate

- **OQ-1 — Where does the new gate live, and can it block a merge?** Branch protection on `master`
  requires exactly `["lint-and-test", "frontend", "amendment"]`. The `e2e` and `lighthouse` jobs run
  but are **advisory by design** — `ci.yml` comments say the CI/CD Doctor tracks their stability and
  proposes graduation to required once proven (#453). A real-browser harness naturally belongs in
  `e2e`, where it **cannot block a regression from merging**. Options: (a) land it advisory in `e2e`
  and accept that regressions are caught after the fact; (b) land it in `e2e` and graduate `e2e` to a
  required check as part of this epic — which changes merge risk for every PR in the repo, not just
  these; (c) split — jsdom-checkable semantics (AC-3) go in `frontend` where they block today, and
  geometry/contrast stay advisory in `e2e`. **(c) is the recommendation**: it blocks what can be
  blocked cheaply today without coupling this epic to a repo-wide CI-policy change.
- **OQ-2 — Where does the conformance matrix live?** Inside `docs/ACCESSIBILITY.md` (one document,
  but it grows long and #1377 owns that file's rebrand), or a sibling `docs/accessibility/conformance.md`
  linked from it (cleaner ownership, but two files to keep honest). Recommendation: sibling file,
  with `docs/ACCESSIBILITY.md` linking to it as the single pointer.
- **OQ-3 — Does the exception register need per-entry expiry?** An exemption recorded once can
  outlive its rationale. A review date per entry makes the register self-auditing at the cost of
  recurring churn.
- **OQ-4 — Which journeys define "critical" for the AT passes (AC-4)?** The audit does not enumerate
  them. Proposal: launch a session, monitor a running session, respond to a permission card, review a
  PR/Delivery item, and change a setting. Needs confirmation.
- **OQ-5 — Sequencing against in-flight work.** G04 and G05 are currently **Blocked** and G18 is
  **Backlog** in the epic's project view; G03 is Ready. The Plan stage should confirm what the native
  blocked-by edges point at before ordering the work, and check for open PRs touching
  `web/src/ui/`, `web/src/index.css`, and `web/src/shell/regions/` before implementation starts.

---

## 8. Constitution check

| Principle | How this spec complies |
| --- | --- |
| I — no forked binaries, gate experimental surfaces | No agent binary touched. No experimental vendor surface. |
| II — account isolation, one credential authority | No account, credential, or session-isolation surface touched. |
| III — verify against the real binary/API | Central to this spec: § 0 re-derives scope from the real tree rather than trusting the audit's stale counts, and FB-0 measures a real browser rather than asserting against jsdom. |
| IV — security-first, exposure-conditional auth | No new exposure. PC-2 constrains evidence handling; PC-3 constrains the public claim. |
| V — TDD + quality gates | AC-0 lands the harness before remediation; AC-5 requires the full four-step `lint-and-test` gate plus the four web scripts. |
| VI — single source of truth / no dead code | FB-0 and AC-0.4 forbid a second target token, threshold, or contrast implementation; AC-5.23 requires replaced paths deleted in the same PR; FB-1 resolves the `#953` doc/code drift. |
| VII — modern vetted deps | PC-4 routes any new dependency through explicit approval. |

---

*This is a propose-only Design artifact. It specifies; it does not implement, amend governance, or
authorize execution.*
