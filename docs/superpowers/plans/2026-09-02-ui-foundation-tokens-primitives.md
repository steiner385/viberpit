# UI Foundation (Tokens + Primitives) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the design-token layer real (Tailwind v4 `@theme` that actually compiles), delete the dead shadcn scaffold, add a theme provider and a lint guard, then ship the primitive set under `web/src/ui/` and move every overlay, banner, tab strip and pill onto it — with **no layout change** to the two existing shells.

**Architecture:** Phase 1 (PR 1) rewrites `web/src/index.css` around raw theme variables (`--t-*`, `--layer-*`, `--fs-*`, `--r-*`, `--target`, `--scale`) that Tailwind's `@theme inline` block turns into utilities (`bg-surface`, `z-drawer`, `min-h-target`…); `.prdash-root` becomes a remap onto those variables; a `ThemeProvider` stamps `data-theme`; an ESLint rule bans raw palette classes in the new folders. Phase 2 (PR 2) adds one file per primitive in `web/src/ui/`, promotes `useFocusTrap`, and migrates the 15 `role="dialog"` sites, 7 banners, 2 tab strips and the status/PR pills onto them.

**Tech Stack:** React 19, TypeScript ~6, Vite 8 + `@tailwindcss/vite` 4.3 (Tailwind v4 — **no `tailwind.config.js`**), `class-variance-authority`, `tailwind-merge`/`clsx` (`cn`), `lucide-react`, vitest 4 + `@testing-library/react` + `jest-axe`, ESLint 10 flat config + `typescript-eslint`.

**Spec:** `docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md` (sub-spec A) — vocabulary and decisions in the umbrella `docs/superpowers/specs/2026-09-02-ui-target-state-design.md`.

## Global Constraints

- Work in a git worktree branched from `origin/master` (`git fetch && git checkout -b <branch> origin/master`); never from local `master`.
- Commands run from the **repo root**; never `cd web/`. Web gates: `npm --prefix web test`, `npm --prefix web run typecheck`, `npm --prefix web run lint`; a single test file: `npm --prefix web run test -- src/path/to/file.test.tsx`. In a fresh worktree first run `ln -s /home/tony/GitHub/ccswitch/web/node_modules web/node_modules`.
- The full gate before every push is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/` (backend, untouched here — run them anyway) plus the three web gates.
- New code under `web/src/ui/**` and `web/src/shell/**`: **no `dark:` variants, no raw palette classes (`gray-*`, `blue-*`, `amber-*`…), no hex literals, no `z-[..]`/`z-50`, no `min-h-[44px]`** — use tokens (`bg-surface`, `text-ink-muted`, `border-edge`, `z-dialog`, `min-h-target`). The ESLint rule from Task 7 enforces this.
- Icons: `lucide-react` only. Path alias `@/` = `web/src/`.
- Every primitive: one file, `cva` variants where there are variants, `forwardRef`, `displayName`, no data fetching, a vitest file with a `jest-axe` assertion.
- Do not change behavior of the two shells (`Dashboard.tsx`, `DesktopShell.tsx`) in this plan beyond swapping components; their deletion is sub-spec B.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. PRs: open non-draft, then `gh pr merge <#> --squash --auto`; body ends with the Claude Code trailer and cites the audit finding ids it closes (`Closes F4.1, F4.2` in prose — these are not GitHub issues).
- Legacy CSS variable names `--bg --surface --text --muted --border --accent --done --fail --amber --purple --accent-strong --done-strong --amber-strong` stay defined on `:root` as **aliases** until sub-spec C removes the last consumer.

---

## File Structure

**Phase 1 — PR 1 (`feat(ui): token layer`)**

| File | Responsibility |
|---|---|
| `web/src/index.css` (rewrite) | raw theme variables on `:root` (light) + dark override; `@theme inline` bridge; legacy aliases; base rules |
| `web/src/pipeline/plotroom.css:14-45` (modify) | `.prdash-root` token block → remap onto `--t-*` / `--layer-*`; dark block deleted |
| `web/tailwind.config.js` (delete) | v3 config ignored by v4 |
| `web/index.html` (modify) | add `<div id="overlays"></div>` portal root (used by Phase 2) |
| `web/src/ui/Button.tsx` (move from `components/ui/button.tsx`) | same API, token classes |
| `web/src/shell/theme.tsx` (create) | `ThemeProvider`, `useTheme`, `readStoredTheme`, `applyTheme` |
| `web/src/main.tsx` (modify) | mount `ThemeProvider` |
| `web/eslint-rules/no-raw-palette.js` (create) | the lint rule |
| `web/eslint.config.js` (modify) | register rule for `src/ui/**`, `src/shell/**` |
| `web/vitest.config.ts` (modify) | Tailwind plugin + CSS processing so the bridge test compiles real CSS |
| `web/src/__tests__/tokenBridge.test.ts` (create) | compiled-CSS assertions + "no dead names in source" |
| `web/src/shell/theme.test.tsx`, `web/eslint-rules/no-raw-palette.test.ts` (create) | unit tests |
| 27 component files (modify) | mechanical class-name replacement (Task 5) |

**Phase 2 — PR 2 (`feat(ui): primitives`)**

| File | Responsibility |
|---|---|
| `web/src/ui/useFocusTrap.ts` (move from `pipeline/hooks/`) | trap + `inert` on `#root` + focus restore |
| `web/src/ui/Button.tsx` (rewrite), `IconButton.tsx` | variants `primary/secondary/ghost/danger` |
| `web/src/ui/Input.tsx`, `Textarea.tsx`, `Select.tsx`, `Field.tsx` | form controls |
| `web/src/ui/Menu.tsx` | keyboard menu with `1–9` selection |
| `web/src/ui/Dialog.tsx`, `Sheet.tsx`, `portal.ts` | overlays |
| `web/src/ui/Tabs.tsx` | generalized `TabBar` |
| `web/src/ui/Banner.tsx`, `BannerStack.tsx` | banners |
| `web/src/ui/Card.tsx`, `StatusPill.tsx`, `Badge.tsx`, `UsageRing.tsx` | surfaces and pills |
| `web/src/ui/PageHeader.tsx`, `PageContent.tsx`, `EmptyState.tsx`, `Skeleton.tsx`, `Kbd.tsx` | page-contract primitives (adopted in sub-spec C) |
| `web/src/session/statusTier.ts` (modify) | tier → token classes |
| 15 dialog sites, 7 banners, `TabBar.tsx`, `SettingsView.tsx`, `UsageBadge.tsx`, `PrPill.tsx`, `EmptyState.tsx`, `HelpDrawer.tsx` (modify) | adoption |

---

# Phase 1 — PR 1: token layer

### Task 1: Token-bridge test harness (failing)

**Files:**
- Modify: `web/vitest.config.ts`
- Create: `web/src/__tests__/tokenBridge.test.ts`

**Interfaces:**
- Produces: a test that imports `@/index.css?inline` and asserts compiled selectors. Tailwind v4 only emits a utility when the class name appears in a scanned file, so the test carries a `PROBE` string listing every class it asserts — that string is what makes the scanner generate them.

- [ ] **Step 1: Enable CSS processing + the Tailwind plugin in vitest**

Replace `web/vitest.config.ts` with:

```ts
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  // The Tailwind plugin is here so `import css from '@/index.css?inline'` in
  // tokenBridge.test.ts yields the *compiled* stylesheet — the same bridge check
  // the 2026-09-02 audit had to do by hand against web/dist.
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    exclude: ['node_modules', 'dist', 'e2e/**'],
    // Only index.css is processed (everything else stays an empty string, as before).
    css: { include: [/src\/index\.css$/] },
  },
})
```

- [ ] **Step 2: Write the failing test**

Create `web/src/__tests__/tokenBridge.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import css from '@/index.css?inline'

// Tailwind v4 generates a utility only when its class name appears in a scanned
// source file. This constant IS that appearance for the token utilities below —
// keep it in sync with REQUIRED.
export const PROBE =
  'bg-canvas bg-surface bg-surface-raised text-ink text-ink-muted text-ink-faint border-edge border-edge-strong ' +
  'bg-accent text-accent-ink bg-accent-wash ring-accent ' +
  'bg-success bg-success-wash text-warning bg-danger bg-info bg-neutral ' +
  'z-raised z-sticky z-nav z-popover z-overlay z-drawer z-dialog z-palette z-toast ' +
  'min-h-target min-w-target rounded-sm rounded-md rounded-lg rounded-pill text-2xs text-xs text-sm text-base'

const REQUIRED = PROBE.split(' ')

// Names the audit found shipping zero CSS. None may remain in source.
const DEAD = [
  'bg-primary', 'text-primary-foreground', 'border-input', 'text-accent-foreground', 'ring-ring',
  'bg-destructive', 'text-destructive-foreground', 'bg-muted', 'text-muted-foreground', 'bg-card',
  'border-border', 'bg-background', 'text-foreground',
  'border-[var(--border)]', 'border-[--border]', 'bg-[var(--muted)]', 'bg-[var(--accent)]',
  'text-[var(--accent-foreground)]', 'bg-[--bg]',
]

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(tsx?|css)$/.test(name)) out.push(p)
  }
  return out
}

describe('token bridge', () => {
  it('compiles every token utility the app relies on', () => {
    for (const cls of REQUIRED) {
      const sel = '.' + cls.replace(/[.\[\]/]/g, (m) => '\\' + m)
      expect(css, `missing utility ${cls}`).toMatch(new RegExp(sel.replace(/\\/g, '\\\\') + '\\s*\\{'))
    }
  })

  it('does not emit the dead shadcn names (they were never defined)', () => {
    for (const cls of ['bg-primary', 'border-border', 'text-muted-foreground']) {
      expect(css).not.toMatch(new RegExp('\\.' + cls + '\\s*\\{'))
    }
  })

  it('has no dead class names left in source', () => {
    const src = join(__dirname, '..')
    const offenders: string[] = []
    for (const file of walk(src)) {
      if (file.endsWith('tokenBridge.test.ts')) continue
      const text = readFileSync(file, 'utf8')
      for (const dead of DEAD) {
        const re = new RegExp('(^|[\\s"\'`:])' + dead.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&') + '(?![\\w-])')
        if (re.test(text)) offenders.push(`${file.slice(src.length + 1)} uses ${dead}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
```

- [ ] **Step 3: Run it to verify it fails**

Run: `npm --prefix web run test -- src/__tests__/tokenBridge.test.ts`
Expected: FAIL — `missing utility bg-canvas` (no `@theme` yet) and a non-empty `offenders` list (14 + 13 files).

If instead the import yields an **empty string**, the CSS was not processed: confirm `css.include` matches `src/index.css` (the regex above) and that `@tailwindcss/vite` resolves from `web/node_modules`.

- [ ] **Step 4: Commit**

```bash
git add web/vitest.config.ts web/src/__tests__/tokenBridge.test.ts
git commit -m "test(ui): token-bridge test asserts compiled Tailwind utilities (red)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `@theme` token layer in `index.css`

**Files:**
- Modify: `web/src/index.css` (full rewrite)
- Test: `web/src/__tests__/tokenBridge.test.ts` (first `it`)

**Interfaces:**
- Produces: raw variables `--t-<name>` (colors), `--layer-<name>` (z), `--fs-<name>` (font sizes), `--r-<name>` (radii), `--target` (44px), `--scale` (1); Tailwind utilities `bg-/text-/border-/ring-<color-token>`, `z-<layer>`, `text-<size>`, `rounded-<r>`, `min-h-target`; legacy aliases `--bg --surface --text --muted --border --accent --done --fail --amber --purple --accent-strong --done-strong --amber-strong`.

- [ ] **Step 1: Rewrite `web/src/index.css`**

```css
@import "tailwindcss";

/* ───────────────────────────────────────────────────────────────────────────
   Design tokens — the ONLY place a color, layer, radius, target size or type
   size is defined (spec: docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md).

   Two layers:
     1. Raw theme variables on :root (light) with a dark override. These are
        plain CSS custom properties, always present, switchable at runtime by
        [data-theme] or prefers-color-scheme.
     2. The @theme inline block, which maps them onto Tailwind namespaces so
        `bg-surface`, `z-drawer`, `min-h-target`, `text-sm` compile to utilities
        that read the raw variable. `inline` makes the utility reference the
        raw var directly, so theme switching never needs a class change.
   ─────────────────────────────────────────────────────────────────────────── */
:root {
  --scale: 1;                    /* kiosk sets 1.25 (sub-spec B) */

  /* surfaces & ink */
  --t-canvas: #f6f7f9;  --t-surface: #ffffff;  --t-surface-raised: #eef1f5;
  --t-edge: #d9dde3;    --t-edge-strong: #b9c1cc;
  --t-ink: #1a1f29;     --t-ink-muted: #69707d;  --t-ink-faint: #8b929e;
  --t-accent: #2563eb;  --t-accent-ink: #ffffff; --t-accent-wash: #e8effc;

  /* semantic status — base is AA as text on surface; -strong is AA behind white
     text; -wash is a tint for backgrounds */
  --t-success: #15803d; --t-success-wash: #e6f4ea; --t-success-strong: #15803d;
  --t-warning: #9a5d00; --t-warning-wash: #fbf0dc; --t-warning-strong: #9a5d00;
  --t-danger:  #d23b3b; --t-danger-wash:  #fbe9e7; --t-danger-strong:  #b42318;
  --t-info:    #4f6d8f; --t-info-wash:    #e9eef5; --t-info-strong:    #3d5674;
  --t-neutral: #69707d; --t-neutral-wash: #eceff3; --t-neutral-strong: #5b6472;

  /* layers (lowest → highest) */
  --layer-raised: 1; --layer-sticky: 20; --layer-nav: 25; --layer-popover: 30;
  --layer-overlay: 40; --layer-drawer: 50; --layer-dialog: 60; --layer-palette: 1000; --layer-toast: 1100;

  /* metrics */
  --target: 44px;
  --r-sm: 4px; --r-md: 6px; --r-lg: 10px; --r-pill: 999px;

  /* type scale (rem × --scale) */
  --fs-2xs: calc(0.6875rem * var(--scale)); --fs-xs: calc(0.75rem * var(--scale));
  --fs-sm: calc(0.875rem * var(--scale));   --fs-base: calc(1rem * var(--scale));
  --fs-lg: calc(1.125rem * var(--scale));   --fs-xl: calc(1.25rem * var(--scale));
  --fs-2xl: calc(1.5rem * var(--scale));

  /* Legacy aliases — consumed by plotroom.css / projects.css / initiatives.css
     and a few settings sections until sub-spec C removes the last use. */
  --bg: var(--t-canvas); --surface: var(--t-surface); --text: var(--t-ink);
  --muted: var(--t-ink-muted); --border: var(--t-edge); --accent: var(--t-accent);
  --done: var(--t-success); --fail: var(--t-danger); --amber: var(--t-warning); --purple: var(--t-info);
  --accent-strong: var(--t-accent); --done-strong: var(--t-success-strong); --amber-strong: var(--t-warning-strong);

  font: 16px/1.5 var(--font-sans);
  color-scheme: light dark;
  color: var(--t-ink);
  background: var(--t-canvas);
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* Dark values. Three viewer states: explicit data-theme="dark" | explicit
   data-theme="light" | nothing stamped (follow the OS). */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --t-canvas: #14171c;  --t-surface: #1d2128;  --t-surface-raised: #262b34;
    --t-edge: #323843;    --t-edge-strong: #46505e;
    --t-ink: #e8eaee;     --t-ink-muted: #98a0ac;  --t-ink-faint: #6f7886;
    --t-accent: #5c9bff;  --t-accent-ink: #0b1416; --t-accent-wash: #1c2a44;
    --t-success: #3dbb66; --t-success-wash: #17301f;
    --t-warning: #e0a23b; --t-warning-wash: #3a2d16;
    --t-danger:  #ef6262; --t-danger-wash:  #3a1d1b;
    --t-info:    #8aa4c8; --t-info-wash:    #1f2836;
    --t-neutral: #98a0ac; --t-neutral-wash: #252a33;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --t-canvas: #14171c;  --t-surface: #1d2128;  --t-surface-raised: #262b34;
  --t-edge: #323843;    --t-edge-strong: #46505e;
  --t-ink: #e8eaee;     --t-ink-muted: #98a0ac;  --t-ink-faint: #6f7886;
  --t-accent: #5c9bff;  --t-accent-ink: #0b1416; --t-accent-wash: #1c2a44;
  --t-success: #3dbb66; --t-success-wash: #17301f;
  --t-warning: #e0a23b; --t-warning-wash: #3a2d16;
  --t-danger:  #ef6262; --t-danger-wash:  #3a1d1b;
  --t-info:    #8aa4c8; --t-info-wash:    #1f2836;
  --t-neutral: #98a0ac; --t-neutral-wash: #252a33;
  color-scheme: dark;
}
:root[data-theme="light"] { color-scheme: light; }

