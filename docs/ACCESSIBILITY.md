# Accessibility (WCAG 2.1 AA)

## 1. Scope

Every page and every shared primitive targets **WCAG 2.1 Level AA** in full. There is no
partial-conformance surface: the terminal exception this document used to carry is retired (§7) —
there is no raw-PTY terminal in the tree, only the WebSocket-bridged transcript view, which is a
normal page under the same contract as everything else.

The app is one shell (`AppShell`) with one route table, built from shared primitives in
`web/src/ui/` — `Dialog`, `Menu`, `Tabs`, `Banner`/`BannerStack`, `StatusPill`, `Button`/`IconButton`,
`EmptyState`, `Card` — plus the domain UI under `web/src/features/<domain>/`. A fix to a primitive
reaches every page that uses it.

## 2. The shell

`web/src/shell/AppShell.tsx` places up to seven regions (`REGION_IDS`), each a fixed
`data-region="app.*"` DOM node with its own landmark role:

| Region | Component | Landmark / role |
|---|---|---|
| `app.banners` | `BannerStack` (`web/src/ui/BannerStack.tsx`) | container for `Banner`s, each its own `role="status"` (polite) or `role="alert"` (assertive) live region |
| `app.rail` | `NavRail` (desktop/tablet) or `NavBar`+`TopBar` (phone) (`web/src/shell/regions/`) | `<nav role="navigation" aria-label="Sections">` |
| `app.list` | `ContextList` (`web/src/shell/regions/ContextList.tsx`) | `aria-labelledby` a heading; holds the section's list (Sessions renders it as a composite `#session-listbox` that the dock drives via `aria-controls`/`aria-activedescendant`) |
| `app.main` | `PageHost` (`web/src/shell/regions/PageHost.tsx`) | the **one** `<main>` on the page |
| `app.dock` | `PromptDock` (`web/src/shell/regions/PromptDock.tsx`) | the session composer, mounted inside `app.main` so it shares the session runtime |
| `app.drawer` | `DrawerHost` (`web/src/shell/regions/DrawerHost.tsx`) | `<aside aria-label={title}>` |
| `app.overlays` | `Overlays` (`web/src/shell/regions/Overlays.tsx`) | stamps `data-region="app.overlays"` onto the portal root that dialogs, menus and toasts render into |

Which regions exist in which mode is a table, not nested conditionals: `REGION_VISIBILITY` in
`AppShell.tsx`, asserted cell-by-cell in `web/src/shell/visibilityMatrix.test.tsx`. The four modes
(`web/src/shell/useViewport.tsx`, `LAYOUT_MODES`) are `phone`, `tablet`, `desktop`, `kiosk`:

- **phone** / **tablet** — one content band shared between `app.list` and `app.main` (whichever is
  open); `app.rail` renders as a bottom `NavBar` + top `TopBar` rather than a side rail.
- **desktop** — master-detail: `app.list` and `app.main` render side by side.
- **kiosk** — an unattended wall display, reached via `?kiosk=1`. `app.rail`, `app.list`, `app.dock`
  and `app.drawer` are all absent; `app.banners` stays mounted but is filtered to `tone === 'danger'`
  only (`bannersForMode`); `Dialog` refuses to open at all (`useInteractive()` is `false`, so
  `PageHeader` also hides its action cluster); and the whole type scale is enlarged by overriding the
  `--scale` custom property on `<html>` (`KIOSK_SCALE = '1.25'`). `NavDef.showInKiosk` gates which
  routes a kiosk URL may land on — the criterion is "verified read-only", not "looks glanceable".

## 3. The page contract

