# Frontend auto-update on new version — Design

**Date:** 2026-06-23
**Status:** Approved (brainstorming) — pending implementation plan
**Topic:** After a deploy, make the browser pick up the newly-built bundle automatically — a banner
immediately, plus a silent reload when it is safe — completing the "near-real-time deploy" picture
that the daemon graceful-drain feature (#190) started.

## Problem

A deploy rebuilds `web/dist` (new `index-*.js` hash) and restarts the edge. The browser's
`SessionSocket` auto-reconnects (data plane), but **nothing reloads the loaded JS bundle**: the tab
keeps running the *previous* frontend code until a manual full reload. Verified in code — there is no
`location.reload`, no service worker/PWA, and no version comparison anywhere in `web/src`; the `sha`
that `/healthz` already returns is not consumed by the client.

Consequences:

- Frontend changes (new UI, bug fixes, even the drain feature's own banner) are invisible until the
  user hard-refreshes.
- A window exists where an **old frontend talks to a new backend**. Usually fine (the WS protocol is
  version-gated), but a breaking API-shape change can misbehave until refresh.

Goal: close the loop so the frontend updates itself, without losing the user's work or interrupting a
stream they are watching.

## Goals

- The browser detects when it is running an outdated bundle and updates to the current one.
- The user is **informed** (a banner) and, when it is **safe**, the tab reloads **silently**.
- A reload never loses unsent work and never interrupts a turn the user is watching.

## Non-goals

- Service worker / PWA / offline support.
- Hot-module replacement of the running app (a full reload is fine — it is fast and the data plane
  reconnects + replays transcript).
- Periodic version polling. A deploy always restarts the edge → WS reconnect, which is the only check
  trigger we need.
- Persisting the drain feature's client-side prompt buffer across a reload (still the deferred v2 of
  that feature; here it is simply a reason to *hold* the auto-reload).

## Key enabling facts (verified)

- `/healthz` already returns `{"sha": "<7-char>"}` (auth-exempt, reachable by the client).
- `SessionSocket` emits `connectionStateChange`; a deploy always causes a reconnect — the natural
  moment to check for a new version.
- Unsent composer drafts are persisted to **localStorage** (`useSessionDraft`, key
  `ccswitch:draft:<sessionId>`, read fresh on mount). **A full reload does not lose typed text** — it
  reappears. So "composer is empty" is NOT needed in the safety bar.
- The drain feature already exposes `getSessionSocket().updatePendingState.count` — the count of
  client-buffered prompts — which IS lost on reload and therefore IS a safety gate.
- The banner UI pattern (`UpdatePendingBanner` / `AllLimitedBanner`) is reusable.

## Chosen approach

**Hybrid (banner + auto-reload-when-safe), conservative safety bar.**

1. **Detect** a version mismatch on every reconnect.
2. **Inform** immediately via a banner with a manual **Reload** button.
3. **Auto-reload silently** when, and only when, it is safe.

### Detection
- Build injects `__BUILD_SHA__` (short git sha) via a `vite.config.ts` `define`, from
  `git rev-parse --short HEAD` at build time, matching the 7-char format `/healthz` returns. Fallback
  to `"dev"` if git is unavailable at build time.
- The client, on `connectionStateChange === 'connected'`, fetches `/healthz`, reads `sha`. If
  `import.meta.env.PROD` **and** `sha` is non-empty **and** `sha !== __BUILD_SHA__`, set a sticky
  `updateAvailable = true`.

### Safety bar (when auto-reload may fire)
Auto-reload only when BOTH hold:
- the **focused** session is **not** mid-turn (status not in the active set — i.e. not generating /
  awaiting input), **and**
- there are **no client-buffered prompts** (`updatePendingState.count === 0`).

When no session is focused (e.g. the dashboard view), the first condition is vacuously satisfied. The
predicate is re-evaluated on state changes, so a mismatch detected while busy auto-reloads the moment
the tab becomes safe.

### Reaction
- `updateAvailable` → render `NewVersionBanner` ("New version available — Reload", button →
  `location.reload()`).
- `updateAvailable && safe` → call `location.reload()` automatically.

## Components & changes

| Unit | Responsibility |
| --- | --- |
| `web/vite.config.ts` + `web/src/vite-env.d.ts` | Inject and type `__BUILD_SHA__` (build-time git sha). |
| `web/src/hooks/useVersionWatch.ts` | On reconnect, fetch `/healthz`, compare `sha` to `__BUILD_SHA__`, set sticky `updateAvailable`. Gated by `import.meta.env.PROD`. |
| `web/src/hooks/useAutoReload.ts` | Combine `updateAvailable` + focused-session status + buffered-prompt count; call `location.reload()` when safe. |
| `web/src/components/NewVersionBanner.tsx` | Banner + manual Reload button (reuses the existing banner pattern). |
| `web/src/components/Dashboard.tsx` | Render `NewVersionBanner` near the existing banners; wire the hooks. |

State (`updateAvailable`) lives in a small shared place readable by both the banner and the
auto-reload hook — either a tiny context/store or, mirroring the drain feature, a field + event on
`SessionSocket`. The implementation plan picks one; the interface is `{ updateAvailable: boolean }`
plus a change event.

## Error handling / edge cases

| Case | Handling |
| --- | --- |
| Vite dev mode | `import.meta.env.PROD` gate → never auto-reloads or banners in dev. |
| `/healthz` unreachable on reconnect | Ignore; re-check on the next reconnect. |
| Rollback deploy | Client reloads to whatever sha the edge now serves — correct by construction. |
| Reload loop | After reload `__BUILD_SHA__ === served sha` → no further mismatch. Defensive backstop: reload at most once per detected mismatch (guard against a proxy serving a stale bundle). |
| Stacking with the drain "Update in progress" banner | Both may show during a daemon deploy; auto-reload is held while prompts are buffered, so they don't fight. |
| `git` unavailable at build | `__BUILD_SHA__ = "dev"`; `/healthz` sha never equals `"dev"` in prod, but the `PROD` gate + a non-empty-sha check prevent spurious behavior (a `"dev"` build is not a prod deploy). |

## Testing

- **vitest**:
  - `useVersionWatch`: mock `fetch('/healthz')`, simulate a `connected` transition, assert
    `updateAvailable` flips only on a real sha mismatch and only under `PROD`.
  - safety predicate / `useAutoReload`: mid-turn focused session OR buffered prompts → no reload;
    idle + zero buffered → `location.reload()` called (mock `location.reload`).
  - `NewVersionBanner`: renders the message; the button calls reload.
- Type/lint gates as usual (`tsc -b --force`, `eslint --max-warnings 0`).

## Decisions (resolved during brainstorming)

- **Hybrid (C):** banner immediately + auto-reload when safe (not manual-only, not eager).
- **Conservative safety bar (A):** focused session not mid-turn AND no buffered prompts. Composer
  draft is NOT a gate (localStorage persists it across reload).
- **Focused-session** turn state, not all-sessions-busy (more auto-reloads; the user is only watching
  the focused one).
- **Reconnect-triggered** detection only; no periodic polling.

## Open follow-ups (out of scope)

- A "what changed" link in the banner (changelog) — cosmetic.
- Persisting the drain prompt buffer across reload (would remove the buffered-prompt safety gate).
