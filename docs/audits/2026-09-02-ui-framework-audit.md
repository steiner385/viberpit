# UI Framework & Design System Audit — 2026-09-02

**Scope:** the *structure* of the ccswitch web UI (`web/src`) — region model, shell/layout architecture,
responsive/kiosk/mobile strategy, design tokens, primitives, page and widget contracts. Not individual
screens or features.

**Method:** static read of every shell/layout/page-root file, `web/src/index.css` and the three feature
CSS files, `tailwind.config.js`, `package.json`, `web/dist` (to verify what CSS actually ships), and the
existing design docs (`docs/ACCESSIBILITY.md`, `docs/product/features/web-dashboard-mobile.md`,
`docs/superpowers/plans/2026-06-21-master-detail-sessions-layout.md`, `specs/001-ccswitch-web/*`).
Counts below are `grep` over non-test `.ts/.tsx/.css` (~43k lines). An open-issues sweep was attempted
but blocked by a GitHub API rate limit; nothing below depends on it.

**Verdict in one line:** ccswitch has strong *behavioral* single-sources-of-truth (tab model, status
tiers, shortcut registry, session runtime) and almost no *structural* ones — no named region model,
no page-type contract, an unbridged token layer that ships dead CSS, two app shells that have drifted,
and "kiosk" that is a query flag inside one tab rather than an app mode.

---

## Scorecard

| # | Question | Status | Headline |
|---|----------|--------|----------|
| 1 | Are the app's regions named? | **No** | Names exist only as an ASCII diagram in a `DesktopShell` doc-comment. No region has a type, a component, or a slot. |
| 2 | Are region relationships understood; any overlaps? | **Partially** | Two shells (`Dashboard` mobile, `DesktopShell` desktop) render different region sets with different state models; 11 concrete parity gaps and 6 duplicated region implementations. |
| 3 | Do we know how the UI changes desktop ↔ kiosk ↔ mobile? | **No** | One JS breakpoint at 1024px swaps the entire component tree. Kiosk exists only for the Pipeline tab. Tablet is undefined. |
| 4 | Are we violating standards/conventions? | **Yes** | shadcn semantic classes ship **zero CSS** (verified in `dist`); URL-as-state broken on desktop; constitution VI violated at the structural layer (2 shells, 2 tablists, 2 z-index scales, 2 type scales, 3 palettes). |
| 5 | Page-type and widget-type standards? | **No** | Six de-facto page kinds, five naming suffixes, no shared header/scroll/empty/loading contract; `EmptyState` used once against 17 ad-hoc empties. |

Severity legend used below — **Critical**: ships a visible defect or blocks the target state;
**Major**: structural debt that every new feature pays twice; **Minor**: inconsistency worth fixing in the
migration, not on its own.

---

## Q1 — Region model: what exists, what it's called, where it lives

There is no region vocabulary in code. The closest thing is the comment atop `DesktopShell.tsx`
(`header / sidebar / main pane / unified bottom prompt`). The table below is the **de-facto** inventory
reconstructed from the render trees of `App.tsx`, `DesktopShell.tsx`, `Dashboard.tsx`, `SessionView.tsx`,
`SessionBody.tsx`.

