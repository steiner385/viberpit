# Sub-spec C — Page migration: variants, contract, per-page plan, sweep

**Parent:** `2026-09-02-ui-target-state-design.md` (umbrella) · **Requires:** sub-specs A and B (PR 3)
merged · **Delivers:** umbrella §8 PRs 5–9, the page half of 10, and 11 · **Closes:** audit F4.3
(repo-wide), F5.1, F5.2, F6.1, F6.4, the empty-state / icon / type-scale / radius rows.

## 1. Scope

One page per PR. A page PR: (1) creates `features/<feature>/<Name>Page.tsx` on the variant
skeleton, (2) moves the page's widgets into the feature folder and onto `Card`/`Field`/…, (3)
deletes its feature CSS and ad-hoc headers, (4) adds it to `pageContract.test.tsx`. The last PR
sweeps what's left and widens the lint rule.

## 2. Page contract

### 2.1 Skeleton

```tsx
<div className="flex flex-col h-full min-h-0" data-page-variant="standard">
  <PageHeader title="Automations" meta={<…/>} actions={<Button …/>} />        {/* page.header */}
  <Tabs … />                                                                   {/* page.subnav, optional */}
  <PageContent busy={isLoading}>…</PageContent>                                {/* page.content */}
</div>
```

- `PageHeader`: renders the page's **only `h1`**, optional `meta` (subtitle / counts), `actions`
  (right slot); sets `document.title = "${title} — ccswitch"` on mount and title change; on phone
  the padding is `px-4 py-3`, desktop `px-6 py-4`; in kiosk `actions` are not rendered.
- `PageContent`: `flex-1 min-h-0`; `overflow-y-auto` (Standard/Split/Reader) or `overflow-hidden`
  (Workspace); `aria-busy` while `busy`; a `role="status"` live region announces "Loaded".
- Errors render as `<Banner tone="danger">` as the first child of `PageContent`; the header always
  renders.
- Loading: `Skeleton` rows/tiles matching the settled layout; never a full-page spinner and skeleton
  together. Empty: `EmptyState` (`icon`, `title`, `description`, `action?`).

### 2.2 Variants

| Variant | Rule |
|---|---|
| **Standard** | content scrolls; stat tiles (if any) first; list/cards after; `Skeleton` → `EmptyState` → content |
| **Split** | no `page.subnav`; `SettingsNav` (vertical, from `settingsDefs`) + scrolling panel on desktop/tablet-landscape; on phone/tablet-portrait the nav is `/settings` and each section is `/settings/:section` with Back |
| **Workspace** | `PageContent overflow-hidden`; inner panes own scroll; the header may be the feature's own (`SessionHeader` for Sessions, Pipeline's `HealthHeader` row inside `PageHeader.meta`) |
| **Reader** | read-only; sticky header with Back; `app.dock` hidden (the route tells `AppShell`) |

### 2.3 Invariants (tested)

One `h1` · `document.title` set · `data-page-variant` present · header renders while `busy` ·
`scrollWidth ≤ clientWidth` on `#root` · tabs/sections are URL state · phone `px-4`, desktop
`px-6` · no `dark:`/raw palette · lucide icons only.

## 3. Per-page migration

