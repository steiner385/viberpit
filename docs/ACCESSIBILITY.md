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
| `app.list` | `ContextList` (`web/src/shell/regions/ContextList.tsx`) | `<aside aria-labelledby>` a heading; holds the section's list (Sessions puts a deliberately **role-less** `<div id="session-listbox" aria-label="Sessions">` inside it — the dock's textarea `aria-controls` that id and owns `aria-activedescendant` itself, because that attribute is not allowed on a role-less element) |
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

Every migrated page (`features/settings/pageContract.test.tsx`'s `PAGES` table) is asserted on
exactly four things, in every layout mode it is reachable in:

- **exactly one `<h1>`** — found by its accessible name (`hidden: true`, since on phone/tablet a
  section that owns a list column renders its page behind the list), then counted: one. It is
  rendered by `ui/PageHeader.tsx` (Zone 1 of a page), the only place a page may put a heading.
- **`document.title` matches `/<title> — ViberPit$/`** — the suffix comes from
  `shell/documentTitle.ts`'s base-title stack. The title may differ from the `h1` where the two must
  (Settings: one page, many sections — see `PageHeader`'s `documentTitle` prop).
- **the declared layout variant is in the DOM** — `[data-page-variant="<variant>"]` (`standard` /
  `split` / `reader` / `workspace`), stamped by the page itself, not by the shell.
- **no horizontal scroll** — `#root`'s `scrollWidth` never exceeds its `clientWidth`.

Behaviours of the Zone 1/Zone 3 primitives themselves (the `PageContent` busy announcer, for
instance) are §4's contract, not this table's — `PAGES` asserts the four bullets above and nothing
more.

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
- **`PageContent`** (`ui/PageContent.tsx`) — Zone 3 of every page. Owns scrolling (or clips, with
  `scroll={false}`, for a workspace layout whose inner panes scroll themselves), carries `aria-busy`
  while `busy`, and renders an `sr-only` `role="status" aria-live="polite"` span announcing
  "Loading" / "Loaded" for screen-reader users who cannot see the (aria-hidden) `Skeleton`.
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

Colour lives in exactly one place, `web/src/index.css`, in two layers (documented at `index.css:24`).
The values are raw `--t-*` custom properties in three blocks: `:root` (light), a
`@media (prefers-color-scheme: dark)` block, and a `:root[data-theme="dark"]` block for the explicit
theme toggle. A separate `@theme inline` block (from `index.css:135`) then bridges those variables
onto Tailwind's namespaces — `--color-*` → `bg-`/`text-`/`border-`, `--z-index-*` → `z-*`,
`--spacing-*` → `min-h-target` and friends — so a utility reads the raw variable directly and a theme
switch never needs a class change. The `--t-*` tokens are not themselves inside `@theme`.
`web/src/__tests__/contrast.test.ts` parses those three value blocks straight out of the CSS source and
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

`prefers-reduced-motion` has one **layout media-query subscriber**: `useViewport().reducedMotion`
(`web/src/shell/useViewport.tsx`). `SessionCard`/`CardFrame` degrade their pulse animation and swipe
transition to static by reading that, rather than each subscribing to the media query themselves.
Two modules that cannot consume a React context read the query directly and are the documented
exceptions — `features/sessions/brailleSpinner.ts`'s `prefersReducedMotion()` (swaps the cycling
spinner for a static glyph, consumed by `WorkingIndicator.tsx`) and `pipeline/motion.ts`'s
`scrollBehavior()` (`'auto'` instead of `'smooth'`). CSS covers the rest without any JS reader at
all: the `motion-reduce:` utility variant, plus the `@media (prefers-reduced-motion: reduce)` block
at `index.css:266` that stops the working-shimmer animation. This matches `web/README.md`'s
"one media-query reader" convention and its two stated exceptions.

## 6. Tests

- **Shell accessibility**: `web/src/shell/AppShell.test.tsx`'s `describe('AppShell — accessibility')`
  block runs `jest-axe` over the whole rendered shell (`document.body`, so a portalled drawer or
  overlay is included) once per layout mode, with `AXE_OPTIONS = {}` — no rule disabled, `region`
  included. `PageHost` is the app's single `<main>` landmark specifically so that rule can stay on.
- **Page contract**: `web/src/features/settings/pageContract.test.tsx`'s `PAGES` table (§3) exercises
  every migrated page, in every layout mode it is reachable in.
- **Playwright**: each `web/e2e/*.spec.ts` picks the widths that matter to *it*, in one of two
  styles. Per-`describe` `test.use({ viewport })` blocks: `delivery.spec.ts` (desktop 1440×900 +
  phone 390×844) and `roadmap.spec.ts` (the same pair). A loop over a width list:
  `pipeline.spec.ts` (phone 390×844 + tablet 820×1180, with desktop and kiosk as separate
  `test.use` describes) and `archived-reader.spec.ts` (phone + desktop). The rest are deliberately
  narrower — `dashboard.spec.ts` is single-viewport at 375×812 and says so in its header,
  `transcript-virtualization.spec.ts` is phone-only, `short-viewport.spec.ts` uses 393×480 and
  393×852 to reproduce the Android keyboard squeeze, and `rotation-reconnect.spec.ts` sets no
  viewport at all. So phone is near-universal and **820×1180 appears in exactly one spec**; there is
  no "every spec covers three widths" guarantee to rely on.
  `playwright.config.ts:17-22` defines one project — `chromium` on `devices['Desktop Chrome']`,
  whose viewport is what a spec that sets none inherits. What does not exist is a viewport *matrix*:
  the per-spec widths cover what one would, without multiplying specs that already loop on their own.

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