/* Tailwind bridge. Namespaces: --color-* → bg-/text-/border-/ring-/fill-…,
   --z-index-* → z-*, --spacing-* → h-/w-/min-h-/p-…, --radius-* → rounded-*,
   --text-* → text-<size>, --font-* → font-<family>. */
@theme inline {
  --color-canvas: var(--t-canvas);
  --color-surface: var(--t-surface);
  --color-surface-raised: var(--t-surface-raised);
  --color-edge: var(--t-edge);
  --color-edge-strong: var(--t-edge-strong);
  --color-ink: var(--t-ink);
  --color-ink-muted: var(--t-ink-muted);
  --color-ink-faint: var(--t-ink-faint);
  --color-accent: var(--t-accent);
  --color-accent-ink: var(--t-accent-ink);
  --color-accent-wash: var(--t-accent-wash);
  --color-success: var(--t-success);  --color-success-wash: var(--t-success-wash);  --color-success-strong: var(--t-success-strong);
  --color-warning: var(--t-warning);  --color-warning-wash: var(--t-warning-wash);  --color-warning-strong: var(--t-warning-strong);
  --color-danger:  var(--t-danger);   --color-danger-wash:  var(--t-danger-wash);   --color-danger-strong:  var(--t-danger-strong);
  --color-info:    var(--t-info);     --color-info-wash:    var(--t-info-wash);     --color-info-strong:    var(--t-info-strong);
  --color-neutral: var(--t-neutral);  --color-neutral-wash: var(--t-neutral-wash);  --color-neutral-strong: var(--t-neutral-strong);

  --z-index-raised: var(--layer-raised);   --z-index-sticky: var(--layer-sticky);
  --z-index-nav: var(--layer-nav);         --z-index-popover: var(--layer-popover);
  --z-index-overlay: var(--layer-overlay); --z-index-drawer: var(--layer-drawer);
  --z-index-dialog: var(--layer-dialog);   --z-index-palette: var(--layer-palette);
  --z-index-toast: var(--layer-toast);

  --spacing-target: var(--target);

  --radius-sm: var(--r-sm); --radius-md: var(--r-md); --radius-lg: var(--r-lg); --radius-pill: var(--r-pill);

  --text-2xs: var(--fs-2xs); --text-xs: var(--fs-xs); --text-sm: var(--fs-sm); --text-base: var(--fs-base);
  --text-lg: var(--fs-lg);   --text-xl: var(--fs-xl); --text-2xl: var(--fs-2xl);

  --font-sans: system-ui, 'Segoe UI', Roboto, sans-serif;
  --font-mono: ui-monospace, Consolas, monospace;
}

/* The app is a fixed-viewport column: the *document* must never scroll. Without
   this clamp, any descendant that (even transiently) lays out past the viewport
   makes the page itself scrollable, and a focus()/scrollIntoView then scrolls
   the whole app partly off-screen — leaving dead whitespace below the composer
   that persists until reload. Interior panes own all scrolling via
   `overflow-y-auto` regions. */
html, body {
  height: 100%;
  overflow: hidden;
}

#root {
  /* Full-bleed; the shells size with `flex-1 min-h-0`, never their own viewport
     units. dvh so the Android keyboard (interactive-widget=resizes-content, see
     index.html) shrinks the layout instead of covering the composer. */
  width: 100%;
  text-align: left;
  height: 100dvh;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  /* viewport-fit=cover extends the CSS viewport into notch/side-bar regions in
     landscape; keep horizontal content clear of those gutters. */
  padding-left: env(safe-area-inset-left);
  padding-right: env(safe-area-inset-right);
}

body {
  margin: 0;
}

/* Inline code (excludes <code> inside <pre>, which keeps highlight.js styling). */
@layer base {
  :not(pre) > code {
    font-family: var(--font-mono);
    font-size: 0.875em;
    padding: 0.1em 0.35em;
    border-radius: var(--r-sm);
    background: var(--t-surface-raised);
    color: var(--t-ink);
    overflow-wrap: anywhere;
  }
}

