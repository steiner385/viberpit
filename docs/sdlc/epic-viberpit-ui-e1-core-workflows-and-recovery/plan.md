# Plan — ViberPit UI E1: Core workflows and recovery

| | |
|---|---|
| **Initiative** | `epic-viberpit-ui-e1-core-workflows-and-recovery` |
| **Stage** | Plan (propose-only; this artifact merges at a human gate, same as the spec) |
| **Spec** | [`spec.md`](./spec.md) — read-only input, not modified by this plan |
| **Planned against** | `4e50c7e6` (master, 2026-09-12 20:31 EDT) |
| **Epic / children** | [#1394](https://github.com/steiner385/viberpit/issues/1394) · G01 [#1399](https://github.com/steiner385/viberpit/issues/1399) · G02 [#1400](https://github.com/steiner385/viberpit/issues/1400) · G06 [#1404](https://github.com/steiner385/viberpit/issues/1404) · G07 [#1405](https://github.com/steiner385/viberpit/issues/1405) · G08 [#1406](https://github.com/steiner385/viberpit/issues/1406) · G20 [#1418](https://github.com/steiner385/viberpit/issues/1418) |

## 0. What changed between the spec and this plan

The spec was verified against `95eaeeeb`. Master advanced to `4e50c7e6` before this Plan stage ran.
**The spec's own "Scope" file lists have drifted a second time** — the same phenomenon §0 of the spec
documents for the audit it superseded. Re-verified paths used throughout this plan:

| Spec's scope reference | Current path |
|---|---|
| `web/src/components/SessionListControls.tsx` | `web/src/features/sessions/list/SessionListControls.tsx` |
| `web/src/components/session/SessionMeta.tsx` | `web/src/features/sessions/SessionMeta.tsx` |
| `web/src/components/session/useSessionDraft.ts` | `web/src/features/sessions/composer/useSessionDraft.ts` |
| `web/src/components/DispatchBar.tsx` | `web/src/features/sessions/list/DispatchBar.tsx` (rendered by `web/src/shell/regions/PromptDock.tsx`'s `NewSessionBar`) |
| `web/src/components/ErrorBoundary.tsx` | `web/src/shell/ErrorBoundary.tsx` |

`index.css`, `SessionListColumn.tsx`, `ScopeBody.tsx`, `SessionHeader.tsx`, `AppShell.tsx`,
`short-viewport.spec.ts`, `queryClient.ts`, `PrRow.tsx`, `QueueTrain.tsx` (now at
`web/src/features/pipeline/charts/QueueTrain.tsx`), `routes.tsx`, `client.ts`, `storageKey.ts`,
`definitions.ts`, `server/obs/middleware.py` all matched the spec's description on read. All six
mechanisms the spec says to extend (`short:` variant, `PageHeader`, `useSessionDraft`, `queryClient`,
the scoped-PR-identity gap, `X-Request-ID`) are still exactly where and what the spec says — the drift
is path-only, not behavioral. No re-scope of §2's functional behavior was needed.

One quantitative claim is already stale and will drift further before G07 lands: the spec's "71 `??
[]` occurrences" / "205 files referencing `isError`" (both counted over `web/src/**/*.tsx`) are **75**
and **73** respectively as of `4e50c7e6` (see G07 Task 7.0). AC-G07.5's audit must be run fresh at
implementation time, not taken from the spec's numbers.

## 1. Global constraints (apply to every task below)

- **Frontend-only.** No server endpoint, model, column, or migration (spec §4). The `create_all`
  reconcile / Postgres-suite rules do not apply to any task in this plan.
- **Branch off `origin/master`**, never the local `master` ref (core.md; spec §5.6).
- **TDD.** Every acceptance criterion is a test that fails on `4e50c7e6` first (spec §5.4). Tasks
  below are ordered test-then-implementation.
- **Single source of truth (constitution VI; spec §5.1–2).** Each task names the existing mechanism
  it extends and the old code it deletes in the same PR. No task introduces a second, parallel
  correct implementation of a mechanism named below.
- **`no-raw-palette` and the design-token layer stand** (spec §5.7) — any new color/radius/z-index/
  target-size value comes from `index.css`'s `@theme` block, never a literal.
- **Check in-flight PRs by file, not branch name**, before starting each task
  (`gh api graphql` query in core.md § "Before implementing, check whether the same work is already
  in flight") — the six children touch disjoint files but a stray drive-by PR could still collide.
- **`npm run test` is the full suite**, not changed-files — ~47 web test files mock `@/api/hooks`.
- Gates for every child PR: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
  `pytest tests/`, `pytest server/tests/`, `npm run typecheck`, `npm run lint --max-warnings 0`,
  `npm run test`, `npm run build` (AC-X.1/X.2). None of these six children touch `server/` or
  `src/ccswitch/` — the Python gates are expected to be no-op passes, not skipped.

## 2. Plan-stage resolutions of the spec's open questions

The spec (§8) left six questions for the human gate. Four have a clear, low-risk default this plan
adopts so implementation is not blocked; two remain genuinely open and are called out as such. The
gate can override any of these without invalidating the rest of the plan.

### Q1 — Does G01 need a JS height branch? → **No. CSS-only, two tiers.**

Verified in `useViewport.test.tsx`: the guard that is supposed to enforce "one JS layout-query reader"
(`LAYOUT_QUERY = /(?:matchMedia|useMediaQuery)\(\s*['"`]\((?:(?:min|max)-width|orientation)/`) **does
not match `max-height` at all**. Today, nothing stops a second file from reading a raw
`matchMedia('(max-height:…)')` — the documented "CSS-only, on purpose" decision in `index.css` is
enforced by convention, not by the guard it cites. This plan treats widening that regex as part of
G01, whether or not a JS branch is ultimately needed, because the gap exists regardless.

A single boolean (`short:`) cannot cover the acceptance matrix: 320×568 and 852×240 need chrome shed
at ~250–570px tall, while 393×480 (the one case the current suite pins) needs it at ~480. Resolution:
keep it CSS-only by adding a **second, more aggressive tier** rather than reaching for JS:

- Raise `short` from `560px` to **`620px`** (covers the 568px-tall failure case the spec opens with).
- Add `xshort` at **`340px`** (covers 320×256, 852×240, 320×568-landscape-adjacent cases where even
  the `short:hidden` controls-strip removal isn't enough headroom) — sheds additional chrome
  (e.g. collapses `NavBar` to icon-only, tightens `PromptDock` padding) via `xshort:` utility classes,
  still a pure CSS show/hide with no new JS reader.

If manual/E2E verification at the smallest sizes (Task 1.4) shows even `xshort` can't reach the 96px
floor without changing what code branches on (not just what CSS shows), that is an escalation back to
the gate — not a silent JS workaround.

### Q2 — How is the new-session draft scoped? → **Per dispatch-account selection, repo as a restored field.**

Verified in `DispatchBar.tsx`: `repo` and `engineChoice` already persist durably and globally via
`lib/lastRepo.ts` and `lib/enginePreference.ts` — separate, pre-existing mechanisms, unscoped to any
draft. Duplicating them inside the new-session draft would be a second, competing store for the same
value (constitution VI). The account picker's local state (`account: string | null`, `null` =
"Auto-select") is the one relevant "identity" the composer already tracks, and it is exactly the
boundary FR-G06.5 cares about — so the draft key is scoped to it, not to `repo` or `engine`.

**Resolution:** key `draft.new.<accountKey>` where `accountKey` is the selected account name or the
literal `auto` when unselected. The draft body carries `text`, `attachments`, `model`, `host`,
`reasoningEffort` — **not** `repo` or `engine`, which stay owned by their existing modules. Switching
the Account dropdown surfaces a different (usually empty) draft; switching only repo or engine does
not, matching the spec's Q2 recommendation ("repo as a restored field rather than part of the key")
extended to engine for the same reason.

### Q3 — Correlation id for non-HTTP failures? → **Mint one, visibly distinct from the server's.**

`server/obs/context.py:new_request_id()` returns `uuid.uuid4().hex[:12]` — 12 lowercase hex chars, no
separator, no prefix. **Resolution:** a client-minted id is `client-` + 8 hex chars
(`crypto.randomUUID().replace(/-/g, '').slice(0, 8)`), used only when `ApiRequestError` has no
`requestId` (a raw render exception, a chunk-load failure) — never for an HTTP failure, which always
carries the real one. The `client-` prefix makes the two schemes visually undoubtable in the fallback
UI, satisfying "acceptable only if visibly distinct" (spec §8 Q3).

### Q4 — Is 96px the right list-viewport floor? → **Ratify as a named constant.**

Adopted as written. `MIN_LIST_VIEWPORT_PX = 96` lives beside the other geometry constants in
`short-viewport.spec.ts` (Task 1.4) so a future change to the number is a one-line diff with a visible
test consequence, not a silently-drifting magic number re-typed at each assertion.

### Q5 — Where is the root scorecard / conformance record? → **Genuinely unresolved — flagged, not decided.**

Searched the repo for an existing candidate: no file matches "scorecard" or "conformance" that is
scoped to this epic. Two adjacent-but-distinct docs exist and are **not** it:
`docs/audits/2026-09-02-ui-framework-audit.md` has a "Scorecard" section, but it audits shell/region
*structure*, is dated before this epic, and its findings are already implemented; `docs/ACCESSIBILITY.md`
names the conformance *target* (WCAG 2.1 AA — this answers spec P5's "name the target" half) but is a
contract document, not a per-fix evidence log. **This plan does not invent one.** Each child PR's
AC-X.4 evidence goes into the PR description until the gate names a location; if the gate wants a
durable file, `docs/sdlc/epic-viberpit-ui-e1-core-workflows-and-recovery/scorecard.md` is the natural
default (same directory as this plan and the spec) but is *not* created by this plan pending that
decision.

### Q6 — G07 sweep: one PR or several? → **Two, as the spec recommends.**

PR 1: the shared four-state mechanism + the two audited call sites (`?? []` at `SessionListColumn.tsx`
and the projects/roadmap surface) + fresh audit output (Task 7.0). PR 2: the remaining sweep. The
interim state between the two PRs carries `// @deprecated — replaced by the query-state hook from
G07 PR1, remove in #<PR2 issue>` on any local error-rendering left in place, per the "purge in the
same PR unless it's a migration too large for one" exception (core.md).

## 3. Task plan

Each of the six lettered tasks (matching the spec's children) is independently shippable — #1394
already records ordering dependencies between children via native GitHub links, re-read at PR-creation
time, not inferred from the order below. The order here reflects file-overlap risk and review
sequencing preference, not a hard dependency: G01/G02 both touch the Sessions shell area and are
easier to review back-to-back; G07/G08/G20 are fully independent of everything else and of each other.

---

### Task G01 — Sessions usable at short heights

**Files**
- Modify: `web/src/index.css` — raise `short` to 620px, add `xshort` at 340px, extend the doc comment.
- Modify: `web/src/features/sessions/SessionListColumn.tsx` — `xshort:` chrome reductions on the bands
  that remain after `short:hidden` (scope segment padding, "New Session" button density).
- Modify: `web/src/features/sessions/ScopeBody.tsx` — no structural change expected; re-verify the
  `empty:hidden short:hidden` strip still collapses correctly under the new thresholds.
- Modify: `web/src/features/sessions/list/SessionListControls.tsx` — audit for any control that would
  become unreachable once shed at `xshort:` (must have another path, per FR-G01.2).
- Modify: `web/src/shell/AppShell.tsx` — verify `app.dock` / `app.rail` don't need their own `xshort:`
  treatment; add if the E2E task shows the dock or nav clipped at the smallest sizes.
- Modify: `web/src/shell/useViewport.test.tsx` — widen `LAYOUT_QUERY` to also flag a raw
  `max-height` reader (see Q1); this is a net-new coverage gap, not a refactor of the existing test.
- Modify: `web/src/__tests__/viewportMeta.test.ts` — assert both `short` (620px) and `xshort` (340px)
  variants exist and sit in the documented bands; update the threshold-band comment.
- Modify: `web/e2e/short-viewport.spec.ts` — extend to the full AC-G01.1 matrix; add rotation
  (AC-G01.2) and keyboard-reachability (AC-G01.3) cases; add the `MIN_LIST_VIEWPORT_PX` constant.

**Order**
1. **Write the failing E2E cases first** (`short-viewport.spec.ts`): for each of the seven sizes in
   AC-G01.1 (320×568, 568×320, 393×852, 852×393, 393×480, 852×240, 320×256), assert
   `bandHeight(page) >= MIN_LIST_VIEWPORT_PX` and that New Session / dock / primary nav are each
   `toBeVisible()` **and** pass a hit-test (`el.evaluate(el => document.elementFromPoint(cx, cy) === el
   || el.contains(document.elementFromPoint(cx, cy)))` at the element's own center — presence in the
   DOM is explicitly not sufficient per the spec). Run against `4e50c7e6`: 320×568 and 852×240 must
   fail (the `short:` variant never fires at 568px tall or catches a 240px-tall case at all).
2. Add the two rotation-pair cases (AC-G01.2) reusing the same helpers with `page.setViewportSize`
   toggling width/height, no reload between.
3. Add the keyboard-reachability case (AC-G01.3) at the two smallest sizes: `page.keyboard.press('Tab')`
   in a loop, collecting `document.activeElement`, asserting each retained control is reached.
4. Confirm all new cases fail for the right reason (chrome doesn't fit / a control has no visible
   rect), not for an unrelated error.
5. Widen `index.css`'s `short` to 620px and add `xshort` at 340px; update the doc comment to explain
   the two-tier rationale (keyboard-open phone vs. extreme landscape/short).
6. Update `viewportMeta.test.ts` for both variants; run it, confirm green.
7. Widen `useViewport.test.tsx`'s `LAYOUT_QUERY` regex to include `max-height`; run it against the
   current tree, confirm it still reports zero offenders (the fix must not itself trip the guard).
8. Apply `xshort:` chrome reductions in `SessionListColumn.tsx` / `SessionListControls.tsx` /
   `AppShell.tsx` until the E2E matrix passes. Every element `short:hidden` or `xshort:hidden` removes
   must have a control elsewhere still reachable for the same function (FR-G01.2) — if one doesn't,
   that is new work, not a regression to fix around.
9. Run the full E2E matrix; run `npm run test` (full suite, not changed-files); run
   `grep -rn max-height web/src` and confirm only `index.css`'s two `@custom-variant` lines match
   (AC-G01.4).
10. Commit, push, open PR.

**Tests**
- `web/e2e/short-viewport.spec.ts` — the 7-size matrix, the two rotation pairs, the keyboard-reachability
  cases (Playwright; real layout, per spec §5.3 — this is exactly the surface a jsdom test can't verify).
- `web/src/__tests__/viewportMeta.test.ts` — both variants exist, at the right thresholds.
- `web/src/shell/useViewport.test.tsx` — the widened guard still passes with zero offenders.
- `web/src/features/sessions/SessionListColumn.test.tsx` — unit-level class-contract pins for
  `xshort:hidden` (kept as documentation, not as the source of truth — the E2E suite is).

**Risks**
- *Widening `short` to 620px changes behavior for every existing `short:hidden` consumer, not just
  Sessions* — a laptop window that used to keep its filter strip at, say, 600px tall now loses it.
  Mitigation: `grep -rn "short:"` across `web/src` (five hits found during planning, all in
  Sessions/list-adjacent files) and re-verify each visually at 600–620px before merging; none is
  expected to be a live regression, but this must be confirmed, not assumed.
- *`xshort:` chrome reduction has no existing precedent in this codebase* — every other adaptive class
  is a single boolean. Getting the two-tier CSS to compose cleanly (both `short:hidden` and a
  `xshort:`-only alternate rendering on the same element) may need the element split into two, not
  toggled — budget for that if the first attempt produces illegible Tailwind class strings.
- *The keyboard-reachability assertion (AC-G01.3) is new territory for this suite* — Playwright's
  `page.keyboard.press('Tab')` reachability loop is easy to write in a way that passes vacuously (e.g.
  if focus never leaves `<body>` because a click handler intercepts it). Assert a monotonically
  advancing `document.activeElement` and a minimum count of distinct focused elements, not just "no
  error was thrown."

---

### Task G02 — Readable session identity on narrow screens

**Files**
- Modify: `web/src/features/sessions/SessionHeader.tsx` (255 lines) — title gets a guaranteed
  `min-w` share; secondary metadata/actions move behind a disclosure or a second row.
- Modify: `web/src/features/sessions/SessionMeta.tsx` (329 lines) — same disclosure treatment for the
  identity cluster (account/agent/model/effort) it currently renders `hidden md:flex`.
- Test: `web/src/features/sessions/SessionHeader.test.tsx` (existing) — extend with narrow-width and
  disclosure assertions.
- Test: `web/src/features/sessions/SessionMeta.test.tsx` / `SessionMeta.switchers.test.tsx` (existing).

**A placement decision, made here rather than left to the implementer:** `SessionHeader`'s Back
control calls `onBack={() => leave(onDetach)}` (`MainPaneSession.tsx:111`) — it runs session-detach
cleanup, not a pure navigation. `PageHeader`'s `back` slot (`ui/PageHeader.tsx:32`) is a real `<Link>`
with an `href`, because every other page's Back is pure navigation. **These are not interchangeable**
— routing `SessionHeader` through `PageHeader`'s `back` prop would silently drop the detach call. This
plan does **not** migrate `SessionHeader` onto `<PageHeader>`; "reuse the back slot" (spec, G02 scope
line) is satisfied by keeping the *visual and DOM contract* identical (same `aria-label`, same
leading-position, same `ArrowLeft` icon via `PageHeader`'s existing markup as the copy source) without
adopting a component whose `back` prop can't express a side-effecting Back. If the gate disagrees and
wants `SessionHeader` to literally render `<PageHeader>`, that requires `PageHeader.back` to gain an
`onClick` variant — a small, additive change to `PageHeader`, tracked here as an open follow-up rather
than done silently as part of G02.

**Order**
1. Write failing tests in `SessionHeader.test.tsx` at 393px/852px/393×480 widths (jsdom width via
   `Object.defineProperty(window, 'innerWidth', ...)` + resize event, matching this suite's existing
   pattern) asserting: the title's accessible name is the full untruncated string (already likely
   true via native truncation + full text content — pin it explicitly); a disclosure trigger exists
   with `aria-expanded="false"` and `aria-controls` pointing at the metadata panel's id; opening it
   flips `aria-expanded` and the panel's contents become visible; the safety controls
   (stop/interrupt — `SessionActions`) and the status pill remain rendered and not inside the
   disclosure at every width tested.
2. Confirm these fail against the current header (no disclosure exists; metadata renders unconditionally
   or is simply `hidden md:flex`, never behind an accessible control).
3. Restructure `SessionHeader.tsx`: give the title `<h1>`/title span a `min-w-0 flex-1` (or equivalent)
   so it earns space before the identity cluster; move the identity cluster into a `<details>`/button+
   panel disclosure (matching this codebase's existing disclosure idiom — check `ui/Menu.tsx` or
   `Accordion`-equivalent if one exists before inventing a new one, per constitution VI) at narrow
   widths, staying inline at desktop widths (`md:` breakpoint, consistent with the rest of the file's
   existing `hidden md:flex` usage).
4. Wire `aria-expanded`/`aria-controls`/an announced open state (an `aria-live="polite"` region or the
   `aria-expanded` change itself, whichever this codebase's existing disclosures use — grep for a
   precedent before adding a new announcement pattern).
5. Repeat 1–4 for `SessionMeta.tsx`'s own identity cluster if it renders independently of
   `SessionHeader`'s (confirm the overlap/ownership between the two files before touching both blindly
   — `SessionHeader.tsx`'s doc comment describes a "Session identity (Task 5.3)" cluster that may
   already be `SessionMeta`, in which case this is one change site, not two).
6. Run the full test file plus a manual check in a real browser at 393×852 and 852×393 with a long
   title/goal fixture (AC-G02.1/2 call for measured geometry — a jsdom pass is necessary but not
   sufficient; note in the PR whether real-browser verification was done, per the top-level
   instruction to test UI changes in a browser when feasible).
7. Commit, push, open PR.

**Tests**
- `web/src/features/sessions/SessionHeader.test.tsx` — title min-share, disclosure semantics,
  safety-controls-never-hidden, keyboard operability (`Tab` to the trigger, `Enter`/`Space` to toggle).
- `web/src/features/sessions/SessionMeta.test.tsx` / `.switchers.test.tsx` — same, for the identity
  cluster, both engines (Claude and Codex fixtures already exist in this suite per its name).
- Manual/E2E: a screenshot or Playwright geometry check at 393×852, 852×393, 393×480 for AC-G02.1/2
  (add to an existing session-focused E2E spec rather than inventing a new `.spec.ts` file, per
  constitution VI — check `web/e2e/` for one that already opens a session before creating one).

**Risks**
- *`SessionHeader` vs `SessionMeta` ownership of "identity" is ambiguous from the file names alone* —
  confirmed only by reading both fully at implementation time (this plan read `SessionHeader`'s top
  and the doc comment referencing "Task 5.3", not `SessionMeta`'s body). Get this wrong and the
  disclosure lands in the wrong file, or twice.
- *No confirmed existing disclosure primitive.* If this codebase has no shared collapsible/disclosure
  component, this task either builds one (a `ui/` primitive, reusable rather than session-specific —
  right home per the file-structure guidance in `writing-plans`) or uses the native `<details>` element
  (simpler, but its default styling and animation are harder to match to the design-token layer).
  Decide before starting; don't discover it mid-task.
- *Two engines, three widths, two orientations is a wide test matrix for one component* — budget
  for a parametrized test table rather than six near-duplicate `it()` blocks.

---

### Task G06 — Unsent new-session drafts survive navigation and refresh

**Files**
- Modify: `web/src/features/sessions/composer/useSessionDraft.ts` — extend to accept a draft identity
  that isn't a real session id (the "new" identity), still going through `storageKey()`/
  `migrateStorageKey()`; extend the persisted shape to the additional fields (Q2).
- Test: `web/src/features/sessions/composer/useSessionDraft.test.ts` (existing) — extend for the new
  identity and shape.
- Modify: `web/src/features/sessions/list/DispatchBar.tsx` — wire `text`/`attachments`/`model`/`host`/
  `reasoningEffort` through the extended hook; add the discard control (FR-G06.4).
- Modify: `web/src/shell/regions/PromptDock.tsx` (`NewSessionBar`, `handleDispatch`) — clear the "new"
  draft on `createSession.mutate`'s `onSuccess`; leave it untouched on `onError`.
- Test: `web/src/features/sessions/list/DispatchBar.test.tsx` (existing).

**Order**
1. Write failing tests in `useSessionDraft.test.ts`: a call with the reserved "new" identity (e.g.
   `useSessionDraft('new:auto')` or however the identity is spelled — decide the exact string format
   in this step, since it becomes the literal storage key suffix) no longer hits the current
   `if (!sessionId) return` no-op path; `save`/`clear`/the lazy `read` all work for it exactly as for a
   real session id; the persisted shape gains `model`/`host`/`reasoningEffort` as optional fields
   (`SessionDraft` interface), tolerated-when-absent by `read()`'s existing defensive parsing (spec §4
   — "any new field is additive and tolerated-when-absent").
2. Confirm these fail (today, calling the hook with no real session id always no-ops).
3. Extend `useSessionDraft.ts`: widen `SessionDraft` with the new optional fields; the hook's contract
   stops being "no-op when `sessionId` is undefined" and becomes "no-op only when no identity is
   given at all" — `DispatchBar`/`NewSessionBar` always has *an* identity (real session id, or the
   reserved new-draft key), so the undefined case should no longer occur in practice for this call
   site; keep the guard for callers that still pass `undefined` legitimately (verify none currently
   rely on the no-op behavior with a defined-but-not-yet-created session before removing it — grep all
   `useSessionDraft(` call sites first).
4. Write failing tests in `DispatchBar.test.tsx`: type a sentinel, remount the component (simulating
   navigation away/back), confirm the sentinel is restored; a discard button/icon exists, is
   keyboard-operable, and clears both the field and the stored key; switching the Account dropdown
   surfaces a different (default-empty, unless that account already has its own stored draft) value
   for `text`.
5. Wire `DispatchBar` to read the initial draft on mount (keyed by the currently-selected account,
   defaulting to `auto`) and call `save` on every relevant field change; add the discard control.
6. Handle the account-switch case: re-reading the draft when `account` changes requires either
   re-mounting the hook's consumer per account (simplest, matches the "one identity per call" shape
   `useSessionDraft` already has) or extending the hook to accept a changing identity and re-read on
   change (the current hook reads `initial` exactly once via `useState`'s lazy initializer — switching
   account needs a fresh read, so this is the one point where the hook's contract must genuinely
   change, not just widen).
7. Wire `PromptDock.tsx`'s `handleDispatch`: call `clear()` inside `onSuccess`, before or alongside the
   existing `openSession(...)` call; confirm `onError`'s existing behavior (leaving `DispatchBar`'s
   local state intact) is now also backed by the persisted draft, so a page refresh after a failed
   dispatch still shows the sentinel (this is the part today's component-state-only draft cannot do).
8. Attachment lifecycle (FR-G06.6): confirm `DraftAttachment`'s `{id, mime, name}` shape is unchanged
   by this task (it must stay byte-free) and add a restore-time check — if an attachment id referenced
   by a restored draft no longer resolves (the upload expired), surface that inline rather than
   silently submitting a prompt with a dangling reference. Locate wherever attachment ids currently
   resolve to blobs (`useImageAttachments`/`AttachmentChips`, imported by `DispatchBar.tsx`) and thread
   an expired/missing state through it.
9. Run `npm run test` (full suite — this touches a hook and a component imported widely).
10. Manually verify in a browser: type in New Session, navigate to Pipeline, return — text persists;
    refresh — text persists; force a dispatch failure (e.g. block the network tab) — text persists and
    the failure is visible; succeed — the field clears and, per AC-G06.3, no *other* `ccswitch.draft.*`
    key changed (check via DevTools → Application → Local Storage before/after).
11. Commit, push, open PR.

**Tests**
- `web/src/features/sessions/composer/useSessionDraft.test.ts` — the "new" identity path, additive
  fields, no-op-only-when-truly-no-identity.
- `web/src/features/sessions/list/DispatchBar.test.tsx` — restore-after-remount, discard control,
  account-switch scoping, attachment-expiry inline message.
- A storage-namespace assertion (AC-G06.7) — this repo already has a row-7b namespace lint/test per
  the spec; extend it rather than writing a second one (grep for it before adding).
- AC-G06.3's "no other draft key touched" assertion belongs in `DispatchBar.test.tsx` or a
  `PromptDock.test.tsx` integration test: seed two `ccswitch.draft.*` keys, dispatch successfully,
  assert only the new-draft key was removed.

**Risks**
- *`repo`/`engine` exclusion from the draft is a judgment call this plan makes, not something the spec
  states explicitly* — FR-G06.1 lists "repo, engine, model, account" as things that must survive
  navigation. This plan's position is that repo/engine already survive (via `lastRepo.ts`/
  `enginePreference.ts`) and duplicating them would violate constitution VI. If the gate reads
  FR-G06.1 as requiring repo/engine inside the *draft* specifically (e.g., so an account-scoped draft
  can carry a different repo than the global last-used one), that's a small addition to the draft
  shape, not a redesign — flag it in the PR description either way so the gate can confirm the
  reading.
- *Changing `useSessionDraft`'s "no-op when undefined" contract is a behavior change to a hook three
  other things import* (`Composer.tsx` plus this task's new call sites) — a mis-scoped change could
  make a real session's draft start double-reading or double-writing. Grep every call site before
  editing, and keep the change additive (new optional identity forms) rather than restructuring the
  existing per-session path.
- *Attachment-expiry detection has no existing precedent named in the spec* — this is new product
  surface (an inline "these attachments are no longer available" state), not just a storage change;
  budget accordingly and confirm with a quick look at `useImageAttachments` before assuming the check
  is cheap.

---

### Task G07 — Query failures distinguishable from empty and healthy

*(PR 1 of 2, per Q6 — mechanism + the two audited surfaces.)*

**Files**
- Create: `web/src/api/queryState.ts` (or `useQueryState.ts` — name to match this codebase's existing
  `api/` naming; check for a convention before deciding) — the shared hook/component pair mapping a
  TanStack Query result to `loading | empty | failed-first-load | failed-refresh` plus a retry handle.
  Lives beside `queryClient.ts` per spec §4/FR-G07.5.
- Test: `web/src/api/queryState.test.ts` (or co-located `.test.tsx` if it exports a component, not
  just a hook).
- Modify: `web/src/features/sessions/SessionListColumn.tsx:110` — migrate `const { data: archived = []
  } = useArchivedSessions()` to the shared mechanism.
- Modify: the roadmap surface's equivalent `?? []` (was `ProjectsTab.tsx`; now under
  `web/src/features/roadmap/` — locate the specific hook call at implementation time, since this plan
  did not read that file).
- Modify: `web/src/api/queryClient.ts` — no functional change expected (`shouldRetryQuery` is
  consumed, not altered, per spec §4), but the new module's doc comment should cross-reference it.

**Order**
1. **Task 7.0 — re-run the audit.** `grep -rn '?? \[\]' web/src --include=*.tsx | wc -l` and
   `grep -rl isError web/src --include=*.tsx | wc -l` at the start of implementation, not from this
   plan's numbers (75 / 73 as of `4e50c7e6`; expect further drift). Attach the fresh count to the PR
   per AC-G07.5 — this audit output is itself a required deliverable, not a preamble.
2. Write failing tests for the new hook: given a query result in each of `isPending`, `isSuccess` with
   empty data, `isSuccess` with data, `isError` with no prior data, `isError` with prior data present
   (`isPlaceholderData`/`data` still populated from a previous success — TanStack Query keeps
   `data` set through a refetch error unless the query key changed), and post-retry-exhaustion — assert
   the hook/component returns the correct one of the four named states and a working `retry()`.
3. Confirm these fail (the mechanism doesn't exist yet).
4. Implement the hook: it wraps a `UseQueryResult`, does not re-implement retry counting (consumes
   `shouldRetryQuery`'s outcome — the query's own `failureCount`/`isError`), and returns
   `{state, retry}` where `state` is a discriminated union so a consumer can exhaustively switch on it.
5. Implement the rendering half (a small component, e.g. `<QueryStateView>`, or a render-prop/children
   pattern — match how this codebase's other "handle four branches" surfaces are shaped, e.g.
   `EmptyState`/`Card` in `ui/`) with: a live region (`role="status"` or `aria-live="polite"`) for the
   failure message (FR-G07.4); a retry button always present on a failure state, including
   post-exhaustion (FR-G07.3); the "stale" label rendered alongside retained data on `failed-refresh`
   (FR-G07.2) — do not discard `data` when `isError` fires with prior data present.
6. Migrate `SessionListColumn.tsx`'s `useArchivedSessions()` call to the new mechanism; delete the bare
   `?? []` fallback (constitution VI — purge in the same PR).
7. Migrate the roadmap surface's `?? []`/`isError` pair the same way.
8. Write/extend E2E or integration tests injecting 401/403/429/500/503/timeout/recovery responses at
   the two migrated call sites (AC-G07.1) — likely via MSW or the existing `page.route`/mock-fetch
   pattern this repo's other API tests use (check `queryClient.ts`'s or `hooks.ts`'s existing test file
   for the mocking convention before introducing a new one).
9. Run `npm run test` (full suite).
10. Commit, push, open PR with the Task 7.0 audit output attached.

**Tests**
- `web/src/api/queryState.test.ts` — all four states, retry semantics, retry-after-exhaustion.
- `web/src/features/sessions/SessionListColumn.test.tsx` — the migrated archived-count surface now
  distinguishes a failed read from a genuinely empty one.
- Whatever the roadmap surface's existing test file is named — same treatment.
- A live-region assertion (`getByRole('status')` or equivalent) for AC-G07.4.

**Risks**
- *TanStack Query v5's exact `data`-retention-through-error semantics must be verified against this
  app's actual `QueryClient` config, not assumed from general v5 behavior* — `queryClient.ts` sets
  custom `retry` but not `placeholderData`/`keepPreviousData`; confirm empirically (a quick test
  against a real query) that `data` survives a refetch failure before building the "stale" state on
  top of an assumption.
- *Two-PR split (Q6) means the interim state has some `?? []` sites migrated and most not* — tag the
  unmigrated sites' PR (issue) number in this PR's description so PR 2 has a concrete list to work
  from, not a second full re-grep starting from zero.
- *The 71→75 (or further) count drift is a moving target* — if PR 2 lands weeks later, re-run Task 7.0
  fresh rather than trusting this PR's snapshot.

---

### Task G08 — Pipeline PR anchors scoped by repository and occurrence

**Files**
- Create/modify: `web/src/pipeline/definitions.ts` (or a new adjacent module, e.g.
  `web/src/pipeline/prIdentity.ts` — this plan does not force the choice; spec §2 explicitly leaves
  placement to the Plan stage, and `definitions.ts` is `Definition`-shaped tooltip copy, not identity
  logic, so a new small module is more likely the right fit — confirm at implementation time whether
  `definitions.ts` already imports anything identity-shaped before deciding) — one
  `scopedPrId(repo: string, number: number, occurrence?: number): string` helper.
- Modify: `web/src/pipeline/PrRow.tsx:200` — `id={scopedPrId(pr.repo, pr.number, occurrence)}` in place
  of `id={\`pr-${pr.number}\`}`.
- Modify: `web/src/features/pipeline/charts/QueueTrain.tsx` — `handlePrLinkClick`/`PrLinks` need a
  `repo` prop threaded in (currently absent — verified `QueueTrain({ queue })` has no `repo` prop
  today); the anchor target becomes `scopedPrId(repo, n)`.
- Modify: `web/src/pipeline/PipelineView.tsx:148` — `<QueueTrain queue={r.queue} repo={r.repo} />`
  (verified `r.repo` exists on the same iteration item that already supplies `r.queue`).
- Modify: `web/src/features/pipeline/charts/__tests__/QueueTrain.test.tsx` — every existing
  `<QueueTrain queue={queue} />` call needs a `repo` prop added (currently ~35 call sites in this
  file); rework the `getElementById` mock the spec calls out into an assertion on resolved element
  identity.

**Order**
1. Write a failing unit test for `scopedPrId`: same repo/number → same id; different repo, same
   number → different ids; an occurrence discriminator, when given, differentiates two calls with
   identical repo+number (e.g. `scopedPrId('a/b', 8943)` vs. `scopedPrId('a/b', 8943, 1)`).
2. Implement `scopedPrId` (e.g. `pr-${repo.replace(/[^a-zA-Z0-9]/g, '-')}-${number}` plus an optional
   `-${occurrence}` suffix — DOM ids can't contain `/`, so `repo` needs sanitizing; decide the exact
   scheme here since both call sites must agree byte-for-byte).
3. Write a failing test in `PrRow.test.tsx` (or wherever `PrRow` is tested — locate it; not read
   during planning) rendering two repos sharing PR number `N`: assert the two rendered `id`s differ.
4. Wire `PrRow.tsx` to call `scopedPrId`.
5. Write failing tests in `QueueTrain.test.tsx`: pass `repo` through every existing call (mechanical,
   but touches ~35 lines — budget time for it); add a new case rendering two `QueueTrain`s for
   different repos sharing a PR number, asserting their generated anchor hrefs/click targets differ
   and that activating repo A's link resolves to repo A's `PrRow`'s actual DOM node (not a
   `getElementById` spy — render both `PrRow`s and `QueueTrain`s in the same test and assert on the
   resolved element, per the spec's explicit instruction to rework this test away from mocking
   `getElementById`).
6. Wire `QueueTrain`'s `repo` prop through `PrLinks`/`handlePrLinkClick`; update `PipelineView.tsx`'s
   call site.
7. Keyboard-activation parity (AC-G08.3): confirm `handlePrLinkClick`'s `scrollIntoView` also moves
   focus (`el.focus({ preventScroll: true })` alongside the scroll, since `scrollIntoView` alone does
   not move focus) — this is a real gap in the current implementation, not a rename.
8. Assert DOM-id uniqueness (AC-G08.3): a document-level test (or an addition to an existing
   `PipelineView`-level test) rendering a fixture with a repeated PR number across two repos and
   within one repeated section, asserting no two rendered elements share an `id`.
9. Run `npm run test`.
10. Commit, push, open PR.

**Tests**
- A new `scopedPrId` unit test (co-located with wherever the helper lands).
- `PrRow.test.tsx` — cross-repo id uniqueness.
- `QueueTrain.test.tsx` (reworked, not just extended) — cross-repo click resolves to the correct
  element; keyboard activation moves focus.
- A `PipelineView`-level (or new) test for document-wide id uniqueness across a fixture with the
  problem case (two repos, same PR number, one repeated section).

**Risks**
- *`QueueTrain.test.tsx` has ~35 existing `<QueueTrain queue={...} />` calls that all need a `repo`
  prop* — mechanical but easy to miss one and get a silently-passing test with `repo={undefined}`.
  Consider making `repo` a required prop (TypeScript will then force every call site to be touched,
  catching any miss at compile time rather than at test-review time).
- *The occurrence discriminator (FR-G08.2) has no concrete "repeated section" example in the codebase
  read during planning* — confirm at implementation time where a PR can legitimately appear twice on
  one Pipeline render (e.g., in both a "queue" car and a flat status-bucket row) before designing the
  discriminator's call convention; if no such case actually exists in the current data model, the
  discriminator can be a narrow, rarely-used optional parameter rather than a pervasive third id
  component.
- *DOM id sanitization of `repo` (which contains `/`) must exactly match between `PrRow.tsx` and
  `QueueTrain.tsx`* — since both call the same `scopedPrId` helper this is structurally guaranteed,
  but only if both are actually migrated in this same PR (constitution VI's "cannot drift because they
  call the same function" only holds once neither site still constructs an id manually).

---

### Task G20 — Route render failures contained and recoverable

**Files**
- Modify: `web/src/api/client.ts` — capture `res.headers.get('X-Request-ID')`; add `requestId:
  string | null` to `ApiRequestError`.
- Modify: `web/src/shell/routes.tsx` — add `errorElement` per route (or one shared element referenced
  by every route entry in `buildRoutes`), replacing reliance on the root `ErrorBoundary` (which,
  per `web/src/main.tsx:33`, wraps `<App />` outside the router and structurally cannot catch a
  React Router-owned render error).
- Create: `web/src/shell/RouteErrorFallback.tsx` — the route-level fallback: safe message, retry
  (`useRevalidator` or a simple re-navigate), a Home/Back link, correlation id display (real
  `X-Request-ID` from a thrown `ApiRequestError`, or the client-minted `client-XXXXXXXX` id from Q3
  when none exists), and a `clientLogger.error(...)` call carrying message/stack/correlation id/route
  (FR-G20.4) — logged, never rendered raw to the user (FR-G20.4, P4).
- Modify: `web/src/shell/ErrorBoundary.tsx` — no functional change expected (it stays the last-resort
  catch for anything outside the router, e.g. a throw during `<AppShell>`'s own render before routes
  mount); confirm its copy doesn't need to change now that route-level failures have their own,
  richer fallback.
- Modify wherever response-shape validation for API responses currently lives (or doesn't) — spec
  FR-G20.5 requires malformed payloads to be treated as G07 failures, not crashes; locate the
  boundary (likely inside `api/hooks.ts`'s query functions, wherever `request<T>()`'s return is
  consumed) and add a schema check (this repo's existing validation approach — check for `zod` or
  hand-rolled guards in `package.json`/`api/` before introducing a new validation library, per
  constitution VII "modern vetted deps" and VI "don't add a second way to do this").
- Test: `web/src/shell/routes.test.tsx` (if it exists; else co-located with `routes.tsx`).
- Test: `web/src/shell/RouteErrorFallback.test.tsx`.
- Test: `web/src/api/client.test.ts` (existing, presumably) — `requestId` capture.

**Order**
1. Write a failing test on `client.ts`: mock a `fetch` response with an `X-Request-ID` header and a
   non-2xx status; assert the thrown `ApiRequestError` carries `requestId` equal to the header value.
   Also assert a response with no such header yields `requestId: null`.
2. Implement: read the header in `request()`, pass it into the `ApiRequestError` constructor
   (widen the constructor signature; audit all existing `new ApiRequestError(...)` call sites — likely
   just the one in `client.ts` itself, plus any test fixtures constructing one directly).
3. Write a failing test rendering a route that throws during render (a test-only component that
   throws) inside `buildRoutes`' router: assert the DOM contains no stack trace text, no React
   Router default-error copy, and does contain a retry control and a Home/Back link (AC-G20.1/2).
4. Confirm it fails against `4e50c7e6` (React Router's default `ErrorBoundary` copy renders instead).
5. Implement `RouteErrorFallback.tsx`: use `useRouteError()` to get the thrown value; branch on
   whether it's an `ApiRequestError` (has `requestId`) vs. a plain `Error`/router-internal error (mint
   the `client-XXXXXXXX` id); call `clientLogger.error` with message/stack/correlation id/`useLocation()`'s
   pathname; render only the fixed fallback copy plus the correlation id — never `error.message` for a
   non-`ApiRequestError` (P4: an uncaught exception's message can embed paths/payload fragments).
6. Wire it into `routes.tsx`'s `buildRoutes`: add `errorElement={<RouteErrorFallback />}` to the routes
   that need it (likely all of them, or the top-level layout route so children inherit it — confirm
   React Router's inheritance semantics for `errorElement` before deciding whether this needs to be
   per-route or once at the root of the table).
7. Write a failing test confirming shell regions (nav/rail/dock) stay mounted and interactive when one
   route errors (AC-G20.2) — this depends on where in the route tree `errorElement` is attached: if
   it's on a leaf route nested inside `<AppShell>`, the shell survives by construction; if it's on the
   whole table, it doesn't. Get this right by testing it, not by inspecting the tree by eye.
8. Write a failing test for FR-G20.5: feed a malformed (shape-invalid) response through the boundary
   where responses are validated; assert it renders as a G07 failed-state, not as an uncaught
   exception reaching `RouteErrorFallback`.
9. If route-level code splitting exists anywhere in `routes.tsx` (grep for `lazy(` / `React.lazy` —
   not confirmed present during planning), add a chunk-load-failure test (AC-G20.5); if it doesn't
   exist anywhere in the app, this criterion is vacuously satisfied per spec §6 ("conditional... if
   splitting is not added, chunk-load coverage is out of scope") — state which case applies in the PR.
10. Run `npm run test`, `npm run build` (confirm the build doesn't warm any splitting that changes the
    answer to step 9).
11. Commit, push, open PR.

**Tests**
- `web/src/api/client.test.ts` — `requestId` capture, present and absent cases.
- `web/src/shell/RouteErrorFallback.test.tsx` — no stack/framework text in the DOM; retry present;
  Home/Back present; correlation id shown and matches the thrown error's `requestId` when present, or
  is a `client-`-prefixed id when absent; `clientLogger` received the failure (spy/mock it).
- `web/src/shell/routes.test.tsx` (or wherever route-table tests live) — shell regions survive a route
  error; navigating away recovers without reload.
- A malformed-response test wherever the validation boundary lands (FR-G20.5).

**Risks**
- *React Router's `errorElement` inheritance is easy to get wrong* — attach it too high and one
  broken route brings down siblings that share the boundary; attach it too low (only on the deepest
  leaf) and a failure in a shared layout/loader above it still isn't caught. This needs one concrete
  test (step 7) before trusting the placement, not an assumption from the React Router docs alone.
- *FR-G20.5's "boundary where a malformed payload would otherwise reach render" may not have one
  consistent location today* — if response shape is currently trusted ad hoc across many hooks in
  `api/hooks.ts` rather than at one seam, this task either finds the narrowest true seam (per-hook) or
  escalates that "add response validation everywhere" is bigger than this issue's stated scope; check
  before committing to an approach.
- *The correlation id shown must never leak `error.message` for a non-`ApiRequestError`* (P4) — the
  easiest implementation bug here is a well-meaning `{error.message}` left in the fallback for
  debugging and forgotten; the test in step 3/5 must assert the DOM does *not* contain the raw message
  string, not just that it contains the fixed copy.

## 4. Cross-task risks

- **P1/P2/P5 (spec §7) are policy decisions this plan does not make.** G06's draft-retention TTL (P1),
  the sanitization standard for attached evidence (P2), and the accessibility target's *location as a
  tracked record* (P5, distinct from the target itself, which `docs/ACCESSIBILITY.md` already names)
  remain for the human gate. No task above should be blocked from starting on these, but no task
  should be marked done against AC-X.4 until the gate resolves P2/Q5.
- **G01 and G02 both touch files under `web/src/features/sessions/`** — no line overlap identified
  (G01: `SessionListColumn.tsx`/`ScopeBody.tsx`/list controls; G02: `SessionHeader.tsx`/`SessionMeta.tsx`),
  but review them in the same sitting if both are in flight, since a shell-level CSS change (G01) and a
  header-shape change (G02) both alter what's visible in the same viewport screenshots used as
  AC-X.4 evidence.
- **G07's shared mechanism is a prerequisite people will reach for informally** — once
  `web/src/api/queryState.ts` exists (even mid-PR-1), a developer touching an unrelated `?? []` site in
  a different PR may want to use it. That's fine and expected; it does not need to wait for G07 PR 2.
- **None of the six tasks touch `server/`** — the `pytest server/tests/` / `pytest tests/` gates are
  expected to be unaffected no-ops on every PR in this plan; if any of them start failing, that is a
  sign of test pollution or an unrelated master-branch regression, not a change these tasks made, and
  should be investigated as such rather than patched around.

## 5. Execution handoff

This artifact is Plan-stage output and is not executed by this session (propose-only, per the
dispatch instructions). When a human approves it and the initiative advances to Execute, `core.md`'s
standing rule applies: **execute subagent-driven, one fresh subagent per task, review between tasks**
(`superpowers:subagent-driven-development`) — not inline batch execution. The six tasks above are
already sized for that: each has its own Files/Order/Tests/Risks and can be handed to a subagent
independently, in any order, since #1394 records no blocking relationship the Plan stage found reason
to override.
