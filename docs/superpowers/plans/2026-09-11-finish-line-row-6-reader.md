# Archived review and sub-agent view onto the chassis as Reader pages (finish-line row 6) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the two transcript-only pages of the Sessions section — the archived session's review (`/sessions/archived/:archivedId`) and the live sub-agent drill-down (`/sessions/:id/agents/:agentId`, and its background twin) — on the page contract as the first **Reader**-variant pages: `PageHeader` with a leading Back link, `PageContent scroll={false}` with `TranscriptView` owning the scroll, token classes only, and the shell's `app.dock` hidden while a Reader is open.

**Architecture:** Two new pages under `features/sessions/` (`ReviewPage.tsx`, `SubAgentPage.tsx`) replace `components/ReviewView.tsx` and `components/session/SubSessionView.tsx`, which are deleted with their tests. The shell learns "a Reader is open" from the route (`session/sessionNavState.ts: readerRouteForPath`), carried into `RegionContext.reader`, and the dock rule becomes `mode !== 'kiosk' && inSessions && !reader`. `PageHeader` gains one optional `back` slot (a `Link` before the title) so both readers and, later, Settings share a single Back implementation. Nothing about the transcript machinery changes: `TranscriptView`, `transcriptReducer`, `useSessionTranscript`, `useSessionRuntime`, the pinned permission/question panels and the Stop mutation are consumed as they are.

**Tech Stack:** React 19 + react-router + Tailwind v4 tokens (`web/`), vitest + Testing Library, Playwright. No `server/` changes.

**Spec:** `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` §5 row 6 (issue #1470); `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` §2 (page contract; §2.2 Reader rule — amended by R1 below; §2.3 invariants), §3 PR 7; `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md` §5.5 (the sub-agent view's header, Stop and in-place cards — the newer spec of record for that page); `docs/superpowers/specs/2026-09-02-ui-shell-workspace-design.md` §4.2 (region matrix: "app.dock: 1 in Sessions").

## Global Constraints

- Page contract (page-migration §2): `<div className="flex flex-col h-full min-h-0" data-page-variant="reader">` → `PageHeader` (the only `h1`; `document.title` "<title> — ViberPit" via `PageHeader`) → `PageContent scroll={false}` (the transcript virtualizer owns scroll: `TranscriptView` renders `data-testid="transcript-scroll"` with `overflow-y-auto`). No page-level `<main>` (`PageHost` is the single main landmark — `AppShell.test.tsx` asserts the axe `region` rule). Lucide icons only (`ArrowLeft`, `Square` for Stop).
- Reader (page-migration §2.2 as amended by R1): no composer, `app.dock` hidden, header outside the scroller (so it is "sticky" by construction — `PageHeader` is `shrink-0` above a `min-h-0` content zone), Back is the first control in the header.
- Token classes only under `web/src/features/`, `web/src/shell/`, `web/src/ui/` (`ccswitch/no-raw-palette`). Success greps at the end: `grep -rn "dark:" web/src/features/sessions/ReviewPage.tsx web/src/features/sessions/SubAgentPage.tsx web/src/ui/PageHeader.tsx` → 0; `grep -rnE "(bg|text|border)-(gray|blue|red|amber|green|emerald|violet)-" <same files>` → 0; `grep -rn "ReviewView\|SubSessionView" web/src web/e2e docs/superpowers/specs` → 0 (comments included; plans are history and stay).
- Single sources of truth: the Reader signal is derived ONCE, in `session/sessionNavState.ts` (`readerRouteForPath`), beside `listScopeForPath` — never re-tested from a pathname elsewhere; the Back link has ONE implementation (`PageHeader.back`), the pages do not render their own; sub-agent status → tone lives in `SubAgentPage.tsx`'s `STATUS_TONE` only (the `STATUS_DOT`/`STATUS_LABEL` maps move there, on tokens).
- `OpenItem` kinds are NOT touched: the sub-agent route must keep resolving to `{ kind: 'session', id }` so `useOpenSessionId` mounts the live runtime `SubAgentPage` reads through `useSessionRuntime`. The Reader signal is a separate, route-derived boolean.
- Deleted, each with its test in the same task: `web/src/components/ReviewView.tsx` + `web/src/components/__tests__/ReviewView.test.tsx` (Task 3); `web/src/components/session/SubSessionView.tsx` + `web/src/components/session/__tests__/SubSessionView.test.tsx` (Task 4).
- Every routed page change adds its row in `web/src/features/settings/pageContract.test.tsx` (Task 5). Sessions is `showInKiosk: false`, so no kiosk cell is asserted (the guard redirects) — the existing filter in that file handles it.
- Web gate on every task: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)` then `rm -rf web/dist`. Full vitest every time (79 files mock `@/api/hooks`). No server change; `ruff check src/ server/` once at the end for form.
- Bash hook: `(cd web && …)` subshells only — a bare `cd` persists and every later call fails; commit messages via `git commit -F <file>`; never pair deploy/release/rollout/promote with prod/production in command text.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Branch `feat/finish-line-row-6-reader` off `origin/master` (created, at c20211a3). One PR: `Fixes #1470`; ready + `gh pr merge --squash --auto` when green. Never push, open the PR or rebase from a task.
- Known local flake: `ReviewView.test.tsx`'s react-virtual teardown warning (passes alone) — it is deleted by Task 3; if `ReviewPage.test.tsx` inherits it, re-run the file alone before treating it as a failure.

## Rulings (controller, pre-execution)