Every migrated page (`features/settings/pageContract.test.tsx`'s `PAGES` table) makes four claims,
true in every layout mode it is reachable in:

- exactly one `<h1>`, rendered by `ui/PageHeader.tsx` (Zone 1 of a page) — the only place a page may
  put a heading;
- `document.title` synced to it (`shell/documentTitle.ts`'s base-title stack), formatted
  `"<title> — ViberPit"`, and distinct from the `h1` where the two must differ (Settings: one page,
  many sections — see `PageHeader`'s `documentTitle` prop);
- `ui/PageContent.tsx` (Zone 3) carries `aria-busy` while loading and a `role="status"
  aria-live="polite"` span that announces "Loading" / "Loaded" for screen-reader users who cannot see
  the (aria-hidden) `Skeleton`;
- a declared layout variant (`standard` / `split` / `reader` / `workspace`), stamped as
  `data-page-variant="<variant>"` by the page itself, and no horizontal scroll on `#root`.

## 4. Primitives

- **`Dialog`** (`ui/Dialog.tsx`) — `role="dialog"` + `aria-modal="true"`, named by exactly one of a
  visible `title` (`aria-labelledby`) or an `aria-label` (the type forbids passing both, or neither).
  Traps focus via `useFocusTrap` while open, restores focus to the trigger on close, and refuses to
  open at all in kiosk (`useInteractive()`).
- **`Menu`** (`ui/Menu.tsx`) — `role="menu"`/`menuitem"` (or `menuitemradio` for a single-choice
  picker), roving `tabIndex`, arrow/Home/End/typeahead navigation, digit shortcuts, registers itself
  with `shell/useOverlayState` so the app's one global keydown listener knows an overlay is open.
- **`Tabs`** (`ui/Tabs.tsx`) — `role="tablist"`/`"tab"`, roving `tabIndex`, arrow-key movement
  (horizontal or vertical), optional `aria-controls` linking a tab to its panel.
- **`Banner`** (`ui/Banner.tsx`) — tone-derived live-region role: `alert` (assertive) for `danger`,
  `status` (polite) otherwise, or `role="none"` (rendered as no `role` attribute) for content that is
  not safe as a live region (e.g. `DeployPanel`'s changing commit/phase list).
- **Touch targets** — the `--target` token (44px, `web/src/index.css`) is exposed as
  `min-h-target`/`min-w-target`. `IconButton` and `Menu`/`Tabs` items carry it by default; an
  on-screen `Button` in a touch context must be `size="lg"` (`sm`/`md` are 36/40px — tracked as a
  base-class change in issue #953).
- **`StatusPill`** (`ui/StatusPill.tsx`) — colour is never the only signal: every pill pairs an icon
  with a text label (`aria-label="Status: <label>"`), so a status reads correctly with colour vision
  off. `features/accounts/UsageBadge.tsx` re-exports a thin `StatusPill` adapter over this primitive
  for account statuses; `UsageBadge` itself adds a numeric percentage alongside its bar.

## 5. Tokens and contrast

Colour lives in exactly one place, `web/src/index.css`'s `@theme` tokens (`--t-*` in `:root`, a
`prefers-color-scheme` block, and a `:root[data-theme="dark"]` block for the explicit theme toggle).
`web/src/__tests__/contrast.test.ts` parses those three blocks straight out of the CSS source and
applies the WCAG 2.x relative-luminance formula to every text/background pair the UI renders (each
tone's text on its own wash, banner body copy on every wash, body text on `surface`), asserting
≥4.5:1 in all three blocks — plus that the two dark blocks never drift from each other. This exists
as arithmetic rather than an axe assertion because jsdom (where the component-level axe tests run)
applies no stylesheet, so axe's `color-contrast` rule always reports `incomplete`, never a violation;
this is the only check that actually fails when a hex value regresses.

The `-strong` fills (`--t-*-strong`) are declared once, on `:root`, with **no** dark override — they
are the same colour in both themes specifically so `--t-on-strong` (white) stays AA against them
regardless of theme. Ink on a `-strong` fill is always `text-on-strong`; ink on a plain `accent` fill
is `text-accent-ink` (`bg-accent` itself flips per theme, so plain white text on it is not
guaranteed AA). `eslint-rules/no-raw-palette.js`'s `whiteOnFill` check reports a bare `text-white`
beside either kind of fill rather than silently accepting it.

`prefers-reduced-motion` has one reader: `useViewport().reducedMotion`
(`web/src/shell/useViewport.tsx`). `SessionCard`/`CardFrame` degrade their pulse animation and swipe
transition to static under it rather than each subscribing to the media query themselves.

## 6. Tests

- **Shell accessibility**: `web/src/shell/AppShell.test.tsx`'s `describe('AppShell — accessibility')`
  block runs `jest-axe` over the whole rendered shell (`document.body`, so a portalled drawer or
  overlay is included) once per layout mode, with `AXE_OPTIONS = {}` — no rule disabled, `region`
  included. `PageHost` is the app's single `<main>` landmark specifically so that rule can stay on.
- **Page contract**: `web/src/features/settings/pageContract.test.tsx`'s `PAGES` table (§3) exercises
  every migrated page, in every layout mode it is reachable in.
- **Playwright**: each `web/e2e/*.spec.ts` exercises its page across the widths that matter to it —
  phone (390×844), tablet (820×1180), desktop (1440×900) — either via `test.use({ viewport })` per
  `describe` block or, where a spec already loops several widths itself
  (`delivery.spec.ts`, `pipeline.spec.ts`, `archived-reader.spec.ts`), via that loop. There are no
  global Playwright viewport projects (`playwright.config.ts`) — the per-spec widths already cover
  what a project matrix would, without tripling specs that loop on their own.

## 7. Retired

The xterm.js terminal partial-conformance exception (a raw PTY surface with its own WCAG 1.1.1 /
2.2.2 carve-outs) no longer applies: there is no terminal surface in the tree. The session transcript
is a normal WebSocket-bridged view (`features/sessions/transcript/`) under the same full-AA contract
as every other page — there is nothing left to document an exception for.

---

## Appendix: Violations found and fixed

### `aria-label` on `<div>` with no valid role (axe rule: `aria-prohibited-attr`)

**Component:** `SessionList` loading skeleton container
(`web/src/features/sessions/list/SessionList.tsx`).

**Before:**
```tsx
<div className="space-y-2" aria-label="Loading sessions" aria-busy="true">
```

**After (fixed):** Added `role="status"` so `aria-label` is valid and the element is announced
by screen readers as a live region when content changes.

```tsx
<div className="space-y-2" role="status" aria-label="Loading sessions" aria-busy="true">
```

**Impact:** Screen readers (NVDA, VoiceOver) would previously ignore or mis-announce the loading
state. With `role="status"` the container is a live region and `aria-label` is permitted.

---

### Audit components (current paths)

The automated axe audit in `web/src/features/sessions/list/a11y.test.tsx` (moved from
`web/src/components/__tests__/a11y.test.tsx`) covers:

| Component | Current path | States tested |
|-----------|--------------|----------------|
| `AllLimitedBanner` | `web/src/shell/AllLimitedBanner.tsx` | with reset time, with null reset |
| `EmptyState` | `web/src/ui/EmptyState.tsx` | default |
| `SessionList` | `web/src/features/sessions/list/SessionList.tsx` | empty, loading skeleton, 2-session list |
| `UsageBadge` | `web/src/features/accounts/UsageBadge.tsx` | low (20%), high (95%), no label |
| `StatusPill` | `web/src/features/accounts/UsageBadge.tsx` (adapter over `web/src/ui/StatusPill.tsx`) | all 5 account statuses, all 4 session statuses |
| `DispatchBar` | `web/src/features/sessions/list/DispatchBar.tsx` | single engine, two engines + runner picker, reasoning-effort selector |
| `AddAccountDialog` | `web/src/features/accounts/AddAccountDialog.tsx` | default (Anthropic) provider, OpenAI device-code panel |
