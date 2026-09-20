# ViberPit web

The browser UI for ViberPit: a dashboard of accounts and their usage, a live session list, an
inline transcript/composer per session, delivery (CI + automations) and roadmap views, and settings
— reachable from any device on the tailnet via `tailscale serve`, phone to desktop, behind one
adaptive shell (`AppShell`) rather than a mobile/desktop fork. React 19 + Vite + TypeScript, styled
entirely from Tailwind v4 tokens (`src/index.css`), talking to the FastAPI edge in `server/` over
REST + WebSocket.

## Folders

Every entry is a top-level folder of `web/src`, except the last: `e2e/` sits at `web/e2e`, beside
`src`.

- `api/` — fetch wrappers (`client.ts`) and React Query hooks, one file per domain (`hooks.ts`,
  `automations.ts`, `sdlc.ts`, `roadmap.ts`, `fleet.ts`, …).
- `session/` — pure session-domain logic with no rendering: ordering, event reconciliation, nav-path
  helpers, permission modes.
- `ws/` — the WebSocket client behind it: reconnect/backoff, channel fan-out, gap healing, catch-up.
- `shell/` — the app chrome: `AppShell.tsx` (regions, layout modes), `regions/` (11 components for
  the seven `data-region="app.*"` nodes — `app.rail` alone is stamped by `NavRail`, `NavBar` and
  `TopBar`, while `app.banners` is stamped outside, in `ui/BannerStack.tsx`), `routes.tsx` +
  `navDefs.ts`, `useViewport.tsx` (the one layout `matchMedia` reader), theming, kiosk, banners.
- `ui/` — presentation primitives with no domain knowledge: `Dialog`, `Menu`, `Tabs`, `Banner`,
  `StatusPill`, `Button`/`IconButton`, `PageHeader`/`PageContent`, `ui/list/` (list-row chrome shared
  across sections).
- `features/<domain>/` — one folder per section: `sessions/` (`transcript/`, `composer/`, `panels/`,
  `list/`, `browser/`), `accounts/`, `delivery/` (`automation/`, `ci/`), `settings/` (`chassis/`,
  `sections/`), `roadmap/`, `pipeline/` (`charts/`), `assistant/`, `products/`.
- `pipeline/` — the CI/pipeline layer `features/pipeline/` renders: domain model (`types.ts`,
  `ordering.ts`, `leadtime.ts`, `useDashboard.ts`) *and* the view components ported alongside it
  (`PipelineView.tsx`, `PrRow.tsx`, `StatusStrip.tsx`, `spine/`).
- `lib/` — cross-cutting utilities with no UI: `storageKey.ts`, `utils.ts` (`cn`), formatting,
  swipe/gesture math, dictation.
- `hooks/` — small standalone hooks not tied to one feature (`useNow`, `usePageVisible`, …).
- `initiatives/`, `projects/`, `vnc/`, `pwa/`, `obs/` — narrower surfaces: roadmap initiative and
  incident tracks; dependency panel and onboarding wizard; the noVNC client (zoom, key relay,
  clipboard); the web-app manifest; browser logging/instrumentation.
- `test/` — shared test helpers (`renderApp.tsx`, `factories.ts`, `matchMedia.ts`, `regions.ts`,
  `setup.ts`); this is what a new page's test imports.
- `__tests__/` — whole-tree guards that walk the source instead of rendering it (`contrast.test.ts`,
  `noCollapsedClassTernary.test.ts`, `dedupeMobileDesktop.test.ts`, `tokenBridge.test.ts`, …).
- `e2e/` (i.e. `web/e2e`) — Playwright specs, one per page/flow.

## Gates

Run from `web/`:

```
npm run typecheck   # tsc -b
npm run lint        # eslint . --ext ts,tsx --report-unused-disable-directives --max-warnings 0
npm run test        # vitest run
npm run build       # tsc -b && vite build
npm run test:e2e    # playwright test
```

CI (`.github/workflows/ci.yml`) runs those four in the `frontend` job on every pull request and on
pushes to `master` — but only when the `changes` job's path filter saw a `web/` (or `ci.yml`) change;
otherwise the job prints "No web/ files changed" and green-lights in seconds. It always *runs*,
because it is a required check and a skipped required check never reports. `e2e` and `lighthouse` are
separate, advisory (not required) jobs on the same condition. Treat all four as required locally too
— `npm run lint` alone can pass where `typecheck` or `build` would not.
`npm run lint:fix-tokens` runs
`eslint --fix --fix-type problem` — it auto-converts anything `viberpit/no-raw-palette` has a mapping
for and leaves an error (no fix) on anything it doesn't, for you to hand-map.

