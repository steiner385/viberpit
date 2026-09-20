# Page tabs standard: audit and reorganisation — design

**Date:** 2026-09-20 · **Standard:** `docs/PAGE-TABS.md` (the durable rule; this document is the
change record) · **Parents:** page-migration spec §2 (page contract), roadmap drill-down spec §7
(detail panes), finish-line spec §4.4 (automation detail).

## 1. Problem

The owner's report: "several pages across the app try to lump a bunch of different things into a
single page; it makes navigability a nightmare" — naming the product overview and the Backlog
Runner page. Both pages already have a five-tab strip on `?tab=`. What is lumped is the **first
tab** of each: the product's Overview renders eight blocks (a needs-you notice, a hygiene banner,
identity, a 299-line automation-policy editor, an add-capability button, a map-changes card, the
onboarding wizard, and a full in-flight list that the In-flight tab also renders), and the
automation's Status tab renders seven (description, oversight, workload notes, actionable items, a
products table, and two id-gated runner control surfaces). The page contract said where a strip
goes and how its state is stored; nothing said what a tab may hold. So the strip landed and the
lump moved one level down.

## 2. Audit (2026-09-20)

Every route page and every detail pane was inventoried (sections, existing tabs, state storage).

| Surface | Tabbed? | Finding | Verdict |
|---|---|---|---|
| Roadmap → product (`ProductDetail.tsx`) | yes, 5 | Overview holds 8 blocks incl. a form with Save and a duplicate of the In-flight list | **reorganise** |
| Delivery → automation (`AutomationDetail.tsx`) | yes, 5 | Status holds 7 blocks incl. a table and the runner's dispatch controls; first tab is labelled "Status" while every other page says "Overview" | **reorganise** |
| Session body (`SessionBody.tsx`) | hand-rolled | The only `role="tablist"` outside `ui/Tabs`; state in `useState`, not the URL | **align** (primitive + `?view=`) |
| Delivery → Capacity (`CapacityDetail.tsx`) | no | 4 concerns on one scroll — but pinned single-scroll by the capacity settings spec §5.2 (owner decision, same day) | **exception on record** |
| Settings › Notifications | n/a (Split) | 4 concerns separated by `border-t`, two CRUD lists; Split pages never get a strip | **follow-up issue** (settings-group idiom / sibling sections) |
| Roadmap portfolio, capability; initiative page | yes | on the standard already (3–5 tabs, URL state, tiles → tabs) | fine |
| Delivery → CI repo | no | four homogeneous report tables under one window | fine (one question: "how healthy is CI?") |
| Settings sections (General, Sessions, Appearance, …) | n/a | 1–4 `SettingsGroup`s each | fine |
| Inbox proposal detail | no | one decision task | fine (exception on record) |
| Pipeline | selector strip | repo selector, not a partition | fine |
| Pipeline legend | no | glossary | fine |

Mechanics were consistent everywhere the primitive is used: ids in a `const` tuple, `?tab=` via
`useSearchParamState`/`useTabParam` with replace semantics, one `role="tabpanel"`, tiles calling
`setTab`. The standard codifies that shape rather than changing it.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The rule is **one tab, one question**, with a fixed question → label table (`docs/PAGE-TABS.md` §3). | Two pages already disagreed on the first tab's name; a shared vocabulary is what stops the next page inventing a third. |
| D2 | **Overview has a budget**: one identity block plus at most three summary blocks that are either self-hiding notices or ≤3-row previews linking to the owning tab; never a form, table, CRUD list or a copy of another tab's list. | This is the rule whose absence produced both lumps. |
| D3 | Cap at **six tabs**; hide a tab only by object kind, never because it is empty now. | Six keeps three visible on a phone; hiding on emptiness teaches the user the question does not exist. |
| D4 | `ui/Tabs` is the **only** tablist; a source-scan test enforces it. | Constitution VI — the session body's hand-rolled strip is a parallel implementation. |
| D5 | Delivery → Capacity stays a single scroll, **as an exception on record** with a re-review trigger. | The owner walked through the capacity spec this morning and chose one page; a comparison surface where you set a dial while watching the stack is a defensible exception, and reversing a same-day decision by side effect is not this change's call. |
| D6 | Settings › Notifications is a **follow-up**, not part of this change. | Split pages never take a strip; its fix is the settings-group idiom that the settings redesign owns. |

## 4. Reorganisation

### 4.1 Product detail (`web/src/features/roadmap/detail/ProductDetail.tsx`)

