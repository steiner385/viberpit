---
name: ViberPit
description: A calm, glanceable operator console — one accent, four status tones, hairline surfaces, system type, one shell for phone, tablet, desktop and wall.
colors:
  # Light-theme values are canonical here; every token flips at :root in dark mode.
  # The dark values live in .impeccable/design.json → extensions.colorMeta.<token>.dark.
  canvas: "#f6f7f9"
  surface: "#ffffff"
  surface-raised: "#eef1f5"
  edge: "#d9dde3"
  edge-strong: "#b9c1cc"
  ink: "#1a1f29"
  ink-muted: "#646a77"
  ink-faint: "#8b929e"
  accent: "#2461e7"
  accent-ink: "#ffffff"
  accent-wash: "#e8effc"
  on-strong: "#ffffff"
  success: "#147c3b"
  success-wash: "#e6f4ea"
  success-strong: "#15803d"
  warning: "#9a5d00"
  warning-wash: "#fbf0dc"
  warning-strong: "#9a5d00"
  danger: "#c23636"
  danger-wash: "#fbe9e7"
  danger-strong: "#b42318"
  info: "#4f6d8f"
  info-wash: "#e9eef5"
  info-strong: "#3d5674"
  neutral: "#646a77"
  neutral-wash: "#eceff3"
  neutral-strong: "#5b6472"
  brand: "#22c55e"
typography:
  display:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(2.25rem * var(--scale))"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "normal"
  headline:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(1.5rem * var(--scale))"
    fontWeight: 600
    lineHeight: 1.25
  title:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(1.25rem * var(--scale))"
    fontWeight: 600
    lineHeight: 1.3
  title-sm:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(1.125rem * var(--scale))"
    fontWeight: 600
    lineHeight: 1.3
  body:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(0.875rem * var(--scale))"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(0.75rem * var(--scale))"
    fontWeight: 500
    lineHeight: 1.5
  micro:
    fontFamily: "system-ui, 'Segoe UI', Roboto, sans-serif"
    fontSize: "calc(0.6875rem * var(--scale))"
    fontWeight: 700
    lineHeight: 1
  mono:
    fontFamily: "ui-monospace, Consolas, monospace"
    fontSize: "0.875em"
    fontWeight: 400
rounded:
  sm: "4px"
  md: "6px"
  lg: "10px"
  pill: "999px"
spacing:
  "1": "4px"
  "1.5": "6px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "6": "24px"
  target: "44px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "color-mix(in oklab, var(--color-accent) 90%, transparent)"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-raised}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-ghost-hover:
    backgroundColor: "{colors.surface-raised}"
  button-danger:
    backgroundColor: "{colors.danger-strong}"
    textColor: "{colors.on-strong}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "40px"
  button-danger-hover:
    backgroundColor: "color-mix(in oklab, var(--color-danger-strong) 90%, transparent)"
  badge:
    backgroundColor: "{colors.neutral-wash}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "2px 6px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "40px"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted}"
    typography: "{typography.body}"
    padding: "8px 12px"
    height: "{spacing.target}"
  tab-active:
    backgroundColor: "transparent"
    textColor: "{colors.accent}"
  tile:
    backgroundColor: "{colors.neutral-wash}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
    height: "{spacing.target}"
---

# Design System: ViberPit

## Overview

**Creative North Star: "The Pit Wall"**

A race team's pit wall is a bank of quiet screens read at a glance by one person who is responsible for everything on them. Nothing on the wall is decorative; the only colour is status, the controls are few and deliberate, and the same screens are read from a stool and from across the garage. ViberPit's interface is built the same way. It is a console for one operator who is interrupt-driven: the queue of things that need a human is the product, and everything else is calm, dense context around it. (Alternates, if the owner prefers: **"The Instrument Panel"** or **"The Dispatch Desk"**. Swap the name; the rules below do not change.)

