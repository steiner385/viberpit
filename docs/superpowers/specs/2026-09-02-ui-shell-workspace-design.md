# Sub-spec B — Shell & workspace: `AppShell`, regions, layout modes, routing, session workspace, kiosk, drawer

**Parent:** `2026-09-02-ui-target-state-design.md` (umbrella) · **Requires:** sub-spec A merged ·
**Delivers:** umbrella §8 PRs 3, 4, the shell half of 10, and 12 · **Closes:** audit F1.1, F1.2,
F1.3, F2.1, F2.2, F2.3, F3.1, F3.2, F3.3, F3.4, F4.5, F6.2, F6.3.

## 1. Scope

- PR 3: `AppShell` with every region, `useViewport`, `navDefs`, the route table, `BannerStack` and
  `DrawerHost` (help only), kiosk at shell level (chrome hiding + read-only + `--scale`; attract
  cycle and wake-lock come in PR 10). Hosts the **existing** page components unchanged. Deletes
  `Dashboard.tsx`, `DesktopShell.tsx`, `tabDefs.ts`, the `App.tsx` mode fork.
- PR 4: the Sessions workspace — `SessionHeader`, composer row, shortcuts registry, list filters,
  `PromptDock`.
- PR 10 (shell half): `useAttractCycle`, wake-lock, cursor hiding, Playwright viewport projects.
- PR 12: `AssistantSheet` against the assistant API (after the backend spec). **Landed as #1160**
  (`docs/product/features/ai-assistant.md`).

## 2. Files

```
web/src/shell/
  AppShell.tsx           layout per mode; renders regions; the only place regions mount
  regions/
    BannerStack.tsx      app.banners — registry-driven, priority-sorted
    NavRail.tsx          app.rail (tablet/desktop)
    NavBar.tsx           app.rail (phone, bottom)
    TopBar.tsx           app.rail companion (phone, brand + global actions)
    ContextList.tsx      app.list — hosts the section's list provider
    PageHost.tsx         app.main — <Outlet/> + SessionRuntimeProvider when a session is open
    PromptDock.tsx       app.dock
    DrawerHost.tsx       app.drawer
    Overlays.tsx         app.overlays portal root + Toaster
  navDefs.ts             the app map
  routes.tsx             route table + legacy redirects
  useViewport.ts         LayoutMode + tiers + pointer
  kiosk.ts               readKioskConfig (moved from pipeline/), KioskContext
  InteractionContext.tsx interactive: boolean (false in kiosk)
  useAttractCycle.ts     PR 10
  theme.tsx              from sub-spec A
  shortcuts.ts           moved from lib/, extended
web/src/features/sessions/
  SessionsPage.tsx       workspace page: welcome | SessionWorkspace
  SessionWorkspace.tsx   session.header/meta/transcript/pinned/panes
  SessionHeader.tsx
  SessionList*.tsx       moved from components/
  DispatchBar.tsx        moved; row per §6
  SessionComposer.tsx    moved; row per §6
```

## 3. `useViewport()`

```ts
type LayoutMode = 'phone' | 'tablet' | 'desktop' | 'kiosk'
interface Viewport { mode: LayoutMode; width: 'phone'|'tablet'|'desktop'|'wide'; coarsePointer: boolean; kiosk: KioskConfig | null }
```

- Width tiers from `matchMedia('(min-width: 768px)')`, `(min-width: 1024px)`, `(min-width: 1440px)`;
  `coarsePointer` from `(pointer: coarse)`. jsdom (no `matchMedia`) → `phone`, `coarsePointer: false`.
- `kiosk` = `readKioskConfig(location.search)` once at mount; when `kiosk.enabled`, `mode = 'kiosk'`
  regardless of width, and `width` still reports the tier (kiosk layouts use it for column counts).
- Exposed via `ViewportContext`; the hook throws outside `AppShell` so no component can subscribe to
  `matchMedia` on its own.

## 4. `AppShell` and regions

### 4.1 Layout per mode

