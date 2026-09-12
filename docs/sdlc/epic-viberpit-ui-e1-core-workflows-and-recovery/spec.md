# Spec — ViberPit UI E1: Core workflows and recovery

| | |
|---|---|
| **Initiative** | `epic-viberpit-ui-e1-core-workflows-and-recovery` |
| **Stage** | Design (propose-only; this artifact merges at a human gate) |
| **Epic** | [steiner385/viberpit#1394](https://github.com/steiner385/viberpit/issues/1394) — part of review initiative #1393 |
| **Children** | G01 [#1399](https://github.com/steiner385/viberpit/issues/1399) · G02 [#1400](https://github.com/steiner385/viberpit/issues/1400) · G06 [#1404](https://github.com/steiner385/viberpit/issues/1404) · G07 [#1405](https://github.com/steiner385/viberpit/issues/1405) · G08 [#1406](https://github.com/steiner385/viberpit/issues/1406) · G20 [#1418](https://github.com/steiner385/viberpit/issues/1418) |
| **Specced against** | `95eaeeeb` (master, 2026-09-11) |
| **Audit reviewed** | `07c02a99` (2026-09-07) |

## 0. Provenance — read this first

**There is no `intent.md` for this initiative, and this spec was not written from one.**

The Design prompt for this stage inlined
`docs/sdlc/epic-viberpit-ui-e1-core-workflows-and-recovery/intent.md` as its upstream input. That
file does not exist — not on `origin/master`, not on any remote branch, not in any open or merged
PR, and not in the initiative's artifact index (which lists exactly one artifact: this spec).

The cause is structural, not a lost file. This initiative's `origin` is `epic-adoption` with
`originRef` `steiner385/viberpit#1394`: it was adopted from an existing GitHub epic rather than
seeded by an intake trigger, so it entered the pipeline at Design and the intent stage that writes
`intent.md` never ran. `build_spec_prompt` assumes the intake path unconditionally.

**The upstream intent used here is epic #1394 plus the six sub-issues**, which carry the
current/target state, source evidence and acceptance tests that an `intent.md` would have carried.
They are read-only inputs and were not modified. See [§7 P6](#p6-governance--the-design-stage-ran-without-its-declared-upstream-input) —
this is filed as a governance concern for the gate, because the same gap will recur for every
epic-adopted initiative.

### The audit's scope references are stale — use the paths in this spec

Every child issue links scope files pinned to blob `07c02a99` (2026-09-07). Master has moved
substantially since (finish-line rows 4–7b). Three referenced files no longer exist:

| Audit reference (`07c02a99`) | Status on `95eaeeeb` | Current home |
|---|---|---|
| `web/src/components/InsightsView.tsx` | **deleted** | Delivery section — `web/src/features/delivery/` (row 4, #1552) |
| `web/src/projects/ProjectsTab.tsx` | **deleted** | Roadmap section — `web/src/features/roadmap/` (#1478) |
| `web/src/pipeline/QueueTrain.tsx` | **moved** | `web/src/features/pipeline/charts/QueueTrain.tsx` (row 5, #1556) |

All current-state claims below were verified against `95eaeeeb`, not inherited from the audit.

---

## 1. Summary

Six independent defects share one theme: **the UI stops being recoverable when conditions leave the
happy path** — a short viewport, a narrow screen, a navigation away, a failed read, an ambiguous
anchor, a render exception. Each ends with the user unable to reach a control, unable to identify
what they are looking at, or unable to tell broken from empty.

This spec is scoped to behavior. It names the source of truth each fix must route through, because
five of the six have an existing mechanism that the fix must extend rather than duplicate
(constitution VI).

Each requirement is independently shippable. No child blocks another; #1394 records four children as
`blocked` by native links, which the Plan stage must re-read at decomposition time rather than
inferring order from this document.

---

## 2. Functional behavior

### G01 — Sessions stays usable at short heights, in both orientations

**Current state (verified).** `web/src/index.css:18` defines a single height breakpoint,
`@custom-variant short (@media (max-height: 560px))`, and `SessionListColumn` /
`ScopeBody` shed the controls strip with `short:hidden`. `SessionListColumn.tsx:117-126` documents
the design: the column is a stack of `shrink-0` bands around one scroll band, clipped with
`overflow-hidden` so surplus chrome cannot paint under the dock.

The mechanism is sound but the threshold under-covers. **At 320×568 — the audit's first reported
failure — the viewport is 568px tall, so `short:` never fires** and no chrome is shed at all; the
populated scroll band collapses to ~8px while every band around it keeps full height. At 568×320 the
New Session button is reported as covered. The existing regression vehicle,
`web/e2e/short-viewport.spec.ts`, pins exactly one size (393×480) where the variant does fire, which
is why the suite is green against a live defect.

**Target behavior.**

- **FR-G01.1** At every supported viewport, the session list scroll band, the prompt dock and
  primary navigation are all reachable and hit-testable. Proposed floor: **96 CSS px of list
  viewport**. Below that floor the shell uses compact chrome or a deliberate alternate layout — it
  does not silently clip.
- **FR-G01.2** No interactive control is positioned behind, or clipped by, the prompt dock. Anything
  `overflow-hidden` removes must be independently reachable (the current code asserts this informally
  in a comment; it becomes a tested property).
- **FR-G01.3** The behavior holds under rotation and with the software keyboard raised (`index.html`
  opts into `interactive-widget=resizes-content`, so the layout viewport really does shrink), and
  every retained control stays keyboard-reachable.
- **FR-G01.4** The height threshold remains **one decision with one home**. Widening coverage means
  changing or supplementing the `short:` variant in `index.css`, not writing an
  ad-hoc `[@media(max-height:…)]` at a use site.

> **Constraint — a documented decision is in the way.** `index.css:16-17` states the threshold is a
> CSS variant *and deliberately not* a `useViewport()` field, "on purpose: nothing branches on it in
> JS, and `useViewport` stays the only JS reader of a layout query" — a rule enforced by a guard in
> `useViewport.test.tsx`. If FR-G01.1's "deliberate alternate layout" requires a JS branch, that
> decision must be **explicitly revisited and the comment updated in the same change**. Adding a
> second `matchMedia` reader elsewhere is the parallel-implementation anti-pattern and is
> out of bounds. See [§8 Q1](#q1-does-g01-need-a-js-height-branch).

**Scope.** `web/src/index.css` · `web/src/features/sessions/SessionListColumn.tsx` ·
`web/src/features/sessions/ScopeBody.tsx` · `web/src/components/SessionListControls.tsx` ·
`web/src/shell/AppShell.tsx` · `web/e2e/short-viewport.spec.ts` (extend, do not duplicate).

### G02 — Readable session identity on narrow screens

**Current state (verified).** `SessionHeader.tsx` (255 lines) and `components/session/SessionMeta.tsx`
(331 lines) render back navigation, prev/next arrows, status, actions and metadata. With a long
title, a goal and neighboring sessions, the title compresses to roughly one character at 393px while
metadata consumes several rows.

**Target behavior.**

- **FR-G02.1** The session title is recognizable **without hover** at every supported width. It gets
  a guaranteed minimum share of the header's inline space; truncation is middle- or end-ellipsis with
  the full value exposed to assistive technology.
- **FR-G02.2** Secondary metadata and actions move behind an accessible disclosure, or onto a
  separate row — not into the title's row where they compete for space.
- **FR-G02.3** Account/agent context and safety controls (stop/interrupt, permission state) remain
  directly available at every width. They are never the thing put behind a disclosure.
- **FR-G02.4** Any disclosure is operable by keyboard and touch, with correct
  `aria-expanded`/`aria-controls`, and its open state is announced.

**Scope.** `web/src/features/sessions/SessionHeader.tsx` · `web/src/components/session/SessionMeta.tsx`.
Reuse the `PageHeader` back slot introduced by row 6 (#1563) rather than adding a second header
affordance.

### G06 — Unsent new-session drafts survive navigation and refresh

**Current state (verified).** `web/src/components/session/useSessionDraft.ts` is the canonical draft
store: per-session `localStorage` under `storageKey('draft.<sessionId>')`, with a lazy read on mount,
`save` on change, `clear` on submit, and a forward migration from the pre-namespace
`ccswitch:draft:<id>` key (row 7b, #1566).

**The hook is explicitly a no-op when `sessionId` is undefined** (`if (!sessionId) return` in both
`save` and `clear`; `read` returns `EMPTY`). The new-session composer has no session id yet, so it
gets the no-op path and its text is held in component state only — which is why typing into New
session, navigating to Pipeline and returning yields an empty field, while per-session Composer
drafts survive the identical journey.

**Target behavior.**

- **FR-G06.1** Unsent new-session prompt text, and the selections that belong with it (repo, engine,
  model, account, attachments), survive navigation, rotation and refresh.
- **FR-G06.2** The fix **extends `useSessionDraft`** — the existing source of truth — by giving the
  not-yet-created session a reserved draft identity. It does not introduce a second draft store,
  a second storage key convention, or a bare `localStorage` call. All keys go through
  `storageKey()`.
- **FR-G06.3** A **failed** creation retains the draft. A **successful** creation clears **only**
  that draft and leaves every per-session draft untouched.
- **FR-G06.4** An explicit, discoverable discard control clears the draft. Clearing a field to empty
  removes the stored entry (current `save` already treats empty text with no attachments as a
  delete — preserve that).
- **FR-G06.5** Draft content does not cross account or device boundaries. The draft identity is
  scoped so that switching accounts in the same browser profile does not surface another account's
  text. See [§7 P1](#p1-data-handling--drafts-are-unencrypted-and-profile-scoped) and
  [§8 Q2](#q2-how-is-the-new-session-draft-scoped).
- **FR-G06.6** Attachments have a defined persistence lifetime. `DraftAttachment` currently stores
  `{id, mime, name}` — metadata, not bytes. The spec requires that this stay true: **attachment
  bytes are never written to `localStorage`**, and a restored draft whose attachment references have
  expired says so rather than silently submitting a prompt with missing files.

**Scope.** `web/src/components/session/useSessionDraft.ts` (extend) · `web/src/components/DispatchBar.tsx` ·
`web/src/shell/regions/PromptDock.tsx` · `web/src/lib/storageKey.ts` (consume).

### G07 — Query failures are distinguishable from empty and from healthy

**Current state (verified).** `web/src/api/queryClient.ts` has a considered retry policy
(`shouldRetryQuery`: retry 502/503/504 and network failures up to `TRANSIENT_RETRY_LIMIT = 5`, fail
fast on deterministic 4xx/500) and a global `MutationCache.onError` that logs and toasts. **That
coverage is mutation-side only — there is no equivalent convention for a failed query.**

Consumers destructure with a default (`const { data: archived = [] } = useArchivedSessions()`), so a
failed read is indistinguishable from an empty one: a 503 renders "No repositories with pipeline
history yet", a 500 renders "No projects yet". There are **71 `?? []` occurrences** across
`web/src/**/*.tsx` and 205 files referencing `isError`, so the fallback shape is widespread and
inconsistently paired with error handling.

**Target behavior.**

- **FR-G07.1** A data surface distinguishes four states: **loading**, **empty (read succeeded, no
  rows)**, **failed first load (no data to show)**, and **failed refresh (last-good data on screen)**.
- **FR-G07.2** On failed refresh, last-good data is retained and labeled **stale**, with the failure
  visible. The UI does not silently present stale data as current, and does not discard usable data
  because a refresh failed.
- **FR-G07.3** Every failure state exposes an explicit **retry**, including after the retry budget is
  exhausted.
- **FR-G07.4** Failures are announced to assistive technology (a live region), not conveyed by color
  or icon alone.
- **FR-G07.5** The four states are expressed through **one shared mechanism** — a single
  hook/component pair living beside `queryClient.ts` — not re-derived per view. Constitution VI:
  adding a second correct-but-parallel error surface is the anti-pattern.
- **FR-G07.6** The change **sweeps the sibling `?? []` fallbacks**, not just the two the audit
  sampled. Each of the 71 occurrences is either migrated to the shared mechanism or explicitly
  justified in-place as a genuine empty default. Replaced local error rendering is deleted in the
  same PR.

**Scope.** `web/src/api/queryClient.ts` · `web/src/api/` hooks · `web/src/features/delivery/` (was
`InsightsView.tsx`) · `web/src/features/roadmap/` (was `ProjectsTab.tsx`) · every `?? []` consumer.

### G08 — Pipeline PR anchors are scoped by repository and occurrence

**Current state (verified — defect is live on `95eaeeeb`).**

- `web/src/pipeline/PrRow.tsx:200` — `<div id={\`pr-${pr.number}\`} …>`
- `web/src/features/pipeline/charts/QueueTrain.tsx:15` — `document.getElementById(\`pr-${n}\`)?.scrollIntoView({ behavior: scrollBehavior() })`

The identity is the PR number alone. Two repositories sharing a PR number produce **duplicate DOM
ids** (invalid HTML), and `getElementById` returns the first match — so a jump can land on the wrong
repository's row, or on a repeated occurrence of the right one.

**Target behavior.**

- **FR-G08.1** One **canonical scoped PR identity**, derived in a single helper, used by both the row
  that renders the anchor and the link that targets it. The two sites cannot drift because they call
  the same function. `web/src/pipeline/definitions.ts` or an adjacent module is the natural home —
  the Plan stage confirms placement rather than creating a new module by default.
- **FR-G08.2** The identity includes the repository. Where a PR legitimately appears more than once
  (repeated sections), it also includes an occurrence discriminator.
- **FR-G08.3** DOM ids rendered by Pipeline are **unique** for any dataset, asserted by test.
- **FR-G08.4** Each jump has exactly one intended target, and activating it by keyboard reaches the
  same element as by pointer — including moving focus, not only scrolling it into view.

**Scope.** `web/src/pipeline/PrRow.tsx` · `web/src/features/pipeline/charts/QueueTrain.tsx` ·
`web/src/features/pipeline/charts/__tests__/QueueTrain.test.tsx` (the existing test mocks
`getElementById` and must be reworked to assert target identity, not call shape).

### G20 — Route render failures are contained and recoverable

**Current state (verified).** `web/src/components/ErrorBoundary.tsx` (40 lines) is mounted **once**,
in `web/src/main.tsx:33`, wrapping the app. `web/src/shell/routes.tsx` (355 lines) defines **no
`errorElement`** on any route. React Router owns errors thrown inside route rendering and does not
propagate them to an ancestor React error boundary, so a malformed response surfaces Router's default
"Unexpected Application Error" screen — minified stack, developer instructions, navigation replaced.

**Correlation is closer than the audit implies.** `server/obs/middleware.py:32` already sets
`response.headers["X-Request-ID"] = rid` on every response, and `server/obs/context.py` threads that
id through server logs. But `web/src/api/client.ts` reads only `res.status` and the JSON body —
`res.headers` is discarded entirely, and `ApiRequestError` carries `{status, body}` with no id. The
correlation id the user would need to quote **already exists on the wire and is thrown away in the
client**.

**Target behavior.**

- **FR-G20.1** Route rendering failures render a **safe fallback**: no developer stack, no minified
  frames, no framework instructions, in any build. Routes get an `errorElement` (or the equivalent
  route-level boundary) rather than relying on the root `ErrorBoundary`, which structurally cannot
  catch them.
- **FR-G20.2** The fallback offers **retry and navigation**, and unaffected shell regions (nav, rail,
  dock) stay usable. A failed route does not take down the shell.
- **FR-G20.3** The fallback surfaces a **correlation id** the user can quote. Implementation:
  capture `X-Request-ID` from the response in `client.ts:request()` and carry it on
  `ApiRequestError`; mint a client-side id only for failures with no HTTP response behind them
  (see [§8 Q3](#q3-correlation-id-for-non-http-failures)). Do **not** add a second request-id
  scheme alongside the server's.
- **FR-G20.4** Failure details (message, stack, correlation id, route) are logged via the existing
  `clientLogger` (`web/src/obs/logger.ts`) — not rendered to the user.
- **FR-G20.5** Response shapes are validated at the boundary where a malformed payload would
  otherwise reach render. Validation failures are treated as failed reads (G07), not as crashes.
- **FR-G20.6** If route-level code splitting exists or is added, chunk-load failures resolve to the
  same recoverable surface.

**Scope.** `web/src/shell/routes.tsx` · `web/src/components/ErrorBoundary.tsx` ·
`web/src/api/client.ts` · `web/src/obs/logger.ts` · `web/src/main.tsx`.

---

## 3. Acceptance criteria

Verification-first. Each criterion must fail against `95eaeeeb` and pass after the change.

### G01
- **AC-G01.1** With **18 sessions and 6 queued rows**, at **320×568, 568×320, 393×852, 852×393,
  393×480, 852×240 and 320×256**: measured list-viewport height ≥ 96px (or the deliberate alternate
  layout is active), and the New Session control, the prompt dock and primary nav are each present
  **and hit-testable at their measured rect** — presence in the DOM is not sufficient evidence.
- **AC-G01.2** Rotation between each portrait/landscape pair preserves reachability with no manual
  reload.
- **AC-G01.3** Every control retained at the two smallest sizes is reachable by sequential keyboard
  navigation.
- **AC-G01.4** `grep` for `max-height` across `web/src` yields only the `index.css` variant
  definition — no ad-hoc height queries at use sites.

### G02
- **AC-G02.1** At **393×852, 852×393 and 393×480**, with a long title, a long goal and ≥2 neighboring
  sessions, on **both engines** (Claude and Codex): the rendered title's measured width is at least
  the agreed minimum share of the header, and its accessible name is the untruncated title.
- **AC-G02.2** Transcript geometry is asserted at the same sizes — the transcript retains a usable
  viewport once the header reflows.
- **AC-G02.3** Safety controls and account/agent context are present and hit-testable at every size,
  without opening a disclosure.
- **AC-G02.4** The disclosure is operable by keyboard and by touch expansion, with `aria-expanded`
  reflecting state.

### G06
- **AC-G06.1** Type an **unsent sentinel** into New session → navigate to Pipeline → return: the
  sentinel is present. Repeat across a full page refresh and across a rotation.
- **AC-G06.2** Force creation to fail: the sentinel is retained and the failure is visible.
- **AC-G06.3** Succeed creation: that draft's key is removed from storage and **no other**
  `ccswitch.draft.*` key is touched (assert by enumerating keys before and after).
- **AC-G06.4** Discard clears both the field and the stored key.
- **AC-G06.5** With two accounts in one browser profile, a draft written under account A is not
  readable by account B.
- **AC-G06.6** No attachment bytes appear in `localStorage` — assert the serialized draft contains
  only `{id, mime, name}` per attachment.
- **AC-G06.7** No new `localStorage` key bypasses `storageKey()` (the row 7b namespace lint/test
  continues to pass).

### G07
- **AC-G07.1** For each injected status — **401, 403, 429, 500, 503**, a network **timeout**, and a
  **recovery** — the surface renders the correct one of the four states, asserted by accessible text,
  not by class name.
- **AC-G07.2** With cached last-good data present, a failed refresh keeps the rows on screen and
  labels them stale.
- **AC-G07.3** After retry-budget exhaustion, a retry control is present and a further click issues a
  new request.
- **AC-G07.4** The failure message is in a live region.
- **AC-G07.5** A documented audit of all **71** `?? []` occurrences is attached to the PR, each
  marked migrated or justified.

### G08
- **AC-G08.1** Render two repositories sharing PR number `N`, plus a repeated section occurrence.
  Assert **all** rendered ids are unique across the document.
- **AC-G08.2** Activating repo A's link — by **pointer** and by **keyboard** — reaches repo A's row,
  asserted by the resolved element's identity, not by a spy on `getElementById`.
- **AC-G08.3** Keyboard activation moves focus to the target, not only the scroll position.

### G20
- **AC-G20.1** Inject a render exception and an invalid response shape into a route. Assert: no stack
  text, no framework error copy, no minified frames in the DOM.
- **AC-G20.2** Nav, rail and dock remain present and interactive; navigating away from the failed
  route recovers without a reload.
- **AC-G20.3** A correlation id is displayed and **matches** the `X-Request-ID` of the failing
  response.
- **AC-G20.4** `clientLogger` received the failure with stack and correlation id.
- **AC-G20.5** If route splitting exists, a simulated chunk-load failure reaches the same surface.

### Gates applying to every child
- **AC-X.1** `lint-and-test` — all four steps: `ruff check src/ server/`,
  `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- **AC-X.2** `frontend` — `npm run typecheck`, `npm run lint` (`--max-warnings 0`), the **full**
  `npm run test`, `npm run build`. Full suite, not changed-files: ~47 web test files mock
  `@/api/hooks`, so shared-component changes break untouched files.
- **AC-X.3** Sibling occurrences audited and the existing source of truth used; **replaced paths
  deleted in the same PR** (constitution VI).
- **AC-X.4** Sanitized before/after evidence attached; root scorecard / conformance record updated.
  Sanitized means no account identifiers, session goals, repo-private prompt text or tailnet
  hostnames — see [§7 P2](#p2-data-handling--ui-evidence-can-leak-session-content).

---

## 4. Data and API surface

**This epic is frontend-only. No new server endpoint, model, column or migration is required**, so
the `create_all`/`_reconcile_columns` and Postgres-suite rules do not apply to any child.

### Client-side persisted state

| Key | Owner | Change |
|---|---|---|
| `ccswitch.draft.<sessionId>` | `useSessionDraft.ts` | **Extended** by G06 to cover the not-yet-created session. Shape `{text, attachments: [{id, mime, name}]}` is preserved; any new field is additive and tolerated-when-absent by the existing defensive `read()`. |

No other persisted key is added. Every key routes through `storageKey()` (`web/src/lib/storageKey.ts`).
If G06's draft identity changes the key shape for existing drafts, `migrateStorageKey()` is the
migration path — the same mechanism row 7b used.

### HTTP surface — consumed, not changed

| Surface | Current | Change |
|---|---|---|
| `X-Request-ID` response header | Set by `server/obs/middleware.py:32` on every response | **Newly read** by `web/src/api/client.ts`; attached to `ApiRequestError` for G20. Server unchanged. |
| Error body `{error\|detail}.{code\|error}` | Decoded by `apiErrorCode()` in `client.ts` | Unchanged. G07's taxonomy keys off HTTP status, not a new code vocabulary. |
| Query retry policy | `shouldRetryQuery` in `queryClient.ts` | Unchanged semantics. G07 consumes its outcome (budget exhausted) to select a state. |

### Internal contracts introduced

- **G07** — one shared query-state hook/component pair beside `queryClient.ts`, mapping a TanStack
  Query result to `loading | empty | failed-first-load | failed-refresh` plus a retry handle.
- **G08** — one scoped-PR-identity helper in the Pipeline module, consumed by both the anchor and
  the link.
- **G20** — `ApiRequestError` gains a correlation id field; a route-level error element.

Each is **one** new symbol replacing N ad-hoc ones. The Plan stage must grep for an existing
equivalent before creating any of them, and must delete what each replaces.

---

## 5. Cross-cutting constraints

1. **Single source of truth (VI).** Five of six fixes extend an existing mechanism: `short:` variant,
   `PageHeader` back slot, `useSessionDraft`, `queryClient`, `X-Request-ID`. Introducing a parallel
   correct implementation is a spec violation, not a style preference.
2. **Purge replaced code in the same PR (VI).** G07's per-view error rendering and G08's unscoped id
   construction are deleted, not left beside the new path.
3. **Verify against the real thing (III).** G01 and G02 assert **measured geometry and hit testing**.
   A passing render test that never measures is the reason the 393×480 case is green today against a
   live defect.
4. **TDD (V).** Each acceptance criterion is expressed as a test that fails on `95eaeeeb` first.
5. **Check for in-flight duplicates before implementing.** Query open PRs by the **files** each child
   touches (not by branch name) before writing code — #1142/#1145 independently implemented the same
   issue nine minutes apart.
6. **Branch off `origin/master`, never local `master`** — in a worktree the local ref is a stale
   leftover hundreds of commits behind.
7. **Design-token and lint rules stand.** `no-raw-palette` is enforced and auto-fixable outside
   `components/` (row 7a, #1565); all color, radius, z-index and target-size values come from the
   `index.css` token layer.

---

## 6. Non-goals

- **Visual redesign.** G02 reallocates space and adds disclosure; it does not restyle the header.
- **A new responsive framework or breakpoint system.** `useViewport` and the `short:` variant stay
  the only layout query readers.
- **Server changes.** No endpoint, schema, migration or daemon change. `X-Request-ID` is consumed as
  it already ships.
- **A global error-taxonomy vocabulary.** G07 keys off HTTP status; it does not introduce a new
  server error-code scheme.
- **Offline or cross-device draft sync.** G06 is local, single-device (and FR-G06.5 forbids crossing
  device boundaries).
- **Draft persistence for the per-session Composer.** Already works; G06 must not regress it.
- **Route code splitting.** FR-G20.6 is conditional — if splitting is not added, chunk-load coverage
  is out of scope.
- **The other UI-audit epics under #1393.** Only G01, G02, G06, G07, G08 and G20 are in scope.
- **Re-measuring the audit's unknown user/field metrics.** #1394 notes these must be measured and
  that expert scores are not substitutes; that measurement is a separate work item.
- **Estimate refinement.** The children carry provisional effort (16/12/12/16/8/12 h, ±50%). The Plan
  stage re-estimates; this spec does not ratify those numbers.

---

## 7. Policy concerns — flagged for the human gate

These are **not resolved by this spec**. Each needs a human decision at the Design gate.

### P1. Data handling — drafts are unencrypted and profile-scoped

**Severity: medium.** G06 widens what `localStorage` holds: the new-session prompt is where a user
pastes the most context-rich text in the product — repository paths, goals, sometimes credentials or
customer detail pasted by accident — and unlike a per-session draft it persists **before** any
session, account or repo binding exists.

`localStorage` is unencrypted, origin-scoped (not account-scoped), and survives logout. ViberPit is
Tailscale-identity authenticated with multiple accounts per user, so **one browser profile can serve
several identities against one origin**. FR-G06.5 states the requirement; it does not say how.

*For the gate:* confirm that persisting pre-session prompt text at rest in the browser is acceptable,
and decide the retention rule — indefinite until discarded (matching per-session drafts today), or a
bounded TTL. A TTL is the more conservative answer and is not currently implemented for any draft.

### P2. Data handling — UI evidence can leak session content

**Severity: medium.** Every child requires "sanitized before/after evidence" attached to a public
GitHub PR, and G01/G02 need screenshots of a **populated** Sessions list (18 sessions, 6 queued) and
a session header with real identity strings. These naturally contain account names, repo names,
session goals and prompt text. #1394 records that the source audit deliberately kept private
screenshots and account/session details off GitHub.

*For the gate:* ratify a concrete sanitization standard — seeded fixture data rather than live
sessions for all attached evidence, and a named reviewer for anything captured from a live instance.
"Sanitized" as an unelaborated checkbox has already been shipped six times in these issue templates
without a definition.

### P3. Security — correlation id exposure

**Severity: low.** FR-G20.3 displays a server-generated `X-Request-ID` in the UI. The value is
already client-visible as a response header and `new_request_id()` describes it as a short
log-friendly id, so this is a presentation change, not new exposure. Confirm nonetheless that the id
is non-guessable enough that displaying it grants no cross-request inference, and that the fallback
copy carries **only** the id — never the exception message, which may embed paths or payload
fragments.

### P4. Security — error-state disclosure

**Severity: low-medium.** G07 makes failures visible where they are currently swallowed. Rendering a
raw server message risks surfacing internal detail (stack fragments, hostnames, SQL). The four-state
mechanism must render a **fixed message per status class** plus the correlation id, with the server
message logged rather than displayed. `extractMutationErrorMessage()` currently *does* prefer the
server's `detail.message` for toasts — G07 should not extend that policy to queries without a
decision here.

### P5. Accessibility as a compliance surface

**Severity: medium.** Several criteria (live-region announcements, keyboard reachability at minimum
viewports, disclosure semantics, non-color-only failure indication) are accessibility conformance,
not preference. If the project holds a stated WCAG target, these must be measured against it, and the
"root scorecard/conformance record" referenced by every child issue is the artifact of record.

*For the gate:* name the conformance target and the record's location — no scorecard path is given in
any child issue.

### P6. Governance — the Design stage ran without its declared upstream input

**Severity: high (process, not product).** As [§0](#0-provenance--read-this-first) documents, this
initiative has no `intent.md`; the Design prompt inlined a nonexistent file and the stage proceeded
from the epic issues instead. This spec is therefore grounded in an input the pipeline did not
select, and the human gate is approving an artifact whose declared provenance chain has a hole.

The cause is generic: **every `epic-adoption` initiative will hit this**, because adoption enters at
Design and the intent stage never runs. Options for the gate: (a) treat the epic issue body as the
intent of record and make the prompt builder read it when no `intent.md` exists; (b) backfill an
`intent.md` for adopted epics before Design is dispatched; (c) make the Design stage fail loudly on a
missing upstream rather than emitting a placeholder line and continuing.

Option (c) is worth doing regardless of (a)/(b) — the current behavior degrades silently, and a stage
that proceeds without its input can produce a plausible-looking artifact from nothing.

Filed separately via ViberPit feedback. **This spec should not be taken as evidence that the gap is
harmless** — it was caught only because the missing file was noticed before drafting.

### P7. ToS / vendor

**None identified.** This epic is entirely first-party frontend work: no vendor surface, no agent
binary, no credential authority, no new dependency. Constitution I and II are not engaged.

---

## 8. Open questions for the gate

#### Q1. Does G01 need a JS height branch?
FR-G01.1 allows a "deliberate alternate layout" below the floor. If that is CSS-only, the existing
`short:` variant suffices. If it needs a JS branch, `index.css`'s documented "not a `useViewport()`
field, on purpose" decision must be reversed deliberately — and `useViewport` extended rather than a
second `matchMedia` reader added. **Which?** The threshold also needs re-deciding: 560px does not
cover the reported 320×568 failure.

#### Q2. How is the new-session draft scoped?
Candidates: a single global `new` draft; per-repo; per-account; per-account-and-repo. This determines
the storage key, whether switching repos loses text, and how FR-G06.5's account boundary is enforced.
Per-account-and-repo is the most privacy-conservative and the most surprising (text seems to vanish
on a repo switch); a single global draft is the most predictable and the weakest boundary.
**Recommendation: per-account, with repo as a restored field rather than part of the key** — it
satisfies FR-G06.5 without making text disappear on a repo switch.

#### Q3. Correlation id for non-HTTP failures?
`X-Request-ID` exists only when a response came back. A pure render exception or a chunk-load failure
has none. Either show "no correlation id available" or mint a client-side id for those cases. Minting
adds a second id scheme — acceptable only if it is visibly distinct from the server's.

#### Q4. Is the 96px list-viewport floor the right number?
FR-G01.1 carries the audit's proposal. 96px is roughly one and a half rows. Ratify or replace it —
it becomes a tested constant.

#### Q5. Where is the root scorecard / conformance record?
Every child requires updating it; no child names it. Needed before any child can satisfy AC-X.4.

#### Q6. Does G07's sweep land in one PR or several?
71 `?? []` occurrences is a large blast radius for one review. A shared mechanism plus a staged
migration is safer, but staging leaves the old path alive across PRs — which the purge rule permits
only with a `# @deprecated — replaced by X, remove in #N` marker and a tracking issue.
**Recommendation: mechanism + the two audited surfaces in PR 1; the remaining sweep in PR 2**, with
the deprecation marker on the interim path.

---

## 9. Traceability

| Child | Issue | Requirements | Acceptance | Primary source of truth to extend |
|---|---|---|---|---|
| G01 | [#1399](https://github.com/steiner385/viberpit/issues/1399) | FR-G01.1–4 | AC-G01.1–4 | `index.css` `short:` variant |
| G02 | [#1400](https://github.com/steiner385/viberpit/issues/1400) | FR-G02.1–4 | AC-G02.1–4 | `PageHeader` back slot (#1563) |
| G06 | [#1404](https://github.com/steiner385/viberpit/issues/1404) | FR-G06.1–6 | AC-G06.1–7 | `useSessionDraft.ts` + `storageKey()` |
| G07 | [#1405](https://github.com/steiner385/viberpit/issues/1405) | FR-G07.1–6 | AC-G07.1–5 | `queryClient.ts` |
| G08 | [#1406](https://github.com/steiner385/viberpit/issues/1406) | FR-G08.1–4 | AC-G08.1–3 | Pipeline PR identity helper (new, single) |
| G20 | [#1418](https://github.com/steiner385/viberpit/issues/1418) | FR-G20.1–6 | AC-G20.1–5 | `X-Request-ID` (`server/obs/middleware.py:32`) |
