# PWA Installable Shell (Phase A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ViberPit installable to a phone home screen — the missing prerequisite for iOS Web Push — and stop the notifications setting from dead-ending iPhone users.

**Architecture:** A pure `buildManifest()` function derives the web app manifest from the existing `BRAND_NAME` constant; a small Vite plugin emits it at build time. Home-screen icons are pre-rendered from `public/favicon.svg` by a committed script and checked in. The service worker is not touched — phase A caches nothing, so the frontend auto-update flow is unaffected.

**Tech Stack:** TypeScript, React 19, Vite 8, vitest, Playwright (icon rendering only — already a devDependency), FastAPI/Starlette, pytest.

**Spec:** [`docs/superpowers/specs/2026-09-08-pwa-installable-shell-design.md`](../specs/2026-09-08-pwa-installable-shell-design.md)

## Global Constraints

- **Never spell the brand outside `brand.ts`.** `src/ccswitch/brand.py`: *"Nothing outside this module may spell the brand."* Every user-visible string containing "ViberPit" — manifest `name`, install-instruction copy — must interpolate `BRAND_NAME` from `web/src/brand.ts`. This includes JSX copy.
- **Do not modify `web/public/sw.js`.** Phase A adds no `fetch` handler and no caching. A second service worker registration at the same scope would replace the push one and silently break notifications.
- **No new runtime or build dependencies.** Icon rendering uses `playwright-core`, already present via the `@playwright/test` devDependency, and runs manually — never as part of `npm run build`.
- **Run web commands in a subshell:** `(cd web && npm run test)`. A persisted `cd web` makes the repo's `PreToolUse` Bash hook fail every subsequent call, including the `cd` back.
- **Run the full web suite**, `(cd web && npm run test)`, not changed-files only: ~47 web test files manually mock `@/api/hooks`, so shared-component edits break tests in files you never touched.
- **`web/node_modules` must be a symlink to the main checkout** in this worktree (`ln -s /home/tony/GitHub/viberpit/web/node_modules web/node_modules`). Already created; it is gitignored.
- **Never build `web/dist` to test.** A built `dist` mounts the SPA catch-all and makes two server path-traversal tests return 405. Every assertion in this plan is reachable without it.
- **Theme colors are `#16171d`** (dark) and `#ffffff` (light), matching the existing `theme-color` metas in `web/index.html`.

---

## File Structure

| File | Responsibility |
|---|---|
| `web/scripts/gen-icons.mjs` (create) | One-shot, manually-run renderer: `favicon.svg` → home-screen PNGs |
| `web/public/icons/*.png` (create) | Committed rendered icons |
| `web/src/pwa/manifest.ts` (create) | Pure `buildManifest()` — the manifest's single source of truth |
| `web/src/pwa/__tests__/manifest.test.ts` (create) | Manifest correctness without a `dist` build |
| `web/vite.config.ts` (modify) | Vite plugin that emits `manifest.webmanifest` |
| `web/tsconfig.node.json` (modify) | Let the node-scope project see the files `vite.config.ts` imports |
| `web/index.html` (modify) | `<link rel="manifest">` + iOS status-bar meta |
| `server/spa.py` (modify) | Register the `.webmanifest` MIME type |
| `server/tests/test_spa_fallback.py` (modify) | Assert the served Content-Type |
| `web/src/api/push.ts` (modify) | `installState()` — platform/install detection |
| `web/src/api/__tests__/installState.test.ts` (create) | Platform matrix |
| `web/src/components/settings/NotificationsToggle.tsx` (modify) | Contextual install instructions instead of a dead control |
| `web/src/components/settings/__tests__/NotificationsToggle.test.tsx` (modify) | The user-visible fix |

---

### Task 1: Home-screen icons

Renders the three PNGs the manifest needs. Done first so Task 2's manifest references files that exist.

**Background you need:** `web/public/favicon.svg` is a 32×32-viewBox mark — two grey "session lanes" plus a cyan→violet gradient-stroked signal lane crossing between them, on a dark `rx="7"` rounded plate. **ImageMagick cannot render it**: its `svg` delegate points at `rsvg-convert`, which is not installed on this host, so it silently falls back to an internal renderer that drops the gradient path entirely and exits 0. Use Playwright's Chromium, verified working.