```
desktop:  [banners]
          [rail][list?][main            ][drawer?]      grid: auto auto 1fr auto
          [dock?                                 ]
tablet:   [banners]
          [rail][list | main            ]                list XOR main
          [dock?                        ]
phone:    [banners][topbar]
          [list | main                  ]
          [dock?]
          [navbar]
kiosk:    [banners(danger)]
          [main                                  ]
```

`list?` renders when `navDefs[current].hasList` and (desktop) always, (tablet/phone) only when no item
is open. `dock?` renders when the current section is `sessions` (D10). `drawer?` renders when
`?drawer=` is set; at `width==='wide'||'desktop'&&≥1280` it is a grid column (`mode: push`), otherwise
`Sheet mode=overlay`.

### 4.2 Region contracts

- **`BannerStack`** — a `bannerRegistry` (`registerBanner({ id, priority, when: () => boolean, render })`)
  populated by the existing banner hooks (`useVersionWatch`, `useEpochRestart`, `useCredentialAlerts`,
  `useUpdatePending`, `useInputHold`, `useDaemonHealth`, all-limited). Renders sorted; kiosk filters
  to `tone==='danger'`. Exactly one mount. `UpdatePendingBanner`/`useInputHold` (mobile-only today)
  become mode-independent.
- **`NavRail`** — items from `navDefs`; badges from a `useNavBadges()` hook that unifies the counts
  `buildTabs` computed (sessions, pipeline in-flight, automations active, archived count, settings
  attention). Bottom cluster: **New session** (navigates to `/sessions`, focuses the dispatcher),
  **Assistant** (`?drawer=assistant`), **Help** (`?drawer=help`), **Theme** (`Menu` system/light/dark),
  **Settings** (`/settings/general`). `≥1440`: a persisted expand/collapse toggle shows labels.
  `role="navigation"`, `aria-current="page"` on the active item.