- **R1 — "Reader" means no composer and no dock, not "no controls".** Page-migration §2.2 wrote "read-only" on 2026-09-02; the sub-agent spec of 2026-09-11 §5.5 gives that view a Stop action and answerable permission cards, and the finish-line row still says "sub-agent view → Reader". Both pages are Reader. The variant's rule is amended in the same PR to: *no composer; `app.dock` hidden; header with Back outside the scroller; the page's own few actions live in `PageHeader.actions`.* The archived review's only action is Unarchive; the sub-agent's is Stop (while `canStop`), plus its pinned cards in the content zone. Cost if wrong: one sentence in the spec and moving a button.
- **R2 — The shell learns "Reader open" from the route, not from `OpenItem`.** `readerRouteForPath(pathname)` matches `ARCHIVED_SESSION_ROUTE`, `SUBAGENT_ROUTE` and `BACKGROUND_SUBAGENT_ROUTE` with react-router's `matchPath` and returns `'archived' | 'subagent' | null`; `AppShellInner` puts `reader: readerRouteForPath(pathname) !== null` into `RegionContext`. Adding a `subagent` OpenItem kind would have stopped the live runtime from mounting under the sub-agent page (`useOpenSessionId` requires `kind === 'session'`). Cost if wrong: one helper.
- **R3 — `PageHeader` gains `back?: { to: string; label: string }`.** Rendered as a `Link` (real href; middle-click works) with `ArrowLeft`, `aria-label={label}`, `min-h-target`, before the title block; NOT gated on `useInteractive()` — navigation is not a mutation, and a kiosk display never reaches a Reader anyway (Sessions is kiosk-refused). `ui/Breadcrumb` already imports `Link` from react-router, so `ui/` importing the router has precedent. Settings' Back-in-`actions` is left as is (row 7 may adopt the slot). Accessible names change: "Back to dashboard" → **"Back to archived sessions"**; the sub-agent's stays **"Back to session"**. `App.test.tsx` queries move from `button` to `link`.
- **R4 — `PageContent scroll={false}` and the transcript owns scroll.** Loading = `PageContent busy` + `Skeleton` (the `role="status"` "Loading" announcer comes from `PageContent`); empty = `EmptyState`. The old "Loading transcript…" paragraph and its test go.
- **R5 — The archived page's title is the session's name.** `ReviewPage` reads `useArchivedSessions()` and finds its row by id (`name`, `archivedAt`, `repo`); until the list arrives or if the row is gone (a stale link) the title is the id's first 8 characters. `meta` = "Archived <date> · read-only" (date via `toLocaleDateString()`), plus `repo` when present. Unarchive stays in `actions` as a `Button variant="secondary" size="sm"` with the existing accessible name "Restore this session from archive" and the "Restoring…" pending text; on success the page navigates to `ARCHIVED_PATH` itself (no `onBack` prop any more).
- **R6 — The sub-agent page's `h1` is `type › description`**, exactly the old `h2` text, so `document.title` reads "Explore › map handling — ViberPit". `meta` = `Breadcrumb` (parent title → `sessionPath(id, scope)`, last = type) + the status line (dot + word + usage) + the activity/summary line. Stop = `Button variant="danger" size="sm"` in `actions`, `aria-label="Stop sub-agent"`, `iconLeft={<Square size={14} />}`, disabled on `isPending || isSuccess`, rendered only while `canStop`. The pinned `PendingQuestionPanel`/`PendingPermissionPanel` stay in the content zone under the transcript, unchanged.
- **R7 — The shared transcript pieces are out of scope.** `TranscriptView`, `PendingPermissionPanel`, `PendingQuestionPanel` keep their handful of `dark:` classes for row 7's sweep; this row converts only what it owns. `components/ArchivedSessionList.tsx` keeps `PermanentDeleteDialog` (the issue's "inline dialogs" were `ArchivedManager`'s, retired in #1261; the review page has none).
- **R8 — Routes keep thin wrappers in `routes.tsx`.** `ReviewPage()`/`SubAgentPage()` there still read params and guard missing ones with `<Navigate>`; they render `<ArchivedReviewPage sessionId=…/>` and `<SubAgentReaderPage sessionId=… agentId=…/>` — the page components are named with a suffix to avoid clashing with the wrapper names in that file. `routes.test.tsx` mocks move to the new modules with the same test ids (`page-review`, `page-subagent`).
- **R9 — E2E: one new spec, `web/e2e/archived-reader.spec.ts`,** phone + desktop, no backend (every `/api/*` intercepted, like `delivery.spec.ts`): h1 = session name, Back link, no `[data-region="app.dock"]`, no textbox, `transcript-scroll` present. `transcript-virtualization.spec.ts` keeps working (`transcript-scroll` is `TranscriptView`'s); only its comments naming `ReviewView` change.

## File map

| File | Responsibility |
|------|----------------|
| `web/src/session/sessionNavState.ts` (+ new `sessionNavState.test.ts`) | `readerRouteForPath` — the one Reader derivation. |
| `web/src/shell/AppShell.tsx`, `shell/visibilityMatrix.test.tsx`, `shell/AppShell.test.tsx` | `RegionContext.reader`; dock rule; matrix rows; two rendered-shell cases. |
| `web/src/ui/PageHeader.tsx`, `ui/Page.test.tsx` | The `back` slot. |
| `web/src/features/sessions/ReviewPage.tsx` (+ test) | `ArchivedReviewPage` — Reader for an archived transcript. |
| `web/src/features/sessions/SubAgentPage.tsx` (+ test) | `SubAgentReaderPage` — Reader for a live sub-agent. |
| `web/src/shell/routes.tsx`, `routes.test.tsx`, `App.test.tsx` | Wrappers point at the pages; mocks and Back queries updated. |
| `web/src/features/settings/pageContract.test.tsx` | Two `reader` rows. |
| `web/e2e/archived-reader.spec.ts`, `web/e2e/transcript-virtualization.spec.ts` | New smoke; comment refresh. |
| `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` (§2.2 Reader row, §3 PR 7 row), `2026-09-08-finish-line-persona-modes-design.md` (row 6 status), `2026-09-11-subagent-live-view-design.md` (line 12 wording) | Docs. |
| comment-only refresh: `shell/regions/PageHost.tsx:78`, `shell/regions/TopBar.tsx:27`, `session/sessionNavState.ts:53`, `components/session/NoticeCard.tsx:12`, `components/session/Turn.tsx:32`, `components/session/TranscriptView.tsx:23` | Names of the deleted components. |
| deleted | `components/ReviewView.tsx`, `components/__tests__/ReviewView.test.tsx`, `components/session/SubSessionView.tsx`, `components/session/__tests__/SubSessionView.test.tsx`. |

---

### Task 1: The shell hides the dock behind a Reader

**Files:**
- Modify: `web/src/session/sessionNavState.ts` (add `readerRouteForPath` after `listScopeForPath`, ~line 136)
- Create: `web/src/session/sessionNavState.test.ts`
- Modify: `web/src/shell/AppShell.tsx:91-105` (`RegionContext`), `:124` (dock rule), `:236-243` (context build)
- Modify: `web/src/shell/visibilityMatrix.test.tsx:25-30` (`BASE`), `:106-110` (dock rows)
- Modify: `web/src/shell/AppShell.test.tsx` (two cases after line 408)

**Interfaces:**
- Produces: `export type ReaderRoute = 'archived' | 'subagent'`; `export function readerRouteForPath(pathname: string): ReaderRoute | null`.
- Produces: `RegionContext.reader: boolean` (documented as `readerRouteForPath(pathname) !== null`).
- Consumes: `ARCHIVED_SESSION_ROUTE`, `SUBAGENT_ROUTE`, `BACKGROUND_SUBAGENT_ROUTE` (already exported from the same module); `matchPath` from `react-router-dom`.

- [ ] **Step 1: Failing unit test** — create `web/src/session/sessionNavState.test.ts`:

```ts
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readerRouteForPath, listScopeForPath } from './sessionNavState'

describe('readerRouteForPath', () => {
  it.each([
    ['/sessions/archived/abc', 'archived'],
    ['/sessions/abc/agents/toolu_1', 'subagent'],
    ['/sessions/background/abc/agents/toolu_1', 'subagent'],
  ] as const)('%s → %s', (path, expected) => {
    expect(readerRouteForPath(path)).toBe(expected)
  })

  it.each([
    '/sessions',
    '/sessions/abc',
    '/sessions/archived',          // the archived LIST is not a reader
    '/sessions/background/abc',
    '/pipeline',
    '/archived/abc',               // legacy path: redirected before it ever renders
  ])('%s → null', (path) => {
    expect(readerRouteForPath(path)).toBeNull()
  })

  it('agrees with listScopeForPath about which scope a reader is in', () => {
    expect(listScopeForPath('/sessions/archived/abc')).toBe('archived')
    expect(listScopeForPath('/sessions/background/abc/agents/x')).toBe('background')
  })
})
```

- [ ] **Step 2: Run it** — `(cd web && npx vitest run src/session/sessionNavState.test.ts)`. Expected: FAIL, `readerRouteForPath` is not exported.

- [ ] **Step 3: Implement** — in `web/src/session/sessionNavState.ts`, add `import { matchPath } from 'react-router-dom'` at the top (check the file's existing imports first; it imports `NAV_BY_ID` from `@/shell/navDefs`), and after `listScopeForPath`:

```ts
/** The two page shapes the shell treats as a Reader (page-migration §2.2). */
export type ReaderRoute = 'archived' | 'subagent'

/**
 * Whether the route is a Reader page, and which — the archived transcript or a
 * sub-agent drill-down. The shell hides `app.dock` behind a Reader (there is no
 * composer to sit above it) and this is the ONE derivation of that fact, beside
 * `listScopeForPath` and the route constants it reads. A Reader is recognised
 * by its ROUTE, never by an `OpenItem` kind: the sub-agent route must keep
 * resolving to `{ kind: 'session' }` so `useOpenSessionId` mounts the live
 * runtime the page reads.
 */
export function readerRouteForPath(pathname: string): ReaderRoute | null {
  if (matchPath(ARCHIVED_SESSION_ROUTE, pathname)) return 'archived'
  if (matchPath(SUBAGENT_ROUTE, pathname) || matchPath(BACKGROUND_SUBAGENT_ROUTE, pathname)) return 'subagent'
  return null
}
```

Note `ARCHIVED_SESSION_ROUTE` is `/sessions/archived/:archivedId` and `SESSION_ROUTE` is `/sessions/:id` — `matchPath` is exact by default (`end: true`), so `/sessions/archived` does not match the archived-session pattern and `/sessions/abc/agents/x` does not match `SESSION_ROUTE`.

- [ ] **Step 4: Run it** — same command. Expected: PASS (10 cases).

- [ ] **Step 5: Failing matrix rows** — in `web/src/shell/visibilityMatrix.test.tsx`:

`BASE` (line ~25) gains `reader: false,` after `inSessions: true,`. Update the doc table comment's dock line to `| app.dock     | 1 in Sessions, no Reader | … | 0 |` (keep the column alignment loose; it is prose). After the existing "app.dock — Sessions only (DEC-10)" block add:

```ts
  // app.dock — hidden behind a Reader (page-migration §2.2 as amended by
  // finish-line row 6): the archived transcript and the sub-agent drill-down
  // have no composer, so the new-session dock would be the only control in the
  // band and a phone would give it a row of the transcript for nothing.
  ['phone', 'app.dock', { reader: true }, false],
  ['tablet', 'app.dock', { reader: true }, false],
  ['desktop', 'app.dock', { reader: true }, false],
  ['kiosk', 'app.dock', { reader: true }, false],
```

- [ ] **Step 6: Run it** — `(cd web && npx vitest run src/shell/visibilityMatrix.test.tsx)`. Expected: the four new rows FAIL (dock still `true` on phone/tablet/desktop) — kiosk passes already; typecheck also complains that `reader` is not in `RegionContext`.

- [ ] **Step 7: Implement the rule** — `web/src/shell/AppShell.tsx`:

In `RegionContext` (after `inSessions`):
```ts
  /** `readerRouteForPath(pathname) !== null` — a Reader page (archived
   *  transcript, sub-agent drill-down) is open. Readers have no composer, so
   *  the dock is hidden behind them (page-migration §2.2, finish-line row 6). */
  reader: boolean
```
The dock rule:
```ts
  'app.dock': (c) => c.mode !== 'kiosk' && c.inSessions && !c.reader,
```
In `AppShellInner`'s `region` literal, after `inSessions: def?.id === 'sessions',`:
```ts
    reader: readerRouteForPath(pathname) !== null,
```
and add `readerRouteForPath` to the existing `@/session/sessionNavState` import in that file (it already imports `listScopeForPath` from there — grep `sessionNavState` in `AppShell.tsx`; if the import lives in `useSessionsWorkspace` instead, add a new import line).

- [ ] **Step 8: Run it** — `(cd web && npx vitest run src/shell/visibilityMatrix.test.tsx)`. Expected: PASS, including "covers every mode × region cell" (28 unchanged).

- [ ] **Step 9: Failing rendered-shell cases** — `web/src/shell/AppShell.test.tsx`, after the `drops app.dock outside the Sessions section` case (line ~408):

```ts
  it('drops app.dock behind the archived reader', async () => {
    renderShell({ path: '/sessions/archived/abc', mode: 'desktop' })
    await screen.findByTestId('page-review')
    expect(regionCounts().has('app.dock')).toBe(false)
    // The list column is still beside it on desktop — only the dock goes.
    expect(regionCounts().get('app.list')).toBe(1)
  })

  it('drops app.dock behind the sub-agent reader', async () => {
    renderShell({ path: '/sessions/abc/agents/toolu_1', mode: 'phone' })
    await screen.findByTestId('page-subagent')
    expect(regionCounts().has('app.dock')).toBe(false)
  })
```

Check the file's `probeRoutes()` (grep `page-review`/`page-subagent`/`probeRoutes` in `AppShell.test.tsx`): the two "archived reader on a phone/desktop" cases at lines 326–337 already render `/sessions/archived/abc`, so the probe route table already has a review probe; if its test id differs, use the id it renders (the assertion is about the dock, not the id). If there is no sub-agent probe route, add `{ path: SUBAGENT_ROUTE, element: <div data-testid="page-subagent" /> }` beside the review probe in that table.

- [ ] **Step 10: Run it** — `(cd web && npx vitest run src/shell/AppShell.test.tsx)`. Expected: PASS for the two new cases and every existing one (the archived LIST cases at lines 423–445 and 668–695 still see the dock: `/sessions/archived` is not a Reader).

- [ ] **Step 11: Gate** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist`. Expected: all green.

- [ ] **Step 12: Commit**

```bash
git add web/src/session/sessionNavState.ts web/src/session/sessionNavState.test.ts web/src/shell/AppShell.tsx web/src/shell/visibilityMatrix.test.tsx web/src/shell/AppShell.test.tsx
printf '%s\n' 'feat(shell): hide app.dock behind a Reader page' '' 'readerRouteForPath is the one derivation of "a Reader is open" (archived transcript, sub-agent drill-down); RegionContext.reader carries it and the dock rule reads it. Matrix rows and rendered-shell cases added.' '' 'Refs #1470' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/row6-t1.msg
git commit -F /tmp/row6-t1.msg
```

---

### Task 2: `PageHeader.back` — one Back link for every Reader

**Files:**
- Modify: `web/src/ui/PageHeader.tsx:7-25` (props), `:60-67` (markup)
- Modify: `web/src/ui/Page.test.tsx` (two cases after line 68)

**Interfaces:**
- Produces: `PageHeaderProps.back?: { to: string; label: string }` — a `Link` rendered before the title block with `aria-label={label}` and an `ArrowLeft` icon; visible text "Back".

- [ ] **Step 1: Failing tests** — in `web/src/ui/Page.test.tsx`, add `MemoryRouter` to the imports (`import { MemoryRouter } from 'react-router-dom'`) and, inside `describe('page primitives')` after the `PageHeader renders meta and actions` case:

```tsx
  it('PageHeader renders the back link before the title', () => {
    render(
      <MemoryRouter>
        <PageHeader title="Old work" back={{ to: '/sessions/archived', label: 'Back to archived sessions' }} />
      </MemoryRouter>,
    )
    const back = screen.getByRole('link', { name: 'Back to archived sessions' })
    expect(back).toHaveAttribute('href', '/sessions/archived')
    // Reading order: Back, then the h1 — a screen reader lands on the way out first.
    const h1 = screen.getByRole('heading', { level: 1 })
    expect(back.compareDocumentPosition(h1) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('PageHeader renders no link without back', () => {
    render(<PageHeader title="Plain" />)
    expect(screen.queryByRole('link')).toBeNull()
  })
```

- [ ] **Step 2: Run it** — `(cd web && npx vitest run src/ui/Page.test.tsx)`. Expected: FAIL — typecheck rejects `back`, no link rendered.

- [ ] **Step 3: Implement** — `web/src/ui/PageHeader.tsx`:

Imports: add `import { Link } from 'react-router-dom'` and `import { ArrowLeft } from 'lucide-react'`.

Props (after `documentTitle`):
```ts
  /**
   * A leading Back link (Reader pages; page-migration §2.2). Rendered BEFORE
   * the title so it is the first thing keyboard and screen-reader users reach,
   * and as a real link so it has an href. Not gated on `useInteractive()`:
   * leaving a page is navigation, not a mutation.
   */
  back?: { to: string; label: string }
```

Signature: `export function PageHeader({ title, documentTitle, back, meta, actions, className }: PageHeaderProps)`.

Markup — inside the `<header>`, before the `<div className="min-w-0 flex-1">`:
```tsx
      {back && (
        <Link
          to={back.to}
          aria-label={back.label}
          className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 text-sm font-medium text-accent min-h-target hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <ArrowLeft size={16} aria-hidden="true" /> Back
        </Link>
      )}
```

- [ ] **Step 4: Run it** — same command. Expected: PASS (both new cases; the kiosk `actions` cases unchanged).

- [ ] **Step 5: Gate** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist`.

- [ ] **Step 6: Commit**

```bash
git add web/src/ui/PageHeader.tsx web/src/ui/Page.test.tsx
printf '%s\n' 'feat(ui): PageHeader gains a leading back link slot' '' 'One Back implementation for Reader pages (page-migration §2.2): a real Link with ArrowLeft, before the title, not gated on the kiosk interactive flag.' '' 'Refs #1470' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/row6-t2.msg
git commit -F /tmp/row6-t2.msg
```

---

### Task 3: `ArchivedReviewPage` replaces `ReviewView`

**Files:**
- Create: `web/src/features/sessions/ReviewPage.tsx`, `web/src/features/sessions/ReviewPage.test.tsx`
- Modify: `web/src/shell/routes.tsx:73` (import), `:159-166` (`ReviewPage` wrapper)
- Modify: `web/src/shell/routes.test.tsx:59-63` (mock)
- Modify: `web/src/App.test.tsx:151-152`, `:174-175` (Back queries)
- Delete: `web/src/components/ReviewView.tsx`, `web/src/components/__tests__/ReviewView.test.tsx`

**Interfaces:**
- Produces: `export function ArchivedReviewPage({ sessionId }: { sessionId: string })`.
- Consumes: `PageHeader` with `back` (Task 2); `useSessionTranscript(sessionId, true, 100)`, `useUnarchiveSession()`, `useArchivedSessions()` from `@/api/hooks`; `fetchOlderTurns` from `@/api/transcript`; `transcriptReducer`/`initialTranscript` from `@/session/reducer`; `TranscriptView` from `@/components/session/TranscriptView`; `ARCHIVED_PATH` from `@/session/sessionNavState`; `PageContent`, `Skeleton`, `EmptyState`, `Button` from `@/ui/*`.

- [ ] **Step 1: Write the test** — `web/src/features/sessions/ReviewPage.test.tsx` (port of the old file; every behaviour it pinned survives except the "Loading transcript…" paragraph, replaced by the busy state, and the "Read-only (archived)" note, replaced by the meta line; Back becomes a link):

```tsx
/**
 * ArchivedReviewPage — the archived transcript as a Reader page (finish-line
 * row 6): one h1 named after the session, a Back link, Unarchive as the header
 * action, no composer, busy/empty states from the page primitives, and the
 * scroll-up paging that `ReviewView` had.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ArchivedReviewPage } from './ReviewPage'
import * as hooks from '@/api/hooks'
import { fetchOlderTurns } from '@/api/transcript'
import { mutationResult, queryResult } from '@/test/reactQuery'
import { resetDocumentTitle } from '@/shell/documentTitle'

vi.mock('@/api/hooks', () => ({
  useSessionTranscript: vi.fn(),
  useUnarchiveSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useArchivedSessions: vi.fn(() => ({ data: undefined })),
}))
vi.mock('@/api/transcript', () => ({ fetchOlderTurns: vi.fn() }))

const ARCHIVED = [{
  id: 'arc-99', name: 'Implement issue 16', account: 'alice', cwd: '/tmp/x', repo: 'o/r',
  engine: 'claude', engineSessionId: 'c-1', permissionMode: 'default',
  createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z',
}]

function renderPage(sessionId = 'arc-99') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/sessions/archived/${sessionId}`]}>
        <Routes>
          <Route path="/sessions/archived/:archivedId" element={<ArchivedReviewPage sessionId={sessionId} />} />
          <Route path="/sessions/archived" element={<div data-testid="archived-list" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const TURNS = {
  events: [
    { type: 'user', turnId: 't1', text: 'Hello world' },
    { type: 'turnStart', turnId: 't2' },
    { type: 'textDelta', turnId: 't2', text: 'Hi there!' },
    { type: 'turnEnd', turnId: 't2' },
  ],
  earliestTurn: 0,
  totalTurns: 2,
}

describe('ArchivedReviewPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetDocumentTitle()
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult(ARCHIVED) as never)
  })

  it('is a Reader page: h1 = session name, document.title, variant, Back link', async () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    expect(screen.getByRole('heading', { level: 1, name: 'Implement issue 16' })).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('Implement issue 16 — ViberPit'))
    expect(document.querySelector('[data-page-variant="reader"]')).not.toBeNull()
    expect(screen.getByRole('link', { name: 'Back to archived sessions' })).toHaveAttribute('href', '/sessions/archived')
    expect(screen.getByText(/archived .* · read-only/i)).toBeInTheDocument()
  })

  it('falls back to the short id while the archived list has not arrived', () => {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue(queryResult(undefined) as never)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage('arc-99abcdef')
    expect(screen.getByRole('heading', { level: 1, name: 'arc-99ab' })).toBeInTheDocument()
  })

  it('shows the busy state while the transcript is fetching', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({ data: undefined, isLoading: true } as never)
    renderPage()
    expect(screen.getByTestId('page-content')).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByTestId('transcript-scroll')).toBeNull()
  })

  it('shows the empty state when the events array is empty', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    expect(screen.getByText(/no transcript available/i)).toBeInTheDocument()
  })

  it('renders turns from transcript events without a composer', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult(TURNS) as never)
    renderPage()
    expect(screen.getByText('Hello world')).toBeInTheDocument()
    expect(screen.getByText('Hi there!')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('calls useSessionTranscript with enabled=true and a bounded initial window', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue({ data: undefined, isLoading: true } as never)
    renderPage()
    expect(vi.mocked(hooks.useSessionTranscript)).toHaveBeenCalledWith('arc-99', true, 100)
  })

  it('pages in older turns when scrolled to the top', async () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ ...TURNS, earliestTurn: 10, totalTurns: 30 }) as never)
    vi.mocked(fetchOlderTurns).mockResolvedValue({ events: [], earliestTurn: 0, totalTurns: 30 })
    renderPage()
    const scroller = screen.getByTestId('transcript-scroll')
    Object.defineProperty(scroller, 'scrollTop', { value: 0, configurable: true })
    fireEvent.scroll(scroller)
    await waitFor(() => expect(fetchOlderTurns).toHaveBeenCalledWith('arc-99', 10))
  })

  it('does not page when already at the session start', () => {
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult(TURNS) as never)
    renderPage()
    const scroller = screen.getByTestId('transcript-scroll')
    Object.defineProperty(scroller, 'scrollTop', { value: 0, configurable: true })
    fireEvent.scroll(scroller)
    expect(fetchOlderTurns).not.toHaveBeenCalled()
  })

  it('Unarchive is the header action and navigates to the archived list on success', async () => {
    const user = userEvent.setup()
    const mutate = vi.fn((_id: string, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.())
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(mutationResult({ mutate }) as never)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    await user.click(screen.getByRole('button', { name: /restore this session from archive/i }))
    expect(mutate).toHaveBeenCalledWith('arc-99', expect.objectContaining({ onSuccess: expect.any(Function) }))
    expect(await screen.findByTestId('archived-list')).toBeInTheDocument()
  })

  it('shows "Restoring…" and disables the button while unarchive is pending', () => {
    vi.mocked(hooks.useUnarchiveSession).mockReturnValue(mutationResult({ mutate: vi.fn(), isPending: true }) as never)
    vi.mocked(hooks.useSessionTranscript).mockReturnValue(queryResult({ events: [] }) as never)
    renderPage()
    const btn = screen.getByRole('button', { name: /restore this session from archive/i })
    expect(btn).toBeDisabled()
    expect(btn).toHaveTextContent('Restoring…')
  })
})
```

Before running: open the OLD `web/src/components/__tests__/ReviewView.test.tsx` lines 117–170 and copy the exact scroll-trigger mechanics it uses for the paging cases (it may drive `TranscriptView`'s `onLoadOlder` via a different event or a mocked virtualizer). Use the same mechanics here — the assertions above are the contract, the trigger must match how `TranscriptView` really fires `onLoadOlder`. Also read `web/src/test/reactQuery.ts` for the exact `queryResult`/`mutationResult` signatures.

- [ ] **Step 2: Run it** — `(cd web && npx vitest run src/features/sessions/ReviewPage.test.tsx)`. Expected: FAIL — module not found.

- [ ] **Step 3: Implement** — `web/src/features/sessions/ReviewPage.tsx`:

```tsx
/**
 * ArchivedReviewPage — an archived session's transcript as a Reader page
 * (finish-line row 6; page-migration §2.2).
 *
 * Fetches a bounded tail of the transcript via `useSessionTranscript`, folds
 * the events through `transcriptReducer` into turns, and renders them with
 * `TranscriptView` — no composer, no WS connection. Scrolling to the top pages
 * in older turns (mirrors `useSessionRuntime`'s onLoadOlder, self-contained
 * over plain HTTP). The shell hides `app.dock` behind this route
 * (`readerRouteForPath`), and `PageContent scroll={false}` because the
 * virtualizer inside `TranscriptView` owns the scroll region.
 *
 * The title is the session's name from the archived list (the transcript
 * payload carries none); until the list arrives — or if the row is gone, as
 * after a permanent delete with a stale link — the id's first 8 characters.
 */