## Conventions

- **Tokens only.** Every colour comes from `src/index.css`'s `@theme` tokens (`bg-surface`,
  `text-ink`, `border-edge`, `bg-accent`, tone washes, `z-raised`/`z-sticky`/`z-popover`/`z-overlay`/
  `z-dialog`, `min-h-target`). No raw Tailwind palette class (`bg-gray-500`, `text-blue-600`), no
  `dark:` variant (the tokens themselves flip per theme), no hex literal, no `z-\d+`/`min-h-[44px]`
  arbitrary value in a class string. `eslint-rules/no-raw-palette.js` enforces this over `src/**`
  minus one allowlist — `features/pipeline/charts/**`, which draws with literal colours (the rule
  block in `eslint.config.js` is `files: ['src/**/*.{ts,tsx}']` + that `ignores`, so `e2e/`,
  `eslint-rules/` and the root configs sit outside it).
- **Storage keys** go through `lib/storageKey.ts`'s `storageKey(name)` (→ `` `viberpit.${name}` ``),
  never a raw string literal to `localStorage`/`sessionStorage`. Renaming a key uses
  `migrateStorageKey()` to carry the old value forward once; a reader also calls
  `migrateStorageNamespace()` so a key still under the pre-rebrand namespace comes forward
  on first read (#1368).
- **One media-query reader.** `shell/useViewport.tsx` is the only module that subscribes to a layout
  `matchMedia` (width tier, orientation, coarse pointer, reduced motion); everything else reads
  `useViewport()`. (`shell/theme.tsx`'s `prefers-color-scheme` subscription and the two
  `prefers-reduced-motion` reads that can't consume a React context are the documented exceptions.)
- **Icons are lucide only** (`lucide-react`) — no other icon set, no bare emoji used as a control
  (emoji in copy or test fixtures is fine).

## Adding a page

1. Pick a layout variant: `standard`, `split`, `reader`, or `workspace`.
2. Build it from `ui/PageHeader` (the page's one `<h1>`, synced to `document.title`) and
   `ui/PageContent` (scrolling + the busy live region), and stamp your own
   `data-page-variant="<variant>"` on the page root.
3. Add the route element to the one route table, `shell/routes.tsx` (`buildRoutes`) — e.g.
   `{ path: NAV_BY_ID.pipeline.path, element: <PipelinePage /> }`. Without this the page is
   unroutable no matter what the nav says.
4. Add the nav entry in `shell/navDefs.ts` (path — it must match the route you just added — label,
   icon, help text, whether it has a list column, whether it is kiosk-safe; read `NavDefShape`'s doc
   comments before setting `showInKiosk: true`).
5. Add a row to the `PAGES` table in `features/settings/pageContract.test.tsx` — this is what proves
   the page has exactly one `h1`, a synced title, its declared variant, and no horizontal scroll, in
   every layout mode it's reachable in.
6. Add an e2e spec under `web/e2e/` covering the widths that matter to the page. Two styles are in
   use: a `test.use({ viewport })` per `test.describe` block (`delivery.spec.ts` — desktop 1440×900
   and phone 390×844) or a loop over a width list (`pipeline.spec.ts` — phone 390×844 and tablet
   820×1180, with desktop in its own describe). There is no viewport matrix in
   `playwright.config.ts`, so a width you do not name is a width you do not test.

## Adding a primitive

Put it in `ui/`, cover it with a test — `ui/` uses both per-primitive files (`Button.test.tsx`,
`Menu.test.tsx`, `Tabs.test.tsx`) and grouped ones (`Page.test.tsx`, `Pills.test.tsx`,
`FormControls.test.tsx`, `Overlays.test.tsx`), and nothing enforces one file per primitive, so use
whichever fits — and build its class strings from tokens only —
`npm run lint` fails the build otherwise. If it needs a colour with no existing semantic home, don't
invent one: pick the nearest token family and flag it for review rather than adding a new `--t-*`
token to `index.css` on the spot.