**Files:**
- Create: `web/scripts/gen-icons.mjs`
- Create: `web/public/icons/icon-192.png`, `web/public/icons/icon-512.png`, `web/public/icons/icon-512-maskable.png`

**Interfaces:**
- Consumes: nothing.
- Produces: three PNG paths, referenced by `buildManifest()` in Task 2 as `/icons/icon-192.png`, `/icons/icon-512.png`, `/icons/icon-512-maskable.png`.

- [ ] **Step 1: Write the generator script**

Create `web/scripts/gen-icons.mjs`:

```js
/**
 * Renders web/public/favicon.svg into the home-screen PNGs referenced by the
 * web app manifest (web/src/pwa/manifest.ts).
 *
 * Run MANUALLY after changing the mark, then commit the PNGs:
 *     (cd web && node scripts/gen-icons.mjs)
 *
 * Not part of `npm run build` — that would add an image-rendering step to every
 * CI build for assets that change roughly never.
 *
 * Chromium (via playwright-core, already a devDependency) is the renderer.
 * ImageMagick is NOT a substitute: without an rsvg-convert delegate it drops
 * the gradient-stroked signal lane and still exits 0.
 */
import { chromium } from 'playwright-core'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const svg = readFileSync(resolve(here, '../public/favicon.svg'), 'utf8')
const outDir = resolve(here, '../public/icons')

/** Android crops maskable icons to arbitrary shapes; only the centre 80% is
 *  guaranteed visible. Scale the mark to that safe zone and let the plate
 *  colour bleed to the edges. */
const MASKABLE_SAFE_ZONE = 0.8
const PLATE = '#0e0f14' // the favicon's rounded-plate fill

const targets = [
  { file: 'icon-192.png', size: 192, maskable: false },
  { file: 'icon-512.png', size: 512, maskable: false },
  { file: 'icon-512-maskable.png', size: 512, maskable: true },
]

const browser = await chromium.launch()
try {
  mkdirSync(outDir, { recursive: true })
  for (const { file, size, maskable } of targets) {
    const page = await browser.newPage({
      viewport: { width: size, height: size },
      deviceScaleFactor: 1,
    })
    const inner = maskable ? Math.round(size * MASKABLE_SAFE_ZONE) : size
    await page.setContent(`
      <style>
        html, body { margin: 0; padding: 0; }
        body {
          width: ${size}px; height: ${size}px;
          display: flex; align-items: center; justify-content: center;
          background: ${maskable ? PLATE : 'transparent'};
        }
        svg { display: block; width: ${inner}px; height: ${inner}px; }
      </style>
      ${svg}
    `)
    writeFileSync(resolve(outDir, file), await page.screenshot({ omitBackground: !maskable }))
    await page.close()
    console.log(`wrote ${file} (${size}x${size}${maskable ? ', maskable' : ''})`)
  }
} finally {
  await browser.close()
}
```

- [ ] **Step 2: Run it**

Run: `(cd web && node scripts/gen-icons.mjs)`
Expected: three `wrote …` lines, exit 0.

- [ ] **Step 3: Verify the icons are correct — LOOK at them**

Run: `file web/public/icons/*.png`
Expected: `PNG image data, 192 x 192`, `512 x 512`, `512 x 512`.

Then **open `web/public/icons/icon-512.png` and actually look at it.** It must show the cyan→violet signal lane crossing diagonally between two grey horizontal lanes. If you see only the two grey lanes, the gradient path was dropped and the renderer is wrong — do not proceed.

Then look at `icon-512-maskable.png`: same mark, visibly smaller within a solid dark square that reaches all four edges (no transparent corners, no rounded plate edge).

- [ ] **Step 4: Commit**

```bash
git add web/scripts/gen-icons.mjs web/public/icons
git commit -m "feat(pwa): render home-screen icons from the brand mark"
```

---

### Task 2: Manifest module, Vite plugin, and HTML wiring

**Files:**
- Create: `web/src/pwa/manifest.ts`
- Create: `web/src/pwa/__tests__/manifest.test.ts`
- Modify: `web/vite.config.ts`
- Modify: `web/tsconfig.node.json`
- Modify: `web/index.html`

