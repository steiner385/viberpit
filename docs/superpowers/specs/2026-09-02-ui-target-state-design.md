# ccswitch Web UI — Target-State Design (umbrella spec)

**Status:** approved design, 2026-09-02 · **Feeds:** three sub-specs (A/B/C below), each with its own
implementation plan · **Supersedes:** the layout described in
`docs/superpowers/plans/2026-06-21-master-detail-sessions-layout.md` · **Evidence:**
`docs/audits/2026-09-02-ui-framework-audit.md` (the audit whose findings F1.1–F6.4 this design resolves).

This document is the **vocabulary and the decisions**. It names every region, layout mode, page
variant and token family once, so the sub-specs and their plans cannot drift. Detailed component
contracts live in the sub-specs:

| Sub-spec | Scope | File |
|---|---|---|
| **A — Foundation** | `@theme` token layer, primitives, lint guard, theme provider | `2026-09-02-ui-foundation-tokens-primitives-design.md` |
| **B — Shell & workspace** | `AppShell`, regions, layout modes, routing, session workspace, kiosk, drawer host | `2026-09-02-ui-shell-workspace-design.md` |
| **C — Page migration** | page variants, per-page migration, palette sweep, docs | `2026-09-02-ui-page-migration-design.md` |

The AI assistant's **backend** (tool surface, model/account routing, permission posture) is a
separate spec — `2026-09-04-ai-assistant-design.md` (research basis:
`2026-09-04-ai-assistant-research.md`; plan: `docs/superpowers/plans/2026-09-04-ai-assistant.md`);
sub-spec B defines only the drawer and the API shape it expects.

---

## 1. Goals and non-goals

**Goals**

1. One app shell with named regions whose placement and visibility are a per-mode table, not a fact
   about which of two shells someone edited.
2. The router is the single source of navigation truth in every layout mode.
3. One token layer that Tailwind actually compiles, with dark mode expressed once.
4. A page-variant contract every routed page conforms to, enforced by tests.
5. Kiosk as a first-class read-only layout mode for any section.
6. Mimic Claude Code Desktop's Code tab wherever both products have an opinion.

**Non-goals (this design)**

- The assistant backend; drag-and-drop pane grids; an integrated terminal or file editor; side chats;
  Cmd+click split view; a public design-system package; visual-regression screenshots.
- Changing any server API except the assistant endpoint shape reserved in sub-spec B.

## 2. Governing principles

1. **Constitution VI at the structural layer.** One shell, one nav model, one token layer, one
   z-scale, one type scale, one focus trap, one banner stack. Replaced code is deleted in the same PR.
2. **Defer to Claude Code Desktop.** Where ccswitch and the Claude Code Desktop *Code* tab both have
   an opinion (session list, session toolbar, prompt box, mode names, shortcuts, permission cards),
   Desktop's choice wins — strictly, including `Esc` = stop and no `Shift+Tab` mode cycling. Where
   Desktop has no opinion (fleet sections, accounts, kiosk, pipeline), ccswitch decides. Source of
   truth: https://code.claude.com/docs/en/desktop (verified 2026-09-02).
3. **Foundation before pages.** Tokens and primitives ship before the shell; the shell ships before
   any page is migrated; a page migrates whole or not at all.
4. **URL is state.** Section, session, sub-agent, settings section, dialog, drawer and kiosk are all
   in the URL. Per-browser preferences (filters, widths, theme, view mode) are the only client state
   that survives navigation and they live under one `localStorage` namespace, `ccswitch.`.
5. **Verify against the real artifact.** Every "the CSS exists" claim is a build-output test, not a
   code-reading claim (constitution III).

## 3. Decisions log (from the 2026-09-02 design session)

