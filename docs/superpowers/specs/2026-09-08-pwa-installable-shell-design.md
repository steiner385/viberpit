# PWA — installable shell (phase A) — Design

**Date:** 2026-09-08
**Status:** Approved (brainstorming) — pending implementation plan
**Topic:** Make ViberPit installable to a phone home screen, which is the missing prerequisite for
iOS Web Push — the capability
[`2026-06-25-self-hosted-web-push-design.md`](2026-06-25-self-hosted-web-push-design.md) built the
server half of and then deferred.

## Problem

ViberPit is "a web-hosted, mobile-accessible interface for running and switching between live agent
sessions." The self-hosted Web Push feature (`server/push/`, `web/src/api/push.ts`,
`web/public/sw.js`) delivers session alerts to a phone — except on iPhone, where the push spec
records the gap plainly:

> iOS is explicitly out of scope (it requires an installed PWA + iOS 16.4+, a distinct …)

iOS Safari does not deliver Web Push to an ordinary browser tab. It delivers only to a web app the
user has added to the Home Screen, and a site is only addable-as-an-app if it serves a **web app
manifest**. ViberPit has no manifest.

The gap has a second face in the settings UI. `pushSupported()` in `web/src/api/push.ts` tests for
`serviceWorker`, `PushManager`, and `Notification`, and `NotificationsToggle` branches on it alone.
Whichever way that predicate currently evaluates in un-installed iOS Safari, the result is wrong for
the user:

- If the three interfaces **are** exposed, the toggle offers an "Enable" button that cannot succeed.
- If they are **not**, the toggle says "not supported in this browser (needs HTTPS + a modern
  browser)" — false and dead-ending, when the truth is "supported, once you install it."

**Which branch is live on iOS is an explicit verification step for implementation** (measured on a
real iPhone, not inferred), because it decides the copy. It does not change the design: either way
the fix is to distinguish "cannot" from "not yet installed" and say so.

## Goals

- ViberPit is installable to an iOS and Android home screen, presenting as a standalone app.
- The notifications setting stops offering an impossible action on iOS and instead explains, in the
  correct order, how to get notifications working.
- The brand name in the manifest derives from the existing single source of truth, not a fourth
  hardcoded copy.
- Zero change to how the frontend updates itself, and zero risk to the working push path.

## Non-goals (phase A)

- **Offline support and precaching.** Deferred to phase B, deliberately — see "Phase B" below.
- **A fetch handler in the service worker.** Nothing in phase A needs one; see "Key enabling facts".
- **`beforeinstallprompt` capture / an install banner.** The chosen install affordance is contextual
  (option 2 of three considered), not proactive. A standing banner in an app opened dozens of times
  a day is a permanent tax for a one-time action.
- **Manifest `shortcuts`.** YAGNI: the app already opens on the Sessions list, which is the
  destination a shortcut would target.
- **New artwork.** The rebrand spec settled the mark (§ Phase 1a: "keep the existing mark (two
  session lanes) and change only the wordmark").
- **Offline reading of session data.** ViberPit is a live controller. A cached transcript is a stale
  transcript, and rendering a session as "working" while the device has no connectivity is worse
  than an honest offline state. Not planned for phase B either.

## Key enabling facts (verified)

- The app is served over **HTTPS** — `deploy/README.md` § "Access ViberPit at
  `https://dobby.tail0dd570.ts.net/`", nginx (`deploy/ccswitch-tailnet.nginx`) in front of the edge
  on loopback `127.0.0.1:8787`. Secure context requirements for install and service workers are
  already satisfied.
- **A service worker already exists and is not ours to duplicate.** `web/public/sw.js` handles
  `push` and `notificationclick`; `enablePush()` registers it at `/sw.js`. Two registrations at one
  scope do not coexist — the second replaces the first. Any generated service worker (phase B) must
  therefore use Workbox's `injectManifest` mode with these handlers moved into it, never
  `generateSW` alongside it.
