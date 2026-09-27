# Automations pages — layout, navigability and formatting audit

**Date:** 2026-09-27 · **Surface:** Delivery → Automations (`web/src/features/delivery/`): the list
column, the automation detail pane and its six tabs, the page header, and the sibling scopes (CI
health, Capacity) where they share a chassis. **Trigger:** "the tabs disappear in certain views".
**Standards applied:** the page contract (`docs/superpowers/specs/2026-09-02-ui-page-migration-design.md`
§2), the page-tabs standard (`docs/PAGE-TABS.md`), WAI-ARIA tabs/navigation patterns, and the usual
master–detail conventions (compact fixed header, no nested scroll for primary content, a panel never
restates its own tab, primary identifier never truncates before secondary metadata).

## Method

Every view was rendered against the live edge (`127.0.0.1:8787`, real data) in headless Chromium at
four widths — phone 375, tablet 820, desktop 1100, wide 1500 — and probed for tab-strip geometry
(`clientWidth` vs `scrollWidth`, which tabs sit fully inside the strip), header height, heading
levels and scope-segment overflow. The same probe re-ran against this branch through the Vite dev
proxy. Numbers below are from those runs.

## Findings

| # | Severity | Finding | Where |
|---|---|---|---|
| 1 | **High** | **The reported bug.** At 375px the six-tab strip is 472px in a 343px box with its scrollbar hidden and no edge cue, so Events and Sessions do not exist to the reader. A deep link (`?tab=events`) landed with the *selected* tab off-screen. Same primitive, same defect on Product (7 tabs) and Capability (6). | `ui/Tabs.tsx` |
| 2 | **High** | On a phone the page header — which never scrolls — was 14 lines (status text 5 lines, mode description 4) and pushed the strip to y=406 of a 740px viewport, leaving ~200px for the panel. On desktop the same block was 7 lines. | `DeliveryPage.tsx` header meta |
| 3 | **High** | The list column's scope segment clips its last scope at the default column width (296px): "Capacit" on Delivery (needs 345px), "Archived" on Sessions (324px). Three copies of the segment (`features/{sessions,roadmap,delivery}`) shared the defect. | scope segments |
| 4 | **Medium** | List rows truncate the automation's *name* to one or two letters ("S…", "D…", "A…") because the trailing badge spells out "Configured mode: propose". The primary identifier lost to secondary metadata. | `rows/AutomationRow.tsx` |
| 5 | **Medium** | Four panels restate their own tab as a heading, with a *different* word each time: tab Events → "ACTIVITY", Sessions → "SPAWNED SESSIONS", Config → "CONFIGURATION", Schedule → "SCHEDULE" twice (panel + editor). | Schedule/Events/Sessions/Config panels |
| 6 | **Medium** | Events and Sessions render in a 160px inner scroll box inside a page that already scrolls — a nested scroll region for the panel's primary content, 3–4 rows tall on a phone. | `EventsList.tsx`, `SessionsList.tsx` |
| 7 | **Medium** | Heading treatment is inconsistent across the app: delivery used `h4 font-semibold`, roadmap `h3 font-medium`, the Queue tab mixed `h3` (Products) and `h4` (Planned groups) in one panel. | 11 sites in delivery + roadmap |
| 8 | **Low** | Event times were a UTC substring (`ts.slice(11, 19)`) with no date; "planned 2026-09-27 14:27" likewise UTC. Elsewhere the app shows local time and relative time. | `EventsList.tsx`, `BacklogGroupsPanel.tsx` |
| 9 | **Low** | The "Custom dispatch…" button floated right-aligned between the Products table and the Planned groups list, belonging to neither; an empty plan hid the whole section, so the button lost its context. | `AutomationDetail.tsx` Queue |
| 10 | **Low** | The Sessions tab carried no count although the standard (§7) gives a list tab a badge; the count lived in a heading instead. | `AutomationDetail.tsx` |
| 11 | **Low** | The tab strip had no baseline rule on detail panes (the tabs' `-mb-px` exists to sit the indicator on one) while Pipeline and the session body added their own. | `ui/Tabs.tsx` callers |
| 12 | **Low** | Capacity's scope badge showed a count of "1" for a yes/no signal (a binding constraint). | `DeliveryScopeSegment.tsx` |

## What this change does

- **Tabs (1, 11):** the primitive scrolls the selected tab into view when the value changes (and
  again when its count badge arrives and widens it), fades the edge that hides tabs with a
  `mask-image`, and draws its own baseline; the two callers that added a border lost it. Measured:
  `?tab=sessions` at 375px now shows the Sessions tab fully; before, it was clipped.
- **Header (2):** three short lines. Line one: tier dot, tier line, capacity link, engine badge.
  Line two: `status.text` clamped to two lines with the whole string as its title (the runner's
  tail is the per-product breakdown the Queue tab tabulates). Line three: last observed, refresh
  failure, "Configured mode: on". The mode's *meaning* moved to the Overview identity block as a
  sentence ("**Mode:** on — Apply each product policy…"), where a paragraph belongs; it is also the
  header span's title. Measured at 375px: strip top 406 → 300 (with a 55px dev banner present).
- **Scope segment (3, 12):** one `ui/ScopeSegment` primitive replaces the three copies. `flex-auto`
  (widths follow labels), `text-xs`, `px-1.5`, an attention dot for yes/no signals, and `flex-wrap`
  as the fallback for a column dragged narrower than the labels. Measured at the default column:
  Sessions 310 → 296, Delivery 345 → 296 (scroll width = client width; nothing clipped).
- **Rows (4):** the mode badge is the mode value ("propose") with "Configured mode: …" as its title
  and sr-only prefix. Names now read ("Archive Audit", "Error Triage") at the default width.
- **Headings (5, 7, 9, 10):** `ui/SectionHeading` (an `h3`, one treatment) at every section-label
  site in delivery *and* roadmap; the four tab-restating headings are gone; "Planned groups" is a
  section heading with Custom dispatch in its `actions` slot and an empty plan is a sentence under
  it; the Sessions tab wears its count and the panel says how many are active.
- **Lists (6, 8):** no inner scroll boxes on Events/Sessions; event times are `<time>` elements in
  the reader's clock (seconds today, date otherwise, full stamp as title); "Planned 14m ago" is
  relative with the ISO stamp as title.
- `docs/PAGE-TABS.md` §7 records the primitive's overflow behaviour and the section-heading rule.

## Recommendations not taken here (need a decision or a spec)

1. **Actionable items want a tab.** Error Triage has 50 actionable items and no Queue tab; the runner
   has 19. Every automation's list lives on Overview in a 192px scroll box — a "list longer than
   three rows" the standard forbids there (§5). The standard's own answer is a **Needs you** tab
   (count + attention) with a three-row preview on Overview. That makes 7 tabs for the runner and
   PR Doctor, past the §4 cap: either record an exception (as Product Capabilities did) or fold
   Schedule into Config (the standard's table lists them as one question, but a Save-form beside
   live-patching fields is its own inconsistency). Product call.
2. **`status.text` is doing two jobs.** The runner's string carries fleet counts *and* a per-product
   breakdown; the header clamps it, but the counts ("63 blocked, 47 untriaged, 72 needs-human")
   would be stat tiles above the strip (§6) if the server exposed them in `status.counts`.
3. **Phone has no way back but the breadcrumb.** With the list column hidden behind an open detail,
   the 12px "Delivery › Automations" links are the only exit; Roadmap has the same shape. A
   `PageHeader.back` on phone/tablet for list/detail sections is a shell-level decision. The
   breadcrumb's last item also duplicates the `h1` on every detail page.
4. **Session status vocabulary.** The Sessions tab prints API keys (`waiting_external`,
   `pending_approval`); the Sessions page speaks in tiers. One label map, shared, is the fix — not
   a third vocabulary.
5. **Overview's actionable box keeps its 192px cap** until item 1 gives the list a home.