| # | Question | Decision |
|---|---|---|
| D1 | App frame | **Left nav rail** for sections + a **contextual second column** (session list inside Sessions). No persistent app header on tablet/desktop; brand + global actions live in the rail. Phone: rail becomes a bottom nav bar; slim top bar for brand + actions. |
| D2 | "AI drawer" | A right-side **meta-assistant for ccswitch itself** (fleet questions, PR diagnosis, "start a session on X"), reachable from every section. Frontend + API shape here; backend: `2026-09-04-ai-assistant-design.md` (live, see `docs/product/features/ai-assistant.md`). |
| D3 | Kiosk | **App-level read-only mode for any section**, URL-entered, chrome hidden, larger type, wake-lock, optional attract cycle over a URL-given page list. |
| D4 | Tablet (768–1023) | **Own mode:** icon-only rail + one column (session replaces list), dock stays. Desktop from 1024. |
| D5 | Theme | **System + explicit light/dark override**, per browser, toggle in rail and Settings → General; kiosk pins via URL. |
| D6 | Desktop routing | Router is the source of truth in every mode (was: `useState` on desktop). |
| D7 | Reference UX | **Claude Code Desktop app, Code tab** (not the TUI, not claude.ai/code). |
| D8 | Shortcuts | Strict Desktop parity: `Esc` stops the turn; `Cmd+W` / Back detaches; **no `Shift+Tab`** alias; mode via selector / `Cmd+Shift+M`. |
| D9 | Migration | **Strategy A — foundation-first strangler** (tokens → primitives → shell hosting old pages → one page per PR). Never more than one shell for longer than one PR. |
| D10 | Dock scope | `app.dock` appears **only in the Sessions section**; "New session" in the rail jumps there. |
| D11 | Settings | **Split variant** with a vertical `SettingsNav`; sections are path segments `/settings/:section`. |
| D12 | Folders | Reorganize into `web/src/{ui,shell,features/*}`; the flat `components/` bucket empties as pages migrate. |
| D13 | Drawer placement | Right `Sheet`; pushes `app.main` at ≥1280px, overlays below; full-height sheet on phone/tablet. |
| D14 | Specs | One umbrella (this) + sub-specs A, B, C. |

## 4. Vocabulary

### 4.1 Layout modes

`LayoutMode = 'phone' | 'tablet' | 'desktop' | 'kiosk'`, derived by **one** hook, `useViewport()`
(`web/src/shell/useViewport.ts`), from `matchMedia` width tiers and the `?kiosk=1` URL flag, read once
at mount for kiosk. No other module reads `matchMedia` for layout.

| Mode | Trigger | Summary |
|---|---|---|
| `phone` | width `< 768px` | bottom nav bar; list and session alternate full-screen |
| `tablet` | `768–1023px` | icon-only rail; single column; dock present |
| `desktop` | `≥ 1024px` (`≥ 1440px`: rail may expand to labels; `≥ 1280px`: drawer pushes) | rail + list + main side by side |
| `kiosk` | `?kiosk=1` at any width | chrome hidden, read-only, `--scale: 1.25`, attract cycle |

### 4.2 Regions

A region is a component rendered **only by `AppShell`**, carrying `data-region="<id>"`. The shell
decides placement and visibility per mode; the region decides its content.

