# Sub-spec A — Foundation: token layer, primitives, guardrails

**Parent:** `2026-09-02-ui-target-state-design.md` (umbrella; vocabulary in §4, decisions in §3) ·
**Delivers:** umbrella §8 PRs 1–2 · **Closes:** audit F4.1, F4.2, F4.3 (changed-files scope), F4.4,
the focus-trap and banner rows of the "parallel scales" table.

## 1. Scope

Two PRs. PR 1 makes the token layer real and removes the dead scaffold; PR 2 adds the primitive set
and moves every overlay and banner onto it. **No layout changes** — the two shells keep rendering
exactly as today, on tokens.

Out of scope: any page or shell restructuring (sub-spec B/C), the repo-wide palette sweep (C, PR 11).

## 2. PR 1 — Token layer

### 2.1 `web/src/index.css`

Replace the scaffold `:root` variables and the missing `@theme` with:

```css
@import "tailwindcss";

@theme {
  --color-canvas: var(--t-canvas);          --color-surface: var(--t-surface);
  --color-surface-raised: var(--t-surface-raised);
  --color-edge: var(--t-edge);              --color-edge-strong: var(--t-edge-strong);
  --color-ink: var(--t-ink);                --color-ink-muted: var(--t-ink-muted);
  --color-ink-faint: var(--t-ink-faint);
  --color-accent: var(--t-accent);          --color-accent-ink: var(--t-accent-ink);
  --color-accent-wash: var(--t-accent-wash);
  --color-success: var(--t-success);  --color-success-wash: var(--t-success-wash);  --color-success-strong: var(--t-success-strong);
  --color-warning: var(--t-warning);  --color-warning-wash: var(--t-warning-wash);  --color-warning-strong: var(--t-warning-strong);
  --color-danger:  var(--t-danger);   --color-danger-wash:  var(--t-danger-wash);   --color-danger-strong:  var(--t-danger-strong);
  --color-info:    var(--t-info);     --color-info-wash:    var(--t-info-wash);     --color-info-strong:    var(--t-info-strong);
  --color-neutral: var(--t-neutral);  --color-neutral-wash: var(--t-neutral-wash);  --color-neutral-strong: var(--t-neutral-strong);

  --z-raised: 1; --z-sticky: 20; --z-nav: 25; --z-popover: 30; --z-overlay: 40;
  --z-drawer: 50; --z-dialog: 60; --z-palette: 1000; --z-toast: 1100;

  --spacing-target: 44px;
  --radius-sm: 4px; --radius-md: 6px; --radius-lg: 10px; --radius-pill: 999px;

  --text-2xs: calc(0.6875rem * var(--scale)); --text-xs: calc(0.75rem * var(--scale));
  --text-sm: calc(0.875rem * var(--scale)); --text-base: calc(1rem * var(--scale));
  --text-lg: calc(1.125rem * var(--scale)); --text-xl: calc(1.25rem * var(--scale));
  --text-2xl: calc(1.5rem * var(--scale));

  --font-sans: system-ui, 'Segoe UI', Roboto, sans-serif;
  --font-mono: ui-monospace, Consolas, monospace;
}

:root { --scale: 1; /* light values for every --t-* */ }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* dark --t-* */ } }
:root[data-theme="dark"] { /* dark --t-* */ }
```

- `--t-*` are the **theme values**; `--color-*` in `@theme` reference them so Tailwind emits one
  utility per token and the theme flips by variable, never by class. Light and dark values are the
  ones already in `plotroom.css` (`#f6f7f9/#fff/#1a1f29/#69707d/#d9dde3/#2563eb` and the dark set),
  which were tuned to AA — extended with `-wash` (10–15% tint over surface) and `-strong` (AA vs
  white) per status.
- `--scale` is the only kiosk hook this PR adds; nothing sets it yet.
- The `.markdown` and `.working-shimmer` rules stay, rewritten on the new tokens.

### 2.2 `web/src/pipeline/plotroom.css`

The `.prdash-root { --bg … --z-* }` block becomes a **remap**:
`--bg: var(--color-canvas); --surface: var(--color-surface); --text: var(--color-ink);
--muted: var(--color-ink-muted); --border: var(--color-edge); --accent: var(--color-accent);
--done: var(--color-success); --fail: var(--color-danger); --amber: var(--color-warning);
--purple: var(--color-info); --*-strong: var(--color-*-strong); --z-*: var(--z-*)`. The dark media
block inside `.prdash-root` is deleted. `projects.css` and `initiatives.css` resolve because the
same names are now global on `:root` as well (`:root` also declares the legacy aliases
`--bg --surface --text --muted --border --accent --amber` for the duration of sub-spec C).

### 2.3 Deletions and moves

