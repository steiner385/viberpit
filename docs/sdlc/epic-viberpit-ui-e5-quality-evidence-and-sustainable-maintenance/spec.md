# Spec — ViberPit UI E5: Quality evidence and sustainable maintenance

| | |
|---|---|
| **Initiative** | `epic-viberpit-ui-e5-quality-evidence-and-sustainable-maintenance` |
| **Stage** | Design (propose-only; this artifact merges at a human gate) |
| **Epic** | [steiner385/viberpit#1398](https://github.com/steiner385/viberpit/issues/1398) — part of review initiative #1393 |
| **Children** | G13 [#1411](https://github.com/steiner385/viberpit/issues/1411) · G14 [#1412](https://github.com/steiner385/viberpit/issues/1412) · G15 [#1413](https://github.com/steiner385/viberpit/issues/1413) · G16 [#1414](https://github.com/steiner385/viberpit/issues/1414) · G17 [#1415](https://github.com/steiner385/viberpit/issues/1415) |
| **Specced against** | `3ba8665c` (master, 2026-09-17) |
| **Audit reviewed** | `07c02a99` (2026-09-07) |

## 0. Provenance — read this first

**There is no `intent.md` for this initiative, and this spec was not written from one.** The Design
dispatch for this stage carried the literal placeholder "could not inline
`docs/sdlc/epic-viberpit-ui-e5-quality-evidence-and-sustainable-maintenance/intent.md` from
`origin/master` — read that file from the repository before starting." That file does not exist —
not on `origin/master`, not on any remote branch, not in any open or merged PR, and not in the
initiative's own artifact index (`GET /api/sdlc/initiatives`, which lists exactly one artifact: this
spec).

The cause is structural: this initiative's `origin` is `epic-adoption` with `originRef`
`steiner385/viberpit#1398`. It was adopted from an existing GitHub epic rather than seeded by an
intake trigger, so it entered the pipeline at Design and the intent stage that writes `intent.md`
never ran. `build_spec_prompt` (`server/sdlc/prompts.py`) inlines `intent.md` unconditionally, so
every epic-adopted initiative hits this same gap — confirmed for a sibling on
`epic-viberpit-ui-e1-core-workflows-and-recovery` (spec PR #1578, see its own §0). **The upstream
intent used here is epic #1398 plus its five sub-issues (G13–G17)**, which carry current/target
state, proposed acceptance tests, and planning metadata in place of an `intent.md`. They are
read-only inputs and were not modified. This recurring gap is filed again below as a policy concern
(§7 P4) rather than re-litigated per initiative — the fix belongs in `server/sdlc/prompts.py`, not
in any one spec.

### This epic is deliberately parked — the pipeline dispatched Design anyway

This is the governance issue specific to E5, distinct from the missing-`intent.md` structural gap
above. Epic #1398 and all five of its children (#1411–#1415) currently carry the `parked` label.
The epic's most recent comment (2026-09-08, `steiner385`) reads verbatim:

> Parked 2026-09-08: evidence and kiosk-trust work comes after the surfaces it measures are
> settled. Resumes after the finish line
> (`docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` §6).

That design doc's §6 ("The parking lot") independently confirms the same disposition: *"UI-audit
epics E3–E5 (#1396–#1398) and their leaves (#1408–#1418): evidence and kiosk-trust work after the
surfaces they measure are settled."* No comment, commit, or label change on #1398 or any child
issue since 2026-09-08 lifts the `parked` label or declares the finish line met. Nonetheless the
`sdlc-synthesizer` automation dispatched this Design session against a parked epic on 2026-09-17.

This spec proceeds — refusing to produce it would just leave the initiative stuck at
`awaiting_human` with no artifact to react to — but **whether Plan should be allowed to start from
it is exactly the parking decision the epic owner already made and has not yet reversed.** See §7 P1
and §8.

### The audit's scope references are partially stale — this spec uses current paths

Every child issue links scope files pinned to blob `07c02a995e4c0cf72a42efcf5b9bc61496dad377`
(2026-09-07). Master has moved substantially since — specifically the finish-line rows that this
same parking comment says E5 is waiting on (rows 2–8, landed 2026-09-09 through 2026-09-12). Four of
the nineteen distinct paths the five issues cite no longer exist:

| Audit reference (`07c02a99`, cited by) | Status on `3ba8665c` | Current home |
|---|---|---|
| `web/src/pipeline/plotroom.css` (G16) | **moved** | `web/src/features/pipeline/charts/plotroom.css` — now the sole CSS-allowlist exception (chart canvas styling) |
| `web/src/projects/projects.css` (G16) | **deleted** | Roadmap section — `web/src/features/roadmap/` (Products/Initiatives consolidation, #993, row 2) |
| `web/src/initiatives/initiatives.css` (G16) | **deleted** | same — `web/src/features/roadmap/` |
| `web/src/components/InsightsView.tsx` (G17) | **deleted** | Delivery section — `web/src/features/delivery/` (CI health scope, row 4, #1468) |

All current-state claims in §2 below were re-verified against `3ba8665c`, not inherited from the
audit. Fifteen of the nineteen cited paths (`routes.tsx`, `main.tsx`, `vite.config.ts`,
`playwright.config.ts`, `web/e2e`, `package.json`, `navDefs.ts`, `docs/product`, `eslint.config.js`,
`.github/workflows/ci.yml`, `web/src/obs`) are unchanged and were re-read directly.

### The referenced "root scorecard/conformance record" is not in this repository

All five child issues' acceptance checklists end with the same line: *"Attach sanitized before/after
evidence and update the root scorecard/conformance record."* Their shared evidence source is *"Local
complete report: `~/Documents/ViberPit-UI-Review-2026-09-07/report.html`"* — a file on the auditor's
own machine, not checked into `docs/`, not linked from any issue as a URL, and not reachable by this
or any future pipeline session. No in-repo document currently plays that role either; the closest
artifact, `docs/audits/2026-09-02-ui-framework-audit.md`, is a different (earlier, structural) audit.
Flagged at §7 P5 — an acceptance criterion that points at an artifact automation cannot read or
write needs a human decision about what "the scorecard" means before Plan can treat it as
verifiable.

---

## 1. Summary

E5 differs in kind from its sibling epics: it does not ask for defect fixes but for **measurement
infrastructure and baselines** so that the next UI audit can cite numbers instead of expert opinion.
Every one of its five leaves is explicitly self-classified *"benchmark opportunity... a product audit
finding/opportunity, not a PR approval."* The five leaves are independent:

- **G13** — field performance measurement (Core Web Vitals RUM) and an initial-bundle size budget.
- **G14** — cross-browser (not just Chromium) visual and accessibility regression gates.
- **G15** — a human usability baseline (SUS/SEQ/VisAWI-style scores, task success rate).
- **G16** — finish the shared page/design-token migration alongside the portfolio consolidation.
- **G17** — a UI maintenance-cost and delivery-stability scorecard (DORA-style plus UI-specific cost).

Because the audit is ten days stale relative to master and master's finish-line work (§0) already
targeted several of the same surfaces, the leaves are **not equally live**: G13, G14, G15, and G17's
core evidence (no RUM, Chromium-only Playwright, no usability baseline, no cost/DORA scorecard) is
unchanged and confirmed current. **G16 is substantially already resolved by other landed work** — see
§2 — and its remaining scope has narrowed sharply.

Each leaf is independently shippable; #1398 records no native blocked-by edges between G13–G17, so
Plan may sequence them in any order (subject to the parking question in §7 P1).

---

## 2. Functional behavior

### G13 — Field performance measurement and an initial-bundle budget (#1411)

**Current state (re-verified against `3ba8665c`).** `web/src/obs/` contains only `logger.ts` (a
redacted client-log sink posting to `POST /api/client-logs`, batched and beacon-flushed on
`pagehide`/`visibilitychange`) and `install.ts` (wires `window.onerror` /
`unhandledrejection` into that logger). Neither collects Core Web Vitals (LCP/INP/CLS) or any other
field-performance signal — the audit's "no web-vitals collection found" still holds. `web/vite.config.ts`
has no `manualChunks` or chunking strategy, and a full-text search of `web/src/shell/routes.tsx` for
`lazy(` or dynamic `import(` returns nothing: every route is still imported eagerly, matching the
audit. The audit's build numbers (main JS 1,553.85 kB / 453.61 kB gzip; CSS 145.43 kB / 25.67 kB gzip;
a separate 344.72 kB gzip HEIC asset) are ten days old and MUST be re-measured at Plan/Build time
against master's current tip, not carried forward as fact.

**Target behavior.** Populate p75 field LCP/INP/CLS, segmented by browser and device/route, from real
sessions — not lab numbers alone — and set a lab budget (issue proposes ≤250 kB gzip main-JS,
explicitly a starting point, not a mandate) enforced in CI. `.github/workflows/ci.yml` already runs an
advisory (non-required) `lighthouse` job against a production `vite preview` build; it currently
enforces no budget, only uploads a report.

**What this asks Plan to design**, per constitution VI (route through the existing source of truth
rather than add a parallel one):

- Extend the existing `POST /api/client-logs` sink for RUM events rather than standing up a second
  telemetry endpoint — it is already the "one redacted, correlated JSON sink" for client-side signal.
  Plan must confirm its existing redaction covers whatever new event shape RUM adds (§7 P3).
- A CI-enforced bundle-size gate is a new mechanism; no existing one exists to reuse. Plan should
  decide whether it is a check inside the existing `lighthouse` job (promoting some or all of that
  job from advisory to required, per constitution V's "zero linter/type-checker errors"-style gate
  philosophy) or a separate step, and whether route-level code-splitting is in scope for this leaf or
  deferred to whoever owns bundle-size remediation.

### G14 — Representative cross-browser visual and accessibility regressions (#1412)

**Current state (re-verified).** `web/playwright.config.ts` still declares exactly one project,
`chromium`, using `devices['Desktop Chrome']` — no Firefox or WebKit project exists, matching the
audit. `web/e2e/` holds the existing 13 specs the audit counted. Unlike the audit's framing, unit-level
accessibility tooling is *not* absent: `jest-axe` (`toHaveNoViolations`) is already used across 30
files in `web/src`, and `EmptyState` (a shared empty/error primitive the 2026-09-02 structural audit
found used exactly once) is now used in 24 files — most of that gap closed by unrelated
finish-line/page-migration work, not by this epic. What the audit called out as still missing — a
second/third real browser engine in Playwright, deterministic seeded E2E fixtures, and geometry/visual
assertions — is unchanged.

**Target behavior.** Deterministic fixtures; Chromium **and** Firefox **and** WebKit coverage for a
representative (not exhaustive) set of flows; layout/geometry and visual-diff assertions, not just
functional pass/fail; a validated accessibility toolchain (the audit flagged specific false-positive
categories — "invalid modern-color and native-checkbox-switch findings" — from whatever analyzer
produced them, which needs identifying and either fixing or documenting as a known limitation rather
than blanket-suppressing); a proposed flake budget under 1%; stable subset promoted to a required
check, broader coverage kept nightly/advisory.

### G15 — Usability, simplicity, intuitiveness, and visual-appeal baseline with real users (#1413)

**Current state (re-verified).** `docs/product/` (39 feature/guide docs) contains no user-research
artifact — no SUS/SEQ/VisAWI/UEQ score, no task-success-rate data, no session-recording or
think-aloud study. `web/src/obs/` has no task-outcome instrumentation. The audit's "expert inspection
and screenshots cannot supply those scores" premise is unchanged: nothing in the repo substitutes for
a real user study.

**Target behavior.** A **product-chosen** target, not a universal mandate (the issue is explicit about
this): proposed ≥95% critical-task success, SUS≥80/100, SEQ≥6/7, first-attempt navigation≥90%,
VisAWI-S≥5.5/7. Method: 5–8 participants per cohort for formative rounds, 20–30+ with confidence
intervals for a quantitative baseline; cohorts spanning novice/expert and keyboard/touch input;
outcome measures, not engagement-time proxies.

**This leaf is qualitatively different from the other four**: it requires recruiting and running
sessions with real human participants, which no Design/Plan/Build pipeline stage can do on its own.
See §6 (non-goals) and §7 P2 (participant data handling) — this is the leaf most in need of explicit
human ownership before any code-facing plan is written.

### G16 — Complete shared page/design-token adoption alongside the portfolio plan (#1414)

**Current state (re-verified — this is where the ten-day-stale audit diverges most from reality).**
The audit's premise was: *"Legacy Pipeline/Products/Initiatives/Insights still use raw CSS or
palettes; token lint covers ui/shell/features only."* On `3ba8665c`:

- `web/src/components/` (the audit's "bucket" of ungoverned components) **no longer exists** — emptied
  by finish-line row 7.
- Repo-wide, exactly **two** CSS files remain under `web/src`: `web/src/index.css` and
  `web/src/features/pipeline/charts/plotroom.css` — the latter is the chart-canvas allowlist exception
  the finish-line spec's own success criteria (§7 there) name explicitly. `projects.css` and
  `initiatives.css` are gone (§0 table).
- `web/eslint.config.js`'s `viberpit/no-raw-palette` rule already applies repo-wide — its comment
  reads *"Token discipline covers the whole tree (row 7, 2026-09)"* — not scoped to `ui/shell/features`
  as the audit reported.
- `EmptyState` usage grew from the structural audit's "used once" to 24 call sites; not yet
  universal, but no longer the near-total gap G16 describes.
- `#993` (the portfolio/Initiatives consolidation G16 was told to coordinate with rather than
  duplicate) is closed and merged (Roadmap Phase 3, row 2, 2026-09-09; Phases 4–5, row 8,
  2026-09-12) — the coordination point this issue asked for no longer has open work to coordinate
  with.
- One narrow gap remains: four `dark:` Tailwind-variant literals still exist outside `index.css` —
  `web/src/shell/canvas.ts`, `web/src/ui/Button.tsx` (and its test), and `web/src/ui/Pills.test.tsx`
  — short of the finish-line success criterion of zero.

**Target behavior.** The issue's original ask (extend the page/token contract, centralize
empty/error/loading presentation, delete replaced paths, avoid a second portfolio implementation) is
**already met in substance** by #993 and finish-line row 7, both of which landed after this issue was
filed and neither of which this epic's SDLC lineage produced. What's left is materially smaller than
the issue text implies: close the four residual `dark:` literals, confirm remaining pages not yet on
`EmptyState`/the shared page contract are enumerated and either migrated or explicitly deferred, and
disposition #1414 itself (resolve as "target substantially met by #993 + row 7, remainder is the
four-file cleanup" rather than re-scoping a page migration that already happened).

### G17 — UI maintenance cost and delivery stability (#1415)

**Current state (re-verified).** The Delivery section (`web/src/features/delivery/`, specifically
`ci/CiRepoDetail.tsx` and `ci/CheckTable.tsx`) already surfaces per-repo CI check health — this is the
successor to the audit's referenced (now-deleted) `InsightsView.tsx` (§0 table), built by finish-line
row 4 independent of this epic. It is a check-health/timing view, not a cost or DORA scorecard: a
repo-wide search of `docs/` for DORA-framework terms ("deployment frequency," "lead time for
changes," "change failure rate") returns only two unrelated check-health design docs
(`docs/superpowers/specs/2026-07-01-insights-*-design.md`). No UI-specific repair-hour, CI-cost, or
dependency-patch-lag baseline exists anywhere in the repo, matching the audit.

**Target behavior.** Track the five DORA metrics plus UI-specific repair hours, CI minutes/cost,
dependency-patch lag, and routine-change spread. Collect four weeks with explicit attribution and
denominators; separate labor cost from infrastructure cost, using **owner-supplied loaded hourly
rates** (the issue's own words) — this is a business input only the repo owner can provide, not
something Plan or Build can infer or default (§7 P6). Proposed 90-day goal: 25% less UI rework than
the measured baseline, reviewed at 30/60/90 days without retroactively inventing savings.

---

## 3. Acceptance criteria

All five child issues share four boilerplate checklist lines beyond their leaf-specific first bullet
("Add focused regression evidence and apply the relevant repository quality gates"; "Audit sibling
occurrences and use the existing source of truth; delete replaced paths in the same change"; "Attach
sanitized before/after evidence and update the root scorecard/conformance record" — see §0 on why
that last one needs a gate decision before it is verifiable). Leaf-specific criteria:

### G13
- [ ] p75 field LCP/INP/CLS collected and segmented by browser/device/route, distinguishing lab from
  field measurement; optional-chunk loads recorded; **no prompts, tokens, or account names ever
  collected** — this is the issue's own explicit constraint and MUST be verified against whatever
  event shape Plan designs, not assumed from the endpoint's existing redaction.
- [ ] A populated (not placeholder) lab budget is enforced somewhere in CI, with sibling occurrences
  of ad-hoc perf checks (if any) consolidated into it.

### G14
- [ ] At least one currently-unverified defect (a real cross-browser/geometry/a11y gap, not a
  synthetic one) is seeded as a regression and demonstrated to fail under deliberate breakage, with
  screenshots/traces published as evidence.
- [ ] Chromium + at least one additional real engine run in CI for the representative flow set.
- [ ] Each analyzer-limitation category the audit flagged is classified (fixed vs. documented
  limitation), not blanket-suppressed.
- [ ] Flake rate on the promoted subset is measured and under the proposed 1% budget before promotion
  to required.

### G15
- [ ] Sample sizes meet the issue's own floor (5–8 formative per cohort; 20–30+ with stated confidence
  intervals for the quantitative baseline).
- [ ] Both novice/expert and keyboard/touch cohorts are represented.
- [ ] Reported metrics are outcome-based (task success, SUS/SEQ/VisAWI scores), not proxies like time-
  on-task alone.
- [ ] Participant data handling matches §7 P2 before any session is run.

### G16
- [ ] The four residual `dark:` literals (§2) are resolved or each has a documented, reviewed
  exception in the same place the finish-line spec's success criteria are checked.
- [ ] Any page still outside the shared `EmptyState`/page-contract convention is either migrated or
  explicitly listed as an intentional exception — not silently left uncounted.
- [ ] #1414 is dispositioned against #993 and finish-line row 7 rather than treated as a fresh page
  migration.

### G17
- [ ] Four consecutive weeks of DORA + UI-cost data collected with explicit attribution and
  denominators (not estimates).
- [ ] Labor cost uses an owner-supplied loaded hourly rate (§7 P6), never an invented default.
- [ ] 30/60/90-day reviews are scheduled and report against the baseline without retroactive
  adjustment.

### Gates applying to every child
- `npm run typecheck`, `npm run lint` (`--max-warnings 0`), `npm run test` (full suite, not
  changed-files-only — per the repo's own testing norms, ~47 files mock shared hooks), `npm run
  build` for any web-touching change; the four-step server gate (`ruff`, `mypy`, `pytest tests/`,
  `pytest server/tests/`) for anything touching `server/obs` or `server/api`.
- No collected evidence (screenshots, traces, RUM payloads, usability recordings) may contain session
  transcript content, prompts, tokens, or account identifiers, matching constitution IV's
  secrets-never-logged rule and the existing "audio is never persisted or logged" precedent set for
  STT (`server/stt/`).
- Any new CI job or promoted-to-required check must state its added runtime/cost and be reviewed
  against the existing CI budget before promotion (§7 P7).

---

## 4. Data and API surface

### Client-side persisted state
None of G13/G14/G16 require new client-persisted state. G13's RUM collection is transient
(batched in-memory, flushed via the existing beacon path) — it does not need a new local-storage or
IndexedDB surface distinct from what `web/src/obs/logger.ts` already has.

### HTTP surface — extend, don't duplicate
- **G13**: extend `POST /api/client-logs` (existing, `server/obs/`) for RUM events rather than adding
  a new ingestion endpoint. Plan must define the event schema and confirm the existing sink's
  redaction/correlation logic covers it.
- **G17**: a DORA/cost scorecard should read from data ViberPit's own CI/PR observability likely
  already surfaces (Delivery's `ci/CiRepoDetail.tsx`, GitHub Actions/PR history, existing CI-cost
  reporting if any) before Plan considers a new ingestion pipeline — constitution VI applies here as
  much as to any UI component.
- **G14/G15/G16**: no new HTTP surface identified; these are test-infrastructure, research-process,
  and cleanup changes respectively.

### Internal contracts introduced
None yet — this Design artifact does not introduce a RUM event schema, a bundle-budget config format,
or a DORA-metric data model. Those are Plan-stage decisions; this spec constrains them (reuse
`/api/client-logs`, reuse Delivery's CI data, no new telemetry pipeline without justification) rather
than defining them.

---

## 5. Cross-cutting constraints

- **Constitution VI (single source of truth).** G13 must extend `/api/client-logs`, not add a second
  telemetry endpoint. G16 must not re-implement page/token migration work #993 already did. G17 must
  not duplicate whatever CI-cost/timing data the Delivery section or existing CI tooling already
  captures.
- **Constitution IV (security-first, secrets-safe) and its RUM analogue.** No field-performance,
  visual-regression, or usability-research artifact may capture session transcript content, prompts,
  tokens, or account identifiers — this is the issue text's own stated constraint for G13 and the
  spirit clearly extends to G14's traces/screenshots and G15's recordings.
- **Constitution V (test-driven quality gates).** Any newly-required CI check (promoted Lighthouse
  budget, Firefox/WebKit Playwright projects) becomes part of the non-negotiable gate set once
  promoted — it must be demonstrably stable (the proposed <1% flake budget) before promotion, not
  promoted first and stabilized after.
- **CI cost/runtime.** Adding two more Playwright browser engines and any new required Lighthouse
  budget check increases CI minutes on every PR touching `web/`. This should be sized and reviewed at
  Plan time, not discovered after rollout (§7 P7).

---

## 6. Non-goals

- Re-doing the page/token migration #993 and finish-line row 7 already completed — G16's remaining
  scope is the residual cleanup in §2, not a restart.
- Building a new telemetry ingestion pipeline for RUM or DORA data when `/api/client-logs` and
  existing CI/Delivery data can be extended instead.
- Actually recruiting, scheduling, or running the G15 usability study sessions from within this SDLC
  pipeline — no Design/Plan/Build stage can conduct human-subjects research; this spec defines the
  target and constraints, not the study logistics.
- Resolving whether epic #1398 should be unparked — that decision belongs to whoever set the `parked`
  label, not to this artifact (§7 P1).
- Fixing the four residual `dark:` literals or any other implementation change — this is a Design
  artifact; remediation is Plan/Build's job.
- Inventing a loaded hourly rate, a bundle-size budget number, or usability-score thresholds as final
  — the issue text itself calls all of these "proposed"/"starting" values pending product-owner sign-
  off, not mandates this spec ratifies.

---

## 7. Policy concerns — flagged for the human gate

### P1. Governance — this epic is parked; the pipeline dispatched Design anyway
Epic #1398 and all five children carry the `parked` label with an explicit resume condition ("after
the finish line," §0). Finish-line rows 2–8 (the UI surfaces this epic measures: Roadmap, Delivery,
Pipeline Workspace, Archived Reader, the token/palette sweep, Roadmap Phases 4–5) landed
2026-09-09 through 2026-09-12 per `git log`. Row 9 (server-side backlog-runner work, tracked at
#1520) and any explicit action to remove the `parked` label or declare the finish line met have not
happened as of this spec's writing (2026-09-17). **Recommendation: hold this spec at the gate — do
not advance to Plan — until whoever owns the `parked` label either lifts it or explicitly authorizes
Plan to proceed despite it.** Advancing an epic past its own owner's parking decision, purely because
automation dispatched a session against it, is the exact failure mode the parking mechanism exists to
prevent.

### P2. Data handling — G15 requires human-subjects research
Running a usability study means recruiting and recording real participants. This needs, at minimum:
informed consent, a data-retention/deletion policy for any recordings or session logs, and assurance
that no recording captures actual ViberPit session content (prompts, account identifiers, or other
users' data) alongside the participant's think-aloud audio/video. None of this is addressed in the
source issue, and no pipeline stage can supply it — a human must own the research-ethics and consent
process before G15 work starts.

### P3. Data handling — RUM must not widen what the client-logs sink accepts
G13's own text says "never collect prompts, tokens, or account names." Extending
`/api/client-logs` for RUM (§4) makes this a live risk, not a hypothetical one: whatever RUM event
shape Plan designs must be checked against the existing endpoint's redaction logic, not assumed safe
because the endpoint is already "redacted."

### P4. Governance — recurring provenance gap for every epic-adopted initiative
As in §0: `build_spec_prompt` inlines a nonexistent `intent.md` for any `origin: epic-adoption`
initiative. This is now confirmed on at least two initiatives (E1, E5) and will recur for every
remaining epic-adopted initiative until `server/sdlc/prompts.py` is fixed to detect `epic-adoption`
origin and inline the epic + children instead. Filed as a process defect, not a per-spec one.

### P5. Provenance — the acceptance criteria's "root scorecard" is unreachable
All five children's acceptance checklists require updating "the root scorecard/conformance record,"
whose only named instance is a local file on the auditor's machine (§0). Until a human either
designates an in-repo document as that scorecard or removes/rewrites this criterion, it cannot be
verified by any automated stage.

### P6. Business input required — G17 needs an owner-supplied cost rate
G17's cost-tracking target explicitly requires "owner-supplied loaded hourly rates" to convert time
into dollars. No default is safe to assume; Plan cannot proceed on the cost half of G17 without this
number from the repo owner.

### P7. Cost/ops — expanding required CI surface
Adding Firefox/WebKit Playwright projects (G14) and a bundle-size/Lighthouse budget gate (G13)
increases per-PR CI minutes and, if promoted to required checks, increases the surface that can turn
`master`-bound PRs red. Recommend sizing the added runtime before promotion and keeping new gates
advisory until the flake-budget evidence in the acceptance criteria is in hand — mirroring how
`lighthouse` is already deliberately advisory today.

---

## 8. Open questions for the gate

1. Does the human owner of the `parked` label consider the finish-line condition satisfied? If not,
   should this spec (and the initiative) stay parked rather than advance to Plan? (P1)
2. Who recruits and consents G15's participants, and under what data-retention policy? (P2)
3. What in-repo document, if any, should become "the root scorecard/conformance record" the
   acceptance criteria reference? (P5)
4. What loaded hourly rate should G17's cost model use? (P6)
5. Should any part of G13/G14's new checks be required from day one, or must they run advisory until
   flake/stability evidence exists, per the existing `lighthouse` precedent? (P7)
6. Given G16's scope has substantially collapsed since the issue was filed (§2), should #1414 be
   re-scoped/re-titled to the residual cleanup, or closed with a comment pointing at #993 and
   finish-line row 7?

---

## 9. Traceability

| Child | Title | Spec section(s) |
|---|---|---|
| G13 (#1411) | Measure field performance and budget the initial bundle | §2 G13, §3 G13, §4 (client-logs extension), §7 P3, P7 |
| G14 (#1412) | Gate representative cross-browser visual and accessibility regressions | §2 G14, §3 G14, §5, §7 P7 |
| G15 (#1413) | Baseline usability, simplicity, intuitiveness and visual appeal with users | §2 G15, §3 G15, §6, §7 P2 |
| G16 (#1414) | Complete shared page and token adoption alongside the portfolio plan | §0 (stale-reference table), §2 G16, §3 G16, §6 |
| G17 (#1415) | Track UI maintenance cost and delivery stability | §2 G17, §3 G17, §4 (data source reuse), §7 P6 |
| — | Epic-adoption provenance gap | §0, §7 P4 |
| — | Parked-epic governance gap | §0, §7 P1, §8.1 |