**Interfaces:**
- Consumes: the three icon paths from Task 1; `BRAND_NAME` from `web/src/brand.ts`.
- Produces: `buildManifest(): WebAppManifest` exported from `@/pwa/manifest`, and a `dist/manifest.webmanifest` artifact served at `/manifest.webmanifest`.

- [ ] **Step 1: Write the failing test**

Create `web/src/pwa/__tests__/manifest.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { buildManifest } from '../manifest'
import { BRAND_NAME } from '@/brand'

describe('buildManifest', () => {
  it('names the app from the brand single source of truth', () => {
    const m = buildManifest()
    expect(m.name).toBe(BRAND_NAME)
    expect(m.short_name).toBe(BRAND_NAME)
  })

  it('is installable: standalone display, start_url, and a pinned id', () => {
    const m = buildManifest()
    expect(m.display).toBe('standalone')
    expect(m.start_url).toBe('/')
    // Pins app identity so a later start_url change updates the installed app
    // instead of installing a second one.
    expect(m.id).toBe('/')
  })

  it('ships both icon sizes browsers require, plus a maskable variant', () => {
    const icons = buildManifest().icons
    expect(icons.map((i) => i.sizes)).toEqual(
      expect.arrayContaining(['192x192', '512x512']),
    )
    expect(icons.every((i) => i.type === 'image/png')).toBe(true)
    expect(icons.some((i) => i.purpose === 'maskable')).toBe(true)
    expect(icons.every((i) => i.src.startsWith('/icons/'))).toBe(true)
  })

  it('matches the app background so the splash screen does not flash', () => {
    const m = buildManifest()
    expect(m.background_color).toBe('#16171d')
    expect(m.theme_color).toBe('#16171d')
  })
})
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `(cd web && npx vitest run src/pwa)`
Expected: FAIL — `Failed to resolve import "../manifest"`.

- [ ] **Step 3: Write the implementation**

Create `web/src/pwa/manifest.ts`:

```ts
/**
 * Web app manifest — the document that makes ViberPit installable to a phone
 * home screen, which is what unlocks Web Push on iOS.
 *
 * A pure function rather than a static public/manifest.webmanifest for two
 * reasons: the brand may be spelled in exactly one place (see brand.ts), and a
 * pure function is unit-testable without building web/dist — which we avoid,
 * since a built dist changes server test behaviour.
 *
 * Emitted at build time by the manifest plugin in vite.config.ts.
 */
import { BRAND_NAME } from '@/brand'

export interface ManifestIcon {
  src: string
  sizes: string
  type: 'image/png'
  purpose?: 'any' | 'maskable'
}

export interface WebAppManifest {
  id: string
  name: string
  short_name: string
  start_url: string
  scope: string
  display: 'standalone'
  theme_color: string
  background_color: string
  icons: ManifestIcon[]
}

/** Matches the dark `theme-color` meta in index.html, so the installed app's
 *  splash and chrome do not flash a different colour on launch. */
const APP_BACKGROUND = '#16171d'