- **`sw.js` has no `fetch` handler, and phase A does not need one.** Chrome's gate on a
  fetch-handling service worker applies to *automatic* install-prompt eligibility
  (`beforeinstallprompt`), which the chosen affordance does not use. Menu-driven "Install app" and
  iOS's Share → Add to Home Screen require only the manifest.
- **The brand may be spelled in exactly one place per surface.** `src/ccswitch/brand.py`: *"Nothing
  outside this module may spell the brand."* `web/src/brand.ts` mirrors it for the web. The rebrand
  (`2026-09-06-viberpit-rebrand-design.md`) is mid-flight, phases 2–7 outstanding, so a hardcoded
  `"name": "ViberPit"` in a static JSON file would be a fourth copy that a later phase must find.
- **`vite.config.ts` already injects build-time constants** (`__BUILD_SHA__` via `define`), so
  build-time generation is an established pattern here, not a new one.
- **Auth survives the install with no re-login.** `server/auth/middleware.py` verifies Tailscale
  identity per request; the signed cookie only resolves-or-creates an `AppSession`
  (`_resolve_or_create_app_session`). iOS gives a home-screen app a separate cookie jar, which here
  costs one new `AppSession` row and nothing else.
- **`mimetypes` does not know `.webmanifest`.** `server/spa.py` mounts `StaticFiles`, which guesses
  Content-Type by extension; unregistered, the manifest would be served as `text/plain` and Chrome
  would reject it.
- **A built `web/dist` changes server-test behaviour** (it mounts the SPA catch-all, so path
  traversal tests see 405). Manifest correctness must therefore be testable *without* building the
  frontend.

## Chosen approach

Generate the manifest at build time from `brand.ts`; leave the service worker alone; fix the
notifications setting to tell the truth on iOS.

### 1. Manifest generation

New pure module `web/src/pwa/manifest.ts`:

```ts
export function buildManifest(): WebAppManifest
```

Returns the manifest object, deriving `name` and `short_name` from `BRAND_NAME` in
`web/src/brand.ts`. A small Vite plugin in `web/vite.config.ts` calls it and `emitFile`s
`manifest.webmanifest` into `dist/`.

Splitting the pure function from the plugin is what makes the manifest unit-testable in vitest with
no `dist` build — see "Key enabling facts" on why building `dist` to test is not acceptable here.

Fields:

| Field | Value | Why |
|---|---|---|
| `name`, `short_name` | from `BRAND_NAME` | single source of truth; survives the rebrand |
| `id` | `"/"` | pins app identity, so a later `start_url` change updates the installed app instead of installing a second one |
| `start_url` | `"/"` | the Sessions list |
| `display` | `"standalone"` | app chrome, no browser URL bar |
| `theme_color` | `#16171d` | matches the existing dark `theme-color` meta in `index.html` |
| `background_color` | `#16171d` | splash screen continuity with the app background |
| `icons` | see below | |

### 2. Icons

`web/scripts/gen-icons.mjs` renders `web/public/favicon.svg` into `web/public/icons/`:

- `icon-192.png`, `icon-512.png` — `purpose: "any"`
- `icon-512-maskable.png` — `purpose: "maskable"`, re-rendered with the glyph inset to Android's
  80% safe zone so a circular launcher crop does not clip the lanes. The mark's `rx="7"` dark plate
  survives the crop as the background.

The PNGs are **committed**; the script is the reproducible regeneration path when the mark changes
and is run manually, not on every build. This keeps `npm run build` free of an image-rendering
dependency. `web/public/apple-touch-icon.png` (180×180) already covers iOS and is unchanged.

### 3. Serving

- `web/index.html` gains `<link rel="manifest" href="/manifest.webmanifest">` and
  `<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">`. The latter
  pairs with the `viewport-fit=cover` already present, so the installed app draws under the notch
  rather than letterboxing.
- `server/spa.py` registers the MIME type at import:
  `mimetypes.add_type("application/manifest+json", ".webmanifest")`.