- Delete `web/tailwind.config.js` (v3 shape, ignored by v4).
- The `web/src/index.css` scaffold values for `--text --text-h --bg --border --code-bg` are removed;
  `--bg`, `--border` and `--text` survive only as the transitional aliases from §2.2 (pointing at
  tokens), `--text-h` and `--code-bg` are dropped, and the inline-code rule uses
  `--color-surface-raised` / `--color-ink`.
- `components/ui/button.tsx` moves to `web/src/ui/Button.tsx` (PR 2 rewrites it); the shadcn names
  `bg-primary`, `text-primary-foreground`, `border-input`, `bg-accent`, `ring-ring`,
  `bg-destructive`, `bg-muted`, `text-muted-foreground`, `bg-card`, `border-border`,
  `bg-background`, `text-foreground` are **search-replaced** to the new tokens in this PR
  (`bg-surface`, `text-accent-ink`, `border-edge`, `bg-accent-wash`, `ring-accent`, `bg-danger`,
  `bg-surface-raised`, `text-ink-muted`, `bg-surface`, `border-edge`, `bg-canvas`, `text-ink`).
  The 45 `border-[var(--border)]`, 11 `bg-[var(--muted)]`, 4 `var(--accent-foreground)` arbitrary
  values are replaced the same way.

### 2.4 `ThemeProvider` (`web/src/shell/theme.tsx`)

`type ThemeChoice = 'system' | 'light' | 'dark'`; persisted at `localStorage['ccswitch.theme']`;
stamps `data-theme` on `<html>` (removes it for `system`); updates both `<meta name="theme-color">`
entries; exposes `useTheme(): { choice, resolved, setChoice }`. Mounted in `main.tsx`. A `?theme=`
URL param, when present, overrides the stored choice for the page load (kiosk uses this). The UI
toggle itself lands in sub-spec B (rail) and C (Settings → General).

### 2.5 ESLint guard (`web/eslint-rules/no-raw-palette.js`)

A local rule (loaded via `eslint.config.js`) that reports, inside JSX `className` strings, template
literals passed to `cn()`, and `cva()` arguments:

- `\b(bg|text|border|ring|fill|stroke|from|to|via)-(gray|slate|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b`
- `\bdark:` · `#[0-9a-f]{3,8}\b` inside `className` · `z-\[` · `z-(10|20|30|40|50)\b`
- `\b(text|min-h|min-w|h|w)-\[\d+px\]` (arbitrary pixel sizes; the 44px target has a token)

Scope is **by folder**, not by changed file (a changed-file scope would fail on PR 1's own edits,
since the files it touches still carry legacy `dark:`/palette classes): the rule is `error` for
`web/src/ui/**` and `web/src/shell/**` from PR 1; each feature folder is added to the scope by the
sub-spec C PR that migrates it; PR 11 widens it to `web/src/**`. Allowlist:
`web/src/features/pipeline/charts/**`.

### 2.6 Build-output test (`web/src/__tests__/tokenBridge.test.ts`)

Runs `tailwindcss` via the Vite build in test mode (or reads `dist/assets/*.css` when present) and
asserts these selectors exist: `.bg-surface`, `.text-ink-muted`, `.border-edge`, `.z-drawer`,
`.min-h-target`, `.rounded-lg`, `.text-sm`; and that none of `.bg-primary`, `.border-border`,
`.text-muted-foreground` appear in source (`grep` over `web/src`).

### 2.7 Acceptance (PR 1)

- `Button` default variant renders a filled accent background (snapshot on computed style).
- Products and Templates/Presets/Profiles/Skills sections render surface/muted/accent colors outside
  `.prdash-root` (jsdom computed-style test on one element each).
- All four gates green; `tokenBridge.test.ts` green; `lint-changed` green.

## 3. PR 2 — Primitives (`web/src/ui/`)

One file per primitive, `cva` variants, `forwardRef`, no data fetching, no `dark:`, no raw palette.
Each ships with a vitest + `jest-axe` test and a story-like `*.example.tsx` fixture used by tests
(no Storybook dependency).