The material is near-neutral paper and ink. Surfaces are flat white or cool grey, separated by hairlines rather than shadows; depth is a one-step tonal lift (`surface` → `surface-raised`) and a border, and a drop shadow appears only under the three things that actually float (dialog, sheet, menu). There is exactly one interactive accent, a signal blue used for focus rings, the selected tab, the active nav item and the primary button, and four status tones (success, warning, danger, info) plus a neutral, each with a text value, a tint and a theme-invariant fill. Status is always a tone *and* a word or glyph, never a colour alone. Type is the system face at a small working size, 14px body on a 4px grid, with weight (500/600/700) doing the hierarchical work rather than size. The kiosk reads the same screens at ten feet by multiplying one variable, not by a second design.

The voice is serious about the work, not about itself. Density, speed and glanceability beat explanation. Confirmed rejections, all enforced in code: no brand colour in the UI (the Viper Green logotype is the one exception and is text-exempt), no webfonts, no hard-coded colour outside `index.css` (an ESLint rule and a contrast test fail the build), no dark-mode class variants (the tokens flip at `:root`), and no motion that is not a live signal.

**Key Characteristics:**
- Near-neutral ink/edge/surface palette with one accent and four status tones; every pair held at ≥ 4.5:1 in light and dark by a test.
- Flat surfaces, 1px hairline borders, one tonal step for raised chrome; shadows only on floating overlays.
- System type (`system-ui`) on a 4px rhythm; 14px body, 12px labels, weight carries hierarchy.
- One type scale: the kiosk sets `--scale: 1.25` and every size follows.
- 44px minimum target on every control a finger can reach; `focus-visible` ring in the accent everywhere.
- Status rails, dots and pills carry tone; the word or glyph beside them carries the meaning.
- Motion is rare, short (150–200ms) and always behind `motion-safe` / `prefers-reduced-motion`.
- Four layout modes (phone, tablet, desktop, kiosk) from one shell; chrome is shed, never shrunk, when the viewport is short or narrow.

## Colors

A restrained cool-grey paper-and-ink system with one signal blue and a small, tested set of status tones; the palette has no decorative colour at all.