import { useReducer, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Archive } from 'lucide-react'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import { useSessionTranscript, useUnarchiveSession, useArchivedSessions } from '@/api/hooks'
import { fetchOlderTurns } from '@/api/transcript'
import { ARCHIVED_PATH } from '@/session/sessionNavState'
import { TranscriptView } from '@/components/session/TranscriptView'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { Skeleton } from '@/ui/Skeleton'
import { EmptyState } from '@/ui/EmptyState'
import { Button } from '@/ui/Button'

/** Initial payload cap: the last N turns; older history pages in on scroll-up. */
const INITIAL_TURN_LIMIT = 100

export function ArchivedReviewPage({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const { data, isLoading } = useSessionTranscript(sessionId, true, INITIAL_TURN_LIMIT)
  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)
  const unarchive = useUnarchiveSession()
  const record = useArchivedSessions().data?.find((s) => s.id === sessionId)
  const title = record?.name || sessionId.slice(0, 8)

  // Fold every transcript event into turns once the data arrives, then close
  // the replay: replayComplete stores earliestTurn/totalTurns (which drive the
  // scroll-up paging trigger) and clears the replaying flag.
  useEffect(() => {
    if (!data?.events) return
    for (const ev of data.events) dispatch(ev)
    dispatch({ type: 'replayComplete', earliestTurn: data.earliestTurn, totalTurns: data.totalTurns })
  }, [data])

  // Scroll-up paging — mirrors useSessionRuntime.onLoadOlder.
  const loadingOlderRef = useRef(false)
  const onLoadOlder = useCallback(() => {
    if (loadingOlderRef.current) return
    if (state.earliestTurn <= 0) return // already at session start
    loadingOlderRef.current = true
    dispatch({ type: '_setLoadingOlder', value: true })
    fetchOlderTurns(sessionId, state.earliestTurn)
      .then((res) => dispatch({ type: '_prependHistory', events: res.events, earliestTurn: res.earliestTurn }))
      .catch(() => dispatch({ type: '_setLoadingOlder', value: false }))
      .finally(() => { loadingOlderRef.current = false })
  }, [sessionId, state.earliestTurn])

  const archivedOn = record ? new Date(record.archivedAt).toLocaleDateString() : null
  const meta = [archivedOn ? `Archived ${archivedOn}` : 'Archived', 'read-only', record?.repo ?? null]
    .filter(Boolean).join(' · ')

  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="reader">
      <PageHeader
        title={title}
        back={{ to: ARCHIVED_PATH, label: 'Back to archived sessions' }}
        meta={meta}
        actions={
          <Button
            variant="secondary"
            size="sm"
            disabled={unarchive.isPending}
            onClick={() => unarchive.mutate(sessionId, { onSuccess: () => navigate(ARCHIVED_PATH) })}
            aria-label="Restore this session from archive"
          >
            {unarchive.isPending ? 'Restoring…' : 'Unarchive'}
          </Button>
        }
      />
      <PageContent scroll={false} busy={isLoading}>
        {isLoading ? (
          <Skeleton lines={8} className="px-4 py-4 md:px-6" />
        ) : state.turns.length === 0 ? (
          <EmptyState icon={<Archive size={32} />} title="No transcript available for this session." />
        ) : (
          <TranscriptView
            turns={state.turns}
            toolDetailMode="auto"
            thinkingCollapsed={true}
            onAnswer={() => { /* read-only */ }}
            earliestTurn={state.earliestTurn}
            loadingOlder={state.loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        )}
      </PageContent>
    </div>
  )
}
```

`useArchivedSessions` polls every 30 s app-wide already (the archived scope uses it), so this adds no request when the list column is mounted beside the page; on a phone deep link it is one extra fetch.

- [ ] **Step 4: Run it** — same command. Expected: PASS.

- [ ] **Step 5: Wire the route and delete the old view.**

`web/src/shell/routes.tsx`: replace `import { ReviewView } from '@/components/ReviewView'` with `import { ArchivedReviewPage } from '@/features/sessions/ReviewPage'`; the wrapper becomes:

```tsx
/** `/sessions/archived/:archivedId` — the archived transcript as a Reader page
 *  (DEC-6). Never gets a live runtime: the param is not `id`, so
 *  `useOpenSessionId` finds nothing. The page owns its Back link and the
 *  post-unarchive navigation. */