| Primitive | API (essentials) | Replaces in PR 2 |
|---|---|---|
| `Button` | `variant: primary\|secondary\|ghost\|danger` · `size: sm\|md\|lg` · `loading` · `iconLeft/iconRight` | current `Button` (45 sites) |
| `IconButton` | requires `aria-label`; `size: sm\|md`; `min-h/w-target` when `pointer: coarse` | header/rail/card icon buttons |
| `Input`, `Textarea`, `Select` | native elements, `invalid`, `size`; `Textarea` `autoGrow` (moves `lib/autoGrow`) | not swept in PR 2 (C) |
| `Field` | `label`, `help`, `error`, `htmlFor` wiring, `required` | new |
| `Menu` | `items: {id,label,shortcut?,icon?,disabled?}[]`, `onSelect`, `1–9` selection, arrow keys, typeahead, `role=menu` | new; consumed by B |
| `Dialog` | `open`, `onClose`, `title`, `description`, `size`, `initialFocus`; portal to `app.overlays`; `useFocusTrap`; `inert` on `#root` while open; focus restore; `Esc` closes | `AddAccountDialog`, `PermanentDeleteDialog`, `ManualDispatchDialog`, `ItemEditorDialog`, `McpServerDialog`, `ModelSwitchDialog`, `RenameDialog`, `NewSessionDialog`, the inline dialogs in `AccountsManager`, `ArchivedManager`, `AdoptPanel`, `CommandResultOverlay`, `DeployOverlay`, `LegendPanel` |
| `Sheet` | `side: right\|bottom`, `mode: overlay\|push`, `width`, same focus contract as `Dialog` | `HelpDrawer` |
| `Tabs` | `items: {id,label,count?,attention?}[]`, `value`, `onChange`, `label`, `panelId?`, `orientation`; roving tabindex, arrows, Home/End | `TabBar` (becomes a thin wrapper), `SettingsView` tablist. (`PipelineSwitcher` is a type-to-filter combobox, not a tab strip — it stays until sub-spec C's Pipeline PR rebuilds it on `Menu`.) |
| `Banner` | `tone: info\|warning\|danger\|success`, `title`, `children`, `actions`, `onDismiss?`, `role=status\|alert` by tone | `GlobalUpdateBanner`, `DaemonRestartBanner`, `GitCredentialBanner`, `AllLimitedBanner`, `UpdatePendingBanner`, `NewVersionBanner`, `DeployPanel`'s chrome |
| `BannerStack` | `items: {id, priority, node}[]`; renders sorted; one mount (B moves it into `app.banners`; in PR 2 both shells mount it at their current banner position) | the three banner mount points |
| `Card` | `tone?`, `rail?` (left status rail), `padding`, `interactive?` (hover/focus affordance) | none swept in PR 2 (C) |
| `StatusPill` | `presentation: StatusPresentation` → icon + label + tone | `StatusPill` in `UsageBadge.tsx`, `PrPill` |
| `Badge` | `tone`, `count?`, `dot?` | tab count pill, attention dot |
| `UsageRing` | `value 0–1`, `label`, `size` | `UsageBadge` ring (badge text stays) |
| `PageHeader`, `PageContent`, `EmptyState`, `Skeleton` | defined here, adopted in C; `PageHeader` sets `document.title` | `EmptyState` (1 site) |
| `Kbd` | `keys` | `HelpDrawer` |

### 3.1 `useFocusTrap`

`pipeline/hooks/useFocusTrap.ts` moves to `web/src/ui/useFocusTrap.ts` unchanged, gains `inert`
handling (`#root` gets `inert` while any `Dialog`/`Sheet` is open; a counter supports nesting) and
focus restore to the opener. The 10 inline `querySelectorAll` traps are deleted with their dialogs.

### 3.2 `statusTier.ts`

`TIER_ACCENT` values become token classes (`bg-success`, `bg-warning`, `bg-danger`, `bg-neutral`,
`bg-info`); `textClass` constants become `text-warning`, `text-danger`, `text-success`, `text-ink-muted`.
The table shape is unchanged; `StatusPill` consumes it.

### 3.3 Acceptance (PR 2)

- `grep -rl 'fixed inset-0' web/src` → only `ui/Dialog.tsx`, `ui/Sheet.tsx`.
- `grep -rl 'querySelectorAll<HTMLElement>' web/src` → only `ui/useFocusTrap.ts`.
- All overlays open/close with focus restored (existing dialog tests keep passing on the new
  primitives; `HelpDrawer` and `LegendPanel` tests updated to `Sheet`/`Dialog` roles).
- `jest-axe` green for every primitive example.
- `lint-changed` green; both shells visually unchanged apart from color values.

## 4. Testing summary

| Test | Asserts |
|---|---|
| `tokenBridge.test.ts` | compiled CSS contains token utilities; source contains no shadcn names |
| `ui/*.test.tsx` | behavior + axe per primitive; `Menu` `1–9`/arrows; `Dialog` inert+restore; `Tabs` roving tabindex |
| `statusTier.test.ts` | tiers → token classes |
| existing shell/dialog tests | unchanged behavior on new primitives |

## 5. Risks

- **Color drift during the remap:** plotroom's tokens were AA-tuned; using them as the global set
  keeps contrast, but the old scaffold `--text #6b6375` body color disappears — expect a slightly
  darker body ink app-wide. Intentional.
- **`inert` support:** all target browsers support it; jsdom does not — the trap test stubs it.
- **Search-replace of shadcn names** touches ~200 sites across ~40 files; it is mechanical and
  reviewed by the build-output test, not by eye.