/* Markdown rendered by AssistantText — one tight type scale anchored to text-sm. */
@layer components {
  .markdown { font-size: var(--fs-sm); line-height: 1.6; }
  .markdown > * + * { margin-top: 0.75em; }
  .markdown h1 { font-size: var(--fs-xl); font-weight: 600; line-height: 1.3; }
  .markdown h2 { font-size: var(--fs-lg); font-weight: 600; line-height: 1.3; }
  .markdown h3 { font-size: var(--fs-base); font-weight: 600; line-height: 1.4; }
  .markdown h4, .markdown h5, .markdown h6 { font-size: var(--fs-sm); font-weight: 600; }
  .markdown ul { list-style: disc; padding-left: 1.4em; }
  .markdown ol { list-style: decimal; padding-left: 1.4em; }
  .markdown li { margin-top: 0.25em; }
  .markdown li > ul, .markdown li > ol { margin-top: 0.25em; }
  .markdown a { color: var(--t-accent); text-decoration: underline; overflow-wrap: anywhere; }
  .markdown blockquote { border-left: 3px solid var(--t-edge); padding-left: 0.75em; color: var(--t-ink-muted); }
  .markdown pre { overflow-x: auto; border-radius: var(--r-md); padding: 0.625em 0.75em; font-size: var(--fs-xs); line-height: 1.5; }
  .markdown pre code { background: transparent; padding: 0; font-size: inherit; }
  .markdown table { display: block; overflow-x: auto; max-width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
  .markdown th, .markdown td { border: 1px solid var(--t-edge); padding: 0.3em 0.5em; text-align: left; }
  .markdown hr { border: 0; border-top: 1px solid var(--t-edge); }
}

/* WorkingIndicator phrase shimmer (background-clip:text). The component drops
   the class under prefers-reduced-motion; the media query is belt-and-suspenders. */
@layer components {
  .working-shimmer {
    background: linear-gradient(90deg, var(--t-ink-muted) 0%, var(--t-ink-muted) 40%, var(--t-ink) 50%, var(--t-ink-muted) 60%, var(--t-ink-muted) 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    animation: working-shimmer 1.8s linear infinite;
  }
}
@keyframes working-shimmer {
  from { background-position: 200% 0; }
  to   { background-position: -200% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .working-shimmer { animation: none; background: none; -webkit-background-clip: border-box; background-clip: border-box; color: inherit; }
}
```

- [ ] **Step 2: Run the bridge test**

Run: `npm --prefix web run test -- src/__tests__/tokenBridge.test.ts`
Expected: first `it` PASSES (all `REQUIRED` selectors present); third `it` still FAILS (dead names in source — Task 5).

If a `z-*` utility is missing: Tailwind's z-index namespace is `--z-index-*` — verify the `@theme inline` block uses that exact prefix. If `min-h-target` is missing: the spacing namespace is `--spacing-*`.

- [ ] **Step 3: Run the full web test suite**

Run: `npm --prefix web test`
Expected: everything that passed before still passes (the CSS is only loaded by `main.tsx`, which tests do not import). If `tokenBridge` is the only red file, continue.

- [ ] **Step 4: Commit**

```bash
git add web/src/index.css
git commit -m "feat(ui): real @theme token layer — surfaces, status, layers, metrics, type scale

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Remap `.prdash-root` onto the global tokens

**Files:**
- Modify: `web/src/pipeline/plotroom.css:14-45`
- Create: `web/src/pipeline/__tests__/plotroomTokens.test.ts`

**Interfaces:**
- Produces: `.prdash-root` defines its variables as `var(--t-*)`/`var(--layer-*)` only — no hex, no dark block. `projects.css` and `initiatives.css` (which use `--surface`, `--muted`, `--accent`, `--amber`, `--border`, `--text`, `--bg`) resolve everywhere through the `:root` aliases from Task 2.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/pipeline/__tests__/plotroomTokens.test.ts
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const css = readFileSync(join(__dirname, '..', 'plotroom.css'), 'utf8')
const root = readFileSync(join(__dirname, '..', '..', 'index.css'), 'utf8')

// The first `.prdash-root { … }` block is the token block.
const block = css.slice(css.indexOf('.prdash-root {'), css.indexOf('}', css.indexOf('.prdash-root {')) + 1)

describe('plotroom token remap', () => {
  it('defines no color literals in the .prdash-root token block', () => {
    expect(block).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
  })
  it('remaps every legacy token onto a global one', () => {
    for (const v of ['--bg', '--surface', '--text', '--muted', '--border', '--accent', '--done', '--fail', '--purple', '--amber',
      '--accent-strong', '--done-strong', '--amber-strong',
      '--z-raised', '--z-sticky-header', '--z-bottom-nav', '--z-popover', '--z-overlay', '--z-drawer', '--z-command-palette']) {
      expect(block, v).toMatch(new RegExp(v.replace(/-/g, '\\-') + ':\\s*var\\(--(t|layer)-'))
    }
  })
  it('has no dark-scheme override for .prdash-root (the global tokens flip instead)', () => {
    expect(css).not.toMatch(/prefers-color-scheme:\s*dark\)\s*\{\s*\.prdash-root/)
  })
  it('keeps the legacy aliases on :root for projects.css / initiatives.css', () => {
    for (const v of ['--bg', '--surface', '--text', '--muted', '--border', '--accent', '--amber', '--done', '--fail', '--purple']) {
      expect(root).toMatch(new RegExp(v.replace(/-/g, '\\-') + ':\\s*var\\(--t-'))
    }
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm --prefix web run test -- src/pipeline/__tests__/plotroomTokens.test.ts`
Expected: FAIL — hex literals present; dark block present.

- [ ] **Step 3: Replace the token block and delete the dark block**

In `web/src/pipeline/plotroom.css`, replace everything from the line `.prdash-root {` (line 14, the block that begins `color-scheme: light dark;`) through the closing `}` of the `@media (prefers-color-scheme: dark) { .prdash-root { … } }` block (line 45) with:

```css
.prdash-root {
  /* Token REMAP (sub-spec A §2.2): plotroom's names now point at the global
     tokens in src/index.css. No literals here — light/dark flip at :root. */
  --bg: var(--t-canvas); --surface: var(--t-surface); --text: var(--t-ink); --muted: var(--t-ink-muted);
  --border: var(--t-edge); --accent: var(--t-accent); --done: var(--t-success); --fail: var(--t-danger);
  --purple: var(--t-info); --amber: var(--t-warning);
  --accent-strong: var(--t-accent); --done-strong: var(--t-success-strong); --amber-strong: var(--t-warning-strong);
  /* z-index layer system → global layers */
  --z-raised: var(--layer-raised);
  --z-sticky-header: var(--layer-sticky);
  --z-bottom-nav: var(--layer-nav);
  --z-popover: var(--layer-popover);
  --z-overlay: var(--layer-overlay);
  --z-drawer: var(--layer-drawer);
  --z-command-palette: var(--layer-palette);
  font-family: var(--font-sans);
}
```

Keep the header comment line 1 and everything after the old line 45 unchanged.

- [ ] **Step 4: Run the test and the pipeline tests**

Run: `npm --prefix web run test -- src/pipeline`
Expected: PASS (no pipeline test asserts on hex colors; if one does, update the expectation to the `var(--t-…)` form).

- [ ] **Step 5: Commit**

```bash
git add web/src/pipeline/plotroom.css web/src/pipeline/__tests__/plotroomTokens.test.ts
git commit -m "feat(ui): remap .prdash-root tokens onto the global @theme layer

Products/Initiatives/Pipeline now resolve --surface/--muted/--accent everywhere (audit F4.2).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Delete the v3 config, move `Button` to `web/src/ui/`, add the portal root

**Files:**
- Delete: `web/tailwind.config.js`, `web/src/components/ui/button.tsx`
- Create: `web/src/ui/Button.tsx`, `web/src/ui/Button.test.tsx`
- Modify: the 13 importers (`web/src/components/{AccountsManager,AddAccountDialog,ArchivedManager,Dashboard,EmptyState,PermanentDeleteDialog,ScheduleEditor,SessionList}.tsx`, `web/src/components/session/ModelSwitchDialog.tsx`, `web/src/components/settings/{GeneralSection,HooksSection,NotificationsSection,SnapshotsSection}.tsx`), `web/index.html`

**Interfaces:**
- Produces: `import { Button, buttonVariants, type ButtonProps } from '@/ui/Button'` — same API as before (`variant: default|outline|ghost|destructive`, `size: default|sm|lg|icon`); Task 10 renames the variants.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Button.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Button } from './Button'

expect.extend(toHaveNoViolations)

describe('Button', () => {
  it('renders the default variant on the accent token', () => {
    render(<Button>Save</Button>)
    const btn = screen.getByRole('button', { name: 'Save' })
    expect(btn.className).toContain('bg-accent')
    expect(btn.className).toContain('text-accent-ink')
    expect(btn.className).not.toMatch(/bg-primary|dark:/)
  })
  it('renders outline on surface/edge tokens', () => {
    render(<Button variant="outline">Cancel</Button>)
    expect(screen.getByRole('button').className).toContain('border-edge')
  })
  it('has no axe violations', async () => {
    const { container } = render(<Button>Go</Button>)
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm --prefix web run test -- src/ui/Button.test.tsx`
Expected: FAIL — cannot resolve `./Button`.

- [ ] **Step 3: Create `web/src/ui/Button.tsx`**

```tsx
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

// Same API as the old components/ui/button.tsx; only the classes changed —
// every color is a token so light/dark flip at :root (no dark: variants).
const buttonVariants = cva(
  'inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default: 'bg-accent text-accent-ink hover:bg-accent/90',
        outline: 'border border-edge bg-surface text-ink hover:bg-surface-raised',
        ghost: 'text-ink hover:bg-surface-raised',
        destructive: 'bg-danger text-white hover:bg-danger/90',
      },
      size: {
        default: 'h-10 px-4 py-2',
        sm: 'h-9 rounded-md px-3',
        lg: 'h-11 rounded-md px-8',
        icon: 'h-10 w-10',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
  ),
)
Button.displayName = 'Button'

export { Button, buttonVariants }
```

- [ ] **Step 4: Rewrite the imports, delete the old files, add the portal root**

```bash
grep -rl "@/components/ui/button" web/src | xargs sed -i "s#@/components/ui/button#@/ui/Button#g"
git rm -q web/src/components/ui/button.tsx web/tailwind.config.js
```

In `web/index.html`, after `<div id="root"></div>` add:

```html
    <!-- Portal root for Dialog/Sheet/Menu (web/src/ui). Sits outside #root so
         `inert` on #root while an overlay is open never inerts the overlay. -->
    <div id="overlays"></div>
```

- [ ] **Step 5: Run tests + typecheck**

Run: `npm --prefix web run test -- src/ui/Button.test.tsx && npm --prefix web run typecheck`
Expected: PASS; `tsc` clean (no remaining `components/ui/button` imports — `grep -rn "components/ui" web/src` returns nothing).

- [ ] **Step 6: Commit**

```bash
git add -A web/src/ui web/src/components web/index.html
git commit -m "refactor(ui): move Button to web/src/ui on tokens; drop dead tailwind.config.js; add #overlays portal root

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Replace the dead shadcn names and `var(--x)` escape hatches

**Files:**
- Modify (mechanical): `web/src/App.tsx`, `web/src/automations/AutomationsTab.tsx`, `web/src/components/{AutomationCard,AutomationView,BacklogGroupsPanel,BrowserTab,DesktopShell,ManualDispatchDialog,ScheduleEditor}.tsx`, `web/src/components/session/{BlockRow,SessionBody}.tsx`, `web/src/components/settings/{NotificationsToggle,SnapshotsSection,PermissionProfilesSection,PresetsSection,SettingsView,TemplatesSection}.tsx`, `web/src/components/library/{CommandsSection,ItemEditorDialog,McpSection,PluginsSection,SkillsSection}.tsx`, `web/src/pipeline/{LegendPanel,MetroTrack,Waterfall}.tsx`
- Test: `web/src/__tests__/tokenBridge.test.ts` (third `it`)

**Interfaces:**
- Consumes: the utilities from Task 2.

- [ ] **Step 1: Confirm the test is red**

Run: `npm --prefix web run test -- src/__tests__/tokenBridge.test.ts`
Expected: third `it` FAILS listing every offender.

- [ ] **Step 2: Apply the mapping (order matters — old `bg-accent` first)**

```bash
FILES=$(grep -rlE 'bg-primary|text-primary-foreground|border-input|bg-accent|text-accent-foreground|ring-ring|bg-destructive|text-destructive-foreground|bg-muted|text-muted-foreground|bg-card|border-border|bg-background|text-foreground|var\(--(border|muted|accent|accent-foreground|bg)\)|\[--border\]|\[--bg\]' web/src --include='*.tsx' --include='*.ts' | grep -v __tests__ | grep -v tokenBridge)
echo "$FILES" | xargs sed -i -E \
  -e 's/\btext-accent-foreground\b/text-ink/g' \
  -e 's/\bbg-accent(?!-)/bg-accent-wash/g; s/\bbg-accent\b([^-])/bg-accent-wash\1/g' \
  -e 's/\bbg-primary\b/bg-accent/g' \
  -e 's/\btext-primary-foreground\b/text-accent-ink/g' \
  -e 's/\bborder-input\b/border-edge/g' \
  -e 's/\bring-ring\b/ring-accent/g' \
  -e 's/\bbg-destructive\b/bg-danger/g' \
  -e 's/\btext-destructive-foreground\b/text-white/g' \
  -e 's/\bbg-muted\b/bg-surface-raised/g' \
  -e 's/\btext-muted-foreground\b/text-ink-muted/g' \
  -e 's/\bbg-card\b/bg-surface/g' \
  -e 's/\bborder-border\b/border-edge/g' \
  -e 's/\bbg-background\b/bg-canvas/g' \
  -e 's/\btext-foreground\b/text-ink/g' \
  -e 's/border-\[var\(--border\)\]/border-edge/g' \
  -e 's/border-\[--border\]/border-edge/g' \
  -e 's/bg-\[var\(--muted\)\]/bg-surface-raised/g' \
  -e 's/bg-\[var\(--accent\)\]/bg-accent/g' \
  -e 's/text-\[var\(--accent-foreground\)\]/text-accent-ink/g' \
  -e 's/bg-\[--bg\]/bg-canvas/g'
```

GNU `sed -E` has no lookahead: the second expression's first half is a no-op on GNU sed and the second half (`\bbg-accent\b([^-])`) does the work. After running, verify by hand: `grep -rn 'bg-accent-wash' web/src` should list exactly the 4 former `bg-accent` sites (hover states in `AutomationCard`/`BlockRow`/…), and `grep -rn '\bbg-accent\b' web/src` the former `bg-primary` + `bg-[var(--accent)]` sites.

- [ ] **Step 3: Run the bridge test, then the whole suite and typecheck**

Run: `npm --prefix web run test -- src/__tests__/tokenBridge.test.ts && npm --prefix web test && npm --prefix web run typecheck`
Expected: all PASS. Snapshot/class assertions in existing tests that named a dead class (search the failures for `muted-foreground`, `border-border`) are updated to the new name.

- [ ] **Step 4: Commit**

```bash
git add -A web/src
git commit -m "refactor(ui): replace never-compiled shadcn class names with token utilities

Button default variant, border-border and text-muted-foreground shipped no CSS under Tailwind v4 (audit F4.1).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `ThemeProvider`

**Files:**
- Create: `web/src/shell/theme.tsx`, `web/src/shell/theme.test.tsx`
- Modify: `web/src/main.tsx`

**Interfaces:**
- Produces:
  ```ts
  type ThemeChoice = 'system' | 'light' | 'dark'
  const THEME_STORAGE_KEY = 'ccswitch.theme'
  function readStoredTheme(search?: string): ThemeChoice        // ?theme= wins for the page load
  function applyTheme(choice: ThemeChoice): void                // stamps <html data-theme>, meta theme-color
  function ThemeProvider(props: { children: ReactNode }): JSX.Element
  function useTheme(): { choice: ThemeChoice; resolved: 'light' | 'dark'; setChoice: (c: ThemeChoice) => void }
  ```

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/shell/theme.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { ThemeProvider, useTheme, readStoredTheme, applyTheme, THEME_STORAGE_KEY } from './theme'

function Probe() {
  const { choice, resolved, setChoice } = useTheme()
  return (
    <div>
      <span data-testid="choice">{choice}</span>
      <span data-testid="resolved">{resolved}</span>
      <button onClick={() => setChoice('dark')}>dark</button>
      <button onClick={() => setChoice('system')}>system</button>
    </div>
  )
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.head.innerHTML =
    '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">' +
    '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#16171d">'
})

describe('theme', () => {
  it('defaults to system and stamps nothing', () => {
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('choice').textContent).toBe('system')
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })
  it('setChoice persists and stamps data-theme', () => {
    render(<ThemeProvider><Probe /></ThemeProvider>)
    act(() => screen.getByText('dark').click())
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(screen.getByTestId('resolved').textContent).toBe('dark')
    act(() => screen.getByText('system').click())
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })
  it('readStoredTheme: ?theme= overrides storage; garbage falls back to system', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light')
    expect(readStoredTheme('')).toBe('light')
    expect(readStoredTheme('?theme=dark')).toBe('dark')
    localStorage.setItem(THEME_STORAGE_KEY, 'purple')
    expect(readStoredTheme('')).toBe('system')
  })
  it('applyTheme pins both theme-color metas to the chosen scheme and restores them for system', () => {
    applyTheme('dark')
    const metas = [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')]
    expect(metas.map((m) => m.content)).toEqual(['#16171d', '#16171d'])
    applyTheme('system')
    expect(metas.map((m) => m.content)).toEqual(['#ffffff', '#16171d'])
  })
  it('resolved follows matchMedia when choice is system', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('dark'), media: q, addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false,
    }))
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('resolved').textContent).toBe('dark')
    vi.unstubAllGlobals()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/shell/theme.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `web/src/shell/theme.tsx`**

```tsx
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export type ThemeChoice = 'system' | 'light' | 'dark'
export const THEME_STORAGE_KEY = 'ccswitch.theme'

// Must match the two <meta name="theme-color"> entries in index.html and the
// --t-canvas values in index.css.
const CANVAS = { light: '#ffffff', dark: '#16171d' } as const

function isChoice(v: unknown): v is ThemeChoice {
  return v === 'system' || v === 'light' || v === 'dark'
}

/** ?theme= (kiosk pins it per load) beats the stored choice; anything else → system. */
export function readStoredTheme(search: string = typeof location !== 'undefined' ? location.search : ''): ThemeChoice {
  const fromUrl = new URLSearchParams(search).get('theme')
  if (isChoice(fromUrl) && fromUrl !== 'system') return fromUrl
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    if (isChoice(stored)) return stored
  } catch { /* storage unavailable */ }
  return 'system'
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : false
}

/** Stamp <html data-theme> and pin the browser-chrome color to the chosen scheme. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
  const metas = document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')
  metas.forEach((m) => {
    if (choice === 'system') {
      m.content = m.media.includes('dark') ? CANVAS.dark : CANVAS.light
    } else {
      m.content = CANVAS[choice]
    }
  })
}

interface ThemeContextValue {
  choice: ThemeChoice
  resolved: 'light' | 'dark'
  setChoice: (c: ThemeChoice) => void
}
const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readStoredTheme)
  const [systemDark, setSystemDark] = useState(systemPrefersDark)

  useEffect(() => { applyTheme(choice) }, [choice])

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setSystemDark(mql.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])

  const setChoice = useCallback((c: ThemeChoice) => {
    setChoiceState(c)
    try {
      if (c === 'system') localStorage.removeItem(THEME_STORAGE_KEY)
      else localStorage.setItem(THEME_STORAGE_KEY, c)
    } catch { /* storage unavailable */ }
  }, [])

  const value = useMemo<ThemeContextValue>(() => ({
    choice,
    resolved: choice === 'system' ? (systemDark ? 'dark' : 'light') : choice,
    setChoice,
  }), [choice, systemDark, setChoice])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used inside <ThemeProvider>')
  return ctx
}
```

- [ ] **Step 4: Mount it in `web/src/main.tsx`**

Add `import { ThemeProvider } from '@/shell/theme'` and wrap the tree: `<ThemeProvider>` directly inside `<StrictMode>`, around `<QueryClientProvider …>`.

- [ ] **Step 5: Run tests**

Run: `npm --prefix web run test -- src/shell/theme.test.tsx && npm --prefix web run typecheck`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/shell/theme.tsx web/src/shell/theme.test.tsx web/src/main.tsx
git commit -m "feat(ui): ThemeProvider — system/light/dark choice stamped as data-theme (ccswitch.theme)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: ESLint rule `ccswitch/no-raw-palette`

**Files:**
- Create: `web/eslint-rules/no-raw-palette.js`, `web/eslint-rules/no-raw-palette.test.ts`
- Modify: `web/eslint.config.js`

**Interfaces:**
- Produces: rule id `ccswitch/no-raw-palette`, enabled as `error` for `src/ui/**/*.{ts,tsx}` and `src/shell/**/*.{ts,tsx}`.

- [ ] **Step 1: Write the failing test (ESLint `RuleTester` under vitest)**

```ts
// web/eslint-rules/no-raw-palette.test.ts
import { RuleTester } from 'eslint'
import tseslint from 'typescript-eslint'
import rule from './no-raw-palette.js'

const tester = new RuleTester({
  languageOptions: {
    parser: tseslint.parser,
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
})

tester.run('no-raw-palette', rule, {
  valid: [
    { code: '<div className="bg-surface text-ink-muted border-edge z-dialog min-h-target" />' },
    { code: 'cn("rounded-lg", open && "bg-accent-wash")' },
    { code: 'const x = "gray-500"' }, // not a className / cn() context
  ],
  invalid: [
    { code: '<div className="bg-gray-100" />', errors: [{ messageId: 'palette' }] },
    { code: '<div className="text-ink dark:text-white" />', errors: [{ messageId: 'dark' }] },
    { code: 'cn("border", "border-blue-500/50")', errors: [{ messageId: 'palette' }] },
    { code: 'cva("z-50")', errors: [{ messageId: 'zLiteral' }] },
    { code: '<div className={`min-h-[44px] ${x}`} />', errors: [{ messageId: 'pxLiteral' }] },
    { code: '<div className="bg-[#fff]" />', errors: [{ messageId: 'hex' }] },
  ],
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- eslint-rules/no-raw-palette.test.ts`
Expected: FAIL — module not found. (If vitest does not pick the file up, add `include: ['src/**/*.{test,spec}.{ts,tsx}', 'eslint-rules/**/*.test.ts']` under `test` in `vitest.config.ts`.)

- [ ] **Step 3: Implement `web/eslint-rules/no-raw-palette.js`**

```js
// ccswitch/no-raw-palette — bans Tailwind palette literals, dark: variants, hex
// colors, z-index literals and arbitrary pixel sizes inside class strings.
// Scope is decided in eslint.config.js (src/ui, src/shell now; wider per
// sub-spec C). Rationale: docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md §2.5.
const PALETTE =
  /^(bg|text|border|ring|fill|stroke|from|to|via|outline|decoration|divide|caret|shadow|placeholder)-(gray|slate|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}(\/\d+)?$/
const Z_LITERAL = /^z-(\[.*\]|\d+)$/
const PX_LITERAL = /^(text|min-h|min-w|max-h|max-w|h|w|p[xy]?|m[xy]?|gap)-\[\d+px\]$/
const HEX = /\[#[0-9a-fA-F]{3,8}\]/

const CLASS_CALLS = new Set(['cn', 'cva', 'clsx', 'twMerge'])

function inClassContext(node, sourceCode) {
  for (const a of sourceCode.getAncestors(node)) {
    if (a.type === 'JSXAttribute' && a.name && a.name.name === 'className') return true
    if (a.type === 'CallExpression' && a.callee.type === 'Identifier' && CLASS_CALLS.has(a.callee.name)) return true
  }
  return false
}

function check(context, node, text) {
  for (const raw of text.split(/\s+/)) {
    if (!raw) continue
    if (HEX.test(raw)) { context.report({ node, messageId: 'hex', data: { cls: raw } }); continue }
    const variants = raw.split(':')
    const base = variants[variants.length - 1]
    if (variants.slice(0, -1).includes('dark')) { context.report({ node, messageId: 'dark', data: { cls: raw } }); continue }
    if (PALETTE.test(base)) context.report({ node, messageId: 'palette', data: { cls: raw } })
    else if (Z_LITERAL.test(base)) context.report({ node, messageId: 'zLiteral', data: { cls: raw } })
    else if (PX_LITERAL.test(base)) context.report({ node, messageId: 'pxLiteral', data: { cls: raw } })
  }
}

export default {
  meta: {
    type: 'problem',
    docs: { description: 'Use design tokens (bg-surface, text-ink-muted, z-dialog, min-h-target) instead of raw palette / dark: / hex / px literals' },
    messages: {
      palette: '"{{cls}}" is a raw palette class — use a token (bg-surface, text-ink-muted, border-edge, bg-success…).',
      dark: '"{{cls}}" uses dark: — tokens flip with the theme; drop the variant.',
      hex: '"{{cls}}" embeds a hex color — use a token.',
      zLiteral: '"{{cls}}" is a z-index literal — use z-raised|sticky|nav|popover|overlay|drawer|dialog|palette|toast.',
      pxLiteral: '"{{cls}}" is an arbitrary px size — use the scale (min-h-target, text-sm…).',
    },
    schema: [],
  },
  create(context) {
    const sourceCode = context.sourceCode
    return {
      Literal(node) {
        if (typeof node.value === 'string' && inClassContext(node, sourceCode)) check(context, node, node.value)
      },
      TemplateElement(node) {
        if (inClassContext(node, sourceCode)) check(context, node, node.value.cooked ?? '')
      },
    }
  },
}
```

- [ ] **Step 4: Register it in `web/eslint.config.js`**

```js
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import noRawPalette from './eslint-rules/no-raw-palette.js'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    // Token discipline is enforced folder-by-folder: the new UI system first,
    // each feature folder as sub-spec C migrates it, the whole tree in PR 11.
    files: ['src/ui/**/*.{ts,tsx}', 'src/shell/**/*.{ts,tsx}'],
    ignores: ['src/features/pipeline/charts/**'],
    plugins: { ccswitch: { rules: { 'no-raw-palette': noRawPalette } } },
    rules: { 'ccswitch/no-raw-palette': 'error' },
  },
])
```

- [ ] **Step 5: Run the rule test and lint**

Run: `npm --prefix web run test -- eslint-rules/no-raw-palette.test.ts && npm --prefix web run lint`
Expected: PASS; lint clean (`src/ui/Button.tsx` and `src/shell/theme.tsx` contain no palette literals).

- [ ] **Step 6: Commit**

```bash
git add web/eslint-rules web/eslint.config.js web/vitest.config.ts
git commit -m "chore(lint): ccswitch/no-raw-palette guards src/ui and src/shell

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: PR 1 — gates, visual check, PR

**Files:** none new.

- [ ] **Step 1: Run every gate**

```bash
npm --prefix web test 2>&1 | tee /tmp/ui-pr1-web-test.log | tail -5
npm --prefix web run typecheck && npm --prefix web run lint
ruff check src/ server/ && mypy server/ --ignore-missing-imports && pytest tests/ -q && CCSWITCH_DEV=1 pytest server/tests/ -q -n 8
```
Expected: all green.

- [ ] **Step 2: Build and re-run the audit's manual check against the real bundle**

```bash
npm --prefix web run build 2>&1 | tail -3
grep -c '\.bg-surface' web/dist/assets/*.css        # ≥ 1
grep -c '\.bg-primary'  web/dist/assets/*.css        # 0
```

- [ ] **Step 3: Visual spot-check (dev server, both themes)**

Run `npm --prefix web run dev`, open the app; confirm: the **New**/**Save** buttons have a filled accent background; the Products page cards have a surface background and visible borders; toggling the OS theme flips everything; the Pipeline board looks as before.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(ui): real token layer, ThemeProvider, palette lint (sub-spec A, PR 1)" --body "$(cat <<'EOF'
## Summary
Sub-spec A PR 1 of 2 — docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md.
- `@theme inline` bridge in `index.css`: surfaces/ink, semantic status, layers, metrics, type scale; dark mode expressed once at `:root`.
- `.prdash-root` remapped onto the global tokens — Products/Initiatives/Pipeline resolve everywhere.
- Dead shadcn names replaced (Button default variant now renders a background; `border-border`/`text-muted-foreground` no longer no-ops). Closes audit F4.1, F4.2.
- `ThemeProvider` (system/light/dark, `ccswitch.theme`); `ccswitch/no-raw-palette` lint on `src/ui` + `src/shell`; `tokenBridge.test.ts` asserts the compiled CSS.
- Deleted `tailwind.config.js` (v3 file ignored by v4).

## Test plan
- [x] `npm --prefix web test` / typecheck / lint
- [x] backend gates unchanged
- [x] `web/dist` contains `.bg-surface`, not `.bg-primary`
- [x] dev-server spot check in light and dark

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto "$(gh pr view --json number -q .number)"
```

---

# Phase 2 — PR 2: primitives (branch from `origin/master` after PR 1 merges)

### Task 9: Promote `useFocusTrap` with `inert` + focus restore

**Files:**
- Move: `web/src/pipeline/hooks/useFocusTrap.ts` → `web/src/ui/useFocusTrap.ts`
- Create: `web/src/ui/useFocusTrap.test.tsx`
- Modify: `web/src/pipeline/LegendPanel.tsx` (import path only, for now)

**Interfaces:**
- Produces: `useFocusTrap(containerRef, active, { onClose, returnFocusRef?, inertRoot? })` — same signature plus `inertRoot?: string` (default `'root'`); while active, `#root` carries the `inert` attribute; nested traps are counted so the attribute is removed only when the last one closes.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/useFocusTrap.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useRef, useState } from 'react'
import { useFocusTrap } from './useFocusTrap'

function Host({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(ref, open, { onClose: () => { setOpen(false); onClose() } })
  return (
    <div id="root">
      <button onClick={() => setOpen(true)}>open</button>
      {open && (
        <div ref={ref} tabIndex={-1}>
          <button>first</button>
          <button>last</button>
        </div>
      )}
    </div>
  )
}

describe('useFocusTrap', () => {
  it('moves focus in, sets inert on #root, wraps Tab, restores focus and clears inert on close', () => {
    let closed = 0
    render(<Host onClose={() => { closed++ }} />)
    const opener = screen.getByText('open')
    opener.focus()
    fireEvent.click(opener)
    const root = document.getElementById('root')!
    expect(document.activeElement).toBe(screen.getByText('first'))
    expect(root.hasAttribute('inert')).toBe(true)
    screen.getByText('last').focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByText('first'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(closed).toBe(1)
    expect(root.hasAttribute('inert')).toBe(false)
    expect(document.activeElement).toBe(opener)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/useFocusTrap.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Move and extend the hook**

```bash
git mv web/src/pipeline/hooks/useFocusTrap.ts web/src/ui/useFocusTrap.ts
sed -i "s#'./hooks/useFocusTrap'#'@/ui/useFocusTrap'#" web/src/pipeline/LegendPanel.tsx
```

Then edit `web/src/ui/useFocusTrap.ts`: add the inert counter and the option.

```ts
// add near the top, after FOCUSABLE
let inertDepth = 0
function setRootInert(id: string, on: boolean) {
  const root = document.getElementById(id)
  if (!root) return
  inertDepth = Math.max(0, inertDepth + (on ? 1 : -1))
  if (inertDepth > 0) root.setAttribute('inert', '')
  else root.removeAttribute('inert')
}
```

Change the signature to:

```ts
export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  active: boolean,
  opts: {
    onClose: () => void;
    returnFocusRef?: RefObject<HTMLElement | null>;
    /** Element id that becomes `inert` while the trap is active (default 'root'). Pass null to skip. */
    inertRoot?: string | null;
  },
): void {
```

Inside the effect, right after `const previousFocus = …`, add `const inertId = optsRef.current.inertRoot === undefined ? 'root' : optsRef.current.inertRoot; if (inertId) setRootInert(inertId, true)`; and in the cleanup, before returning focus, add `if (inertId) setRootInert(inertId, false)`. Everything else stays as ported.

- [ ] **Step 4: Run the test + LegendPanel tests**

Run: `npm --prefix web run test -- src/ui/useFocusTrap.test.tsx src/pipeline`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A web/src/ui/useFocusTrap.ts web/src/ui/useFocusTrap.test.tsx web/src/pipeline
git commit -m "refactor(ui): promote useFocusTrap to web/src/ui with inert + focus restore

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: `Button` (new variants) + `IconButton`, and the call-site rename

**Files:**
- Modify: `web/src/ui/Button.tsx`, `web/src/ui/Button.test.tsx`
- Create: `web/src/ui/IconButton.tsx`, `web/src/ui/IconButton.test.tsx`
- Modify: every `variant="outline"` (22) → `secondary`, `variant="destructive"` (3) → `danger`; `size="default"` is not used.

**Interfaces:**
- Produces:
  ```ts
  ButtonProps: { variant?: 'primary'|'secondary'|'ghost'|'danger'; size?: 'sm'|'md'|'lg'; loading?: boolean; iconLeft?: ReactNode; iconRight?: ReactNode } & ButtonHTMLAttributes
  IconButtonProps: { 'aria-label': string; size?: 'sm'|'md'; variant?: 'ghost'|'secondary' } & ButtonHTMLAttributes
  ```

- [ ] **Step 1: Update `Button.test.tsx` and write `IconButton.test.tsx` (failing)**

Replace the first two tests in `Button.test.tsx` with:

```tsx
  it('renders primary on the accent token by default', () => {
    render(<Button>Save</Button>)
    const btn = screen.getByRole('button', { name: 'Save' })
    expect(btn.className).toContain('bg-accent')
    expect(btn.className).not.toMatch(/bg-primary|dark:|gray-/)
  })
  it('secondary uses surface/edge; danger uses the danger token', () => {
    render(<><Button variant="secondary">A</Button><Button variant="danger">B</Button></>)
    expect(screen.getByText('A').className).toContain('border-edge')
    expect(screen.getByText('B').className).toContain('bg-danger')
  })
  it('loading disables and shows a spinner', () => {
    render(<Button loading>Save</Button>)
    const btn = screen.getByRole('button')
    expect(btn).toBeDisabled()
    expect(btn.querySelector('[data-spinner]')).not.toBeNull()
  })
```

```tsx
// web/src/ui/IconButton.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { X } from 'lucide-react'
import { IconButton } from './IconButton'

expect.extend(toHaveNoViolations)

describe('IconButton', () => {
  it('requires an accessible name and meets the target size', async () => {
    const { container } = render(<IconButton aria-label="Close"><X /></IconButton>)
    const btn = screen.getByRole('button', { name: 'Close' })
    expect(btn.className).toContain('min-h-target')
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm --prefix web run test -- src/ui/Button.test.tsx src/ui/IconButton.test.tsx`
Expected: FAIL (`secondary`/`danger` unknown; `IconButton` missing).

- [ ] **Step 3: Rewrite `web/src/ui/Button.tsx`**

```tsx
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

export const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ' +
  'disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-ink hover:bg-accent/90',
        secondary: 'border border-edge bg-surface text-ink hover:bg-surface-raised',
        ghost: 'text-ink hover:bg-surface-raised',
        danger: 'bg-danger text-white hover:bg-danger/90',
      },
      size: {
        sm: 'h-9 px-3',
        md: 'h-10 px-4 py-2',
        lg: 'h-11 min-h-target px-6',
      },
    },
    defaultVariants: { variant: 'primary', size: 'md' },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Disables the button and shows a spinner in place of iconLeft. */
  loading?: boolean
  iconLeft?: React.ReactNode
  iconRight?: React.ReactNode
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading = false, iconLeft, iconRight, disabled, children, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading
        ? <Loader2 size={16} aria-hidden="true" data-spinner className="animate-spin motion-reduce:animate-none" />
        : iconLeft}
      {children}
      {iconRight}
    </button>
  ),
)
Button.displayName = 'Button'
```

- [ ] **Step 4: Create `web/src/ui/IconButton.tsx`**

```tsx
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const iconButtonVariants = cva(
  'inline-flex items-center justify-center rounded-md min-h-target min-w-target transition-colors ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        ghost: 'text-ink-muted hover:text-ink hover:bg-surface-raised',
        secondary: 'border border-edge bg-surface text-ink hover:bg-surface-raised',
      },
      size: { sm: 'h-9 w-9', md: 'h-10 w-10' },
    },
    defaultVariants: { variant: 'ghost', size: 'md' },
  },
)