function ReviewPage() {
  const { archivedId } = useParams()
  if (!archivedId) return <Navigate to={ARCHIVED_PATH} replace />
  return <ArchivedReviewPage sessionId={archivedId} />
}
```
(`useNavigate` stays imported — `SubAgentPage` still uses it until Task 4.)

`web/src/shell/routes.test.tsx:59-63`:
```tsx
vi.mock('@/features/sessions/ReviewPage', () => ({
  ArchivedReviewPage: ({ sessionId }: { sessionId: string }) => (
    <div data-testid="page-review">{sessionId}</div>
  ),
}))
```

`web/src/App.test.tsx:152` and `:175`: `screen.findByRole('button', { name: /back to dashboard/i })` → `screen.findByRole('link', { name: /back to archived sessions/i })` (both places). The test at 174 also says "lands on the read-only view" in its comment — leave the comment.

`web/src/shell/AppShell.test.tsx`: if its probe route for `/sessions/archived/abc` renders the REAL `ReviewView` (grep `ReviewView` in that file — the docstring at line 605 names it in prose only; if there is an import, point it at `ArchivedReviewPage`).

Delete: `git rm web/src/components/ReviewView.tsx web/src/components/__tests__/ReviewView.test.tsx`.

- [ ] **Step 6: Gate** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist`. Expected: green. `grep -rn "from '@/components/ReviewView'" web/src` → 0.