### Primary
- **Signal Blue** (`accent`, #2461e7; dark #5c9bff): the single interactive colour. Primary button fill, every `focus-visible` ring (2px), the selected tab's underline and text, the active nav item's text on **Blue Tint**, the metro track's live node and progress bars, and links in rendered markdown. Its rarity is what makes a focus ring or an active state findable.
- **Blue Tint** (`accent-wash`, #e8effc; dark #1c2a44): the accent's background form. Active nav item, selected list card, "server scope" badge. Never used as a text colour.
- **Ink on Blue** (`accent-ink`, #ffffff; dark #0b1416): the only ink permitted on an `accent` fill, because the fill flips per theme and plain white is not AA on the dark value.

### Neutral
- **Cool Paper** (`canvas`, #f6f7f9; dark #14171c): the page and body background, the queued "car" in the queue train, and the bulk-action bar. Everything sits on this.
- **Sheet White** (`surface`, #ffffff; dark #1d2128): cards, inputs, dialogs, sheets, menus, the settings fieldset, the save bar. The default working surface.
- **Pressed Paper** (`surface-raised`, #eef1f5; dark #262b34): hover fill for secondary and ghost buttons and menu items, the skeleton bar, inline `code`, the off-state switch track. It is the one tonal step the system uses to say "raised" or "hovered".
- **Hairline Grey** (`edge`, #d9dde3; dark #323843): every 1px border, divider, tab baseline, the metro track's unfilled segments and bars. The system's primary depth device.
- **Rule Grey** (`edge-strong`, #b9c1cc; dark #46505e): the stronger border for an interactive card on hover, the checkbox and switch outline.
- **Slate Ink** (`ink`, #1a1f29; dark #e8eaee): body text, titles, labels, button text on light fills. 15.6:1 on Sheet White.
- **Graphite** (`ink-muted`, #646a77; dark #98a0ac): secondary copy, descriptions, section headings, inactive tabs and nav items, placeholder text, timestamps. Still 5.4:1, so it is a *reading* colour, not a disabled one.
- **Pencil Grey** (`ink-faint`, #8b929e; dark #6f7886): icons in empty states and the menu's keyboard digit hints only. At 3.1:1 it is below the text floor and is treated as decoration.
- **Fill Ink** (`on-strong`, #ffffff, no dark value): the ink for any `-strong` fill (danger button, done/parked metro nodes). Theme-invariant because the fills it sits on are.

### Status tones
Each tone is a triple: a text value that is AA on `surface`, a `-wash` tint for backgrounds, and a `-strong` fill that is AA behind **Fill Ink** and does **not** change in dark mode. The neutral tone shares its text value with **Graphite** by design.
- **Go Green** (`success`, #147c3b / wash #e6f4ea / strong #15803d): in-flight tier rail and dot, done metro node and segment, "Saved" status in the save bar, the "browser scope" badge, the healthy queue word.
- **Caution Amber** (`warning`, #9a5d00 / wash #fbf0dc / strong #9a5d00): the needs-you tier (the only pulsing tier), the attention dot on tabs and nav, "Unsaved changes", overdue bars, parked metro node.
- **Stop Red** (`danger`, #c23636 / wash #fbe9e7 / strong #b42318): blocked tier, failed node and bar, the Danger-zone fieldset border and legend, field errors, the required asterisk, the danger button (on the `-strong` fill).
- **Steel Blue** (`info`, #4f6d8f / wash #e9eef5 / strong #3d5674): proposed tier, informational banners. Deliberately desaturated so it never competes with Signal Blue for "interactive".
- **Graphite** (`neutral`, #646a77 / wash #eceff3 / strong #5b6472): live and idle tiers, count badges, the default pill.

### Brand (not a UI colour)
- **Viper Green** (`brand`, #22c55e, theme-invariant): the "Pit" half of the text wordmark in `BrandMark` and nothing else. At 2.3:1 on white it fails AA for text; WCAG exempts logotypes, and the token exists so the wordmark is drawn from the same sheet, not so anyone can reuse it. Vibe Purple from the brand sheet has no token at all. The mascot and app icon are rasters with their own palette.

### Named Rules
**The One Accent Rule.** Signal Blue is the only interactive colour. If something is blue it is focusable, selected, active or live; nothing is blue for emphasis.

**The Logotype-Only Rule.** Brand colours are never UI tokens. Viper Green appears in the wordmark and nowhere a person reads, points or taps; Vibe Purple does not appear at all.

**The Tone-Plus-Word Rule.** A status is a tone and a word or glyph together (pill: icon + label; tier: rail/dot + the tier named in the subtitle; failed bars: colour + hatch texture). Colour alone never carries a state.

**The Invariant Fill Rule.** `-strong` fills and `on-strong` ink are declared once and never flip with the theme, so white-on-fill stays AA in both. Ink on a plain `accent` fill is `accent-ink`, never white.

## Typography

**Display Font:** system-ui (with Segoe UI, Roboto, sans-serif)
**Body Font:** system-ui (same stack)
**Label/Mono Font:** ui-monospace (with Consolas, monospace), for inline code, `<pre>` blocks and `Kbd`

**Character:** Deliberately anonymous. The system face at a small working size reads as an instrument, not a publication; hierarchy comes from weight (500 for labels and controls, 600 for every heading and numeral, 700 only for a glyph or word meant to be read from across a room) and from Graphite-versus-Slate ink, not from size jumps. Numerals are always `tabular-nums`. The brand sheet's Poppins, Inter and Caveat live only inside logo rasters.

### Hierarchy
Every size is `calc(<rem> * var(--scale))`; `--scale` is 1 everywhere except the kiosk (1.25). Root is 16px/1.5.
- **Display** (600, `--fs-4xl` = 36px, 45px on the kiosk, line-height tight): the ten-foot numerals in the pipeline status tiles when the shell is read-only. `--fs-3xl` (30px) is the second ten-foot step and is currently used only for the deploy overlay's check glyph.
- **Headline** (600, `--fs-2xl` = 24px): the stat-tile numerals at the top of a detail pane.
- **Title** (600, `--fs-xl` = 20px): a settings section's `h2`; the metro node glyph and the queue-health word on the pipeline (the two glyphs read from a distance). `--fs-lg` (18px, 600) is the page `h1` in `PageHeader` and a dialog title.
- **Body** (400, `--fs-sm` = 14px, 1.5; markdown 1.6): the working size for every row, control, description, banner title and button. `--fs-base` (16px) is the root and the pipeline charts' floor; it is rarely set on chrome directly.
- **Label** (500, `--fs-xs` = 12px): badges and status pills, field help and errors, section headings (uppercase, Graphite), fieldset legends (600, uppercase, `tracking-wide`), card subtitles.
- **Micro** (700, `--fs-2xs` = 11px): counters only — the failed-delta arrow, nav count badge, the menu's digit hints. Never a sentence.

### Named Rules
**The One Scale Rule.** There is one type scale. The kiosk enlarges it by setting `--scale: 1.25` on `<html>` and nothing else; a second kiosk-only size rule anywhere is a defect.

**The Read Ink Rule.** Text a person reads is Slate Ink or Graphite (both ≥ 4.5:1). Pencil Grey (`ink-faint`) is for icons and decoration only. Placeholders are read, so they are Graphite at full opacity, set once in `@layer base`.

**The Weight-Not-Size Rule.** Within a row or card, hierarchy is weight and ink, not size: title 14px/500 Slate, subtitle 12px Graphite. Size steps are reserved for page, section and numeral levels.

## Layout

The app is a fixed-viewport column: `html, body` are `overflow: hidden` at `100dvh`, and only interior panes scroll (`PageContent` owns scrolling with `overflow-y-auto`, or hands it to its panes for a workspace page). Every page has the same three zones: a `PageHeader` (the only `h1`, hairline below, `px-4 py-3` growing to `px-6 py-4` at `md`), optional tabs, and `PageContent` (`px-4 py-4`, `px-6` at `md`). Settings sections cap their measure at `max-w-3xl`; assistant prose caps at `max-w-prose`.

Spacing sits on a 4px grid with 8px (`gap-2`) as the default gap between siblings, 12px inside compact cards and tiles (`p-3`, `px-3 py-2`), 16px inside standard cards and sheet bodies (`p-4`), and 24px inside dialogs (`p-6`). Rows inside a settings fieldset are `py-3` separated by hairlines. Nothing in the primitives uses a step above 24px except vertical breathing room in an empty state (`py-16`).

Breakpoints are Tailwind's `sm` 640px, `md` 768px, `lg` 1024px (the desktop breakpoint; `CardFrame` and the shell branch on it) and, rarely, `xl` 1280px. Two custom variants shed chrome rather than shrink it: `short:` (`max-height: 580px`, a phone with the keyboard up or in landscape) and `narrow:` (`max-width: 430px`, a phone in portrait), used to drop a pill's label or a header's secondary controls so the title keeps its room. Grids collapse predictably: stat tiles are two columns on a phone and four from `sm`; a setting row is stacked on a phone and `[minmax(0,1fr) minmax(200px,42%)]` from `sm`; the pipeline charts reflow below 560px (the width of a phone and of a desktop at 200% zoom).

The four layout modes (phone, tablet, desktop, kiosk) are one shell and one route table. Kiosk is a mode, not a width: it is read-only (dialogs refuse to open, page actions are not rendered), hides the cursor after 5s idle, wraps the queue train instead of scrolling it in portrait, and multiplies the type scale. Z-order is a named ladder, lowest to highest: raised 1, sticky 20, nav 25, popover 30, overlay 40, drawer 50, dialog 60, palette 1000, toast 1100; the save bar is `sticky` at the bottom of its scroll container, which sets `scroll-padding-bottom` so a focused field is never hidden under it.

### Named Rules
**The 44px Rule.** Every control a finger can reach is at least 44px tall (`min-h-target`): tabs, nav items, icon buttons, inputs at `md`, menu items, list cards, the `lg` button that every destructive or primary confirm action in a dialog footer must use. Dense desktop controls lift the floor only behind `@media (hover: hover)`.

**The Shed-Not-Shrink Rule.** When a viewport is short or narrow, optional chrome is hidden with the `short:` / `narrow:` variants; the title is never squeezed to make room.

**The Panes-Scroll Rule.** The document never scrolls. A page that can scroll past the viewport is a bug, not a layout.

## Elevation & Depth

The system is flat. Depth is conveyed by a 1px Hairline Grey border and a single tonal step: Sheet White on Cool Paper for anything that is a card or control, Pressed Paper for anything hovered, pressed or placeholder. There are no resting shadows on cards, inputs, headers or tiles. A shadow appears only under the three things that are genuinely detached from the page and stacked over a 50% black backdrop or over other content: the dialog, the sheet, and the anchored menu. An interactive card signals hover by strengthening its border (`edge` → `edge-strong`), never by lifting. The one glow in the system is the metro track's live node ring, a 4px accent halo at 22% that is a status signal, not elevation.

### Shadow Vocabulary
- **Floating overlay** (`box-shadow: 0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)`): `Dialog` and `Sheet` panels, always paired with a hairline border and the `bg-black/50` backdrop.
- **Anchored popover** (`box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)`): `Menu`, the command palette, mention and export menus; hairline border, 4px padding, no backdrop.
- **Live halo** (`box-shadow: 0 0 0 4px color-mix(in srgb, var(--t-accent) 22%, transparent)`): the metro track's active node, breathing to 7px/11% behind `prefers-reduced-motion: no-preference`.

### Named Rules
**The Hairline-First Rule.** A boundary is a 1px `edge` border before it is anything else. Reach for a tonal step second, and for a shadow only when the element portals out of the page flow.

**The Flat-At-Rest Rule.** Nothing is lifted at rest. Hover is a fill change (to Pressed Paper) or a border change (to Rule Grey); selection is a 2px accent ring plus Blue Tint; focus is the same 2px accent ring.

## Shapes

Corners are small and consistent: 4px (`sm`) for the smallest pieces (checkbox, skeleton bar, menu item, inline code, the tone rail's outer corners), 6px (`md`) for every control (buttons, inputs, selects, tabs' vertical variant, nav items, stat and status tiles, menus), and 10px (`lg`) for containers (cards, list rows, banners, dialogs, the settings fieldset, the bottom sheet's top edge). Anything that reads as a status or a count is a full pill (`999px`): badges, status pills, the 8px dot, the switch and its thumb. The right-side sheet has no radius at all; it is a wall, not a card. Borders are always 1px hairlines, with two exceptions that are intentional: the Danger-zone fieldset and the queue train's cars, which take a 2px solid (building) or 2px dashed (queued, blocked) edge because the border *is* the status there. The recurring silhouette is a rounded rectangle with a 3px tone rail set into its left edge, rounded on its right side only, present on every list row and toned card.

## Components

The component philosophy is "refined and restrained": controls are quiet rectangles that announce themselves on focus, not at rest. Every primitive is token-only (no raw palette, no `dark:` variant) and ships its `focus-visible` ring and 44px target itself, so a new screen inherits accessibility by composition.

### Buttons
- **Shape:** gently rounded controls (6px), 14px/500 text, icon gap 8px, `transition-colors`.
- **Primary:** Signal Blue fill with Ink on Blue text; `md` is 40px tall with 8px 16px padding; `sm` is 36px with 12px side padding; `lg` is 44px (`min-h-target`) with 24px side padding and is mandatory for a dialog's confirm or destructive action.
- **Hover / Focus:** primary and danger dim their fill to 90%; secondary and ghost fill with Pressed Paper. Focus is a 2px Signal Blue ring with a 2px Cool Paper offset. Disabled is 50% opacity with pointer events off; loading swaps the left icon for a spinning glyph (`motion-reduce:animate-none`) and sets `aria-busy`.
- **Secondary:** Sheet White fill, Hairline Grey border, Slate Ink text. The default for anything not the one primary action.
- **Ghost:** no fill or border, Slate Ink text; "Discard", "Keep editing", icon-adjacent actions. `IconButton` is the square form (40px, or 36px at `sm`, never below 44px of hit area), Graphite at rest, Slate on hover, with a required `aria-label`.
- **Danger:** Stop Red `-strong` fill with Fill Ink text, so it reads identically in both themes.

### Badges and Status Pills (chips)
- **Style:** full pills, 12px/500 text, tone text on tone wash (`success` on `success-wash` and so on), 2px 6px padding, `tabular-nums` for counts. The `neutral` tone is Graphite on its grey wash and is the default. `accent` tone exists for the "server scope" badge.
- **Dot:** an 8px pill filled with the tone (`TONE_FILL`), `aria-hidden`, used for "needs attention" on a tab or nav item and as the roadmap tier dot; it pulses only for the needs-you tier and only behind `motion-safe`.
- **Status pill:** the same pill with a 12px tone icon before the label and `aria-label="Status: …"`; action-required tiers may pulse. On a narrow phone the label can hide (`narrow:hidden`) and the glyph and tooltip carry it.
- **State:** there is no selected/unselected chip; selection in this system is a ring, not a chip.

### Cards and Containers
- **Corner Style:** 10px.
- **Background:** Sheet White on Cool Paper; a selected list row turns Blue Tint.
- **Shadow Strategy:** none (see Elevation). Interactive cards strengthen their border on hover and take a 2px accent `focus-within` ring.
- **Border:** 1px Hairline Grey. A toned `Card` and every `CardFrame` row add the **tone rail**: a 3–4px bar (`w-0.75` / `w-1`) inset from the top and bottom, rounded on its right edge, filled with the tone (`bg-warning`, `bg-danger`, `bg-success`; calm tiers fade to `info/40`, `neutral/40`, `neutral/20`). The card's left padding grows (`pl-5`) to clear it.
- **Internal Padding:** 12px (`p-3`, or `px-3 py-2` on a list row) for dense rows and tiles; 16px for a standard card; 24px for a dialog.
- **Settings group:** a real `<fieldset>` with a 10px border and an uppercase 12px/600 `legend` in Graphite, rows divided by hairlines; the Danger zone is the same fieldset with a Stop Red border and legend, always last. Rows are label (14px/500) + description (12px Graphite, always visible, never a tooltip) + one control, right-aligned from `sm`.

### Inputs and Fields
- **Style:** Sheet White fill, 1px Hairline Grey border, 6px corners, Slate text, 14px; `md` is 40px tall with a 44px minimum, `sm` is 36px. Placeholder is Graphite at full opacity. Selects add 32px right padding; the shared `controlClass` is also exported for one-off native inputs so they cannot drift.
- **Focus:** border turns Signal Blue and a 2px Signal Blue ring appears (no offset).
- **Error / Disabled:** `aria-invalid` turns the border Stop Red and the focus ring with it; a `Field` prints the error at 12px Stop Red with `role="alert"` under the control, and the required asterisk is Stop Red. Disabled is 50% opacity with a not-allowed cursor.
- **Checkbox vs Switch:** a 16px checkbox (4px corners, Rule Grey border, `accent-color` Signal Blue) means "save later"; a 44×24px pill switch (Pressed Paper track / Rule Grey border off, Signal Blue on, 16px thumb) means "done now".

### Navigation
- **Nav items:** 44px rounded (6px) rows with a 20px icon and 14px/500 label, Graphite at rest, Pressed Paper fill and Slate on hover, Blue Tint fill and Signal Blue text when current (`aria-current="page"`). Count badges are neutral pills; attention is a Caution Amber dot. The rail is hidden entirely on the kiosk.
- **Tabs:** a hairline-baselined strip; each tab is 44px tall, 14px/500, Graphite with a transparent 2px bottom border, becoming Signal Blue text and underline when selected. Overflow fades at the edges with a 24px mask and the selected tab is kept on screen. Six tabs maximum; Overview first, then the queue tiers in the same order as the tiles (Needs you, Blocked, In flight, then inventory).
- **Breadcrumb / Back:** an accent-coloured text link with a leading arrow, 44px tall, that takes its own line below `sm`.

### Overlays
- **Dialog:** centred from `sm`, bottom-anchored on a phone, Sheet White with hairline border, 10px corners, 24px padding, the floating shadow and a 50% black backdrop; widths `sm` 24rem, `md` 28rem, `lg` 32rem, `xl` 64rem; 18px/600 title, 14px Graphite description. Refuses to open on the kiosk.
- **Sheet:** right (448px, square corners, left hairline) or bottom (85vh, 10px top corners), slides in over 200ms `ease-out` (`motion-reduce:animate-none`); header `px-4 py-3` with a hairline, body `p-4`. A `push` mode renders it inline as a column.
- **Menu:** anchored, Sheet White, 6px corners, 4px padding, the popover shadow; items 44px, 14px, Pressed Paper on focus, digit hints in Pencil Grey.
- **Banner:** 10px corners, tone wash fill with a 40%-alpha tone border, 18px tone icon, 14px/500 title and 12px Graphite body; `danger` is an assertive alert, others polite.

### Signature: the Status Tile Strip
A row of wash-on-hairline tiles (6px corners, `px-3 py-2`, 44px minimum, `flex-1 basis-24`), one per bucket, each a tone wash with a toned numeral (20px/600 at a desk, 36px/600 at ten feet) over an uppercase Graphite label (11px, or 14px on the wall). An empty bucket dims to 50%; the active filter takes the 2px accent ring; the failed tile shows a bold Micro delta arrow in Stop Red or Go Green. Tiles lead with the queue: Needs you, Blocked, In flight, then inventory, and the page's tabs follow the same order.

### Signature: the Metro Track
The pipeline as a transit line: 32px circular nodes (Hairline Grey with a Graphite glyph at rest) joined by 4px Hairline segments. Done nodes and segments fill Go Green (`-strong` with Fill Ink); the live node fills Signal Blue with a Cool Paper glyph and the accent halo, which breathes only when the track is live and motion is allowed; failed is Stop Red, parked is Caution Amber `-strong` in bold. Labels sit under each node at the chart floor (`--fs-base`); the live one turns accent and bold. On a phone the nodes flex to share the row and labels ellipsize; the queue train beside it uses 2px solid or dashed borders as its status channel.

## Do's and Don'ts

### Do:
- **Do** take every colour from a `--t-*` token through its Tailwind namespace (`bg-surface`, `text-ink-muted`, `border-edge`); the ESLint `no-raw-palette` rule and `contrast.test.ts` are the gate.
- **Do** keep status as tone plus word or glyph: a `StatusPill` with its icon, a tier rail with the tier named in the subtitle, a failed bar with its hatch.
- **Do** put `text-on-strong` on any `-strong` fill and `text-accent-ink` on a plain `accent` fill; never `text-white`.
- **Do** give every reachable control a 44px minimum (`min-h-target`) and the 2px Signal Blue `focus-visible` ring; use the `lg` button for a dialog's confirm or destructive action.
- **Do** gate every animation behind `motion-safe:` (or `motion-reduce:animate-none`) and animate only a live signal: one needs-you dot, one active node, one working shimmer.
- **Do** separate with a 1px Hairline Grey border or a step to Pressed Paper before reaching for anything else; reserve shadows for dialog, sheet and menu.
- **Do** write Graphite for secondary text and placeholders (5.4:1), and reserve Pencil Grey for icons and decoration.
- **Do** make headings carry hierarchy by weight (600) and level, not by size jumps; keep the page `h1` in `PageHeader` and section headings as 12px uppercase Graphite.
- **Do** shed optional chrome with `short:` and `narrow:` when a phone runs out of room, and keep the title.

### Don't:
- **Don't** use Viper Green or Vibe Purple anywhere a person reads, points or taps; the wordmark is the only home for `brand`.
- **Don't** add a webfont, a `dark:` variant, a literal hex outside `index.css`, or a second `--on-wash`-style token family.
- **Don't** add a kiosk-only size, a second `@media (prefers-reduced-motion)` subscriber in JS, or a second scale: `--scale` is the one knob.
- **Don't** convey a state by colour alone, or hide a setting's description behind a tooltip.
- **Don't** let a tree of dots or a legend swatch pulse; motion belongs to the one live element on screen.
- **Don't** lift a card at rest, add a shadow to a header, tile or input, or signal hover by elevation; hover is a fill or border change.
- **Don't** let the document scroll, render a dialog on the kiosk, or render disabled page actions on it; the wall shows the title and the numbers.
- **Don't** use a sentence at the 11px Micro step; it is for counters.