| De-facto region | Desktop (≥1024) | Mobile (<1024) | Named in code? | Notes |
|---|---|---|---|---|
| **App banner stack** | `GlobalUpdateBanner`, `DaemonRestartBanner`, `GitCredentialBanner` above the shell (`App.tsx`) | same | no | Outside both shells; a fourth banner (`AllLimitedBanner`) and `DeployPanel` mount *inside* each shell separately; `UpdatePendingBanner` mounts on mobile only. Three mount points for one role. |
| **Brand / app header** | `<header>`: BrandMark + h1 + `TabBar` + Help/Settings icon buttons | `<header>` in `DashboardRoute`: BrandMark + h1 only; tabs live *below* in `Dashboard` | comment only | Help/Settings actions exist in the header on desktop but inside the *Sessions tab panel* on mobile — invisible from other tabs. |
| **Primary navigation** | `TabBar` (horizontal, in header) | `TabBar` (horizontal, under header) | ✅ `TabBar` + `tabDefs.ts` | The one properly canonical region. There is **no left navigation** — the left column is the session list, not nav. |
| **Sidebar (session rail)** | `<aside>`: New Session slot, `SessionListControls`, `QueuedSection`, `SessionList`, `AdoptPanel` + `SidebarResizer` | n/a — the list *is* the Sessions tab content | comment only | Mobile additionally shows `PendingApprovalSection` and `DaemonHealthIndicator`; desktop does not. |
| **Main pane** | `<main>`; content switched by `tab` state | `role="tabpanel"` per tab (some tabs skip the role) | comment only | Wrapper classes differ per tab and per shell (see Q5). |
| **Page header** | none — pages start straight into content | none | no | No page has an `<h1>`; some have an uppercase-xs `<h2>`, some a 16px CSS `<h2>`, most none. |
| **Page sub-navigation** | `SettingsView` hand-rolled `role="tablist"`; `PipelineTab` "chrome bar" (`PipelineSwitcher`, bell, legend) | same | no | Second and third tab-strip idioms alongside `TabBar`. |
| **Session header** | `MainPaneSession`: light `bg-gray-100 dark:bg-gray-900`, title + goal + `SessionActions` | `SessionView`: **always** `bg-gray-900` (dark in light mode), Back + prev/next chevrons + title; actions moved into the meta strip | no | Two implementations of the same region with different theming rules. |
| **Session meta strip** | `SessionBody`: account · model · context · quota · `StatusBar` · sub-agents | same (+ `actionsSlot`) | comment only | Shared — good. |
| **Session transcript** | `SessionBody` → `TranscriptView` | same | ✅ component | Shared. |
| **Pinned action panels** | `PendingPermissionPanel`, `PendingModelApprovalPanel`, `PendingQuestionPanel`, `YourTurnPanel` | same | ✅ components | Shared. |
| **Bottom prompt / dock** | `DispatchBar` *or* `SessionComposer`, spans full width, outside `<main>` | `DispatchBar` inside the Sessions tab panel; `SessionComposer` inside `SessionView` | comment only ("unified bottom prompt") | Desktop `DispatchBar` omits the `hosts` prop → **no fleet host picker on desktop**. |
| **Help drawer** | `HelpDrawer` (right slide-over, `fixed inset-0 z-50`) | same | ✅ component | Its `NAV_MAP` still lists "Accounts" as a top-level tab (stale). |
| **Command palette** | `CommandPalette` inside the composer (slash commands) | same | ✅ component | Not an app-level ⌘K palette; scoped to the session composer. |
| **Command result overlay** | `CommandResultOverlay` | same | ✅ component | Rendered in `MainPaneSession` on desktop, at the top of `SessionView` on mobile. |
| **Dialog layer** | 13 files with their own `fixed inset-0` + backdrop; `NewSessionDialog`/`RenameDialog` live *inside* `Dashboard.tsx` | same | no | 10 hand-rolled focus traps; the `useFocusTrap` hook (pipeline) has one consumer. |
| **Deploy overlay** | `DeployOverlay` (App-level) | same | ✅ component | |
| **Toasts** | `sonner` `<Toaster position="top-center">` (`main.tsx`) | same | ✅ | 79 `toast()` calls in 20 files — one channel, good. |
| **Footer** | none | none | — | Not a region today. |
| **"AI drawer"** | does not exist | — | — | No assistant/AI side panel exists anywhere in `web/src`. |

**Findings**

- **F1.1 (Major)** Regions are implicit. Nothing enforces that a region exists, is unique, or is mounted
  in one place — so `DeployPanel`/`AllLimitedBanner`/`HelpDrawer`/rename+delete dialogs are each mounted
  twice (once per shell), and `UpdatePendingBanner`, `PendingApprovalSection`, `DaemonHealthIndicator`,
  `NewSessionDialog` are mounted in only one shell.