| Region id | Component | phone | tablet | desktop | kiosk |
|---|---|---|---|---|---|
| `app.banners` | `BannerStack` | top | top | top | `danger` tone only |
| `app.rail` | `NavRail` (desktop/tablet) · `NavBar` (phone) · `TopBar` (phone, brand + actions) | bottom bar + slim top bar | icon-only, left | icon-only / expandable, left | hidden |
| `app.list` | `ContextList` (the section's list column; Sessions provides one) | full screen when no item open | full screen when no item open | left of main, resizable | hidden |
| `app.main` | `PageHost` (routed page) | full | full | flex-1 | full |
| `app.dock` | `PromptDock` (`DispatchBar` or `SessionComposer`) | bottom, Sessions only | same | same | hidden |
| `app.drawer` | `DrawerHost` (`assistant` \| `help`) | full-height sheet | sheet | right panel; pushes ≥1280, overlays below | hidden |
| `app.overlays` | portal root: dialogs, menus, command palette, command result, deploy overlay, toaster | — | — | — | dialogs refused |

Page-level regions inside `app.main`: `page.header` · `page.subnav` (optional) · `page.content`.
Session-workspace regions inside a Sessions page: `session.header` · `session.meta` ·
`session.transcript` · `session.pinned` · `session.panes` (Browser / Tasks / PR). The composer is
`app.dock`, not a page region.

### 4.3 Page variants

`data-page-variant = 'standard' | 'split' | 'workspace' | 'reader'` — see sub-spec C §2.

| Variant | Pages |
|---|---|
| Standard | Automations, Initiatives, Products, Insights, Archived |
| Split | Settings |
| Workspace | Sessions, Pipeline |
| Reader | Archived review, sub-agent view |

### 4.4 Token families (names are the contract; values in sub-spec A)

- **Surfaces & ink:** `canvas`, `surface`, `surface-raised`, `edge`, `edge-strong`, `ink`,
  `ink-muted`, `ink-faint`, `accent`, `accent-ink`, `accent-wash`.
- **Status:** `success`, `warning`, `danger`, `info`, `neutral`, each with `-wash` and `-strong`.
  Session tiers map onto them: `busy→success`, `needs-you→warning`, `error→danger`,
  `waiting→info`, `your-turn→neutral`.
- **Layers:** `z-raised 1` · `z-sticky 20` · `z-nav 25` · `z-popover 30` · `z-overlay 40` ·
  `z-drawer 50` · `z-dialog 60` · `z-palette 1000` · `z-toast 1100`.
- **Metrics:** `spacing-target 44px`; radii `sm 4` · `md 6` · `lg 10` · `pill 999`; one type scale
  `2xs…2xl` multiplied by `--scale` (1 default, 1.25 kiosk).

### 4.5 Naming

`*Page` routed roots · `*Section` Settings only · `*Panel` pinned in-flow panels only ·
`*Dialog` / `*Sheet` overlays · `*Card` / `*Row` widgets · `*Defs` registries (`navDefs`,
`settingsDefs`, `shortcuts`). Folders: `web/src/ui/` primitives · `web/src/shell/` shell, regions,
routing, viewport, kiosk, theme · `web/src/features/<feature>/` pages and widgets.

## 5. Claude Code Desktop alignment

| Desktop convention (verified) | ccswitch target | Sub-spec |
|---|---|---|
| Sidebar: **+ New session** top; filter by status / project / environment; group by project; hover archive; **Dispatch** badge | `ContextList` for Sessions gains status/project/environment filters + group-by-project; queue position and fleet host shown as badges; hover archive | B |
| Session toolbar: title click-to-rename · **Views** menu | `session.header`: title (click-to-rename) · `StatusPill` · Views menu with **Browser**, **Tasks**, **PR** | B |
| Prompt box: `+` menu · `@` mention · model + **permission-mode selector next to Send** · **Transcript view** (Normal / Verbose / Summary) · usage ring · Stop; mid-run corrections don't interrupt | `SessionComposer` row: `+` (attachments, slash commands, presets) · model · mode · view-mode · `UsageRing` · Send/Stop. `DispatchBar` adds environment (host) · repo · account, mirroring Desktop's pre-flight | B |
| Mode labels **Manual / Accept edits / Plan / Auto / Bypass permissions** | `PERMISSION_MODES` relabeled; keys unchanged | B |
| `Cmd+N` new · `Cmd+W` close · `Ctrl+Tab` cycle · `Esc` stop · `Cmd+Shift+M/I/E` menus · `1–9` pick · `Cmd+/` shortcuts · `Ctrl+O` view modes · `Cmd+;` side chat | `shortcuts.ts` is the sole registry; `Cmd+;` opens the assistant drawer; `Shift+Tab` removed | B |
| Inline permission / question cards with Allow once / Always allow / Deny | `PermissionCard`, `QuestionCard` restyled on primitives | C |
| Diff chip `+12 −1`; CI status bar with **Auto-fix / Auto-merge** | `session.meta` diff chip; CI bar replaces ad-hoc PR indicators; toggles wire to existing autopilot flags | B |
| Cross-session "check on / message / archive"; task chips spawn sessions | Assistant drawer (app level); suggestion chips call `DispatchBar` | B |
| OS notification when a non-viewed session finishes | existing push notifications, unchanged | — |

Not inherited now: pane grid, terminal, file editor, side chat, Cmd+click split. Reserved as future
`session.panes` entries.

## 6. Routing (single source of truth)

| Route | Shows |
|---|---|
| `/sessions` | list (+ welcome pane on desktop) |
| `/sessions/:id` | session workspace; list highlights `:id` |
| `/sessions/:id/agents/:agentId` | Tasks pane focused on the sub-agent |
| `/pipeline` · `/automations` · `/initiatives` · `/projects` · `/insights` | section pages |
| `/archived` · `/archived/:id` | archive list · read-only review |
| `/settings/:section` | Settings split page |
| `?dialog=new` · `?drawer=assistant\|help` | overlays (`replace`, not `push`) |
| `?kiosk=1&pages=a,b&cycle=N&theme=light\|dark` | kiosk mode |

Redirects kept: `/session/:id`, `/session/:id/subagent/:a`, `/review/:id`, `/settings?section=`,
`/library`, `/accounts`, `/automation`, `?dialog=settings`.

`navDefs.ts` (`{ id, label, icon, path, badge, showInKiosk, hasList }`) is the one app map;
`NavRail`, `NavBar`, the kiosk attract cycle, `HelpDrawer` and the route table derive from it.

## 7. Guardrails (each lands with the PR that introduces its subject)

| Guard | Mechanism |
|---|---|
| No raw palette / `dark:` / hex in components | ESLint rule over class strings; allowlist for chart code; changed-files scope from PR 1, repo-wide after the sweep |
| One mount per region | `AppShell.test.tsx`: each `data-region` appears once or not at all, per mode |
| Visibility matrix | table-driven test; §4.2 is the fixture |
| Page contract | every `*Page` × mode: one `h1`, `document.title`, `data-page-variant`, header renders while loading, `scrollWidth ≤ clientWidth` |
| Token bridge | build-output test asserts `.bg-surface`, `.z-drawer`, `.min-h-target` exist in compiled CSS |
| Shortcuts | registry entries all have handlers; `HelpDrawer` lists all |
| Kiosk read-only | every primitive with `onClick` is inert under `interactive=false` |
| a11y | `jest-axe` on every primitive and every page per mode |
| E2E | Playwright projects `desktop-chrome` (1440) · `tablet` · `phone` · `kiosk` |

## 8. Sequence (strategy A) and ownership

| # | PR | Sub-spec | Deletes |
|---|---|---|---|
| 1 | `@theme` + `.prdash-root` remap + ESLint (changed files) + `ThemeProvider` | A | `tailwind.config.js`, `components/ui/button.tsx` (moved) |
| 2 | Primitives in `web/src/ui/`; overlays and banners adopt `Dialog`/`Sheet`/`Tabs`/`Banner` | A | 10 inline focus traps |
| 3 | `navDefs`, `useViewport`, `AppShell` + regions + `BannerStack` + `DrawerHost` (help) + route table; kiosk at shell level; hosts existing pages | B | `Dashboard.tsx`, `DesktopShell.tsx`, `tabDefs.ts`, the `App.tsx` mode fork |
| 4 | Sessions workspace: `SessionHeader`, composer row, shortcuts, list filters, `PromptDock` | B | `SessionView` top bar, `MainPaneSession` |
| 5–9 | Settings (Split) · Automations · Archived + Reader · Insights · Products + Initiatives | C | each page's feature CSS and ad-hoc headers |
| 10 | Pipeline as Workspace; attract cycle + wake-lock + `--scale`; Playwright viewport projects | B/C | `pipeline/kiosk.ts`, `useKioskCycle` (moved) |
| 11 | Repo-wide palette sweep; ESLint to full scope; rewrite `docs/ACCESSIBILITY.md`, `web/README.md` | C | remaining `dark:` / raw palette |
| 12 | `AssistantSheet` UI against the assistant API | B (after backend spec) | "coming soon" sheet |

## 9. Success criteria

- `grep -c 'dark:' web/src` → 0 outside `index.css`; `grep -cE 'bg-(gray|blue|amber|red)-' web/src` → 0
  outside the chart allowlist.
- Exactly one of `Dashboard.tsx` / `DesktopShell.tsx` / `AppShell.tsx` exists: `AppShell.tsx`.
- Every route in §6 renders the same page at 390, 820, 1440 and `?kiosk=1`, verified by Playwright.
- `web/dist` CSS contains every token utility the app uses (build-output test green).
- The audit's F1.1, F2.1, F2.2, F2.3, F3.1–F3.4, F4.1–F4.5, F5.1, F5.2, F6.1 are closed by the PR
  named in §8; each PR body cites the finding ids it closes.