### 4. The notifications fix

`web/src/api/push.ts` gains an install-state helper alongside `pushSupported()`:

```ts
export type InstallState = 'installed' | 'needs-install' | 'not-required'
export function installState(): InstallState
```

- `installed` — running standalone (`matchMedia('(display-mode: standalone)')`, or iOS's
  `navigator.standalone`).
- `needs-install` — iOS Safari in a tab: push is impossible until installed.
- `not-required` — everywhere else: a tab is fine.

`web/src/components/settings/NotificationsToggle.tsx` renders Share-sheet instructions instead of a
dead Enable button when the state is `needs-install`, **with the ordering explicit**: install first,
then open the installed app, then enable notifications inside it. Enabling in the Safari tab does
not carry over to the installed app.

The exact predicate for what counts as `needs-install` — the platform test, and whether a dismissal
is remembered — is the one open implementation decision, resolved during implementation rather than
here.

### 5. Testing

| Test | Where | Covers |
|---|---|---|
| `buildManifest()` returns required fields; `name` tracks `BRAND_NAME` | vitest, `web/src/pwa/__tests__/` | manifest correctness with no `dist` build; catches a rebrand phase that moves `BRAND_NAME` |
| `installState()` across the platform matrix | vitest | iOS-in-tab vs. iOS-installed vs. desktop |
| `NotificationsToggle` renders instructions, not a button, when `needs-install` | vitest, extending `NotificationsToggle.test.tsx` | the user-visible fix; the existing file already fakes `PushManager` / `navigator.serviceWorker` |
| `.webmanifest` resolves to `application/manifest+json` | pytest | the MIME gotcha, in pure Python with no `dist` |

No new E2E. Per `.agents/instructions/core.md`, the web suite runs in full (`npm run test`), not
changed-files only.

## Phase B (named, not designed)

Precaching an app shell for instant cold starts on flaky mobile data. It is deferred because it
carries a real reconciliation cost that phase A does not:

**Two mechanisms would answer one question.** The frontend auto-update feature
([`2026-06-23-frontend-auto-update-design.md`](2026-06-23-frontend-auto-update-design.md)) decides
"am I running the current bundle?" by comparing `__BUILD_SHA__` against `/healthz`'s `sha`, then
reloads when safe. Workbox answers the same question with its `installing` → `waiting` →
`skipWaiting` lifecycle. Run both and a tab can reload and still be served the previous
`index.html` from cache. One must route through the other; that choice is phase B's central design
question. Note that the auto-update spec's non-goals list reads "Service worker / PWA / offline
support" — phase B revises that document, it does not merely extend it.

Phase A changes nothing about updates, because it caches nothing.

## Risks

1. **A future generated service worker silently kills push** → mitigated by recording the
   single-scope constraint above; phase B must use `injectManifest` and move the existing `push` /
   `notificationclick` handlers in, verified by actually receiving a notification, not by tests.
2. **The manifest name drifts from the brand mid-rebrand** → mitigated by deriving from `BRAND_NAME`
   and asserting the linkage in a vitest case.
3. **Committed icon PNGs drift from `favicon.svg`** → the mark is stable per the rebrand spec, and
   `gen-icons.mjs` makes regeneration a single documented command. Accepted; not worth a build-time
   render dependency.
4. **iOS install/notification behaviour cannot be verified from CI** → the acceptance check is
   manual on a real iPhone: install from Safari, open the installed app, enable notifications,
   receive one. Stated as an explicit acceptance step in the implementation plan.

## Acceptance

- `https://dobby.tail0dd570.ts.net/manifest.webmanifest` serves `application/manifest+json` with the
  brand name and both icon sizes.
- On iPhone: Share → Add to Home Screen produces a ViberPit icon; the app opens standalone with no
  URL bar; the notifications setting inside it offers a working Enable; a session notification
  arrives.
- In Safari (not installed): the notifications setting shows install instructions, not a button.
- `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`, and the four `lint-and-test`
  steps are green.