- **F1.2 (Minor)** The canonical `TabId` type — the app's navigation model — is declared in
  `components/Dashboard.tsx`, a mobile page component, and imported *from* it by `DesktopShell`, `TabBar`,
  `tabDefs`. The nav model should not live in a shell.
- **F1.3 (Minor)** `HelpDrawer.NAV_MAP` and the `App.tsx` route comment describe the app's map by hand
  and are already stale (Accounts is a Settings section). The map should be derived from `tabDefs`.

---

## Q2 — Relationships, dependencies, overlaps

### Two shells, two state models

```
App.tsx
├─ isWide = useMediaQuery('(min-width:1024px)')
├─ true  → <DesktopShell/>          tab = useState<TabId>   selection = useState<Target>
└─ false → <Routes>…</Routes>       tab = URL path          session = /session/:id
```

`DesktopShell.tsx` imports **nothing** from `react-router`. On a wide screen the URL is ignored
except for `?section=` (read by `SettingsView` via `useSearchParams`). Consequences:

- `/pipeline`, `/archived`, `/session/:id`, `/review/:id` deep links work **only under 1024px**. On
  desktop they all land on Sessions → New Session.
- Browser Back does nothing for desktop tab or session changes.
- The `App.tsx` header comment "Routing is the source of truth for which view is shown" is true for
  one of the two shells. `PipelineTab.tsx` says the opposite ("ccswitch tabs are component-state driven
  (not URL/hash-routed)") — both comments are half right.
- **F2.1 (Critical)** Navigation state has two sources of truth selected by viewport width.

### Parity drift between shells (rendered-component diff of `Dashboard.tsx` vs `DesktopShell.tsx`)

| Present only on **mobile** | Present only on **desktop** |
|---|---|
| `PendingApprovalSection` (sessions awaiting approval) | `SidebarResizer` (+ `useSidebarWidth` persistence) |
| `DaemonHealthIndicator` | `WelcomePane` |
| `UpdatePendingBanner` + `useInputHold` (deploy input hold) | `ReviewView` rendered in-pane (mobile routes to `/review/:id`) |
| `useNeedsYouTitle` (document.title "needs you" badge) | `SessionRuntimeProvider` keyed remount of the **whole shell** |
| `usePageVisible`, `useSessionsViewing` (viewer presence) | |
| `DispatchBar hosts={enabledFleetHosts}` (fleet host picker) | |
| `NewSessionDialog` via `?dialog=new` | |

- **F2.2 (Critical)** Seven behaviors are mobile-only and four are desktop-only. The #423 "single
  source of truth" invariant test locks the *tab list*, not the region set — so drift is unguarded.
- **F2.3 (Major)** On desktop the `SessionRuntimeProvider` wraps the *entire shell* keyed by session id,
  so selecting a session remounts the header, tabs, sidebar and prompt. The `listScrollTop` ref +
  callback-ref restore in `DesktopShell` is a workaround for that remount. Provider scope should be the
  session region, not the shell.

### Duplicated region implementations (consolidation candidates)

| Role | Implementations | Consolidate into |
|---|---|---|
| Tab strip | `TabBar` (ARIA-correct, roving tabindex) · `SettingsView` inline tablist (no arrow keys, no roving tabindex) · `PipelineSwitcher` | one `Tabs` primitive; `TabBar` is the seed |
| Session header | `MainPaneSession` header · `SessionView` top bar | one `SessionHeader` with a `nav` slot (Back/chevrons on phone) |
| Bottom prompt host | `DesktopShell` `bottomPrompt` · `Dashboard` inline `DispatchBar` · `SessionView` inline composer | one `PromptDock` region |
| Modal/drawer | 13 × `fixed inset-0 … bg-black/50` + 10 hand-rolled `querySelectorAll` focus traps + `useFocusTrap` (1 use) | one `Dialog`/`Sheet` primitive on `useFocusTrap` (or `<dialog>`/`inert`) |
| Banners | `GlobalUpdateBanner`, `DaemonRestartBanner`, `GitCredentialBanner`, `AllLimitedBanner`, `UpdatePendingBanner`, `NewVersionBanner`, `DeployPanel` — each with its own colors/spacing and 3 mount points | one `Banner` primitive + one `BannerStack` region with a priority list |
| Help/Settings actions | desktop header icon buttons · mobile Sessions-panel `Button`s | app header "global actions" slot in all modes |
| Page toolbar | `PipelineTab` chrome bar · `SessionListControls` · `pm-projects-toolbar` (CSS) · `init-board-head` (CSS) | `PageHeader` with an `actions` slot |

---

## Q3 — Desktop vs kiosk vs mobile

### Responsive strategy today = one fork, not a continuum

- **One JS breakpoint** (`useMediaQuery('(min-width:1024px)')`) chooses between two component trees.
  There is no `sm`/`md`/`xl` tier: Tailwind responsive prefixes appear ~50 times in the whole app (10 of
  them in `CommandPalette`), and container queries only in `SessionCard` (`@container`, `@md:`).
- **Tablet is undefined.** 768–1023px portrait gets the phone layout (single column, full-screen session
  routes); 1024px+ landscape gets the desktop master-detail. A 10" tablet flips layout on rotation.
- Mobile ergonomics at the *leaf* level are genuinely good and heavily invested (safe-area insets, `dvh`,
  `interactive-widget=resizes-content`, swipe actions, 44px targets, Android keyboard handling — see
  `docs/product/features/web-dashboard-mobile.md`). The gap is at the *region* level: regions differ in
  kind (route vs pane), not in size.
- `min-h-[44px]` appears **105 times** and `min-w-[44px]` 20 times as arbitrary values. The touch-target
  floor is a policy with no token.
- **F3.1 (Major)** Responsiveness is a binary fork at 1024px with no defined tablet behavior and no
  fluid tier between phone and desktop.

### Kiosk is a pipeline feature, not an app mode

`pipeline/kiosk.ts` (ported verbatim from plotroom) reads `?kiosk=1&cycle=N` **once at mount** inside
`PipelineTab`. Effects: hides the pipeline chrome bar, makes `StatusStrip` tiles and `PrRow`s
non-interactive, auto-cycles the focused repo (`useKioskCycle`). Everything else still renders: the app
header, `TabBar`, the desktop sidebar and `SidebarResizer`, the bottom `DispatchBar`, banners, help key.
There is no wake-lock, no idle/attract state, no large-type scale, no auth/lock posture, and no way to
kiosk any other tab.

- **F3.2 (Major)** "Kiosk" is not a layout mode of the app; it's a read-only flag on one widget tree.
  If a wall display is a real target, it needs a shell-level mode with its own region visibility matrix.

### Theme

- `prefers-color-scheme` only; no user toggle; `color-scheme: light dark` at root.
- Dark mode is expressed as **1,065 `dark:` variant occurrences across 97 files** on raw palette
  literals (`text-gray-900 dark:text-gray-100`), not as semantic tokens. Every color decision is made
  twice at every call site.
- The mobile session header is hard-coded dark (`bg-gray-900 border-gray-700 text-gray-100`) in both
  themes; the desktop one is themed. **F3.3 (Minor)**.

### Test coverage of modes

- jsdom has no `matchMedia`, so **every** test defaults to the mobile branch unless it stubs
  `matchMedia`; desktop coverage lives in a handful of `Desktop*.test.tsx` files.
- Playwright has one project (`Desktop Chrome`). No phone, tablet or kiosk viewport project.
- **F3.4 (Major)** No mode is a first-class test dimension; regressions in one mode are invisible from
  the other.

---

## Q4 — Standards and conventions being violated

### Tokens: three palettes, none bridged into Tailwind

| Palette | Where | Defines | Reaches |
|---|---|---|---|
| A — scaffold vars | `index.css :root` | `--text --text-h --bg --border --code-bg` | global |
| B — plotroom vars | `plotroom.css .prdash-root` | `--bg --surface --text --muted --border --accent --done --fail --purple --amber --*-strong --z-*` | only inside `.prdash-root` (`PipelineTab`, `InitiativesTab`) |
| C — shadcn semantic utilities | `ui/button.tsx` + 200 call sites | `bg-primary text-muted-foreground border-border bg-background …` | **nowhere** — see below |

**F4.1 (Critical) — the shadcn semantic layer ships no CSS.** Tailwind v4 generates `bg-primary`,
`border-border`, etc. only if `--color-primary`, `--color-border` are declared in an `@theme` block.
`index.css` has no `@theme`; `tailwind.config.js` is a Tailwind-3 file that v4 ignores. Verified against
`web/dist/assets/index-*.css`: `.bg-blue-500` exists, **none** of `.bg-primary .bg-background
.text-foreground .text-muted-foreground .border-border .bg-muted .bg-accent .ring-ring .bg-destructive
.bg-card .border-input` exist. Live consequences:

- `Button` default variant (`bg-primary text-primary-foreground`) renders **with no background** —
  every "New", "Save", "Add" button using the default variant (≈20 sites: `EmptyState`, `AccountsManager`,
  `GeneralSection`, `NotificationsSection`, `HooksSection`, `AddAccountDialog` …) is a text-only button.
  The focus ring (`ring-ring`) is likewise undefined.
- `border-border` (35 uses) falls back to Tailwind v4's default border color — `currentColor` — so those
  edges draw in text color.
- `text-muted-foreground` (52 uses) applies nothing; the text inherits whatever is above it.
- `bg-background text-foreground` on every route root applies nothing; the app ground comes from
  `:root { background: var(--bg) }` by luck.
- The 45 occurrences of `border-[var(--border)]` and 11 of `bg-[var(--muted)]` are the workaround people
  reached for when the semantic classes didn't work — and `--muted` is palette B, so
  `bg-[var(--muted)]` in `TemplatesSection`/`PresetsSection`/`PermissionProfilesSection`/`SkillsSection`
  resolves to nothing outside the pipeline island. `--accent-foreground` (4 uses) is defined nowhere.

**F4.2 (Critical) — the Products page depends on tokens it cannot see.** `projects.css` uses
`--surface` (3), `--muted` (10), `--accent` (10), `--amber` (7) with the comment "come from the global
design-token root"; they are only defined on `.prdash-root`, and `ProjectsTab` is not wrapped in it.
`InitiativesTab` wraps itself in `.prdash-root` to borrow them — a page borrowing another page's theme.

**F4.3 (Major) — raw palette literals are the real color system.** Non-test occurrences: `gray` 1,473 ·
`blue` 313 · `red` 244 · `amber` 212 · `green`/`emerald` 87 · plus 51 hex literals in TSX and 91 in CSS.
There is no lint guard (KinDash has `kindash/no-hardcoded-colors`; `web/eslint.config.js` is the Vite
default).

### Parallel scales (constitution VI at the design layer)

| Concern | System 1 | System 2 | System 3 |
|---|---|---|---|
| z-index | Tailwind literals `z-10`(13) `z-20`(9) `z-50`(16) `z-30` `z-[60]` | `--z-raised … --z-command-palette` scale in `plotroom.css` (pipeline only) | |
| Type scale | Tailwind rem (`text-sm` 343, `text-xs` 313) | 17 distinct `font-size: Npx` literals in CSS (11px ×67, 12px ×62, 13px ×30, 10px ×25 …) | `text-[10px]`/`[11px]`/`[13px]` arbitrary |
| Radius | `rounded`(226) `-md`(72) `-lg`(29) `-xl`(10) `-full` | 11 distinct `border-radius: Npx` literals (6/8/10/4/999/3/5/2/9/7/12) | |
| Icons | `lucide-react` (28 files) | emoji glyphs (10 files: 🔔 🔕 ? ✓ …) | |
| Persistence keys | `ccswitch.*` (dot) | `ccswitch:*` (colon) | `prdash.*`, `workspace.*` |
| Focus trap | `useFocusTrap` hook | 10 inline `querySelectorAll('button, input…')` copies | |
| Empty state | `EmptyState` component (1 use) | 17 ad-hoc `<p>No … yet</p>` | |
| Motion | 6 `prefers-reduced-motion` sites | 31 `transition-*` + 24 `animate-*` with no shared policy | |

### Component library: scaffolded, not adopted

`class-variance-authority`, `tailwind-merge`, `clsx` and `components/ui/button.tsx` are the shadcn
footprint. `components/ui/` contains **one** file. Usage: `<Button>` 45 vs raw `<button>` 233; raw
`<input>` 75, `<select>` 31, `<textarea>` 16 with no `Input`/`Select` primitive; no `Dialog`, `Sheet`,
`Tabs`, `Card`, `Banner`, `Skeleton`. **F4.4 (Major)** — the dependency footprint says "shadcn", the code
says "hand-rolled everywhere". Either adopt the primitive set or remove the scaffold.

### Accessibility & web-platform conventions

- **WCAG 2.4.2 (page titled):** `document.title` is only touched by `useNeedsYouTitle` — mobile-only.
  No page sets a title. **F4.5 (Major)**
- **Heading structure:** the only `<h1>` is the brand word "ccswitch" (both shells). Pages have zero,
  one, or several `<h2>`s in three different styles (uppercase-xs gray, 16px bold CSS, none).
- **WAI-ARIA Tabs pattern:** `TabBar` implements it correctly; `SettingsView`'s tablist has
  `role="tab"` without roving tabindex, arrow-key movement, `aria-controls` or `tabpanel`s. Mobile
  `Dashboard` gives some tabs `role="tabpanel"` (sessions, archived, automations, initiatives,
  projects, settings) and not others (pipeline, insights).
- **Dialogs:** all `role="dialog" aria-modal` with hand-rolled traps; none use `inert` on the background
  or the native `<dialog>`; restore-focus behavior varies per file. `docs/ACCESSIBILITY.md` audits
  five leaf components and predates the desktop shell, Settings, Pipeline, Initiatives, Products.
- **URL as state** (deep-linkability, Back button, shareable links) — broken on desktop (F2.1).
- **Theme choice:** system-only; no explicit light/dark override (common expectation for a tool used on
  a phone at night and a bright wall display).
- **Project hygiene:** `web/README.md` is the untouched Vite template; there is no component catalog or
  visual regression surface; `tailwind.config.js` is dead (v3 shape under v4).

---

## Q5 — Page types and widget types

### De-facto page kinds (none has a written contract)

| Kind | Instances | Root wrapper today (desktop / mobile) | Heading |
|---|---|---|---|
| **List + detail (Sessions)** | `DesktopShell` sidebar+pane / `Dashboard` sessions panel | `<aside>`+`<main>` / `flex flex-1 min-h-0 flex-col gap-3` | none / uppercase-xs `h2` "Sessions" |
| **Tab page** | Pipeline, Automations, Initiatives, Products, Insights, Archived (as of 2026-09-05: a SCOPE of the Sessions list, not a Standard page — the reader below is still Reader) | `p-4 overflow-y-auto h-full` · `flex flex-col h-full` · `overflow-y-auto h-full` / `flex-1 min-h-0 overflow-y-auto pb-4` · `flex flex-col flex-1 min-h-0` | none · `h2.init-board-title` (16px CSS) · bare `<h2>Products</h2>` · uppercase-xs `h2` |
| **Split page (Settings)** | `SettingsView` + 14 sections | `max-w-3xl mx-auto w-full` (centered; the only page with a max-width) | sections use `h3 text-sm font-semibold` or none |
| **Workspace (session)** | `MainPaneSession` / `SessionView` | `flex flex-col h-full min-h-0` / `flex flex-col flex-1 min-h-0 bg-white dark:bg-gray-950` | title in header bar |
| **Reader (review)** | `ReviewView` (in-pane / routed) | `flex flex-col flex-1 min-h-0 bg-gray-50 dark:bg-gray-900` | — |
| **Sub-session** | `SubSessionView` (routed on mobile; desktop has no path to it) | | |

Every kind decides its own padding (`p-4`, `p-2`, `px-4`, `12px 14px`), its own scroll owner (wrapper vs
page vs inner list), its own heading, its own max-width, and its own loading/empty/error rendering.
**F5.1 (Major)**.

### Naming carries no meaning

Page roots use five suffixes with no semantic distinction: `*View` (10: some are pages, some are
transcript sub-views), `*Tab` (5), `*Manager` (2), `*Section` (14, but `QueuedSection`/
`PendingApprovalSection` are list groups, not settings sections), `*Panel` (10: some are pinned in-flow
panels, some are modals — `AdoptPanel` is a dialog). `components/` is a flat 46-file bucket while
`pipeline/`, `automations/`, `initiatives/`, `projects/` are feature folders. **F5.2 (Minor)**.

### Widgets

| Family | Instances | Shared base? | Notes |
|---|---|---|---|
| Card | `SessionCard` (761 lines, own `@container` system), `AutomationCard`, `QuestionCard`, `ToolCallCard`, `PermissionCard`, `ErrorCard`, `RecoveringCard`, `.init-card`, `.pm-project-card` | no | Nine card surfaces, nine border/radius/padding choices. |
| Banner | 6 `*Banner` + `DeployPanel` | no | Each picks its own color pair and dismiss affordance. |
| Panel (pinned) | 4 session panels + `LegendPanel` (a dialog) + `AdoptPanel` (a dialog) | no | |
| Section (settings) | 14 | no | Heading, spacing and save-button placement differ per section. |
| Dialog | 6 `*Dialog` + 4 inline in Manager/Dashboard files | no | |
| Pill / badge | `PrPill`, `UsageBadge`, `StatusPill` (in `UsageBadge.tsx`), tab count pill, `.init-status` | no | Status→color is centralized for *sessions* (`statusPresentation`) but not for PRs, accounts, initiatives. |
| Form controls | 75 inputs / 31 selects / 16 textareas | no | |
| Toast | `sonner` | ✅ | The one fully shared feedback channel. |

### What already works and should seed the standard

- `tabDefs.ts` + `TabBar` — a canonical nav model rendered by both shells.
- `session/statusTier.ts` `statusPresentation()` — status → tier → label/icon/color, consumed by list,
  strip and badge. This is the shape every status family (PR, account, initiative, deploy) should take.
- `lib/shortcuts.ts` — a shortcut registry that the `HelpDrawer` renders from (not a hand-copied list).
- `useSessionRuntime` / `SessionRuntimeProvider` / `SessionBody` — one runtime, one body, two chromes.
- `pipeline/hooks/useFocusTrap.ts` — exists, correct, used once.
- `SessionCard`'s `@container` approach — the right responsive primitive, applied to one component.

---

## Additional findings

- **F6.1 (Major) — CSS islands.** `plotroom.css` (1,216 lines) is a ported app with its own tokens,
  z-scale, keyframes, `.btn-ghost`, and `.app { max-width: min(1400px, 96vw) }` rules, scoped under
  `.prdash-root`. `initiatives.css` and `projects.css` inherit its idiom. Three of eight tabs are styled
  by a different design system than the other five.
- **F6.2 (Minor) — churn signal.** `Dashboard.tsx` 81 commits, `DesktopShell.tsx` 57, `SessionView.tsx`
  53, `SessionCard.tsx` 51. Shell-level features are implemented twice; the commit counts show it.
- **F6.3 (Minor) — `Dashboard.tsx` is 1,007 lines** and hosts two dialogs, the mobile shell, and the
  `TabId` type. `DesktopShell.tsx` is 678 lines with the same handler set re-implemented
  (`handleDispatch`, `handleClose`, `handleRename`, `confirmSessionDelete`, all-limited banner logic).
- **F6.4 (Minor)** No design decisions are recorded anywhere a future contributor would look: the
  layout plan lives in `docs/superpowers/plans/…master-detail…`, the a11y commitments in
  `docs/ACCESSIBILITY.md`, mobile in `docs/product/features/…`, tokens nowhere.

---

## Recommendations — direction for the design/spec phase

These are the constraints the target-state design should satisfy; the design itself is the next step.

1. **One shell, N layout modes.** Replace the `isWide ? <DesktopShell/> : <Routes/>` fork with a single
   `AppShell` whose regions are explicit slots, and a `LayoutMode ∈ {phone, tablet, desktop, kiosk}`
   derived from one `useViewport()` (width tier × `?kiosk` × pointer type). Each region declares a
   visibility/placement rule per mode. The router is the source of truth for tab, session, section and
   dialog in every mode.
2. **Name the regions** and give each a component or slot. Proposed vocabulary (stable ids usable in
   tests, docs and `data-region` attributes): `app.banners` · `app.header` (brand · primary-nav ·
   global-actions) · `app.rail` (session list) · `app.main` · `app.dock` (bottom prompt) ·
   `app.overlays` (sheet · dialog · palette · toast) · `page.header` · `page.subnav` · `page.content` ·
   `session.header` · `session.meta` · `session.transcript` · `session.pinned` · `session.composer`.
3. **Adopt a page-variant contract** in the spirit of KinDash's `PAGE-LAYOUT-STANDARD.md` (three-zone
   anatomy, variants, invariants, audit checklist): **Standard** (Automations, Archived, Insights,
   Products, Initiatives), **Split** (Settings), **Workspace** (Sessions, Pipeline), **Reader** (Review).
   As of 2026-09-05, Archived is a SCOPE of the Sessions list rather than its own Standard page (the
   nav section was deleted; the list column's scope switch replaces it) — its transcript reader stays
   classified as Reader, unchanged.
   Invariants: one `h1` per page, `document.title` sync, `page.header` renders before data, content owns
   scroll, URL-driven tabs, no page-level horizontal scroll.
4. **One token layer.** A single `@theme` in `index.css` declaring semantic colors (surface / ink /
   muted / edge / accent / status.*), z-layers, radii, the 44px target size, and the type scale; remap
   `.prdash-root` to consume the global tokens instead of redefining them; delete `tailwind.config.js`;
   add a lint rule against raw palette classes and hex literals.
5. **Primitive set before pages** (KinDash's "components ship before pages" rule): `Button`/`IconButton`,
   `Input`/`Select`/`Textarea`, `Dialog`/`Sheet` (one focus trap), `Tabs` (generalize `TabBar`),
   `Banner` + `BannerStack`, `Card`, `PageHeader`, `EmptyState`, `Skeleton`, `StatusPill` (generalize
   `statusPresentation`).
6. **Kiosk as a shell mode**, not a pipeline flag: hides `app.header`/`app.rail`/`app.dock`, larger
   type tier, wake-lock, attract cycle across any set of pages, read-only widgets via one `interactive`
   context — with the Pipeline wall display as the first consumer.
7. **Naming:** `*Page` for routed/tab roots, `*Section` reserved for Settings, `*Panel` for in-flow
   pinned panels only, `*Dialog`/`*Sheet` for overlays; move everything into feature folders.
8. **Sequence:** tokens (fixes F4.1/F4.2 immediately) → primitives → `AppShell` + regions → page
   migration one page at a time → delete `Dashboard`/`DesktopShell`.

## Open questions for the design phase

- Is **tablet** (768–1023) a target, and if so which layout does it get in each orientation?
- Is **kiosk** only the pipeline wall display, or should any page be kioskable?
- Should desktop adopt **URL routing** fully (recommended — it also unlocks multi-window)?
- **Left navigation vs top tabs:** you referred to a "left-navigation"; today the left column is the
  session rail. Is the target a left nav rail + session list, or top tabs kept?
- Should the **AI drawer** you mentioned be a new region (assistant side panel), and in which modes?
- **Theme toggle:** system-only, or explicit light/dark/auto?