export interface IconButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'>,
    VariantProps<typeof iconButtonVariants> {
  /** Required: an icon-only control has no visible text. */
  'aria-label': string
}

export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ className, variant, size, type = 'button', ...props }, ref) => (
    <button ref={ref} type={type} className={cn(iconButtonVariants({ variant, size }), className)} {...props} />
  ),
)
IconButton.displayName = 'IconButton'
```

- [ ] **Step 5: Rename the call sites**

```bash
grep -rl 'variant="outline"\|variant="destructive"' web/src | xargs sed -i -e 's/variant="outline"/variant="secondary"/g' -e 's/variant="destructive"/variant="danger"/g'
```

`size="sm"` (9 sites) and `size="lg"` (1) already match the new names; there is no `size="icon"` or `size="default"` usage.

- [ ] **Step 6: Run tests, typecheck, lint**

Run: `npm --prefix web test && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS. Any test that asserted `bg-red-600` on the delete button (`PermanentDeleteDialog`) now asserts the `danger` variant in Task 17 — until then those inline overrides still apply; leave them.

- [ ] **Step 7: Commit**

```bash
git add -A web/src
git commit -m "feat(ui): Button primary/secondary/ghost/danger + loading; IconButton with required label

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: `Input`, `Textarea`, `Select`, `Field`

**Files:**
- Create: `web/src/ui/Input.tsx`, `Textarea.tsx`, `Select.tsx`, `Field.tsx`, `FormControls.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  Input: InputHTMLAttributes & { invalid?: boolean; size?: 'sm'|'md' }     // native `size` attr is omitted
  Textarea: TextareaHTMLAttributes & { invalid?: boolean; autoGrow?: boolean }
  Select: SelectHTMLAttributes & { invalid?: boolean; size?: 'sm'|'md' }
  Field: { label: string; htmlFor: string; help?: ReactNode; error?: ReactNode; required?: boolean; children: ReactNode }
  export const controlClass: string   // shared base classes, for one-off native controls
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/FormControls.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Input } from './Input'
import { Textarea } from './Textarea'
import { Select } from './Select'
import { Field } from './Field'

expect.extend(toHaveNoViolations)