- [ ] **Step 7: Commit**

```bash
git add -A web/src/features/sessions/ReviewPage.tsx web/src/features/sessions/ReviewPage.test.tsx web/src/shell/routes.tsx web/src/shell/routes.test.tsx web/src/App.test.tsx web/src/shell/AppShell.test.tsx web/src/components/ReviewView.tsx web/src/components/__tests__/ReviewView.test.tsx
printf '%s\n' 'feat(sessions): archived review becomes a Reader page' '' 'features/sessions/ReviewPage.tsx replaces components/ReviewView.tsx: PageHeader (h1 = session name, Back link, Unarchive action), PageContent scroll=false with TranscriptView owning scroll, Skeleton/EmptyState states, tokens only. Old view and test deleted.' '' 'Refs #1470' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/row6-t3.msg
git commit -F /tmp/row6-t3.msg
```

---

### Task 4: `SubAgentReaderPage` replaces `SubSessionView`

**Files:**
- Create: `web/src/features/sessions/SubAgentPage.tsx`, `web/src/features/sessions/SubAgentPage.test.tsx`
- Modify: `web/src/shell/routes.tsx:72` (import), `:134-146` (`SubAgentPage` wrapper)
- Modify: `web/src/shell/routes.test.tsx:54-58` (mock)
- Delete: `web/src/components/session/SubSessionView.tsx`, `web/src/components/session/__tests__/SubSessionView.test.tsx`

**Interfaces:**
- Produces: `export function SubAgentReaderPage({ sessionId, agentId }: { sessionId: string; agentId: string })`.
- Consumes: `PageHeader.back` (Task 2); `useSessionRuntime(sessionId)` (`rt.state.subAgents[agentId]`, `rt.subAgents`, `rt.title`, `rt.toolDetailMode`, `rt.thinkingCollapsed`, `rt.onAnswer`, `rt.onChat`, `rt.onDecision`, `rt.mode`, `rt.armedQuestionId`); `useStopSubAgent()`; `useNow`; `agentDuration` from `@/components/session/subAgentDuration`; `listScopeForPath`, `sessionPath`; `TranscriptView`, `PendingPermissionPanel`, `PendingQuestionPanel` from `@/components/session/*`; `Breadcrumb`, `PageContent`, `EmptyState`, `Button` from `@/ui/*`; `SubAgentStatus` from `@/session/selectors`.