| PR | Page | Variant | What moves / changes | Deletes |
|---|---|---|---|---|
| 5 | **Settings** (`/settings/:section`) | Split | `settingsDefs.ts` (id, label, icon, component); `SettingsNav`; a `SettingsSection` chassis (`title`, `description`, `children`, `footer` with the Save row) that the 14 sections instantiate; `AccountsManager` and `AutomationView` become sections; theme toggle in General | `SettingsView` tablist; per-section heading markup |
| 6 | **Automations** | Standard | `AutomationCard` on `Card`; `ScheduleEditor`, `ManualDispatchDialog` on primitives; stat tiles for active/queued | ad-hoc headers |
| 7 | **Archived** + **Review** (`/archived`, `/archived/:id`) | Standard + Reader | `ArchivedManager` split into `ArchivedPage` + `ArchivedList`; `ReviewView` → `ReviewPage` (Reader) with sticky Back; sub-agent view → Reader | inline dialogs in `ArchivedManager` |
| 8 | **Insights** | Standard | `InsightsView` → `InsightsPage`; check-health tiles as stat tiles | |
| 9 | **Products** + **Initiatives** | Standard | `ProjectsTab`/`InitiativesTab` → pages; `.pm-*`/`.init-*` CSS rewritten as token utilities on `Card`/`Badge`; `InitiativeTrack` keeps the metro idiom but on tokens | `projects.css`, `initiatives.css`, the `.prdash-root` wrapper on Initiatives |
| 10 | **Pipeline** | Workspace | `PipelineTab` → `PipelinePage`; `PipelineSwitcher` → `page.subnav` `Tabs`; `HealthHeader` into `PageHeader.meta`; chrome bar actions (bell, legend) into `PageHeader.actions`; `plotroom.css` rewritten onto token utilities except the metro/gantt chart rules, which move to `features/pipeline/charts/plotroom.css` (lint allowlist) | `.prdash-root` remap, `pipeline/kiosk.ts`, `useKioskCycle` (→ page-local `useRepoCycle`) |
| 11 | **Sweep** | — | remaining `dark:`, raw palette, hex, emoji glyphs (→ lucide), `text-[Npx]`, `z-*` literals, `min-h-[44px]` (→ `min-h-target`); `localStorage` keys to `ccswitch.*` with one-time migration; ESLint rule to repo scope; `docs/ACCESSIBILITY.md` rewritten from the umbrella + this spec; `web/README.md` replaced with a real one (structure, gates, conventions, how to add a page) | `components/` bucket (must be empty), `lib/useMediaQuery.ts` |

Sessions (PR 4) already conforms; its page test is added in PR 5's `pageContract.test.tsx`.

## 4. Widgets

| Widget | Base | Notes |
|---|---|---|
| `SessionCard` | `Card` + `StatusPill` + `Badge` | `@container` logic kept; colors → tokens; rail tone from `TIER_ACCENT` |
| `AutomationCard`, `ProjectCard`, `InitiativeCard`, `PrRow` | `Card` | one padding/radius set |
| `PermissionCard`, `QuestionCard`, `PendingModelApprovalPanel`, `YourTurnPanel` | `Card tone=warning` | Allow once / Always allow / Deny buttons on `Button` |
| `ToolCallCard`, `ErrorCard`, `RecoveringCard`, `Turn` | compact `Card` | Transcript view modes respect `data-verbosity` |
| `SettingsSection` | `Field` + chassis | 14 instances |
| `EmptyState` | primitive | the 17 ad-hoc "No … yet" paragraphs |
| `Skeleton` | primitive | the 9 `animate-pulse` sites |

## 5. Tests

| Test | Asserts |
|---|---|
| `pageContract.test.tsx` | every `*Page` × mode: §2.3 invariants; each page is registered in a `PAGES` list the test iterates (adding a page without registering fails a count check against `navDefs`) |
| per-page tests | moved with the page; ad-hoc empties replaced by `EmptyState` assertions |
| `a11y.test.tsx` | `jest-axe` per page per mode |
| `lint` (repo scope, PR 11) | zero `dark:`, raw palette, hex, arbitrary px, emoji in `web/src` outside the chart allowlist |
| Playwright smoke | each route per viewport project (from sub-spec B) |

## 6. Docs produced

- `docs/ACCESSIBILITY.md` — rewritten: scope = every page and primitive; the terminal exception is
  retired (no xterm surface exists); commitments list references the contract.
- `web/README.md` — folder map, the four gates, `lint-changed`, how to add a page (variant, register in
  `navDefs`, add to `PAGES`), how to add a primitive.
- `docs/product/features/web-dashboard-mobile.md` — updated links and the four modes.

## 7. Risks

- **Pipeline CSS rewrite** is the largest single page (1,216 lines). Mitigation: charts stay as CSS
  in the allowlisted folder; only chrome/board rules convert.
- **Settings sub-routes on phone** change navigation depth; Back must return to `/settings`, tested.
- The **sweep PR** touches many files mechanically; the lint rule at repo scope is the acceptance,
  reviewed by CI rather than by eye.