- **`NavBar`** (phone) — the first four `navDefs` items + **More** (a `Sheet` listing the rest and the
  global actions). `TopBar` carries brand + **Assistant**/**Help** icons; the page title is the
  page's own `h1`.
- **`ContextList`** — renders the list provider for the current section. Sessions supplies
  `SessionListColumn` (New Session slot, `SessionListControls`, `QueuedSection`,
  `PendingApprovalSection`, `SessionList`, `AdoptPanel`, `DaemonHealthIndicator`). Resizable on
  desktop via `SidebarResizer` (kept; width persisted at `ccswitch.listWidth`, migrating
  `ccswitch.sidebarWidth`). Scroll position survives route changes because `ContextList` never
  remounts on `:id` change.
- **`PageHost`** — `<Outlet/>`; when the route has `:id` under `/sessions`, wraps the outlet **and**
  `PromptDock` in `SessionRuntimeProvider key={id}` (scope fix for F2.3). `useNeedsYouTitle` moves
  here so every mode gets the "needs you" title prefix.
- **`PromptDock`** — `DispatchBar` when no session is open, `SessionComposer` (from the runtime
  context) when one is. Safe-area bottom padding on phone; hidden in kiosk.
- **`DrawerHost`** — one `Sheet`; content by `?drawer=`: `help` → `HelpSheet` (today's `HelpDrawer`
  content rendered from `shortcuts.ts` + `navDefs`), `assistant` → `AssistantSheet` (PR 12, landed
  as #1160). Persisted width at
  `ccswitch.drawerWidth`. The drawer element stays mounted while `?drawer=` is set across route
  changes; `AppShell` keeps the param when navigating sections.
- **`Overlays`** — the portal target for `Dialog`/`Sheet`/`Menu`, plus `Toaster`, `DeployOverlay`,
  `CommandResultOverlay`. In kiosk, `KioskContext` makes `Dialog.open` a no-op that logs.

### 4.3 `navDefs.ts`

```ts
export interface NavDef { id: NavId; label: string; icon: LucideIcon; path: string; badge?: 'sessions'|'pipeline'|'automations'|'archived'|'settings'; showInKiosk: boolean; hasList: boolean }
export const NAV_DEFS: NavDef[] = [
  sessions('Sessions', '/sessions', hasList) · pipeline · automations · initiatives · projects('Products') · archived · insights · settings(showInKiosk:false)
]
```

`TabId` is renamed `NavId` and lives here; `Dashboard.tsx` no longer exists to host it.

### 4.4 Routing (`routes.tsx`)

Exactly the umbrella §6 table, built with `createBrowserRouter`; `AppShell` is the layout route.
Overlay params use `setSearchParams(…, { replace: true })`. Legacy paths redirect with `replace`.
`nextSessionAfterClose` (kept) feeds `navigate(next ? /sessions/${next} : '/sessions', { replace })`.

## 5. Kiosk (PR 3 base, PR 10 completion)

- `readKioskConfig` gains `pages: NavId[]` (validated against `showInKiosk`; default `['pipeline']`),
  `cycle` (min 10 s, default 30), `theme?`.
- PR 3: `mode==='kiosk'` hides `app.rail/list/dock/drawer`; `InteractionContext.interactive=false`;
  `--scale: 1.25` on `<html>`; `page.header` actions hidden; `Dialog` refused; banners `danger` only.
  `StatusStrip.interactive` and `PrRow.expandable` read `InteractionContext` instead of a `kiosk`
  prop; `useKioskCycle` (per-repo rotation) stays inside the Pipeline page.
- PR 10: `useAttractCycle(pages, cycle)` navigates through `pages` on an interval, paused while
  `document.hidden`; `navigator.wakeLock.request('screen')` on mount, re-requested on
  `visibilitychange`; cursor hidden after 5 s idle (`[data-idle] { cursor: none }`).
- `InteractionContext` semantics: `Button`/`IconButton`/`Card interactive` render with
  `aria-disabled` and no handler; list rows lose expand; pinned action cards render their label with a
  "Needs you — answer on your phone" note and no buttons.

## 6. Sessions workspace (PR 4)

> ### 6.0 Amendments — 2026-09-04
>
> §6 was written before PRs 3 and 4 existed. Implementing it found that parts of it
> describe code and data that do not exist, and PR 3 had already done some of what §6.4
> asks. These amendments are **binding**: where they conflict with the text below, they
> win. Each was decided with the human on 2026-09-04.
>
> **A1 — §6.4's `Ctrl+Tab` / `Cmd+Shift+[`/`]` cannot be implemented. Next/previous
> session binds `Alt+↑` / `Alt+↓` instead.**
> Chrome and Safari do not dispatch keydown at all for chords their own UI reserves
> (`⌘N`, `⌘W`, `Ctrl+Tab`, `Ctrl+Shift+Tab`, and — on macOS — `⌘⇧[`/`⌘⇧]`, which are the
> browsers' own tab switching). There is no event to `preventDefault`. §6.4's table comes
> from Claude Code **Desktop**, an Electron app that owns its window; a web page does not.
> `Alt+↑`/`Alt+↓` is unreserved and mirrors the list's existing arrow navigation. It is a
> **ccswitch binding, not Desktop parity**, and the help sheet should not imply otherwise.
> Ordering must come from `ordering.orderedSessions` — the *filtered* list — so the
> shortcut can never jump to a session the list is not showing.
> **Verification note:** CDP-synthesised keys bypass the browser-process handling at
> issue, so devtools automation reports a **false pass** on reserved chords. Any future
> binding needs a human at a real Chrome and Safari.
>
> **A2 — §6.2 splits. The diff chip is in scope; the CI bar and its toggles are deferred.**
> `server/github/pr_status.py`'s `PullRequestInfo` is already populated from a GraphQL
> query that fetches `mergeable`, `checks`, `mergeCommit` and more — and GitHub's PR
> object exposes `additions`/`deletions` on that same query. The diff chip is therefore a
> two-field addition, not new infrastructure.
> The rest of §6.2 is **not** implementable as written: `Session.prs[].checks` is a rollup
> *string*; the per-check types in `web/src/api/types.ts` (`CheckRow`, `CheckGroup`,
> `check_groups`, every `eta_*`) are **declared but dead**, zero consumers; real per-check
> data lives only in the vendored pipeline's SSE feed; and **there is no auto-merge boolean
> anywhere** to bind a toggle to. `autopilotPaused` / `autopilotCooldownUntil` /
> `assistedBy` are not auto-fix/auto-merge flags. The CI bar needs its own API spec.
>
> **A3 — §6.3's five permission-mode labels are withdrawn. ccswitch keeps its six.**
> `session/permissionModes.ts` defines six operationally distinct values — `interactive`,
> `auto`, `allowlist`, `acceptEdits`, `full-auto`, `bypassPermissions` — and **no `plan`
> key**; plan is a separate binary `role="switch"` in `SessionMeta`. Desktop's five are a
> *different vocabulary*, not a relabel: mapping onto them would require merging or
> dropping `allowlist` and `full-auto`, which changes what an existing session's **stored**
> mode means. `PERMISSION_MODES` stays the single source of truth, labels may be shortened
> for the menu, no value changes, and the plan toggle stays where it is.
> This is a deliberate departure from umbrella §2.2 ("defer to Desktop"): the rule assumes
> both products have an opinion about the *same* thing, and here they do not.
>
> **Still deferred, unchanged:** §6.3's `Effort` menu and `Cmd+Shift+E` (no effort field
> exists on `SessionRuntime`, `ModelOption` or `Session`), and §6.3's transcript-view
> control (`defaultToolDetail`/`thinkingCollapsed` are server-owned app settings, so a
> `ccswitch.transcriptView` key would be a second owner of "how much detail", and
> "Summary" has no third `ToolDetailMode` to map to). §6.1's Views menu and `session.panes`
> defer with them — Tasks and PR panes do not exist.
>
> **Delivered in PR 4** (#1022): §6.4's registry (one `keydown` listener replacing five),
> §6.5's filters, and §6.1's click-to-rename and `StatusPill`. §6.4's delete list was
> already stale — PR 3 collapsed the three detach handlers into one and removed
> `Shift+Tab` (D8) in commit `9b64fff8`.

### 6.1 `SessionHeader` (`session.header`; replaces `MainPaneSession` header and `SessionView` top bar)

`[Back (phone/tablet)] [‹ ›] [title — click to rename] [StatusPill] [Views ▾] [SessionActions ⋮]`
— themed on tokens in every mode (fixes the always-dark phone header). `Views` is a `Menu` of
**Browser** (VNC, when X11 available), **Tasks** (sub-agents + background), **PR** (checks/queue);
choosing one opens it as a pane in `session.panes` (a `Tabs`-driven second pane below the transcript
on phone/tablet, beside it on desktop ≥1280). Deep link `/sessions/:id/agents/:agentId` opens
Tasks focused.

### 6.2 `session.meta`

Existing strip, plus a **diff chip** (`+N −M` from the session's PR/insight data when present) and the
**CI status bar** (checks summary, queue position, **Auto-fix** / **Auto-merge** toggles bound to the
existing autopilot flags) replacing the ad-hoc PR indicators.

### 6.3 Composer row (`SessionComposer`, `DispatchBar`)

```
[+ ▾] [textarea …………………………………………………] [Transcript view ▾] [Model ▾] [Mode ▾] [◔ usage] [Send | Stop]
```

- `+` menu: Attach files, Slash commands (opens `CommandPalette`), Presets/Templates.
- `Model`, `Mode`, `Transcript view` are `Menu`s with `1–9` selection; `Mode` items are
  **Manual / Accept edits / Plan / Auto / Bypass permissions** (labels only; keys unchanged in
  `PERMISSION_MODES`); hidden items when the account/model doesn't support them.
- `Transcript view`: **Normal / Verbose / Summary** — maps to the transcript's existing block
  visibility (Normal collapses tool calls; Verbose expands; Summary shows assistant text + diff chips
  only). Persisted at `ccswitch.transcriptView`. `Ctrl+O` cycles.
- `UsageRing` shows context usage; click opens a popover with plan usage (from `useConfig`/accounts).
- `DispatchBar` prepends **Environment** (fleet host), **Repository**, **Account** menus — the
  Desktop pre-flight — and keeps its list-navigation affordances.
- Sending while a turn runs queues the message (existing behavior) — Desktop's "correction without
  stopping".

### 6.4 Shortcuts (`shell/shortcuts.ts`)

Single registry `{ id, keys, mac/other display, description, group, when, run }`; `useShortcuts()`
installs one `keydown` listener in `AppShell`; every in-component listener today
(`useGlobalHelpKey`, `useGlobalListNav`, the three detach handlers, `SessionView`'s Shift+Tab) is
deleted and re-expressed as entries. Bindings (Desktop parity):

| Keys | Action | When |
|---|---|---|
| `Cmd+/` | Help sheet | always |
| `Cmd+N` | New session (→ `/sessions`, focus dispatcher) | not kiosk |
| `Cmd+W` | Close/detach the open session (→ list) | session open |
| `Ctrl+Tab` / `Ctrl+Shift+Tab`, `Cmd+Shift+]` / `[` | next / previous session | session open |
| `Esc` | **Stop the running turn** (interrupt); closes an open menu/sheet first | session open |
| `Cmd+Shift+M` / `I` / `E` | Mode / Model / Effort menus | composer mounted |
| `1–9` | pick item in an open `Menu` | menu open |
| `Ctrl+O` | cycle Transcript view | session open |
| `Cmd+;` | Assistant drawer | not kiosk |
| `←` at the composer's left edge | detach to list (kept; not in Desktop, not contradicted) | session open |
| list-mode keys (`Tab`, `↑↓`, `c r a d`, group `←→`) | unchanged | dispatcher focused |

`Shift+Tab` mode cycling is **removed** (D8). `detachKeyYieldsToTextEntry` stays for `←`.

### 6.5 List filters (`SessionListControls`)

Filters: **Status** (tier), **Project** (repo), **Environment** (host); **Group by** gains
**Project**; search stays. Persisted at `ccswitch.listFilters`. Hover on a row reveals the archive
`IconButton` (desktop); swipe actions unchanged on touch.

## 7. Assistant drawer API (landed, PR 12 = #1160; backend: `2026-09-04-ai-assistant-design.md`)

`AssistantSheet` expects: `POST /api/assistant/session` → `{ id }` (idempotent per user; returns the
existing assistant session), and the normal session WebSocket for `id`. It renders `TranscriptView`
+ `SessionComposer` from the runtime, with a context chip `{ route, sessionId? }` sent as a hidden
prefix on each prompt. See `docs/product/features/ai-assistant.md` for the shipped feature (state:
live) and its full PR list.

## 8. Tests

| Test | Asserts |
|---|---|
| `AppShell.test.tsx` | per mode: each `data-region` present once/absent per umbrella §4.2; regions never mount elsewhere (`document.querySelectorAll('[data-region]')` count) |
| `visibilityMatrix.test.tsx` | table-driven from a `REGION_VISIBILITY` constant exported by `AppShell` (the spec table is the fixture) |
| `routes.test.tsx` | every umbrella §6 route at 390 / 820 / 1440 / kiosk renders the same page; legacy redirects |
| `useViewport.test.ts` | tiers, kiosk override, jsdom default |
| `shortcuts.test.ts` | every entry has `run`; help sheet lists all; `Esc` stops, does not detach; no `Shift+Tab` binding |
| `SessionWorkspace.test.tsx` | header themed per mode; Views menu; deep-link opens Tasks; provider scoping (list does not remount on `:id` change) |
| `kiosk.test.tsx` | chrome hidden; `Dialog` refused; primitives inert; attract cycle navigates and pauses when hidden |
| Existing `Desktop*.test.tsx`, `App.test.tsx`, `SessionView.test.tsx` | deleted; each behavior re-pinned in the tests above |
| Playwright | projects `desktop-chrome`, `tablet`, `phone`, `kiosk`; smoke per route + open session + open drawer |

## 9. Risks

- **PR 3 is the largest change** (two shells → one). Mitigation: it hosts existing pages unchanged,
  and every behavior in the deleted tests is enumerated in the PR body against its new test.
- **`Esc` semantics** change user habit; the help sheet and release note call it out.
- **Wake-lock** needs a secure context; served via Tailscale HTTPS already.