export function buildManifest(): WebAppManifest {
  return {
    id: '/',
    name: BRAND_NAME,
    short_name: BRAND_NAME,
    start_url: '/',
    scope: '/',
    display: 'standalone',
    theme_color: APP_BACKGROUND,
    background_color: APP_BACKGROUND,
    icons: [
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512-maskable.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/pwa)`
Expected: PASS, 4 tests.

- [ ] **Step 5: Add the Vite plugin**

In `web/vite.config.ts`, add the import at the top alongside the existing ones:

```ts
import { buildManifest } from './src/pwa/manifest'
```

Add this function above `export default defineConfig({`:

```ts
/** Emits the web app manifest at build time from the same source the app's
 *  brand comes from. `apply: 'build'` because the dev server serves it from the
 *  configureServer hook instead — emitFile output does not exist in dev. */
function webManifestPlugin(): Plugin {
  const body = () => JSON.stringify(buildManifest(), null, 2)
  return {
    name: 'viberpit-web-manifest',
    apply: 'build',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'manifest.webmanifest', source: body() })
    },
  }
}
```

Add `Plugin` to the vite type import at the top of the file:

```ts
import { defineConfig, type Plugin } from 'vite'
```

Register it in the plugins array:

```ts
  plugins: [react(), tailwindcss(), webManifestPlugin()],
```

- [ ] **Step 6: Let the node-scope TypeScript project see the imported files**

`web/tsconfig.node.json` currently has `"include": ["vite.config.ts"]`. Importing `src/pwa/manifest.ts` from `vite.config.ts` makes `tsc -b` fail with *"File … is not listed within the file list of project"*. Change the include to:

```json
  "include": ["vite.config.ts", "src/pwa/manifest.ts", "src/brand.ts"]
```

Both files are pure TypeScript with no DOM references, so they typecheck cleanly under this project's `"lib": ["ES2023"]`.

- [ ] **Step 7: Wire up index.html**

In `web/index.html`, immediately after the existing `<link rel="apple-touch-icon" …>` line, add:

```html
    <link rel="manifest" href="/manifest.webmanifest" />
    <!-- Draw the installed app under the notch rather than letterboxing it;
         pairs with viewport-fit=cover below. -->
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
```

- [ ] **Step 8: Verify typecheck, lint, and a real build emit the manifest**

Run: `(cd web && npm run typecheck && npm run lint && npm run build)`
Expected: all three pass.

Run: `cat web/dist/manifest.webmanifest`
Expected: JSON with `"name": "ViberPit"`, `"display": "standalone"`, three icons.

Then **remove the build output** so it cannot affect the server suite:

Run: `rm -rf web/dist`

- [ ] **Step 9: Commit**

```bash
git add web/src/pwa web/vite.config.ts web/tsconfig.node.json web/index.html
git commit -m "feat(pwa): emit a web app manifest derived from the brand constant"
```

---

### Task 3: Serve the manifest with the right MIME type

Python's `mimetypes` has no entry for `.webmanifest`, so Starlette's `StaticFiles` would serve it as `text/plain` and Chrome would reject the manifest.

**Files:**
- Modify: `server/spa.py`
- Modify: `server/tests/test_spa_fallback.py`

**Interfaces:**
- Consumes: the `manifest.webmanifest` filename from Task 2.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_spa_fallback.py`:

```python
@pytest.mark.asyncio
async def test_webmanifest_is_served_as_manifest_json(tmp_path):
    """Python's mimetypes has no .webmanifest entry, so without an explicit
    registration StaticFiles serves it as text/plain and Chrome rejects it."""
    dist = _make_dist(tmp_path)
    (dist / "manifest.webmanifest").write_text('{"name": "ViberPit"}')
    app = FastAPI()
    mount_spa(app, dist)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests/test_spa_fallback.py -v`
Expected: the new test FAILS with a content-type of `text/plain` (the four existing tests pass).

- [ ] **Step 3: Register the MIME type**

In `server/spa.py`, add `import mimetypes` to the imports and, immediately below them, before `class SPAStaticFiles`:

```python
# Python's mimetypes table has no `.webmanifest` entry, so StaticFiles would
# guess text/plain and Chrome would refuse the manifest (and with it, the
# install prompt). Registered at import so any StaticFiles mount inherits it.
mimetypes.add_type("application/manifest+json", ".webmanifest")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests/test_spa_fallback.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add server/spa.py server/tests/test_spa_fallback.py
git commit -m "fix(spa): serve .webmanifest as application/manifest+json"
```

---

### Task 4: Install-state detection

**Files:**
- Modify: `web/src/api/push.ts`
- Create: `web/src/api/__tests__/installState.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `type InstallState = 'installed' | 'needs-install' | 'not-required'` and `installState(): InstallState`, both exported from `@/api/push` and consumed by Task 5.

**Semantics:**
- `installed` — running as a home-screen/standalone app. Push works.
- `needs-install` — iOS in a browser tab. Push is impossible until the user installs; this is not a "not supported" case.
- `not-required` — every other platform. A tab is fine.

- [ ] **Step 1: Write the failing test**

Create `web/src/api/__tests__/installState.test.ts`:

```ts
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { installState } from '../push'

const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15'
const DESKTOP_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0'

/** jsdom defines navigator.userAgent/platform/maxTouchPoints on the prototype as
 *  non-configurable getters, so `vi.spyOn(navigator, 'userAgent', 'get')` throws
 *  "property is not declared configurable". Redefining the own property works. */
function setEnv(opts: {
  ua: string
  standaloneMedia?: boolean
  navigatorStandalone?: boolean
  platform?: string
  maxTouchPoints?: number
}) {
  const define = (prop: string, value: unknown) =>
    Object.defineProperty(navigator, prop, { value, configurable: true, writable: true })
  define('userAgent', opts.ua)
  define('platform', opts.platform ?? 'iPhone')
  define('maxTouchPoints', opts.maxTouchPoints ?? 5)
  define('standalone', opts.navigatorStandalone)
  window.matchMedia = vi.fn().mockReturnValue({ matches: opts.standaloneMedia ?? false })
}

describe('installState', () => {
  // Every test calls setEnv with a complete environment, so no restore is
  // needed between them — but clear the matchMedia mock's call history.
  beforeEach(() => { vi.clearAllMocks() })

  it('is installed when the display-mode media query matches', () => {
    setEnv({ ua: DESKTOP_UA, standaloneMedia: true })
    expect(installState()).toBe('installed')
  })

  it('is installed on iOS via the legacy navigator.standalone flag', () => {
    setEnv({ ua: IPHONE_UA, navigatorStandalone: true })
    expect(installState()).toBe('installed')
  })

  it('needs install for iOS in a browser tab', () => {
    setEnv({ ua: IPHONE_UA })
    expect(installState()).toBe('needs-install')
  })

  it('needs install for iPadOS, which reports itself as a Mac', () => {
    // iPadOS 13+ sends a desktop Safari UA; the touch-points check is the
    // only reliable way to tell it from a real Mac.
    setEnv({
      ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      platform: 'MacIntel',
      maxTouchPoints: 5,
    })
    expect(installState()).toBe('needs-install')
  })

  it('does not require install on a real Mac', () => {
    setEnv({
      ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      platform: 'MacIntel',
      maxTouchPoints: 0,
    })
    expect(installState()).toBe('not-required')
  })

  it('does not require install on desktop Chrome', () => {
    setEnv({ ua: DESKTOP_UA, platform: 'Linux x86_64', maxTouchPoints: 0 })
    expect(installState()).toBe('not-required')
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `(cd web && npx vitest run src/api/__tests__/installState.test.ts)`
Expected: FAIL — `installState is not a function` / no matching export.

- [ ] **Step 3: Write the implementation**

Append to `web/src/api/push.ts`:

```ts
/**
 * Whether this browsing context can receive Web Push as-is.
 *
 * `needs-install` is the case that matters: iOS Safari delivers Web Push only
 * to a web app added to the Home Screen, never to an ordinary tab. That is a
 * "not yet" — distinct from a browser that genuinely cannot.
 */
export type InstallState = 'installed' | 'needs-install' | 'not-required'

/** Running as an installed app: the standard media query, or Safari's
 *  pre-standard flag, which older iOS still reports. */
function isStandalone(): boolean {
  if (typeof window === 'undefined') return false
  if (window.matchMedia?.('(display-mode: standalone)').matches) return true
  return (window.navigator as Navigator & { standalone?: boolean }).standalone === true
}

/** iOS or iPadOS. iPadOS 13+ sends a desktop Safari UA and reports
 *  `platform === 'MacIntel'`, so touch points are the only reliable tell. */
function isIos(): boolean {
  if (typeof navigator === 'undefined') return false
  if (/iPad|iPhone|iPod/.test(navigator.userAgent)) return true
  return navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
}

export function installState(): InstallState {
  if (isStandalone()) return 'installed'
  return isIos() ? 'needs-install' : 'not-required'
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `(cd web && npx vitest run src/api/__tests__/installState.test.ts)`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/push.ts web/src/api/__tests__/installState.test.ts
git commit -m "feat(pwa): detect installed vs needs-install browsing contexts"
```

---

### Task 5: Tell the truth in the notifications setting

Today `NotificationsToggle` branches on `pushSupported()` alone. On iOS in a tab that produces either an Enable button that cannot succeed, or the message *"not supported in this browser (needs HTTPS + a modern browser)"* — which is false. The truth is "supported, once you install it."

**Files:**
- Modify: `web/src/components/settings/NotificationsToggle.tsx`
- Modify: `web/src/components/settings/__tests__/NotificationsToggle.test.tsx`

**Interfaces:**
- Consumes: `installState()` and `InstallState` from Task 4; `BRAND_NAME` from `@/brand`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing tests**

Add to `web/src/components/settings/__tests__/NotificationsToggle.test.tsx`, inside the existing `describe` block:

```ts
  // NOTE: assert against `container.textContent`, not `getByText`. The copy
  // interpolates {BRAND_NAME}, which splits "Open ViberPit from your Home
  // Screen" across three text nodes — getByText matches per-node and would miss
  // it, while /home screen/i would match two elements and throw.

  it('offers install instructions, not a dead control, for iOS in a tab', async () => {
    const { installState } = await import('@/api/push')
    vi.mocked(installState).mockReturnValue('needs-install')
    const { container } = render(<NotificationsToggle />)
    expect(container.textContent).toMatch(/add to home screen/i)
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
    expect(container.textContent).not.toMatch(/not supported/i)
  })

  it('states the required order: install, reopen, then enable', async () => {
    const { installState } = await import('@/api/push')
    vi.mocked(installState).mockReturnValue('needs-install')
    const { container } = render(<NotificationsToggle />)
    // Enabling in the Safari tab does not carry over to the installed app, so
    // the copy has to say so — this is the whole point of the branch.
    // Built from BRAND_NAME, not the literal — the brand is spelled in one
    // place, and that rule holds in tests too.
    expect(container.textContent).toMatch(
      new RegExp(`open ${BRAND_NAME} from your home screen`, 'i'),
    )
    expect(container.textContent).toMatch(/order matters/i)
  })
```

At the top of the file, add `BRAND_NAME` to the imports:

```ts
import { BRAND_NAME } from '@/brand'
```

and extend the existing mocks with a partial mock of `@/api/push` so `installState` is controllable while the real `pushSupported`/`enablePush` behaviour the existing tests rely on is preserved:

```ts
vi.mock('@/api/push', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/push')>()),
  installState: vi.fn(() => 'not-required' as const),
}))
```

The default `'not-required'` matters: it keeps the two pre-existing tests on their current code path, so this mock does not silently rewrite what they cover.

- [ ] **Step 2: Run to verify the new tests fail and the old ones still pass**

Run: `(cd web && npx vitest run src/components/settings/__tests__/NotificationsToggle.test.tsx)`
Expected: the 2 new tests FAIL (no "home screen" text); the 2 existing tests PASS.

- [ ] **Step 3: Implement the branch**

In `web/src/components/settings/NotificationsToggle.tsx`, change the import line to:

```ts
import { enablePush, disablePush, pushSupported, installState } from '@/api/push'
import { BRAND_NAME } from '@/brand'
```

Then insert this block **above** the existing `if (!pushSupported())` check — order matters, because on iOS `pushSupported()` may itself be false, and the install case must win:

```tsx
  if (installState() === 'needs-install') {
    return (
      <div className="text-sm text-ink-muted space-y-1">
        <p>
          iPhone and iPad deliver notifications only to an installed app, not to a Safari tab.
        </p>
        <ol className="list-decimal ml-4 space-y-0.5">
          <li>Tap Share, then <span className="font-medium">Add to Home Screen</span>.</li>
          <li>Open {BRAND_NAME} from your Home Screen.</li>
          <li>Come back to this setting and tap Enable.</li>
        </ol>
        <p className="text-xs">
          The order matters — enabling here in Safari does not carry over to the installed app.
        </p>
      </div>
    )
  }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `(cd web && npx vitest run src/components/settings/__tests__/NotificationsToggle.test.tsx)`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/settings/NotificationsToggle.tsx web/src/components/settings/__tests__/NotificationsToggle.test.tsx
git commit -m "fix(notifications): explain the iOS install requirement instead of dead-ending"
```

---

### Task 6: Full verification and PR

**Files:** none created or modified beyond what earlier tasks produced.

- [ ] **Step 1: Run the full web gate**

Run: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build) 2>&1 | tee /tmp/pwa-web-gate.log`
Expected: all four green. Run the **full** `npm run test`, not changed files — ~47 web test files manually mock `@/api/hooks`, so shared-component edits break files you never touched.

- [ ] **Step 2: Remove the build output before touching the server suite**

Run: `rm -rf web/dist`
Expected: gone. A built `dist` mounts the SPA catch-all and makes two server path-traversal tests return 405.

- [ ] **Step 3: Run the four-step `lint-and-test` gate**

Run each; all four must pass:

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest tests/
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 python -m pytest server/tests/test_spa_fallback.py
```

The full `server/tests/` suite is unaffected by this change beyond `test_spa_fallback.py` (the only server file touched is `server/spa.py`), but if you run it, unset `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` first and acquire a test slot per the repo instructions.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/pwa-installable-shell
```

Write the PR body to a file and pass `--body-file` (the Bash hook denies command *text* pairing a deploy verb with a prod word):

```bash
cat > /tmp/pwa-pr-body.md <<'EOF'
Makes ViberPit installable to a phone home screen — the missing prerequisite for
iOS Web Push, which `2026-06-25-self-hosted-web-push-design.md` built the server
half of and then deferred ("iOS is explicitly out of scope (it requires an
installed PWA + iOS 16.4+)").

- Web app manifest generated at build time from `BRAND_NAME`, so the brand stays
  spelled in exactly one place while the rebrand phases are still in flight.
- Home-screen icons rendered from the existing mark, including a maskable
  variant inset to Android's 80% safe zone. Rendered by a committed script via
  Chromium; ImageMagick silently drops the gradient lane on this host.
- `.webmanifest` registered with `mimetypes` — Starlette would otherwise serve
  it as `text/plain` and Chrome would reject it.
- The notifications setting no longer dead-ends on iOS. Instead of an
  impossible Enable button (or a false "not supported"), it explains the
  required order: install, reopen from the Home Screen, then enable.

Phase A caches nothing and does not touch `web/public/sw.js`, so the frontend
auto-update flow is unchanged and the push service worker keeps its scope.
Offline precaching is phase B, which owes the auto-update design a lifecycle
reconciliation — see the spec.

Design: `docs/superpowers/specs/2026-09-08-pwa-installable-shell-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr create --title "feat(pwa): installable shell — manifest, icons, and an honest iOS notifications setting" --body-file /tmp/pwa-pr-body.md
```

Then mark it ready and arm auto-merge — both are required for a finished PR (substitute the number `gh pr create` prints):

```bash
gh pr merge <#> --squash --auto
```

- [ ] **Step 5: Manual acceptance on a real iPhone (cannot be automated)**

After the PR merges and deploys, on an iPhone on the tailnet:

1. Open `https://dobby.tail0dd570.ts.net/` in Safari → Share → Add to Home Screen. A ViberPit icon appears with the signal-lane mark.
2. Open it from the Home Screen. It runs standalone — no URL bar, and the top bar renders below the status bar / clock rather than underneath it (the check for `apple-mobile-web-app-status-bar-style: black-translucent` drawing content under the notch instead of under a legible status bar).
3. Settings → Notifications → Enable. Permission is granted and the subscription registers.
4. Trigger a session notification. It arrives.
5. Back in the **Safari tab** (not installed), the same setting shows the install instructions rather than a button.

Step 5 also settles the open question recorded in the spec: which branch `pushSupported()` actually takes in un-installed iOS Safari. Note the answer in the PR thread.

---

## Notes for the executor

- Tasks 1–5 are independently reviewable and each ends green. Task 3 (Python) has no dependency on Tasks 4–5 (TypeScript) and could run in parallel with them; Task 2 depends on Task 1's icon files existing.
- If `npm run typecheck` fails after Task 2 Step 5 with *"File … is not listed within the file list of project"*, you skipped Step 6.
- Do not add `vite-plugin-pwa` or Workbox. That is phase B, and it must use `injectManifest` mode with the existing `sw.js` handlers moved in — `generateSW` alongside the current service worker would replace the push registration and break notifications.