- [ ] **Step 1: Write the test** — `web/src/features/sessions/SubAgentPage.test.tsx` (port of the old file with the page-contract assertions added; the runtime mock is copied verbatim from the old test's lines 5–43):

```tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { resetDocumentTitle } from '@/shell/documentTitle'

const mutate = vi.fn()
let stopIsSuccess = false
vi.mock('@/api/hooks', () => ({
  useStopSubAgent: () => ({ mutate, isPending: false, isSuccess: stopIsSuccess }),
}))

const onDecision = vi.fn()
const onAnswer = vi.fn()
let thread: Record<string, unknown>
let agents: unknown[]
vi.mock('@/session/useSessionRuntime', () => ({
  useSessionRuntime: () => ({
    sessionId: 's1', title: 'Parent Session', mode: 'interactive',
    toolDetailMode: 'compact', thinkingCollapsed: false,
    subAgents: agents, armedQuestionId: null,
    onDecision, onAnswer, onChat: vi.fn(),
    state: {
      turns: [{ id: 'm1', role: 'assistant', blocks: [{ kind: 'text', id: 'm1:t0', text: 'MAIN_ONLY' }] }],
      subAgents: { toolu_A: thread },
    },
  }),
}))

import { SubAgentReaderPage } from './SubAgentPage'

function view(path = '/sessions/s1/agents/toolu_A') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SubAgentReaderPage sessionId="s1" agentId="toolu_A" />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mutate.mockReset(); onDecision.mockReset(); onAnswer.mockReset()
  stopIsSuccess = false
  resetDocumentTitle()
  thread = {
    turns: [{ id: 's1', role: 'assistant', blocks: [{ kind: 'text', id: 's1:t0', text: 'SUBWORK' }] }],
    openBlockId: null,
    task: { taskId: 'task1', description: 'map handling', agentType: 'Explore', backgrounded: false, depth: 1, status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3 },
  }
  agents = [{ toolId: 'toolu_A', type: 'Explore', description: 'map handling', status: 'running', startedAt: Date.now() - 65_000, activity: 'Running Bash', lastTool: 'Bash', totalTokens: 19591, toolUses: 3, taskId: 'task1' }]
})

describe('SubAgentReaderPage', () => {
  it('is a Reader page: one h1 = type › description, document.title, variant, Back link into the scope', async () => {
    view()
    expect(screen.getByRole('heading', { level: 1, name: 'Explore › map handling' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    await waitFor(() => expect(document.title).toBe('Explore › map handling — ViberPit'))
    expect(document.querySelector('[data-page-variant="reader"]')).not.toBeNull()
    expect(screen.getByRole('link', { name: 'Back to session' })).toHaveAttribute('href', '/sessions/s1')
  })

  it('the Back link and breadcrumb stay inside the background scope', () => {
    view('/sessions/background/s1/agents/toolu_A')
    expect(screen.getByRole('link', { name: 'Back to session' })).toHaveAttribute('href', '/sessions/background/s1')
    expect(screen.getByRole('link', { name: 'Parent Session' })).toHaveAttribute('href', '/sessions/background/s1')
  })

  it('renders the sub-agent thread, not the main turn, with a breadcrumb and no composer', () => {
    view()
    expect(screen.getByText(/SUBWORK/)).toBeInTheDocument()
    expect(screen.queryByText(/MAIN_ONLY/)).toBeNull()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toHaveTextContent('Parent Session')
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('shows the live status line, usage and activity', () => {
    view()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/19,591 tokens/)).toBeInTheDocument()
    expect(screen.getByText(/3 tools/)).toBeInTheDocument()
    expect(screen.getByText(/last: Bash/)).toBeInTheDocument()
    expect(screen.getByText('Running Bash')).toBeInTheDocument()
  })

  it('Stop is the header action and calls the mutation with the session and tool-use id', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: /Stop sub-agent/ }))
    expect(mutate).toHaveBeenCalledWith({ id: 's1', toolUseId: 'toolu_A' })
  })

  it('hides Stop for a running agent with no live task state (replayed/detached)', () => {
    agents = [{ ...(agents[0] as object), taskId: undefined }]
    view()
    expect(screen.queryByRole('button', { name: /Stop sub-agent/ })).toBeNull()
  })

  it('disables Stop once the mutation succeeds (until subAgentEnded unmounts it)', () => {
    stopIsSuccess = true
    view()
    expect(screen.getByRole('button', { name: /Stop sub-agent/ })).toBeDisabled()
  })

  it('a finished sub-agent shows its status and summary and no Stop button', () => {
    agents = [{ ...(agents[0] as object), status: 'stopped', summary: 'halted by user', taskId: 'task1' }]
    view()
    expect(screen.getByText('stopped')).toBeInTheDocument()
    expect(screen.getByText('halted by user')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop sub-agent/ })).toBeNull()
  })

  it('a permission card in the sub-thread is answerable in place', () => {
    thread = {
      ...thread,
      turns: [
        ...(thread.turns as unknown[]),
        { id: 'p1', role: 'assistant', blocks: [{ kind: 'permission', id: 'perm1', permId: 'perm1', toolName: 'Bash', input: { command: 'ls' }, state: 'pending' }] },
      ],
    }
    view()
    fireEvent.click(screen.getByRole('button', { name: /^Allow / }))
    expect(onDecision).toHaveBeenCalled()
  })

  it('shows the starting placeholder for a running agent with no turns yet', () => {
    thread = { ...thread, turns: [] }
    view()
    expect(screen.getByText(/Sub-agent starting/)).toBeInTheDocument()
  })
})
```

Before running: copy the exact permission-block shape from the OLD test's "answerable in place" case (lines 91–100 of `components/session/__tests__/SubSessionView.test.tsx`) into the case above — the block shape above is a sketch and `PendingPermissionPanel` reads specific fields.

- [ ] **Step 2: Run it** — `(cd web && npx vitest run src/features/sessions/SubAgentPage.test.tsx)`. Expected: FAIL — module not found.

- [ ] **Step 3: Implement** — `web/src/features/sessions/SubAgentPage.tsx`:

```tsx
/**
 * SubAgentReaderPage — live drill-down into one Task sub-agent, as a Reader
 * page (finish-line row 6; page-migration §2.2 as amended: no composer, dock
 * hidden, the page's own actions in the header).
 *
 * Parity with Claude Code's `/tasks` agent view (sub-agent spec 2026-09-11
 * §5.5): `type › description` as the title, status + elapsed · tokens · tools ·
 * last tool, the CLI's progress line while running (its summary once ended), a
 * Stop action, and the sub-agent's OWN permission/question cards pinned and
 * answerable here. There is no composer: the CLI drops user input addressed to
 * a sub-agent (spec §1).
 *
 * Data: `rt.subAgents` (merged live task state) for the header;
 * `rt.state.subAgents[agentId]` for the transcript and the cards. The runtime
 * is the PARENT session's — the route keeps `:id`, so `useOpenSessionId`
 * mounts it as for any open session.
 */
import { useLocation } from 'react-router-dom'
import { Square } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { useStopSubAgent } from '@/api/hooks'
import { useSessionRuntime } from '@/session/useSessionRuntime'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import type { SubAgentStatus } from '@/session/selectors'
import { agentDuration } from '@/components/session/subAgentDuration'
import { TranscriptView } from '@/components/session/TranscriptView'
import { PendingPermissionPanel } from '@/components/session/PendingPermissionPanel'
import { PendingQuestionPanel } from '@/components/session/PendingQuestionPanel'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb } from '@/ui/Breadcrumb'
import { Button } from '@/ui/Button'

/** Status → dot tone. The one place a sub-agent status becomes a colour. */
const STATUS_TONE: Record<SubAgentStatus, string> = {
  running: 'bg-info animate-pulse motion-reduce:animate-none',
  done: 'bg-success',
  failed: 'bg-danger',
  stopped: 'bg-neutral',
}
const STATUS_LABEL: Record<SubAgentStatus, string> = {
  running: 'running', done: 'completed', failed: 'failed', stopped: 'stopped',
}

export function SubAgentReaderPage({ sessionId, agentId }: { sessionId: string; agentId: string }) {
  // Back and the breadcrumb return to the parent INSIDE the list scope it is open in.
  const scope = listScopeForPath(useLocation().pathname)
  const parentPath = sessionPath(sessionId, scope)
  const rt = useSessionRuntime(sessionId)
  const thread = rt.state.subAgents[agentId]
  const turns = thread?.turns ?? []
  const meta = rt.subAgents.find((a) => a.toolId === agentId)
  const status: SubAgentStatus = meta?.status ?? 'running'
  const running = status === 'running'
  // Stop can only ever act on a LIVE task (spec §6): a replayed/detached
  // session's Task call with no paired result reads 'running' forever, and
  // taskId is only ever set from a live subAgentStarted/subAgentProgress.
  const canStop = running && meta?.taskId != null
  const now = useNow(1000, running)
  const duration = meta ? agentDuration(meta, now) : null
  const stop = useStopSubAgent()
  const label = meta?.type ?? 'sub-agent'
  const title = meta?.description ? `${label} › ${meta.description}` : label

  const usage = [
    duration,
    meta?.totalTokens != null ? `${meta.totalTokens.toLocaleString()} tokens` : null,
    meta?.toolUses != null ? `${meta.toolUses} ${meta.toolUses === 1 ? 'tool' : 'tools'}` : null,
    running && meta?.lastTool ? `last: ${meta.lastTool}` : null,
  ].filter(Boolean).join(' · ')

  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="reader">
      <PageHeader
        title={title}
        back={{ to: parentPath, label: 'Back to session' }}
        meta={
          <div className="flex min-w-0 flex-col gap-1">
            <Breadcrumb items={[{ label: rt.title || sessionId.slice(0, 8), to: parentPath }, { label }]} />
            <div className="flex items-center gap-2 text-xs">
              <span aria-hidden className={cn('h-2 w-2 rounded-full', STATUS_TONE[status])} />
              <span className="uppercase tracking-wide">{STATUS_LABEL[status]}</span>
              {usage && <span className="tabular-nums">· {usage}</span>}
            </div>
            {running && meta?.activity && <p className="truncate text-xs italic">{meta.activity}</p>}
            {!running && meta?.summary && <p className="line-clamp-3 text-xs text-ink">{meta.summary}</p>}
          </div>
        }
        actions={canStop ? (
          <Button
            variant="danger"
            size="sm"
            iconLeft={<Square size={14} aria-hidden="true" />}
            onClick={() => stop.mutate({ id: sessionId, toolUseId: agentId })}
            disabled={stop.isPending || stop.isSuccess}
            aria-label="Stop sub-agent"
          >
            Stop
          </Button>
        ) : undefined}
      />
      <PageContent scroll={false}>
        {turns.length === 0 ? (
          <EmptyState
            title={running ? 'Sub-agent starting…' : 'No live sub-agent activity'}
            description={running ? undefined : 'Open the parent session while it runs to stream it.'}
          />
        ) : (
          <TranscriptView
            turns={turns}
            toolDetailMode={rt.toolDetailMode}
            thinkingCollapsed={rt.thinkingCollapsed}
            onAnswer={rt.onAnswer}
          />
        )}
        {/* The sub-agent's own asks, pinned like the parent view pins them. Cards
            are mirrored into this thread by the reducer (keyed by permId /
            questionId), so answering here resolves the parent's copy too. */}
        <PendingQuestionPanel
          turns={turns}
          onAnswer={rt.onAnswer}
          onChat={rt.onChat}
          armedQuestionId={rt.armedQuestionId}
        />
        <PendingPermissionPanel
          turns={turns}
          permissionMode={rt.mode}
          onDecision={rt.onDecision}
        />
      </PageContent>
    </div>
  )
}
```

`PageHeader.meta` is wrapped in `text-sm text-ink-muted` by the primitive, so the status line inherits muted ink; the summary line opts back into `text-ink`. `EmptyState` centres itself and takes the whole content zone because `PageContent` is a flex column with `flex-1 min-h-0`; give the empty state `className="flex-1"` if it does not fill (check in the test's DOM or the e2e).

- [ ] **Step 4: Run it** — same command. Expected: PASS.

- [ ] **Step 5: Wire the route and delete the old view.**

`web/src/shell/routes.tsx`: replace `import { SubSessionView } from '@/components/session/SubSessionView'` with `import { SubAgentReaderPage } from '@/features/sessions/SubAgentPage'`; the wrapper becomes:

```tsx
/** `/sessions/:id/agents/:agentId` (and the background twin) — one sub-agent
 *  of an open session, as a Reader page. Keeps `:id` so the parent's live
 *  runtime mounts under it; the page derives its own Back target from the
 *  list scope in the URL. */
function SubAgentPage() {
  const { id, agentId } = useParams()
  if (!id || !agentId) return <Navigate to={SESSIONS_PATH} replace />
  return <SubAgentReaderPage sessionId={id} agentId={agentId} />
}
```
Then drop `useNavigate` and `useLocation` from the react-router import in `routes.tsx` if nothing else in the file uses them (grep before deleting; the legacy redirects use `useLocation` for `search`, so it probably stays), and `listScopeForPath`/`sessionPath` from the `sessionNavState` import likewise.

`web/src/shell/routes.test.tsx:54-58`:
```tsx
vi.mock('@/features/sessions/SubAgentPage', () => ({
  SubAgentReaderPage: ({ sessionId, agentId }: { sessionId: string; agentId: string }) => (
    <div data-testid="page-subagent">{`${sessionId}/${agentId}`}</div>
  ),
}))
```

Delete: `git rm web/src/components/session/SubSessionView.tsx web/src/components/session/__tests__/SubSessionView.test.tsx`.

- [ ] **Step 6: Gate** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist`. Expected: green. `grep -rn "SubSessionView" web/src --include=*.tsx --include=*.ts -l` → only comment mentions (Task 5 clears them).

- [ ] **Step 7: Commit**

```bash
git add -A web/src/features/sessions/SubAgentPage.tsx web/src/features/sessions/SubAgentPage.test.tsx web/src/shell/routes.tsx web/src/shell/routes.test.tsx web/src/components/session/SubSessionView.tsx web/src/components/session/__tests__/SubSessionView.test.tsx
printf '%s\n' 'feat(sessions): sub-agent view becomes a Reader page' '' 'features/sessions/SubAgentPage.tsx replaces components/session/SubSessionView.tsx: PageHeader (h1 = type › description, Back link into the list scope, Breadcrumb + status line as meta, Stop as the header action), PageContent scroll=false with the transcript and the pinned permission/question panels, tokens only. Old view and test deleted.' '' 'Refs #1470' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/row6-t4.msg
git commit -F /tmp/row6-t4.msg
```

---

### Task 5: Contract rows, E2E smoke, docs and comment sweep

**Files:**
- Modify: `web/src/features/settings/pageContract.test.tsx:130-147` (two rows; mocks as needed)
- Create: `web/e2e/archived-reader.spec.ts`
- Modify: `web/e2e/transcript-virtualization.spec.ts:8`, `:37`, `:58-64` (comments)
- Modify: `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md:43` (Reader rule), `:57` (PR 7 row); `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md:147` (row 6 status); `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md:12`
- Modify (comments only): `web/src/shell/regions/PageHost.tsx:78`, `web/src/shell/regions/TopBar.tsx:27`, `web/src/session/sessionNavState.ts:53`, `web/src/components/session/NoticeCard.tsx:12`, `web/src/components/session/Turn.tsx:32`, `web/src/components/session/TranscriptView.tsx:23`, `web/src/shell/AppShell.test.tsx:605`

- [ ] **Step 1: Contract rows** — in `PAGES` after the pipeline row:

```ts
  // The two Reader-variant pages (finish-line row 6). Sessions is not kiosk-safe,
  // so neither has a kiosk cell. The archived title is the session's NAME from
  // the archived list; the sub-agent title is `type › description` from the
  // parent's live task state, which this harness never has — so it reads the
  // fallback (`sub-agent`), and that is what the row asserts.
  { nav: 'sessions', path: '/sessions/archived/arc-1',        variant: 'reader',    title: 'Old work' },
  { nav: 'sessions', path: '/sessions/s-1/agents/toolu_1',    variant: 'reader',    title: 'sub-agent' },
```

Then make the harness answer the requests these pages make. Read the `api.get` mock at the top of the file (lines 27–100) and add, keeping the file's style:
```ts
        if (path === '/sessions/archived') return [{
          id: 'arc-1', name: 'Old work', account: 'alice', cwd: '/tmp/x', repo: null, engine: 'claude',
          engineSessionId: 'c-1', permissionMode: 'default',
          createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z',
        }]
        if (path.startsWith('/sessions/arc-1/transcript')) return { events: [], earliestTurn: 0, totalTurns: 0 }
```
and whatever `/sessions` / `/sessions/s-1` / WS attach the live runtime asks for under the sub-agent route — run the test, read the failing request path, stub it with the smallest shape that renders (the shell's `AppShell.test.tsx` `renderShell` and `App.test.tsx`'s `MOCK_SESSIONS` are the reference shapes; the sub-agent page tolerates a runtime with no thread, showing "Sub-agent starting…"). Note `renderApp` in this file may already mock `ws/client`; if the runtime needs a WS stub, copy the `vi.mock('./ws/client', …)` block from `App.test.tsx:34`.

- [ ] **Step 2: Run it** — `(cd web && npx vitest run src/features/settings/pageContract.test.tsx)`. Expected: PASS for 6 new cells (phone/tablet/desktop × 2) and every existing one.

- [ ] **Step 3: E2E** — create `web/e2e/archived-reader.spec.ts`:

```ts
/**
 * Finish-line row 6 — E2E smoke for the archived Reader page: one h1 named
 * after the session, a Back link, the transcript scroller, and NO new-session
 * dock and NO composer behind it (page-migration §2.2 as amended).
 *
 * Same harness as `delivery.spec.ts`: `npm run build && npm run preview` at
 * http://127.0.0.1:4173 (Playwright's `webServer` starts it); there is NO
 * backend — every `/api/*` call is intercepted below.
 *
 * Run: npx playwright test e2e/archived-reader.spec.ts
 */
import { test, expect, type Page } from '@playwright/test'

const SESSION = {
  id: 'arc-1', name: 'Implement issue 16', account: 'alice', cwd: '/tmp/x', repo: 'o/r', engine: 'claude',
  engineSessionId: 'c-1', permissionMode: 'default',
  createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z',
}

const EVENTS = [
  { type: 'user', turnId: 'u0', text: 'Hello from the past' },
  { type: 'turnStart', turnId: 'a0' },
  { type: 'textDelta', turnId: 'a0', text: 'An archived reply' },
  { type: 'turnEnd', turnId: 'a0' },
]

async function mockApi(page: Page) {
  await page.route('**/api/sessions/*/transcript*', (route) =>
    route.fulfill({ json: { events: EVENTS, earliestTurn: 0, totalTurns: 2 } }))
  await page.route('**/api/sessions/archived*', (route) => route.fulfill({ json: [SESSION] }))
  await page.route('**/api/sessions*', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/accounts*', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/pm/triage*', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }))
  await page.routeWebSocket('**/ws', (ws) => ws.onMessage(() => {}))
}