Tabs: **Overview · In flight · Needs you · Ranked · Dependencies · Automation** (six).

- **Overview** keeps the identity block (description, repositories, mirrored count, Edit,
  `+ Capability`) and exactly three summary blocks (the §5 budget): the needs-you notice; the
  map-changes card / onboarding wizard (one "what the map is waiting on" notice: the wizard
  renders until live, the card only with an open PR, so at most one shows at a time); and the
  in-flight section as a **preview** — the first three in-flight initiatives with their tracks
  and a "See all N in flight" link that calls `setTab('in-flight')`, or one line saying nothing
  is in flight. The roadmap-hygiene card (`RoadmapHealthCard`) is a "what needs a person" notice,
  so it moves to the top of the **Needs you** tab rather than being a second needs-you-class
  block on the Overview.
- **Automation** (new) hosts `AutomationPolicyCard` unchanged. The capacity settings spec §5.3
  refers to "Roadmap → product → Automation card"; the tab keeps that name so the spec still
  resolves, and that spec's PR 4 edits the card in place.
- Stat tiles and the page-level "could not load this product" banner stay above the strip.

### 4.2 Automation detail (`web/src/features/delivery/automation/AutomationDetail.tsx`)

Tabs: **Overview · Queue · Schedule · Events · Sessions · Config** (six for the Backlog Runner and
PR Doctor; five for the others).

- **Overview** (was Status; id `overview`, label from `AUTOMATION_TAB`): description, oversight
  (responsibility, scope, relationships), the runner's workload notes, and the actionable-items
  list (`AutomationActions`) — the last is the page's "needs you" notice and self-hides when empty.
- **Queue** (new, offered only to automations that have dispatchable work: `backlog-runner`,
  `pr-doctor`): for the runner, the per-product table, the "Custom dispatch…" button and
  `BacklogGroupsPanel` (+ its dialog); for PR Doctor, `ConsolidatePanel`. Which automations have a
  Queue is one `const` set beside the tab table, not an `a.id ===` chain in JSX.
- `?tab=status` (old bookmarks) falls back to the default tab, which is Overview — no redirect
  needed. The capacity stack's runner row keeps linking to `?tab=config`.

### 4.3 Session body (`web/src/features/sessions/SessionBody.tsx`)

The Conversation / Browser strip renders through `ui/Tabs` with the same two ids, and the active
view is `?view=conversation|browser` via `useSearchParamState` (the standard's Workspace
view-toggle rule). The strip is still only rendered when the X display is available; with the
Browser view unavailable the param is ignored. The seven test files that mount `SessionBody` gain a
`MemoryRouter` wrapper.

### 4.4 Guard

`web/src/__tests__/tablistSingleSource.test.ts`: walks `web/src`, fails on any
`role="tablist"` / `role='tablist'` / `role: 'tablist'` outside `ui/Tabs.tsx` (tests excluded —
a test may assert the role by name). Same discovery-and-resolution idiom as
`noCollapsedClassTernary.test.ts`.

### 4.5 Docs

- `docs/PAGE-TABS.md` (the standard) — new.
- `docs/product/features/autopilot-backlog-runner.md`: the per-product table now lives on the
  Queue tab; the first tab is Overview.
- `docs/product/features/planning-and-automation.md` (or the product-management-layer doc,
  whichever names the product Overview card): the Automation card is the product's Automation
  tab.

## 5. Testing

- `ProductDetail.test.tsx`: Overview shows identity + at most three in-flight rows + the "See all"
  link that switches tabs; the automation card is on `?tab=automation` and not on Overview; the
  existing per-tab tests unchanged.
- `AutomationDetail.test.tsx`: `?tab=queue` renders the products table / groups panel for the
  runner and the consolidation panel for PR Doctor; a third automation has no Queue tab; Overview
  still renders actionable items; `?tab=status` lands on Overview.
- `SessionBody.browser.test.tsx`: the strip is `ui/Tabs` (roving tabindex); `?view=browser`
  selects Browser when enabled and is ignored when disabled.
- `tablistSingleSource.test.ts`: green on the tree; a fixture string with a second tablist fails.
- Full `npm run typecheck`, `lint`, `test`, `build` (shared-hook mocks touch unrelated files).

## 6. Non-goals

- Restructuring Delivery → Capacity or Settings › Notifications (D5, D6).
- Changing the roadmap portfolio / capability / initiative tabs (already on the standard).
- Any server change.
