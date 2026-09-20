# Page tabs: one tab, one question

**Status:** standard · **Applies to:** every Standard-variant page and every detail pane rendered
in a list/detail chassis (Roadmap, Delivery, Sessions) · **Extends:** the page contract in
`docs/superpowers/specs/2026-09-02-ui-page-migration-design.md` §2 (`PageHeader` → `Tabs` →
`PageContent`; tabs are URL state) · **Audit and change record:**
`docs/superpowers/specs/2026-09-20-page-tabs-standard-design.md`.

## 1. Why this exists

A page that stacks several unrelated concerns on one scroll makes every question cost a scroll,
puts half the page below the fold on a phone, and gives none of its parts an address. The page
contract already says *where* a tab strip goes and *how* its state is stored. What it did not say
is **what goes in a tab and what does not** — so two pages that had a tab strip still carried a
first tab that was a dumping ground of eight blocks. This document is that missing rule.

## 2. Where it applies, and where it does not

| Surface | Rule |
|---|---|
| Standard page; detail pane in a list/detail chassis (product, capability, initiative, automation, CI repo, archived session) | **This standard.** |
| Split page (Settings) | No tab strip (page contract §2.2). The rail is the partition; a section is organised in `SettingsGroup`s. A section with more than four groups or with two CRUD lists splits into **sibling sections**, governed by the settings redesign spec. |
| Workspace page (Sessions, Pipeline) | The board is one surface. A `Tabs` strip there is a **selector** (Pipeline's repo strip) or a **view toggle** (the session's Conversation / Browser), not a partition by question. §7 mechanics still apply. |
| Reader page (archived review, sub-agent view) | One document; no tabs. |
| List column, scope segments | Scope is a route (`aria-current="page"` links), never `role="tab"`. |
| Dialog, sheet, drawer | A single task. Never tabs inside a dialog. |
| Glossary / legend | Scanned, not navigated. Headings, no tabs. |

## 3. The rule: one tab answers one question

Every tab answers exactly **one** question about the object the page is for. The questions, and
the label each one wears everywhere in the app, are fixed — the same question must read the same on
a product, a capability, an initiative and an automation:

| Question about the object | Tab label | Content shape | Badge |
|---|---|---|---|
| What is it? | **Overview** | identity + summaries that link into the other tabs (§5) | none |
| What is moving right now? | **In flight** | list | count |
| What needs a person? | **Needs you** | list of decisions, each with its actions | count + attention dot |
| What could it work on next? | **Ranked** (a scored list) · **Queue** (dispatchable work) | list | count optional |
| What is under it? | **Sub-capabilities** · **Phases** · **Capabilities** · **Dependencies** | tree or chip editor | count |
| What is it configured to do? | **Config** · **Automation** · **Schedule** | form(s) — the *only* place a form with a Save lives | none |
| What happened? | **Events** | log, newest first | none |
| What did it produce? | **Sessions** · **Epic** · **Issues** | list linking out | count |

A tab that would answer two questions is two tabs. A tab whose question is not in this table is
allowed, but its label must be a noun naming the question's subject, and it goes into this table in
the same PR.

## 4. When a page gets tabs, and how many

- A page **gets a tab strip** when its content answers **three or more** of the questions in §3,
  or when it puts an editor (a form with a Save, a CRUD list, an editable table) beside a
  read-only status or list surface, or when any one concern is a list that can exceed ten rows
  beside another concern.
- A page with **one or two** short, read-only concerns stays a single scroll with `<section>`
  headings. Do not add a strip for two tabs.
- **At most six tabs.** Past six, either one tab answers two questions or the page is two
  objects that want two routes. A strip of six keeps three tabs visible at 390 px; the strip
  scrolls horizontally for the rest.
- **Conditional tabs.** Hide a tab only when the object's *kind* never has that question: an
  operator-triggered automation has no Schedule; an automation whose schema has no fields has no
  Config; only the Backlog Runner and PR Doctor have a Queue. Never hide a tab because it is empty
  *right now* — render the tab with a count of zero and an `EmptyState`, so the user learns the
  question exists.