for (const [name, viewport] of [['phone', { width: 390, height: 844 }], ['desktop', { width: 1440, height: 900 }]] as const) {
  test.describe(`archived reader — ${name}`, () => {
    test.use({ viewport })

    test('is a Reader: h1, Back link, transcript, no dock, no composer', async ({ page }) => {
      await mockApi(page)
      await page.goto('/sessions/archived/arc-1')
      await expect(page.getByRole('heading', { level: 1, name: 'Implement issue 16' })).toBeVisible()
      await expect(page).toHaveTitle('Implement issue 16 — ViberPit')
      await expect(page.locator('[data-page-variant="reader"]')).toHaveCount(1)
      await expect(page.getByRole('link', { name: 'Back to archived sessions' })).toBeVisible()
      await expect(page.getByTestId('transcript-scroll')).toBeVisible()
      await expect(page.getByText('An archived reply')).toBeVisible()
      await expect(page.locator('[data-region="app.dock"]')).toHaveCount(0)
      await expect(page.getByRole('textbox')).toHaveCount(0)
    })

    test('Back returns to the archived list, where the dock is back', async ({ page }) => {
      await mockApi(page)
      await page.goto('/sessions/archived/arc-1')
      await page.getByRole('link', { name: 'Back to archived sessions' }).click()
      await expect(page).toHaveURL(/\/sessions\/archived$/)
      await expect(page.locator('[data-region="app.dock"]')).toHaveCount(1)
    })
  })
}
```

Check the route-order: Playwright matches the LAST registered route first, so the catch-all `**/api/**` must be registered FIRST if it is to lose to the specific ones — reorder the `mockApi` calls so the catch-all is registered before the specifics (read `delivery.spec.ts`'s helper for the convention this repo uses and follow it).

- [ ] **Step 4: Run it** — `(cd web && npm run build && npx playwright test e2e/archived-reader.spec.ts) ; rm -rf web/dist`. Expected: 4 passed. If chromium is missing, `npx playwright install chromium` first.

- [ ] **Step 5: Comment and doc sweep.**

`web/e2e/transcript-virtualization.spec.ts`: line 8 "read-only ReviewView route" → "archived Reader page (`features/sessions/ReviewPage.tsx`)"; line 37 "ReviewView folds" → "the reader folds"; lines 58–64 "ReviewView only fetches… ReviewView itself synthesizes that dispatch (ReviewView.tsx:38)" → "The reader page only fetches the transcript… the page itself synthesizes that dispatch from the top-level fields (`ReviewPage.tsx`, the replayComplete effect)".

Comment-only edits (replace the deleted names with the new ones, keep the sentences true):
- `web/src/shell/regions/PageHost.tsx:78` — "`ReviewView` and `SubSessionView` each declared their own" → "the archived review and sub-agent views (now `features/sessions/ReviewPage.tsx` and `SubAgentPage.tsx`) each declared their own".
- `web/src/shell/regions/TopBar.tsx:27` — "(App.tsx, SessionView, ReviewView)" → "(the pre-shell mobile chrome)".
- `web/src/session/sessionNavState.ts:53` — "`ReviewView`'s back button" → "the archived reader's Back link".
- `web/src/components/session/NoticeCard.tsx:12`, `Turn.tsx:32`, `TranscriptView.tsx:23` — "ReviewView" → "the archived reader".
- `web/src/shell/AppShell.test.tsx:605` — "`ReviewView`, `SubSessionView` and `App.tsx`" → "the archived review, the sub-agent view and `App.tsx`".

Docs:
- `docs/superpowers/specs/2026-09-02-ui-page-migration-design.md:43` — Reader row becomes: `| **Reader** | no composer; \`app.dock\` hidden (\`readerRouteForPath\` tells \`AppShell\`); \`PageHeader\` with the \`back\` slot outside the scroller; the page's own few actions (Unarchive, Stop) live in \`PageHeader.actions\`; \`PageContent scroll={false}\` when a virtualized transcript owns the scroll |`.
- Same file line 57 (PR 7 row): append to the "What moves / changes" cell: ` — landed 2026-09-11 (finish-line row 6, #1470): \`ReviewView\` → \`features/sessions/ReviewPage.tsx\`, \`SubSessionView\` → \`features/sessions/SubAgentPage.tsx\`; the Archived list had become a Sessions scope in #1261 and its dialogs were already on \`ui/Dialog\``.
- `docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md:147` — status cell: `landed 2026-09-11 (Closes #1470; Reader = no composer + dock hidden, the sub-agent page keeps Stop and its pinned cards per the 2026-09-11 sub-agent spec)`.
- `docs/superpowers/specs/2026-09-11-subagent-live-view-design.md:12` — "opens `SubSessionView`, a read-only transcript with a" → "opens the sub-agent Reader page (`features/sessions/SubAgentPage.tsx`), a transcript with no composer and a".

- [ ] **Step 6: Success greps** — all must print nothing:

```bash
grep -rn "ReviewView\|SubSessionView" web/src web/e2e docs/superpowers/specs
grep -rn "dark:" web/src/features/sessions/ReviewPage.tsx web/src/features/sessions/SubAgentPage.tsx web/src/ui/PageHeader.tsx
grep -rnE "(bg|text|border)-(gray|blue|red|amber|green|emerald|violet)-" web/src/features/sessions/ReviewPage.tsx web/src/features/sessions/SubAgentPage.tsx web/src/ui/PageHeader.tsx
ls web/src/components/ReviewView.tsx web/src/components/session/SubSessionView.tsx 2>&1 | grep -v "No such file"
```

- [ ] **Step 7: Gate** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) && rm -rf web/dist && ruff check src/ server/`.

- [ ] **Step 8: Commit**

```bash
git add web/src/features/settings/pageContract.test.tsx web/e2e/archived-reader.spec.ts web/e2e/transcript-virtualization.spec.ts web/src/shell/regions/PageHost.tsx web/src/shell/regions/TopBar.tsx web/src/session/sessionNavState.ts web/src/components/session/NoticeCard.tsx web/src/components/session/Turn.tsx web/src/components/session/TranscriptView.tsx web/src/shell/AppShell.test.tsx docs/superpowers/specs/2026-09-02-ui-page-migration-design.md docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md docs/superpowers/specs/2026-09-11-subagent-live-view-design.md
printf '%s\n' 'test(sessions): Reader contract rows, archived-reader e2e, docs for finish-line row 6' '' 'Two reader rows in the page-contract matrix; Playwright smoke for the archived Reader at phone and desktop (h1, Back, transcript, no dock, no composer); page-migration §2.2 Reader rule amended (no composer + dock hidden, actions in the header); PR 7 and finish-line row 6 marked landed; stale ReviewView/SubSessionView mentions refreshed.' '' 'Refs #1470' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/row6-t5.msg
git commit -F /tmp/row6-t5.msg
```

---

## Self-review

- **Spec coverage.** Finish-line row 6: "archived review → Reader" (Task 3), "sub-agent view → Reader" (Task 4), "page-contract rows for both" (Task 5), "inline dialogs onto `ui/Dialog`" (none exist in the review; the list's `PermanentDeleteDialog` is already a dialog component — R7 records it). Page-migration §2.2 Reader: no composer (both pages), sticky header with Back (Task 2 slot + header outside the scroller), `app.dock` hidden with the route telling the shell (Task 1). §2.3 invariants: one `h1`, `document.title`, variant attribute, no page `<main>`, lucide icons (Tasks 3–5). Sub-agent spec §5.5: header fields, Stop, in-place cards preserved (Task 4). Kiosk: unchanged (Sessions kiosk-refused; `back` not gated, R3).
- **Placeholder scan.** Two deliberate "read the old file and copy the exact mechanics" instructions (Task 3 paging trigger; Task 4 permission block) point at concrete line ranges of files that exist on the branch at execution time — they are not TBDs. Task 5 step 1's "stub what the runtime asks for" names the two reference files for the shapes.
- **Type consistency.** `readerRouteForPath(pathname): ReaderRoute | null` (Task 1) is what `AppShell` calls; `RegionContext.reader: boolean` matches the matrix rows' `{ reader: true }`; `PageHeaderProps.back: { to: string; label: string }` (Task 2) is what both pages pass; `ArchivedReviewPage({ sessionId })` and `SubAgentReaderPage({ sessionId, agentId })` match the route wrappers and the `routes.test.tsx` mocks; test ids `page-review`/`page-subagent` unchanged for `MATRIX_PATHS`.