describe('form controls', () => {
  it('Field wires label, help and error to the control', async () => {
    const { container } = render(
      <Field label="Name" htmlFor="n" help="Shown in the list" error="Required">
        <Input id="n" invalid />
      </Field>,
    )
    const input = screen.getByLabelText('Name')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input.getAttribute('aria-describedby')).toContain('n-help')
    expect(input.getAttribute('aria-describedby')).toContain('n-error')
    expect(screen.getByText('Required')).toHaveAttribute('role', 'alert')
    expect(await axe(container)).toHaveNoViolations()
  })
  it('Textarea autoGrow sets height from scrollHeight', () => {
    render(<Textarea aria-label="Prompt" autoGrow defaultValue="a" />)
    const ta = screen.getByLabelText('Prompt') as HTMLTextAreaElement
    Object.defineProperty(ta, 'scrollHeight', { value: 120, configurable: true })
    fireEvent.input(ta, { target: { value: 'a\nb\nc' } })
    expect(ta.style.height).toBe('120px')
  })
  it('Select renders options with token classes', () => {
    render(<Select aria-label="Mode" defaultValue="a"><option value="a">A</option></Select>)
    expect(screen.getByLabelText('Mode').className).toContain('border-edge')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/FormControls.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement the four files**

```tsx
// web/src/ui/Input.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

/** Shared look for native controls; exported for one-off <input>s that can't use the component. */
export const controlClass =
  'w-full rounded-md border border-edge bg-surface text-ink placeholder:text-ink-faint ' +
  'focus:outline-none focus:ring-2 focus:ring-accent focus:border-accent ' +
  'disabled:opacity-50 disabled:cursor-not-allowed aria-invalid:border-danger aria-invalid:focus:ring-danger'

const SIZES = { sm: 'h-9 px-2.5 text-sm', md: 'h-10 min-h-target px-3 text-sm' } as const

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'size'> {
  invalid?: boolean
  size?: keyof typeof SIZES
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, invalid, size = 'md', ...props }, ref) => (
    <input ref={ref} aria-invalid={invalid || undefined} className={cn(controlClass, SIZES[size], className)} {...props} />
  ),
)
Input.displayName = 'Input'
```

```tsx
// web/src/ui/Textarea.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'
import { controlClass } from './Input'

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
  /** Grow to fit content on input (no scrollbar until max-height). */
  autoGrow?: boolean
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, invalid, autoGrow = false, onInput, ...props }, ref) => {
    const handleInput = (e: React.FormEvent<HTMLTextAreaElement>) => {
      if (autoGrow) {
        const el = e.currentTarget
        el.style.height = 'auto'
        el.style.height = `${el.scrollHeight}px`
      }
      onInput?.(e)
    }
    return (
      <textarea
        ref={ref}
        aria-invalid={invalid || undefined}
        onInput={handleInput}
        className={cn(controlClass, 'px-3 py-2 text-sm leading-relaxed resize-y', autoGrow && 'resize-none overflow-hidden', className)}
        {...props}
      />
    )
  },
)
Textarea.displayName = 'Textarea'
```

```tsx
// web/src/ui/Select.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'
import { controlClass } from './Input'

const SIZES = { sm: 'h-9 px-2 text-sm', md: 'h-10 min-h-target px-3 text-sm' } as const

export interface SelectProps extends Omit<React.SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
  invalid?: boolean
  size?: keyof typeof SIZES
}

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, invalid, size = 'md', ...props }, ref) => (
    <select ref={ref} aria-invalid={invalid || undefined} className={cn(controlClass, SIZES[size], 'pr-8', className)} {...props} />
  ),
)
Select.displayName = 'Select'
```

```tsx
// web/src/ui/Field.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface FieldProps {
  label: string
  /** id of the control inside; Field derives `${htmlFor}-help` / `${htmlFor}-error`. */
  htmlFor: string
  help?: React.ReactNode
  error?: React.ReactNode
  required?: boolean
  className?: string
  children: React.ReactElement<{ 'aria-describedby'?: string; required?: boolean }>
}

export function Field({ label, htmlFor, help, error, required, className, children }: FieldProps) {
  const helpId = help ? `${htmlFor}-help` : undefined
  const errorId = error ? `${htmlFor}-error` : undefined
  const describedBy = [helpId, errorId].filter(Boolean).join(' ') || undefined
  const control = React.cloneElement(children, { 'aria-describedby': describedBy, required })
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={htmlFor} className="text-sm font-medium text-ink">
        {label}{required && <span aria-hidden="true" className="text-danger"> *</span>}
      </label>
      {control}
      {help && <p id={helpId} className="text-xs text-ink-muted">{help}</p>}
      {error && <p id={errorId} role="alert" className="text-xs text-danger">{error}</p>}
    </div>
  )
}
```

- [ ] **Step 4: Run the test**

Run: `npm --prefix web run test -- src/ui/FormControls.test.tsx && npm --prefix web run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/Input.tsx web/src/ui/Textarea.tsx web/src/ui/Select.tsx web/src/ui/Field.tsx web/src/ui/FormControls.test.tsx
git commit -m "feat(ui): Input/Textarea/Select/Field primitives on tokens

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: `Menu`

**Files:**
- Create: `web/src/ui/portal.ts`, `web/src/ui/Menu.tsx`, `web/src/ui/Menu.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  getOverlayRoot(): HTMLElement                       // #overlays, else document.body
  interface MenuItem { id: string; label: ReactNode; shortcut?: string; icon?: ReactNode; disabled?: boolean; selected?: boolean }
  MenuProps: { open: boolean; onClose: () => void; anchorRef: RefObject<HTMLElement | null>; items: MenuItem[]; onSelect: (id: string) => void; label: string; align?: 'start'|'end' }
  ```
  Keyboard: `ArrowUp/Down` move, `Home/End`, `Enter/Space` select, `Escape` closes, **digits `1–9` select the Nth enabled item** (Claude Code Desktop parity), typeahead on first letter.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Menu.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useRef, useState } from 'react'
import { Menu } from './Menu'

function Host({ onSelect }: { onSelect: (id: string) => void }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  return (
    <>
      <button ref={ref} onClick={() => setOpen(true)}>Mode</button>
      <Menu open={open} onClose={() => setOpen(false)} anchorRef={ref} label="Permission mode" onSelect={onSelect}
        items={[{ id: 'manual', label: 'Manual' }, { id: 'accept', label: 'Accept edits', disabled: true }, { id: 'plan', label: 'Plan' }]} />
    </>
  )
}

describe('Menu', () => {
  it('opens with focus on the first item, arrows skip disabled, digit picks the Nth enabled item', () => {
    const onSelect = vi.fn()
    render(<Host onSelect={onSelect} />)
    fireEvent.click(screen.getByText('Mode'))
    const menu = screen.getByRole('menu', { name: 'Permission mode' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Manual/ }))
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: /Plan/ }))
    fireEvent.keyDown(menu, { key: '2' })
    expect(onSelect).toHaveBeenCalledWith('plan')
  })
  it('Escape closes and returns focus to the anchor', () => {
    render(<Host onSelect={() => {}} />)
    const anchor = screen.getByText('Mode')
    fireEvent.click(anchor)
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(anchor)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/Menu.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// web/src/ui/portal.ts
/** Portal target for overlays: #overlays (index.html) sits outside #root so
 *  `inert` on #root never disables the overlay itself. */
export function getOverlayRoot(): HTMLElement {
  return document.getElementById('overlays') ?? document.body
}
```

```tsx
// web/src/ui/Menu.tsx
import * as React from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
import { Kbd } from './Kbd'

export interface MenuItem {
  id: string
  label: React.ReactNode
  shortcut?: string
  icon?: React.ReactNode
  disabled?: boolean
  selected?: boolean
}

export interface MenuProps {
  open: boolean
  onClose: () => void
  anchorRef: React.RefObject<HTMLElement | null>
  items: MenuItem[]
  onSelect: (id: string) => void
  /** Accessible name of the menu. */
  label: string
  align?: 'start' | 'end'
  className?: string
}

export function Menu({ open, onClose, anchorRef, items, onSelect, label, align = 'start', className }: MenuProps) {
  const listRef = React.useRef<HTMLDivElement>(null)
  const [active, setActive] = React.useState(0)
  const enabledIdx = React.useMemo(() => items.map((it, i) => (it.disabled ? -1 : i)).filter((i) => i >= 0), [items])

  // Position under the anchor (fixed, so it escapes overflow:hidden ancestors).
  const [pos, setPos] = React.useState<{ top: number; left: number; right?: number }>({ top: 0, left: 0 })
  React.useLayoutEffect(() => {
    if (!open) return
    const r = anchorRef.current?.getBoundingClientRect()
    if (!r) return
    setPos(align === 'end' ? { top: r.bottom + 4, left: 0, right: window.innerWidth - r.right } : { top: r.bottom + 4, left: r.left })
    setActive(enabledIdx[0] ?? 0)
  }, [open, anchorRef, align, enabledIdx])

  React.useEffect(() => {
    if (!open) return
    listRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]')[active]?.focus()
  }, [open, active])

  // Close on outside pointer-down; restore focus to the anchor on close.
  React.useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (listRef.current?.contains(e.target as Node) || anchorRef.current?.contains(e.target as Node)) return
      onClose()
    }
    document.addEventListener('pointerdown', onDown)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      anchorRef.current?.focus()
    }
  }, [open, onClose, anchorRef])

  if (!open) return null

  const pick = (i: number) => {
    const it = items[i]
    if (!it || it.disabled) return
    onSelect(it.id)
    onClose()
  }
  const move = (delta: number) => {
    const k = enabledIdx.indexOf(active)
    const next = enabledIdx[(k + delta + enabledIdx.length) % enabledIdx.length]
    if (next !== undefined) setActive(next)
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return }
    if (e.key === 'Home') { e.preventDefault(); setActive(enabledIdx[0] ?? 0); return }
    if (e.key === 'End') { e.preventDefault(); setActive(enabledIdx[enabledIdx.length - 1] ?? 0); return }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(active); return }
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return }
    if (/^[1-9]$/.test(e.key)) { e.preventDefault(); const i = enabledIdx[Number(e.key) - 1]; if (i !== undefined) pick(i); return }
    if (e.key.length === 1) {
      const ch = e.key.toLowerCase()
      const hit = enabledIdx.find((i) => String(items[i].label).toLowerCase().startsWith(ch))
      if (hit !== undefined) setActive(hit)
    }
  }

  let enabledCounter = 0
  return createPortal(
    <div
      ref={listRef}
      role="menu"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={{ position: 'fixed', top: pos.top, left: pos.right === undefined ? pos.left : undefined, right: pos.right }}
      className={cn('z-popover min-w-48 rounded-md border border-edge bg-surface p-1 shadow-lg', className)}
    >
      {items.map((it, i) => {
        const n = it.disabled ? null : ++enabledCounter
        return (
          <div
            key={it.id}
            role="menuitem"
            tabIndex={i === active ? 0 : -1}
            aria-disabled={it.disabled || undefined}
            aria-checked={it.selected}
            onClick={() => pick(i)}
            onMouseEnter={() => !it.disabled && setActive(i)}
            className={cn(
              'flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-ink cursor-default select-none min-h-9',
              'focus:outline-none focus:bg-surface-raised',
              it.disabled && 'opacity-50',
              it.selected && 'font-semibold',
            )}
          >
            {it.icon}
            <span className="flex-1">{it.label}</span>
            {it.shortcut && <Kbd keys={it.shortcut} />}
            {n !== null && n <= 9 && <span aria-hidden="true" className="text-2xs text-ink-faint tabular-nums">{n}</span>}
          </div>
        )
      })}
    </div>,
    getOverlayRoot(),
  )
}
```

`Kbd` is created in Task 16; create it now as part of this task if you are executing tasks in order (see Task 16 Step 3 for its code) — it is 12 lines.

- [ ] **Step 4: Run the test**

Run: `npm --prefix web run test -- src/ui/Menu.test.tsx && npm --prefix web run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/portal.ts web/src/ui/Menu.tsx web/src/ui/Menu.test.tsx web/src/ui/Kbd.tsx
git commit -m "feat(ui): Menu primitive — arrows, 1–9 selection, typeahead, portal to #overlays

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: `Dialog` and `Sheet`

**Files:**
- Create: `web/src/ui/Dialog.tsx`, `web/src/ui/Sheet.tsx`, `web/src/ui/Overlays.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  DialogProps: { open: boolean; onClose: () => void; title?: string; 'aria-label'?: string; description?: ReactNode; size?: 'sm'|'md'|'lg'|'xl'; initialFocus?: RefObject<HTMLElement|null>; dismissable?: boolean (default true: Esc + backdrop close); children; className?; bodyClassName? }
  SheetProps:  { open; onClose; title: string; side?: 'right'|'bottom'; mode?: 'overlay'|'push'; width?: number|string; initialFocus?; children; className? }
  ```
  Both: `role="dialog" aria-modal="true"`, labelled by the title (`id` auto-generated) or `aria-label`; portal to `#overlays`; `useFocusTrap` (inert on `#root`, focus restore); `Dialog` wrapper is `fixed inset-0 z-dialog`, `Sheet` wrapper `z-drawer`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Overlays.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Dialog } from './Dialog'
import { Sheet } from './Sheet'

expect.extend(toHaveNoViolations)

describe('Dialog', () => {
  it('labels by title, traps focus, closes on Escape and backdrop, not when dismissable=false', async () => {
    const onClose = vi.fn()
    const { rerender } = render(
      <Dialog open onClose={onClose} title="Rename session"><button>Save</button></Dialog>,
    )
    const dlg = screen.getByRole('dialog', { name: 'Rename session' })
    expect(dlg).toHaveAttribute('aria-modal', 'true')
    expect(document.activeElement).toBe(screen.getByText('Save'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
    expect(await axe(document.body)).toHaveNoViolations()

    rerender(<Dialog open onClose={onClose} title="Deploying" dismissable={false}><p>…</p></Dialog>)
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
  it('renders into #overlays and inerts #root', () => {
    document.body.innerHTML = '<div id="root"></div><div id="overlays"></div>'
    render(<Dialog open onClose={() => {}} title="T"><button>x</button></Dialog>, { container: document.getElementById('root')! })
    expect(document.getElementById('overlays')!.querySelector('[role="dialog"]')).not.toBeNull()
    expect(document.getElementById('root')!.hasAttribute('inert')).toBe(true)
  })
})

describe('Sheet', () => {
  it('is a right-anchored dialog with a close button', () => {
    const onClose = vi.fn()
    render(<Sheet open onClose={onClose} title="Help"><p>body</p></Sheet>)
    expect(screen.getByRole('dialog', { name: 'Help' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close Help' }))
    expect(onClose).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/Overlays.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement `Dialog.tsx`**

```tsx
import * as React from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
import { useFocusTrap } from './useFocusTrap'

const SIZES = { sm: 'max-w-sm', md: 'max-w-md', lg: 'max-w-lg', xl: 'max-w-5xl' } as const

export interface DialogProps {
  open: boolean
  onClose: () => void
  title?: string
  'aria-label'?: string
  description?: React.ReactNode
  size?: keyof typeof SIZES
  initialFocus?: React.RefObject<HTMLElement | null>
  /** false → neither Escape nor the backdrop closes it (e.g. a running deploy). */
  dismissable?: boolean
  className?: string
  children: React.ReactNode
}

export function Dialog({ open, onClose, title, description, size = 'md', initialFocus, dismissable = true, className, children, ...rest }: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null)
  const titleId = React.useId()
  const descId = React.useId()
  useFocusTrap(panelRef, open, { onClose: dismissable ? onClose : () => {} })

  React.useEffect(() => {
    if (open && initialFocus?.current) initialFocus.current.focus()
  }, [open, initialFocus])

  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-dialog flex items-end sm:items-center justify-center p-4">
      <div
        data-testid="dialog-backdrop"
        className="absolute inset-0 bg-black/50"
        onClick={dismissable ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-label={title ? undefined : rest['aria-label']}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn(
          'relative w-full rounded-lg border border-edge bg-surface text-ink shadow-xl p-6 space-y-4 max-h-[90vh] overflow-y-auto',
          SIZES[size], className,
        )}
      >
        {title && <h2 id={titleId} className="text-lg font-semibold">{title}</h2>}
        {description && <p id={descId} className="text-sm text-ink-muted">{description}</p>}
        {children}
      </div>
    </div>,
    getOverlayRoot(),
  )
}
```

- [ ] **Step 4: Implement `Sheet.tsx`**

```tsx
import * as React from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { getOverlayRoot } from './portal'
import { useFocusTrap } from './useFocusTrap'
import { IconButton } from './IconButton'

export interface SheetProps {
  open: boolean
  onClose: () => void
  title: string
  side?: 'right' | 'bottom'
  /** `push` renders inline (no portal, backdrop or inert) — the shell lays it out as a column. */
  mode?: 'overlay' | 'push'
  width?: number | string
  initialFocus?: React.RefObject<HTMLElement | null>
  className?: string
  children: React.ReactNode
}

export function Sheet({ open, onClose, title, side = 'right', mode = 'overlay', width = 448, initialFocus, className, children }: SheetProps) {
  const panelRef = React.useRef<HTMLDivElement>(null)
  const titleId = React.useId()
  useFocusTrap(panelRef, open && mode === 'overlay', { onClose })

  React.useEffect(() => {
    if (open && initialFocus?.current) initialFocus.current.focus()
  }, [open, initialFocus])

  if (!open) return null

  const panel = (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal={mode === 'overlay' ? 'true' : undefined}
      aria-labelledby={titleId}
      tabIndex={-1}
      style={side === 'right' ? { width } : undefined}
      className={cn(
        'flex flex-col bg-surface text-ink border-edge shadow-xl overflow-hidden',
        side === 'right' ? 'h-full max-w-full border-l' : 'w-full max-h-[85vh] rounded-t-lg border-t',
        mode === 'overlay' && 'relative',
        className,
      )}
    >
      <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-edge shrink-0">
        <h2 id={titleId} className="text-base font-semibold">{title}</h2>
        <IconButton aria-label={`Close ${title}`} size="sm" onClick={onClose}><X size={16} aria-hidden="true" /></IconButton>
      </header>
      <div className="flex-1 min-h-0 overflow-y-auto p-4">{children}</div>
    </div>
  )

  if (mode === 'push') return panel
  return createPortal(
    <div className={cn('fixed inset-0 z-drawer flex', side === 'right' ? 'justify-end' : 'items-end')}>
      <div data-testid="sheet-backdrop" className="absolute inset-0 bg-black/50" onClick={onClose} aria-hidden="true" />
      {panel}
    </div>,
    getOverlayRoot(),
  )
}
```

- [ ] **Step 5: Run the test**

Run: `npm --prefix web run test -- src/ui/Overlays.test.tsx && npm --prefix web run lint`
Expected: PASS. (`max-h-[90vh]`/`max-h-[85vh]` are viewport fractions, not px — the lint rule allows them.)

- [ ] **Step 6: Commit**

```bash
git add web/src/ui/Dialog.tsx web/src/ui/Sheet.tsx web/src/ui/Overlays.test.tsx
git commit -m "feat(ui): Dialog and Sheet primitives — one focus trap, inert root, portal to #overlays

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: `Tabs`; `TabBar` becomes a wrapper; `SettingsView` adopts it

**Files:**
- Create: `web/src/ui/Tabs.tsx`, `web/src/ui/Tabs.test.tsx`
- Modify: `web/src/components/TabBar.tsx` (thin wrapper), `web/src/components/settings/SettingsView.tsx:50-64`

**Interfaces:**
- Produces:
  ```ts
  interface TabItem { id: string; label: string; count?: number; attention?: boolean }
  TabsProps: { items: TabItem[]; value: string; onChange: (id: string) => void; label: string; panelId?: (id: string) => string; orientation?: 'horizontal'|'vertical'; className?: string }
  ```
  Semantics are exactly today's `TabBar`: `role=tablist`, roving tabindex, Arrow/Home/End, count `Badge`, attention dot, horizontal scroll with hidden scrollbar.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Tabs.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Tabs } from './Tabs'

expect.extend(toHaveNoViolations)
const items = [{ id: 'a', label: 'Alpha', count: 3 }, { id: 'b', label: 'Beta', attention: true }, { id: 'c', label: 'Gamma' }]

describe('Tabs', () => {
  it('roving tabindex + arrow keys move focus; click selects', async () => {
    const onChange = vi.fn()
    const { container } = render(<Tabs items={items} value="a" onChange={onChange} label="Sections" />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs[0]).toHaveAttribute('tabindex', '0')
    expect(tabs[1]).toHaveAttribute('tabindex', '-1')
    expect(tabs[0]).toHaveAccessibleName('Alpha (3)')
    expect(tabs[1]).toHaveAccessibleName('Beta (needs attention)')
    tabs[0].focus()
    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })
    expect(document.activeElement).toBe(tabs[1])
    fireEvent.keyDown(tabs[1], { key: 'End' })
    expect(document.activeElement).toBe(tabs[2])
    fireEvent.click(tabs[2])
    expect(onChange).toHaveBeenCalledWith('c')
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/Tabs.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `Tabs.tsx`** (port of `TabBar`, tokens, `Badge` from Task 16 — create `Badge` first if executing in order; its code is in Task 16 Step 3)

```tsx
import * as React from 'react'
import { cn } from '@/lib/utils'
import { Badge } from './Badge'

export interface TabItem { id: string; label: string; count?: number; attention?: boolean }

export interface TabsProps {
  items: TabItem[]
  value: string
  onChange: (id: string) => void
  /** Accessible name for the tablist. */
  label: string
  /** When provided, each tab gets id="tab-<id>" and aria-controls={panelId(id)}. */
  panelId?: (id: string) => string
  orientation?: 'horizontal' | 'vertical'
  className?: string
}

function tabName({ label, count, attention }: TabItem): string {
  if (attention) return count ? `${label} (${count}, needs attention)` : `${label} (needs attention)`
  return count ? `${label} (${count})` : label
}

export function Tabs({ items, value, onChange, label, panelId, orientation = 'horizontal', className }: TabsProps) {
  const refs = React.useRef<(HTMLButtonElement | null)[]>([])
  const horizontal = orientation === 'horizontal'
  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    const next = e.key === (horizontal ? 'ArrowRight' : 'ArrowDown') ? (i + 1) % items.length
      : e.key === (horizontal ? 'ArrowLeft' : 'ArrowUp') ? (i - 1 + items.length) % items.length
      : e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : -1
    if (next < 0) return
    e.preventDefault()
    refs.current[next]?.focus()
  }
  return (
    <div
      role="tablist"
      aria-label={label}
      aria-orientation={orientation}
      className={cn(
        'flex gap-1',
        horizontal ? 'overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden' : 'flex-col',
        className,
      )}
    >
      {items.map((t, i) => {
        const active = t.id === value
        return (
          <button
            key={t.id}
            ref={(el) => { refs.current[i] = el }}
            type="button"
            role="tab"
            id={panelId ? `tab-${t.id}` : undefined}
            aria-selected={active}
            aria-controls={panelId ? panelId(t.id) : undefined}
            aria-label={tabName(t)}
            tabIndex={active ? 0 : -1}
            onKeyDown={(e) => onKeyDown(e, i)}
            onClick={() => onChange(t.id)}
            className={cn(
              'shrink-0 whitespace-nowrap min-h-target px-3 py-2 text-sm font-medium flex items-center gap-1.5 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset',
              horizontal ? 'border-b-2 -mb-px' : 'border-l-2 rounded-r-md text-left',
              active ? 'border-accent text-accent' : 'border-transparent text-ink-muted hover:text-ink',
            )}
          >
            {t.label}
            {t.attention && <span aria-hidden="true" className="inline-block w-2 h-2 rounded-pill bg-warning" />}
            {t.count != null && t.count > 0 && <Badge tone="neutral" count={t.count} />}
          </button>
        )
      })}
    </div>
  )
}
```

- [ ] **Step 4: Make `TabBar` a wrapper and adopt `Tabs` in `SettingsView`**

Replace the body of `web/src/components/TabBar.tsx` (keep the doc comment and the `TabBarProps` interface) with:

```tsx
import { Tabs } from '@/ui/Tabs'
import type { TabId } from './Dashboard'
import type { TabDef } from './tabDefs'

export interface TabBarProps {
  tabs: TabDef[]
  activeTab: TabId
  onSelect: (id: TabId) => void
  label: string
  panelId?: (id: TabId) => string
  className?: string
}

/** Thin adapter over the Tabs primitive; sub-spec B replaces it with NavRail. */
export function TabBar({ tabs, activeTab, onSelect, label, panelId, className }: TabBarProps) {
  return (
    <Tabs
      items={tabs}
      value={activeTab}
      onChange={(id) => onSelect(id as TabId)}
      label={label}
      panelId={panelId ? (id) => panelId(id as TabId) : undefined}
      className={className}
    />
  )
}
```

In `SettingsView.tsx`, replace the hand-rolled `<div role="tablist" …>…</div>` (lines 50–64) with:

```tsx
      <Tabs
        items={SECTIONS}
        value={section}
        onChange={(id) => setParams((p) => { p.set('section', id); return p }, { replace: true })}
        label="Settings sections"
        className="mb-4 border-b border-edge"
      />
```

and add `import { Tabs } from '@/ui/Tabs'`. `SECTIONS` already has the `{ id, label }` shape `TabItem` needs.

- [ ] **Step 5: Run the affected tests**

Run: `npm --prefix web run test -- src/ui/Tabs.test.tsx src/components src/App.test.tsx && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS. A `TabBar` test that asserted a `border-blue-600` class now asserts `border-accent`.

- [ ] **Step 6: Commit**

```bash
git add -A web/src
git commit -m "feat(ui): Tabs primitive; TabBar and Settings sections use it (one ARIA tabs implementation)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: `Banner` + `BannerStack`; migrate the seven banners

**Files:**
- Create: `web/src/ui/Banner.tsx`, `web/src/ui/BannerStack.tsx`, `web/src/ui/Banner.test.tsx`
- Modify: `web/src/components/{AllLimitedBanner,NewVersionBanner,DaemonRestartBanner,GitCredentialBanner,UpdatePendingBanner,DeployPanel}.tsx`, `web/src/App.tsx` (wrap the three app-level banners), `web/src/components/Dashboard.tsx:748-760`, `web/src/components/DesktopShell.tsx` (the `AllLimitedBanner`/`DeployPanel` block)

**Interfaces:**
- Produces:
  ```ts
  BannerProps: { tone: 'info'|'success'|'warning'|'danger'; title?: ReactNode; icon?: ReactNode; actions?: ReactNode; onDismiss?: () => void; role?: 'status'|'alert' (default: danger→alert, else status); className?; children? }
  BannerStackProps: { items: Array<{ id: string; priority: number; node: ReactNode }>; className? }   // renders highest priority first; null nodes are skipped
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Banner.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Banner } from './Banner'
import { BannerStack } from './BannerStack'

expect.extend(toHaveNoViolations)

describe('Banner', () => {
  it('danger is an alert; warning is a status; dismiss calls back', async () => {
    const onDismiss = vi.fn()
    const { container } = render(
      <>
        <Banner tone="danger" title="Daemon down">restart it</Banner>
        <Banner tone="warning" title="All accounts at limit" onDismiss={onDismiss} />
      </>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Daemon down')
    expect(screen.getByRole('status')).toHaveTextContent('All accounts at limit')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onDismiss).toHaveBeenCalled()
    expect(await axe(container)).toHaveNoViolations()
  })
})

describe('BannerStack', () => {
  it('orders by priority, skips null, mounts once', () => {
    render(<BannerStack items={[
      { id: 'low', priority: 1, node: <Banner tone="info" title="Low" /> },
      { id: 'none', priority: 5, node: null },
      { id: 'high', priority: 9, node: <Banner tone="danger" title="High" /> },
    ]} />)
    const titles = screen.getAllByText(/High|Low/).map((el) => el.textContent)
    expect(titles).toEqual(['High', 'Low'])
    expect(document.querySelectorAll('[data-region="app.banners"]').length).toBe(1)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/Banner.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

```tsx
// web/src/ui/Banner.tsx
import * as React from 'react'
import { X, Info, CircleCheck, TriangleAlert, CircleAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from './IconButton'

export type BannerTone = 'info' | 'success' | 'warning' | 'danger'

const TONE = {
  info:    { box: 'border-info/40 bg-info-wash text-ink',       icon: 'text-info',    Icon: Info },
  success: { box: 'border-success/40 bg-success-wash text-ink', icon: 'text-success', Icon: CircleCheck },
  warning: { box: 'border-warning/40 bg-warning-wash text-ink', icon: 'text-warning', Icon: TriangleAlert },
  danger:  { box: 'border-danger/40 bg-danger-wash text-ink',   icon: 'text-danger',  Icon: CircleAlert },
} as const

export interface BannerProps {
  tone: BannerTone
  title?: React.ReactNode
  /** Replaces the tone's default icon (pass null for none). */
  icon?: React.ReactNode | null
  actions?: React.ReactNode
  onDismiss?: () => void
  role?: 'status' | 'alert'
  className?: string
  children?: React.ReactNode
}

export function Banner({ tone, title, icon, actions, onDismiss, role, className, children }: BannerProps) {
  const t = TONE[tone]
  const DefaultIcon = t.Icon
  return (
    <div
      role={role ?? (tone === 'danger' ? 'alert' : 'status')}
      className={cn('flex items-start gap-3 rounded-lg border px-4 py-3', t.box, className)}
    >
      {icon === undefined
        ? <DefaultIcon size={18} aria-hidden="true" className={cn('shrink-0 mt-0.5', t.icon)} />
        : icon && <span className={cn('shrink-0 mt-0.5', t.icon)}>{icon}</span>}
      <div className="flex-1 min-w-0">
        {title && <p className="text-sm font-medium">{title}</p>}
        {children && <div className="text-xs text-ink-muted mt-0.5">{children}</div>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      {onDismiss && (
        <IconButton aria-label="Dismiss" size="sm" onClick={onDismiss} className={t.icon}>
          <X size={14} aria-hidden="true" />
        </IconButton>
      )}
    </div>
  )
}
```

```tsx
// web/src/ui/BannerStack.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface BannerStackItem { id: string; priority: number; node: React.ReactNode }

/** The one place banners render (region app.banners). Highest priority first. */
export function BannerStack({ items, className }: { items: BannerStackItem[]; className?: string }) {
  const visible = items.filter((i) => i.node != null).sort((a, b) => b.priority - a.priority)
  if (visible.length === 0) return null
  return (
    <div data-region="app.banners" className={cn('flex flex-col gap-2', className)}>
      {visible.map((i) => <React.Fragment key={i.id}>{i.node}</React.Fragment>)}
    </div>
  )
}
```

- [ ] **Step 4: Migrate `AllLimitedBanner` (full rewrite — the pattern for the others)**

```tsx
import { Ban } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { formatResetTime } from '@/lib/formatReset'

interface AllLimitedBannerProps {
  earliestReset: string | null  // ISO timestamp
  onDismiss: () => void
}

export function AllLimitedBanner({ earliestReset, onDismiss }: AllLimitedBannerProps) {
  const resetLabel = formatResetTime(earliestReset)
  return (
    <Banner tone="warning" role="alert" title="All accounts at limit" icon={<Ban size={18} aria-hidden="true" />} onDismiss={onDismiss}>
      {resetLabel
        ? <>Capacity returns at <time dateTime={earliestReset ?? undefined}>{resetLabel}</time></>
        : 'No reset time available'}
    </Banner>
  )
}
```

Apply the same shape to the others — the root `<div role=… className=…>` becomes `<Banner …>`, every `text-<color>-*`/`bg-<color>-*`/`border-<color>-*` class on inner elements is removed (the tone supplies them), buttons become `<Button size="sm" variant="secondary">` in `actions`:

| File | `tone` | `title` | `icon` | `actions` / `children` |
|---|---|---|---|---|
| `NewVersionBanner.tsx` | `info` | "A new version is available" (existing text) | `<RefreshCw size={16} />` | Reload → `actions={<Button size="sm" onClick={onReload}>Reload</Button>}` |
| `DaemonRestartBanner.tsx` | `warning` | existing text | `<Loader2 size={16} className="animate-spin" />` | none |
| `GitCredentialBanner.tsx` | `danger` | one `Banner` per alert (map inside a fragment) with the repo in `<span className="font-mono">` | default | existing buttons in `actions` |
| `UpdatePendingBanner.tsx` | `info` | `{title}` | `<RefreshCw size={18} className="animate-spin" />` | existing detail line as `children` |
| `DeployPanel.tsx` | `warning` | the existing collapsible header button stays as `title` (a `<button>` is valid inside) | default | the existing commit list + errors as `children`; the deploy button in `actions` |

`aria-live`/`role` behavior is preserved: banners that were `role="alert"` pass `role="alert"`.

- [ ] **Step 5: Mount `BannerStack` at the three existing positions**

In `web/src/App.tsx` replace

```tsx
      <GlobalUpdateBanner />
      <DaemonRestartBanner />
      <GitCredentialBanner />
```

with

```tsx
      <BannerStack className="px-4 pt-2" items={[
        { id: 'update', priority: 30, node: <GlobalUpdateBanner /> },
        { id: 'daemon', priority: 90, node: <DaemonRestartBanner /> },
        { id: 'git-credential', priority: 80, node: <GitCredentialBanner /> },
      ]} />
```

(each of those components already returns `null` when idle — the stack collapses to nothing). In `Dashboard.tsx` (lines 748–760) and `DesktopShell.tsx` (the `AllLimitedBanner` + `DeployPanel` wrappers) do the same with `{ id: 'all-limited', priority: 70, node: allLimitedReset !== undefined ? <AllLimitedBanner …/> : null }`, `{ id: 'input-hold', priority: 60, node: inputHold.holding ? <UpdatePendingBanner …/> : null }` (Dashboard only), `{ id: 'deploy', priority: 50, node: <DeployPanel /> }`. Remove the `[&:empty]:hidden` wrapper div in `DesktopShell` — `BannerStack` returns `null` when empty.

- [ ] **Step 6: Run tests, typecheck, lint**

Run: `npm --prefix web test && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS. Banner tests that asserted on amber/sky classes now assert the rendered text/role only.

- [ ] **Step 7: Commit**

```bash
git add -A web/src
git commit -m "feat(ui): Banner + BannerStack; the seven banners and three mount points use them

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 16: `Card`, `Badge`, `StatusPill`, `UsageRing`, `Kbd`; pills adopt them; `statusTier` on tokens

**Files:**
- Create: `web/src/ui/Card.tsx`, `Badge.tsx`, `StatusPill.tsx`, `UsageRing.tsx`, `Kbd.tsx`, `Pills.test.tsx`
- Modify: `web/src/session/statusTier.ts:17-20,99-105`, `web/src/components/UsageBadge.tsx:162-187` (`StatusPill` adapter), `web/src/components/PrPill.tsx`, `web/src/components/HelpDrawer.tsx` (`<kbd>` → `Kbd`)

**Interfaces:**
- Produces:
  ```ts
  type Tone = 'success'|'warning'|'danger'|'info'|'neutral'|'accent'
  Card: { tone?: Tone (left rail); interactive?: boolean; padding?: 'none'|'sm'|'md'; as?: 'div'|'section'|'article'|'li' } & HTMLAttributes
  Badge: { tone?: Tone; count?: number; dot?: boolean; children? }
  StatusPill: { label: string; Icon: ComponentType<{size?:number; className?:string}>; tone: Tone; pulse?: boolean; className? }
  UsageRing: { value: number (0..1); label: string; size?: number }
  Kbd: { keys: string }
  statusTier: TIER_TONE: Record<StatusTier, Tone>; textClass values become text-<tone>; TIER_ACCENT uses bg-<tone>
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Pills.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { Loader2 } from 'lucide-react'
import { Card } from './Card'
import { Badge } from './Badge'
import { StatusPill } from './StatusPill'
import { UsageRing } from './UsageRing'
import { Kbd } from './Kbd'

expect.extend(toHaveNoViolations)

describe('pills & surfaces', () => {
  it('render on tokens with accessible names', async () => {
    const { container } = render(
      <Card tone="warning" interactive>
        <StatusPill label="Working…" Icon={Loader2} tone="success" />
        <Badge tone="neutral" count={12} />
        <UsageRing value={0.42} label="Context 42%" />
        <Kbd keys="Cmd+K" />
      </Card>,
    )
    expect(screen.getByLabelText('Status: Working…').className).toContain('text-success')
    expect(screen.getByText('12').className).toContain('bg-neutral-wash')
    expect(screen.getByRole('img', { name: 'Context 42%' })).toBeInTheDocument()
    expect(container.querySelector('[data-tone="warning"]')).not.toBeNull()
    expect(container.innerHTML).not.toMatch(/dark:|gray-|amber-/)
    expect(await axe(container)).toHaveNoViolations()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/Pills.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement the five files**

```tsx
// web/src/ui/Kbd.tsx
export function Kbd({ keys }: { keys: string }) {
  return (
    <kbd className="inline-block rounded-sm border border-edge-strong bg-surface-raised px-1.5 py-0.5 text-xs font-mono text-ink">
      {keys}
    </kbd>
  )
}
```

```tsx
// web/src/ui/Badge.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral' | 'accent'

export const TONE_TEXT: Record<Tone, string> = {
  success: 'text-success', warning: 'text-warning', danger: 'text-danger', info: 'text-info', neutral: 'text-ink-muted', accent: 'text-accent',
}
export const TONE_WASH: Record<Tone, string> = {
  success: 'bg-success-wash', warning: 'bg-warning-wash', danger: 'bg-danger-wash', info: 'bg-info-wash', neutral: 'bg-neutral-wash', accent: 'bg-accent-wash',
}
export const TONE_FILL: Record<Tone, string> = {
  success: 'bg-success', warning: 'bg-warning', danger: 'bg-danger', info: 'bg-info', neutral: 'bg-neutral', accent: 'bg-accent',
}

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone
  /** Numeric pill (tabular). */
  count?: number
  /** 8px dot only (e.g. "needs attention"). */
  dot?: boolean
}

export function Badge({ tone = 'neutral', count, dot, className, children, ...props }: BadgeProps) {
  if (dot) return <span aria-hidden="true" className={cn('inline-block w-2 h-2 rounded-pill', TONE_FILL[tone], className)} {...props} />
  return (
    <span className={cn('inline-flex items-center rounded-pill px-1.5 py-0.5 text-xs font-medium leading-none tabular-nums', TONE_WASH[tone], TONE_TEXT[tone], className)} {...props}>
      {count ?? children}
    </span>
  )
}
```

```tsx
// web/src/ui/StatusPill.tsx
import type { ComponentType } from 'react'
import { cn } from '@/lib/utils'
import { TONE_TEXT, TONE_WASH, type Tone } from './Badge'

export interface StatusPillProps {
  label: string
  Icon: ComponentType<{ size?: number; className?: string }>
  tone: Tone
  /** Action-required tiers animate (motion-safe). */
  pulse?: boolean
  className?: string
}

export function StatusPill({ label, Icon, tone, pulse, className }: StatusPillProps) {
  return (
    <span
      aria-label={`Status: ${label}`}
      className={cn('inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-xs font-medium', TONE_TEXT[tone], TONE_WASH[tone], pulse && 'motion-safe:animate-pulse', className)}
    >
      <Icon size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}
```

```tsx
// web/src/ui/UsageRing.tsx
import { cn } from '@/lib/utils'

/** Context/plan usage ring (Claude Code Desktop's "usage ring"). value is 0..1. */
export function UsageRing({ value, label, size = 18, className }: { value: number; label: string; size?: number; className?: string }) {
  const v = Math.max(0, Math.min(1, value))
  const r = (size - 3) / 2
  const c = 2 * Math.PI * r
  const tone = v >= 0.9 ? 'stroke-danger' : v >= 0.7 ? 'stroke-warning' : 'stroke-accent'
  return (
    <svg role="img" aria-label={label} width={size} height={size} viewBox={`0 0 ${size} ${size}`} className={cn('shrink-0', className)}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={3} className="stroke-edge" />
      <circle
        cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={3} strokeLinecap="round"
        strokeDasharray={`${c * v} ${c}`} transform={`rotate(-90 ${size / 2} ${size / 2})`} className={tone}
      />
    </svg>
  )
}
```

```tsx
// web/src/ui/Card.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'
import { TONE_FILL, type Tone } from './Badge'

const PAD = { none: '', sm: 'p-3', md: 'p-4' } as const

export interface CardProps extends React.HTMLAttributes<HTMLElement> {
  /** Left status rail. */
  tone?: Tone
  /** Hover/focus affordance for clickable cards. */
  interactive?: boolean
  padding?: keyof typeof PAD
  as?: 'div' | 'section' | 'article' | 'li'
}

export const Card = React.forwardRef<HTMLElement, CardProps>(
  ({ tone, interactive, padding = 'md', as: Tag = 'div', className, children, ...props }, ref) => (
    <Tag
      ref={ref as React.Ref<HTMLDivElement>}
      data-tone={tone}
      className={cn(
        'relative rounded-lg border border-edge bg-surface text-ink',
        tone && 'pl-[calc(var(--spacing)*4+3px)]',
        PAD[padding],
        interactive && 'transition-colors hover:border-edge-strong focus-within:ring-2 focus-within:ring-accent',
        className,
      )}
      {...props}
    >
      {tone && <span aria-hidden="true" className={cn('absolute left-0 top-2 bottom-2 w-[3px] rounded-r-sm', TONE_FILL[tone])} />}
      {children}
    </Tag>
  ),
)
Card.displayName = 'Card'
```

- [ ] **Step 4: Put `statusTier.ts` on tokens**

Replace lines 17–20 with

```ts
const NEEDS_YOU = 'text-warning'
const ERROR = 'text-danger'
const BUSY = 'text-success'
const CALM = 'text-ink-muted'
```

and `TIER_ACCENT` (lines 99–105) with

```ts
export const TIER_TONE: Record<StatusTier, 'success' | 'warning' | 'danger' | 'neutral' | 'info'> = {
  busy: 'success', 'needs-you': 'warning', error: 'danger', 'your-turn': 'neutral', waiting: 'info',
}
export const TIER_ACCENT: Record<StatusTier, { rail: string; dot: string; pulse: boolean }> = {
  busy: { rail: 'bg-success', dot: 'bg-success', pulse: false },
  'needs-you': { rail: 'bg-warning', dot: 'bg-warning', pulse: true },
  error: { rail: 'bg-danger', dot: 'bg-danger', pulse: true },
  'your-turn': { rail: 'bg-neutral/40', dot: 'bg-neutral', pulse: false },
  waiting: { rail: 'bg-info/40', dot: 'bg-info', pulse: false },
}
```

- [ ] **Step 5: Adapt `UsageBadge.StatusPill` and `PrPill`**

In `UsageBadge.tsx`, replace the `StatusPill` function (lines 162–187) with an adapter that keeps the export and props:

```tsx
import { StatusPill as Pill } from '@/ui/StatusPill'
import { TIER_TONE } from '@/session/statusTier'

const ACCOUNT_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  ok: 'success', limited: 'warning', 'needs-reauth': 'danger', 'needs-relogin': 'danger', 'not-max': 'neutral', unknown: 'neutral',
}

export function StatusPill({ status, className, interruptedByRestart }: StatusPillProps) {
  const account = (ACCOUNT_STATUS_MAP as Record<string, StatusConfig>)[status]
  if (account) return <Pill label={account.label} Icon={account.Icon} tone={ACCOUNT_TONE[status] ?? 'neutral'} className={className} />
  const p = statusPresentation(status, { interruptedByRestart })
  return <Pill label={p.label} Icon={p.Icon} tone={TIER_TONE[p.tier]} pulse={p.tier === 'needs-you' || p.tier === 'error'} className={className} />
}
```

and delete the now-unused `pillTone` helper and the `textClass` entries of `ACCOUNT_STATUS_MAP` (keep `label` and `Icon`; adjust the `StatusConfig` type). In `PrPill.tsx`, replace `STATE_STYLES` with

```ts
import { TONE_TEXT, TONE_WASH } from '@/ui/Badge'
const STATE_TONE = { open: 'success', draft: 'neutral', merged: 'info', closed: 'danger' } as const
```

and the `className` to `cn('inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent', TONE_WASH[STATE_TONE[pr.state]], TONE_TEXT[STATE_TONE[pr.state]])`; `ChecksIcon` colors become `text-success` / `text-danger` / `text-warning`, the rocket `text-success`. In `HelpDrawer.tsx`, replace the inline `<kbd className={cn(…)}>{b.keys}</kbd>` with `<Kbd keys={b.keys} />`.

- [ ] **Step 6: Run tests, typecheck, lint**

Run: `npm --prefix web test && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS. `statusTier.test.ts`, `a11y.test.tsx`, `PrPill`/`SessionCard` tests that named a palette class are updated to the token class (`text-warning`, `bg-success-wash`…).

- [ ] **Step 7: Commit**

```bash
git add -A web/src
git commit -m "feat(ui): Card, Badge, StatusPill, UsageRing, Kbd; status tiers and PR pills on tokens

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 17: Migrate the confirm/edit dialogs to `Dialog`

**Files:**
- Modify: `web/src/components/PermanentDeleteDialog.tsx` (full rewrite below), `web/src/components/session/ModelSwitchDialog.tsx:20-70`, `web/src/components/ManualDispatchDialog.tsx:66-85`, `web/src/components/AccountsManager.tsx:95-135` (the delete-account dialog), `web/src/components/ArchivedManager.tsx:70-115` (the bulk permanent-delete dialog), `web/src/components/Dashboard.tsx:130-190` (`NewSessionDialog`) and `:401-440` (`RenameDialog`), `web/src/components/AdoptPanel.tsx:27-60` (`ConfirmDialog`)

**Interfaces:**
- Consumes: `Dialog` (Task 13), `Button` (Task 10).

- [ ] **Step 1: Rewrite `PermanentDeleteDialog.tsx` — the reference migration**

```tsx
/**
 * PermanentDeleteDialog — single-session destructive confirm.
 *
 * Shared source of truth for the "permanently delete this session" modal, used by
 * Dashboard (session card + live session), DesktopShell, and the in-session
 * SessionActions menu. Focus starts on Cancel; Esc/backdrop close; Dialog traps focus.
 */
import { useRef } from 'react'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

interface PermanentDeleteDialogProps {
  sessionName: string
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

export function PermanentDeleteDialog({ sessionName, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  return (
    <Dialog open onClose={onClose} title="Permanently delete session" size="sm" initialFocus={cancelRef}>
      <p className="text-sm text-ink-muted">
        Permanently delete <span className="font-medium text-ink">'{sessionName}'</span>? This removes the
        session folder and all its data. This can't be undone.
      </p>
      <div className="flex gap-3 justify-end pt-2">
        <Button ref={cancelRef} type="button" variant="secondary" onClick={onClose} disabled={isPending}>Cancel</Button>
        <Button type="button" variant="danger" size="lg" onClick={onConfirm} loading={isPending}>
          {isPending ? 'Deleting…' : 'Delete permanently'}
        </Button>
      </div>
    </Dialog>
  )
}
```

- [ ] **Step 2: Run its tests**

Run: `npm --prefix web run test -- src/components/__tests__ src/components/session/__tests__ -t "delete"`
Expected: PASS (tests find the dialog by role/name and the buttons by text; a test asserting a `bg-red-600` class is changed to check `variant` output `bg-danger`).

- [ ] **Step 3: Apply the same transformation to the other seven**

For each file: (a) delete the `useEffect` that adds the `keydown` listener and the `dialogRef`; (b) replace the outer `fixed inset-0 …` `<div role="dialog">` and the inner panel `<div ref={dialogRef} className=…>` with one `<Dialog open onClose={…} title=… size=… initialFocus=…>`; (c) delete the `<h2 id="…-title">` (Dialog renders the title); (d) strip `dark:`/gray/blue classes from the remaining content in favor of `text-ink` / `text-ink-muted` / `Input` / `Select` / `Field` from Task 11 where the markup is a plain labelled control.

| File | `title` | `size` | `initialFocus` | `dismissable` / backdrop condition |
|---|---|---|---|---|
| `ModelSwitchDialog.tsx` | existing `#model-switch-dialog-title` text | `md` | existing first-focus ref | default |
| `ManualDispatchDialog.tsx` | "Custom dispatch" | `lg` | none | default; remove the `✕` button (Dialog has Esc/backdrop; add `<Button variant="ghost" onClick={onClose}>Close</Button>` to the footer) |
| `AccountsManager.tsx` delete-account dialog | existing `#delete-account-dialog-title` text | `sm` | cancel button ref | default |
| `ArchivedManager.tsx` bulk delete dialog | existing `#perm-delete-dialog-title` text | `sm` | cancel ref | `dismissable={!isPending}` |
| `Dashboard.tsx` `NewSessionDialog` | "New session" (existing) | `md` | `firstInputRef` | default; the `inputClass`/`selectClass` constants become `Input`/`Select` components |
| `Dashboard.tsx` `RenameDialog` | "Rename session" (existing) | `sm` | the name input ref | default |
| `AdoptPanel.tsx` `ConfirmDialog` | "Take over this session?" | `md` | none | default |

- [ ] **Step 4: Run the whole suite, typecheck, lint**

Run: `npm --prefix web test && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS. `App.dialogs.test.tsx` and `DesktopShell.test.tsx` still open/close these dialogs by role and name.

- [ ] **Step 5: Commit**

```bash
git add -A web/src
git commit -m "refactor(ui): confirm/edit dialogs on the Dialog primitive (one focus trap)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 18: Migrate the remaining overlays: `AddAccountDialog`, `ItemEditorDialog`, `McpServerDialog`, `CommandResultOverlay`, `DeployOverlay`, `LegendPanel`, `HelpDrawer`

**Files:**
- Modify: `web/src/components/AddAccountDialog.tsx:149-170`, `web/src/components/library/ItemEditorDialog.tsx:169-190`, `web/src/components/library/McpServerDialog.tsx:226-245`, `web/src/components/session/CommandResultOverlay.tsx:11-25`, `web/src/components/DeployOverlay.tsx:33-37`, `web/src/pipeline/LegendPanel.tsx:73-95`, `web/src/components/HelpDrawer.tsx` (rewrite on `Sheet`)

- [ ] **Step 1: `HelpDrawer` on `Sheet` (reference)**

Replace everything from `export function HelpDrawer` to the end of that function with:

```tsx
export function HelpDrawer({ onClose }: { onClose: () => void }) {
  return (
    <Sheet open onClose={onClose} title="Help & Shortcuts" side="right">
      <div className="space-y-6">
        <ShortcutSection title="Compose mode" bindings={shortcutsByGroup('compose')} />
        <ShortcutSection title="List-focus mode" bindings={shortcutsByGroup('list-mode')} />
        <ShortcutSection title="Group headers" bindings={shortcutsByGroup('list-mode-header')} />
        <ShortcutSection title="In-session" bindings={shortcutsByGroup('in-session')} />
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">Navigation</h3>
          <dl className="space-y-1">
            {NAV_MAP.map((n) => (
              <div key={n.name} className="flex items-baseline gap-3">
                <dt className="shrink-0 min-w-28 text-sm font-medium text-ink">{n.name}</dt>
                <dd className="text-sm text-ink-muted">{n.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">Tips</h3>
          <dl className="space-y-2">
            {TIPS.map((t) => (
              <div key={t.title}>
                <dt className="text-sm font-medium text-ink">{t.title}</dt>
                <dd className="text-sm text-ink-muted">{t.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </Sheet>
  )
}
```

`ShortcutSection` keeps its structure with `text-ink`/`text-ink-muted` and `<Kbd keys={b.keys} />`. Remove the `useEffect`/`useRef`/`X`/`cn` imports that are now unused; the `data-testid="help-drawer-backdrop"` used by tests becomes `sheet-backdrop` (update `useGlobalHelpKey`/`DesktopShell` tests accordingly).

- [ ] **Step 2: The rest**

| File | Replacement |
|---|---|
| `AddAccountDialog.tsx` | `<Dialog open onClose={onClose} title=… size={showVnc ? 'xl' : 'sm'} initialFocus={…} dismissable={!isRunning && !copilotStarted} className="transition-[max-width] motion-reduce:transition-none">` — keep the existing title text (`#add-account-dialog-title`) |
| `ItemEditorDialog.tsx` | `<Dialog open onClose={onClose} title={title} size="lg">`; the name/body inputs → `Field` + `Input` / `Textarea` |
| `McpServerDialog.tsx` | `<Dialog open onClose={onClose} title={title} size="lg">`; inputs → `Field` + `Input` / `Select` |
| `CommandResultOverlay.tsx` | `<Dialog open onClose={onClose} aria-label="Command result" size="md">` — content unchanged apart from color classes → `text-ink`/`text-ink-muted` |
| `DeployOverlay.tsx` | `<Dialog open onClose={() => {}} aria-label="Deploying" dismissable={false} size="md" className="w-[28rem]">` — the `z-[60]`/`backdrop-blur` wrapper goes; content unchanged apart from color classes |
| `LegendPanel.tsx` | `<Sheet open={open} onClose={onClose} title="Legend" side="right" width={420}>` replaces the `settings-overlay` + `settings-panel` pair and the `useFocusTrap` call; the `settings-body` content stays; delete the `.legend-panel`/`.settings-*` rules only if no other pipeline component uses them (`grep -n 'settings-panel\|settings-overlay' web/src/pipeline`) |

- [ ] **Step 3: Verify the acceptance greps**

```bash
grep -rl 'fixed inset-0' web/src --include='*.tsx' | grep -v __tests__     # expect: web/src/ui/Dialog.tsx web/src/ui/Sheet.tsx
grep -rl 'querySelectorAll<HTMLElement>' web/src --include='*.ts' --include='*.tsx' | grep -v __tests__   # expect: web/src/ui/useFocusTrap.ts
grep -rn 'role="dialog"' web/src --include='*.tsx' | grep -v __tests__     # expect: only ui/Dialog.tsx and ui/Sheet.tsx
```

- [ ] **Step 4: Run everything**

Run: `npm --prefix web test && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A web/src
git commit -m "refactor(ui): remaining overlays on Dialog/Sheet; HelpDrawer and LegendPanel are Sheets

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 19: Page-contract primitives (`PageHeader`, `PageContent`, `EmptyState`, `Skeleton`) and the `EmptyState` adoption

**Files:**
- Create: `web/src/ui/PageHeader.tsx`, `PageContent.tsx`, `EmptyState.tsx`, `Skeleton.tsx`, `Page.test.tsx`
- Modify: `web/src/components/EmptyState.tsx`

**Interfaces:**
- Produces:
  ```ts
  PageHeader: { title: string; meta?: ReactNode; actions?: ReactNode; className? }   // renders the page's <h1>; sets document.title = `${title} — ccswitch`
  PageContent: { busy?: boolean; scroll?: boolean (default true); className?; children }  // flex-1 min-h-0; aria-busy; role=status live region announcing "Loaded"
  EmptyState: { icon?: ReactNode; title: string; description?: ReactNode; action?: ReactNode }
  Skeleton: { lines?: number; className? }  // animate-pulse rows, aria-hidden
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/ui/Page.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PageHeader } from './PageHeader'
import { PageContent } from './PageContent'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'

describe('page primitives', () => {
  it('PageHeader renders the h1 and sets document.title', () => {
    render(<PageHeader title="Automations" actions={<button>New</button>} />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Automations')
    expect(document.title).toBe('Automations — ccswitch')
  })
  it('PageContent exposes busy state and Skeleton is decorative', () => {
    const { rerender } = render(<PageContent busy><Skeleton lines={3} /></PageContent>)
    expect(screen.getByTestId('page-content')).toHaveAttribute('aria-busy', 'true')
    expect(document.querySelector('[aria-hidden="true"] .animate-pulse')).not.toBeNull()
    rerender(<PageContent><EmptyState title="No automations yet" /></PageContent>)
    expect(screen.getByRole('status')).toHaveTextContent('Loaded')
    expect(screen.getByText('No automations yet')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm --prefix web run test -- src/ui/Page.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement**

```tsx
// web/src/ui/PageHeader.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface PageHeaderProps { title: string; meta?: React.ReactNode; actions?: React.ReactNode; className?: string }

/** Zone 1 of every page: the only h1, synced to document.title (WCAG 2.4.2). */
export function PageHeader({ title, meta, actions, className }: PageHeaderProps) {
  React.useEffect(() => { document.title = `${title} — ccswitch` }, [title])
  return (
    <header className={cn('shrink-0 flex items-center gap-3 px-4 py-3 md:px-6 md:py-4 border-b border-edge', className)}>
      <div className="min-w-0 flex-1">
        <h1 className="text-lg font-semibold text-ink truncate">{title}</h1>
        {meta && <div className="text-sm text-ink-muted">{meta}</div>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </header>
  )
}
```

```tsx
// web/src/ui/PageContent.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface PageContentProps { busy?: boolean; scroll?: boolean; className?: string; children: React.ReactNode }

/** Zone 3: owns scrolling (Standard/Split/Reader) or clips (Workspace, scroll=false). */
export function PageContent({ busy = false, scroll = true, className, children }: PageContentProps) {
  return (
    <div data-testid="page-content" aria-busy={busy || undefined} className={cn('flex-1 min-h-0 flex flex-col', scroll ? 'overflow-y-auto px-4 py-4 md:px-6' : 'overflow-hidden', className)}>
      {children}
      <span role="status" aria-live="polite" className="sr-only">{busy ? 'Loading' : 'Loaded'}</span>
    </div>
  )
}
```

```tsx
// web/src/ui/EmptyState.tsx
import * as React from 'react'
import { cn } from '@/lib/utils'

export interface EmptyStateProps { icon?: React.ReactNode; title: string; description?: React.ReactNode; action?: React.ReactNode; className?: string }

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center gap-3 py-16 px-6', className)}>
      {icon && <div className="text-ink-faint">{icon}</div>}
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="text-sm text-ink-muted max-w-prose">{description}</p>}
      {action}
    </div>
  )
}
```

```tsx
// web/src/ui/Skeleton.tsx
import { cn } from '@/lib/utils'

export function Skeleton({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div aria-hidden="true" className={cn('space-y-2', className)}>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="h-4 rounded-sm bg-surface-raised animate-pulse motion-reduce:animate-none" style={{ width: `${100 - (i % 3) * 12}%` }} />
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Adopt in `web/src/components/EmptyState.tsx`**

```tsx
import { Plus } from 'lucide-react'
import { Button } from '@/ui/Button'
import { EmptyState as Empty } from '@/ui/EmptyState'

export function EmptyState({ onNew }: { onNew: () => void }) {
  return (
    <Empty
      title="No sessions yet"
      action={<Button onClick={onNew} size="lg" iconLeft={<Plus size={16} aria-hidden="true" />}>New Session</Button>}
    />
  )
}
```

- [ ] **Step 5: Run tests, typecheck, lint**

Run: `npm --prefix web run test -- src/ui/Page.test.tsx src/components/__tests__ && npm --prefix web run typecheck && npm --prefix web run lint`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A web/src
git commit -m "feat(ui): PageHeader, PageContent, EmptyState, Skeleton primitives (page contract seeds)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 20: PR 2 — acceptance, gates, PR

- [ ] **Step 1: Acceptance greps (spec §3.3)**

```bash
grep -rl 'fixed inset-0' web/src --include='*.tsx' | grep -v __tests__          # only ui/Dialog.tsx, ui/Sheet.tsx
grep -rl 'querySelectorAll<HTMLElement>' web/src | grep -v __tests__            # only ui/useFocusTrap.ts
ls web/src/ui                                                                    # Badge Banner BannerStack Button Card Dialog EmptyState Field IconButton Input Kbd Menu PageContent PageHeader Select Sheet Skeleton StatusPill Tabs Textarea UsageRing portal useFocusTrap (+ tests)
npm --prefix web run lint                                                        # clean, including ccswitch/no-raw-palette on src/ui
```

- [ ] **Step 2: All gates**

```bash
npm --prefix web test 2>&1 | tee /tmp/ui-pr2-web-test.log | tail -5
npm --prefix web run typecheck && npm --prefix web run lint
ruff check src/ server/ && mypy server/ --ignore-missing-imports && pytest tests/ -q && CCSWITCH_DEV=1 pytest server/tests/ -q -n 8
```

- [ ] **Step 3: Visual spot-check**

Dev server, both themes, both widths (390px and 1440px): open Help (Sheet slides from the right, Esc closes, focus returns to the `?` button); open New Session / Rename / Delete dialogs; switch a model mid-session; open the Pipeline Legend; trigger the all-limited banner (or temporarily render it) and confirm one stacked banner region.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(ui): primitive set under web/src/ui; overlays, banners, tabs and pills adopt it (sub-spec A, PR 2)" --body "$(cat <<'EOF'
## Summary
Sub-spec A PR 2 of 2 — docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md §3.
- `web/src/ui/`: Button, IconButton, Input/Textarea/Select/Field, Menu (1–9 selection), Dialog, Sheet, Tabs, Banner + BannerStack, Card, Badge, StatusPill, UsageRing, PageHeader, PageContent, EmptyState, Skeleton, Kbd; `useFocusTrap` promoted with `inert` + focus restore.
- 15 `role="dialog"` sites, 7 banners, `TabBar`/Settings tablist, status + PR pills migrated. `fixed inset-0` and the inline focus traps exist only in the primitives now.
- Closes audit F4.4 and the focus-trap / banner / empty-state rows of the parallel-implementations table.

## Test plan
- [x] web test / typecheck / lint (ccswitch/no-raw-palette green on src/ui)
- [x] backend gates unchanged
- [x] axe on every primitive
- [x] dev-server spot check: light/dark × 390/1440

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto "$(gh pr view --json number -q .number)"
```

---

## Self-review (done while writing)

- **Spec coverage:** §2.1 → Task 2 · §2.2 → Task 3 · §2.3 → Tasks 4–5 · §2.4 → Task 6 · §2.5 → Task 7 (scope amended in the spec to folders) · §2.6 → Task 1 · §2.7 → Task 8 · §3 table → Tasks 10–19 (PipelineSwitcher intentionally not migrated; spec amended) · §3.1 → Task 9 · §3.2 → Task 16 · §3.3 → Task 20.
- **Names used across tasks:** `getOverlayRoot` (12 → 13), `useFocusTrap(containerRef, active, { onClose, returnFocusRef?, inertRoot? })` (9 → 13), `Badge`/`TONE_*`/`Tone` (16 → 14, 15, 16), `Kbd` (16 → 12, 18), `IconButton` (10 → 13, 15), `Button` `variant` names (10 → 15, 17, 19), `TIER_TONE` (16 → `UsageBadge` adapter), `controlClass` (11 → 17/18 form migrations).
- **Ordering note:** Tasks 12 and 14 reference `Kbd` and `Badge` from Task 16; when executing in order, create those two small files at the point of first use (their full code is in Task 16 Step 3) and commit them with that task.