- **Order.** Overview first, then the tabs a person acts on (Needs you, Queue), then the state
  tabs (In flight, lists), then configuration, then history (Events, Sessions).

## 5. The Overview budget

Overview is a **summary, not a container**. It is the tab that decays into a dumping ground,
because every new card "belongs on the overview". Its budget:

1. **One identity block:** description, links (repositories, doc, board), the Edit action, and
   the object's own creation affordance (`+ Capability` on a product).
2. **At most three summary blocks**, each of exactly one of two shapes:
   - a **notice** that renders *nothing* when there is nothing to say (roadmap hygiene, an open
     map-changes PR, the onboarding wizard until the product is live, the automation's
     actionable items), or
   - a **preview** of at most **three rows** with a "See all N" link that switches to the tab that
     owns the full list.

**Never on Overview:** a form with a Save; a table; a CRUD list; a list longer than three rows;
a second copy of a list another tab already renders in full. Each of those is a tab of its own
(§3). An Overview that ends up holding only the identity block is correct — it means the object
is simple.

## 6. Above the tabs

Only two things sit between the page header and the strip, because they must stay visible on
every tab:

- **Stat tiles** (`StatTiles`): counts; every tile that counts something a tab lists calls
  `setTab` for that tab on click, and the tile reads the **same number** as the tab's badge (the
  roadmap pages compute both from one helper for exactly this reason).
- **Page-level banners:** a failure whose cost is felt on every tab (the product's repositories
  did not load). A failure scoped to one tab's data stays inside that tab.

Dialogs mount **outside** the tab panel, mounted only while open (the RoadmapPage idiom), so a
tab change never unmounts a half-filled form and an abandoned form never leaks into the next
opening.

## 7. Mechanics

- **One primitive.** `web/src/ui/Tabs.tsx` is the only component that renders `role="tablist"`.
  `web/src/__tests__/tablistSingleSource.test.ts` fails the build on any other.
- **Ids and labels.** Ids live in a `const TABS = [...] as const` tuple next to the component;
  a shared label (a §3 word used by more than one page) comes from
  `web/src/features/products/vocab.ts`, never a string literal per page.
- **State is the URL.** `?tab=<id>` through `useSearchParamState('tab', TABS, DEFAULT)`
  (`useTabParam` in the roadmap is the same hook). Replace, never push; an unknown value falls
  back to the default silently; a link that targets a tab is built from the section's path helper
  plus `?tab=` (never a hand-typed path). A Workspace view toggle that is not the page's partition
  uses its own param name (`?view=`), same hook.
- **The default tab is the first in the strip** (Overview when the object has an identity; Needs
  you on a portfolio-style page that has none).
- **Panels.** One `role="tabpanel"` wrapper, `id={panelId(tab)}` and
  `aria-labelledby={\`tab-${tab}\`}`, with the `Tabs` `panelId` prop wiring the tab's
  `aria-controls`. Render only the active panel; do not mount every panel and hide the rest.
- **Badges.** `count` on a tab that lists rows; `attention` only on Needs you, and only when its
  count is above zero. Overview and configuration tabs carry no badge.
- **Tests.** Every tabbed page has one test per tab stating what that tab answers, plus a
  "tabs are URL state" test that mounts at `?tab=<id>` and asserts the selected tab.

## 8. Exceptions on record

An exception is a decision with a reason and a re-review trigger, not an oversight.

| Surface | Why it is not on this standard | Re-review when |
|---|---|---|
| Delivery → Capacity (`CapacityDetail.tsx`) | The capacity settings spec (2026-09-20, §5.2) pins it as one scroll: it is a comparison surface where a person sets a dial while looking at the stack that dial changes. | after that spec's PR 6 lands, if the page still reads as a lump |
| Settings › Notifications | Split variant; the fix is the `SettingsGroup` idiom and sibling sections, owned by the settings redesign, not tabs. | the settings redesign PR that migrates it |
| Pipeline repo strip | A selector, not a partition (§2). | never |
| Inbox proposal detail | One decision task; splitting hides the evidence from the Accept button. | if a third body kind appears |
| Pipeline legend | A glossary (§2). | never |
