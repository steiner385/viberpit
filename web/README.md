# ccswitch web

The browser UI for `ccswitch`: a dashboard of accounts and their usage, a live session list, an
inline transcript/composer per session, delivery (CI + automations) and roadmap views, and settings
— reachable from any device on the tailnet via `tailscale serve`, phone to desktop, behind one
adaptive shell (`AppShell`) rather than a mobile/desktop fork. React 19 + Vite + TypeScript, styled
entirely from Tailwind v4 tokens (`src/index.css`), talking to the FastAPI edge in `server/` over
REST + WebSocket.

## Folders

- `api/` — fetch wrappers (`client.ts`) and React Query hooks, one file per domain (`hooks.ts`,
  `automations.ts`, `sdlc.ts`, `roadmap.ts`, `fleet.ts`, …).
- `session/` — pure session-domain logic with no rendering: ordering, event reconciliation, nav-path
  helpers, permission modes.
- `shell/` — the app chrome: `AppShell.tsx` (regions, layout modes), `regions/` (the seven
  `data-region="app.*"` components), `useViewport.tsx` (the one layout `matchMedia` reader), routing,
  theming, kiosk mode, banners.
- `ui/` — presentation primitives with no domain knowledge: `Dialog`, `Menu`, `Tabs`, `Banner`,
  `StatusPill`, `Button`/`IconButton`, `PageHeader`/`PageContent`, `ui/list/` (list-row chrome shared
  across sections).
- `features/<domain>/` — one folder per section: `sessions/` (`transcript/`, `composer/`, `panels/`,
  `list/`, `browser/`), `accounts/`, `delivery/` (`automation/`, `ci/`), `settings/` (`chassis/`,
  `sections/`), `roadmap/`, `pipeline/` (`charts/`), `assistant/`, `products/`.
- `pipeline/` — the CI/pipeline domain model consumed by `features/pipeline/`.
- `lib/` — cross-cutting utilities with no UI: `storageKey.ts`, `utils.ts` (`cn`), formatting,
  swipe/gesture math, dictation.
- `hooks/` — small standalone hooks not tied to one feature (`useNow`, `usePageVisible`, …).
- `e2e/` — Playwright specs, one per page/flow.

## Gates

Run from `web/`:

```
npm run typecheck   # tsc -b
npm run lint        # eslint . --max-warnings 0
npm run test        # vitest run
npm run build       # tsc -b && vite build
npm run test:e2e    # playwright test
```

CI runs all four non-e2e gates on every push; treat all four as required locally too — `npm run
lint` alone can pass where `typecheck` or `build` would not. `npm run lint:fix-tokens` runs
`eslint --fix --fix-type problem` — it auto-converts anything `ccswitch/no-raw-palette` has a mapping
for and leaves an error (no fix) on anything it doesn't, for you to hand-map.

## Conventions

- **Tokens only.** Every colour comes from `src/index.css`'s `@theme` tokens (`bg-surface`,
  `text-ink`, `border-edge`, `bg-accent`, tone washes, `z-raised`/`z-sticky`/`z-popover`/`z-overlay`/
  `z-dialog`, `min-h-target`). No raw Tailwind palette class (`bg-gray-500`, `text-blue-600`), no
  `dark:` variant (the tokens themselves flip per theme), no hex literal, no `z-\d+`/`min-h-[44px]`
  arbitrary value in a class string. `eslint-rules/no-raw-palette.js` enforces this across the whole
  tree (`eslint.config.js`), with one exemption: `features/pipeline/charts/**`, which draws with
  literal colours.
- **Storage keys** go through `lib/storageKey.ts`'s `storageKey(name)` (→ `` `ccswitch.${name}` ``),
  never a raw string literal to `localStorage`/`sessionStorage`. Renaming a key uses
  `migrateStorageKey()` to carry the old value forward once.
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
3. Register the route and its nav entry in `shell/navDefs.ts` (path, label, icon, help text, whether
   it has a list column, whether it is kiosk-safe — read `NavDefShape`'s doc comments before setting
   `showInKiosk: true`).
4. Add a row to the `PAGES` table in `features/settings/pageContract.test.tsx` — this is what proves
   the page has exactly one `h1`, a synced title, its declared variant, and no horizontal scroll, in
   every layout mode it's reachable in.
5. Add an e2e spec under `e2e/` that exercises phone (390×844), tablet (820×1180) and desktop
   (1440×900) — either with separate `test.use({ viewport })` blocks or, if the spec already loops
   several widths, inside that loop (see `dashboard.spec.ts` vs. `pipeline.spec.ts` for both styles).

## Adding a primitive

Put it in `ui/`, give it its own `<Name>.test.tsx`, and build its class strings from tokens only —
`npm run lint` fails the build otherwise. If it needs a colour with no existing semantic home, don't
invent one: pick the nearest token family and flag it for review rather than adding a new `--t-*`
token to `index.css` on the spot.
