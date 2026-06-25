# Deploy "Upgrading" overlay — front-and-center deploy feedback

**Date:** 2026-06-25
**Status:** Design approved, pending spec review
**Area:** web frontend (`web/src`)

## Problem

When a user triggers a deploy ("upgrade") from the dashboard, the feedback is weak:

1. **No deploy-now button feedback.** `DeployPanel`'s "Deploy now" button is `bg-amber-600`
   with only `disabled:opacity-50` — no hover, no `active:` press, no spinner. On click,
   `start.mutate()` fires but nothing visibly changes until the next 5s poll flips
   `running` on. Users can't tell anything happened.
2. **Progress is buried and inert.** While deploying, `DeployPanel` shows a tiny static
   checklist (`preflight save update report restart resume`) rendered with text glyphs
   (`○ ◐ ● ✗`) in the top banner — no animation, no "this step is happening now"
   emphasis, no sense of overall progress. It is easy to miss.
3. **Daemon-restart reads as failure.** Mid-deploy the daemon restarts, so the `/deploy/status`
   poll itself briefly fails. There is no handling for this, so a transient reconnect can
   look like an error.

## Goal

Make the upgrade the front-and-center highlight while it is happening: a takeover overlay
that clearly shows progress, reassures across the daemon-restart gap, and makes the trigger
button feel responsive.

## Decisions (locked during brainstorming)

- **Prominence:** Takeover overlay — centered card over a dimmed backdrop that dominates the
  screen while deploying.
- **Content:** Animated 6-phase stepper + determinate progress bar + a live status line that
  surfaces sub-states ("restarting service — reconnecting…") and warnings.
- **End behavior:** Auto-close on clean success (~2s "✓ Deployed"); hold open with details +
  manual Close on failure or failed-resume. Locked (no backdrop/Esc close) while running,
  since the deploy restarts the daemon anyway.
- **Failure fallback:** after a failure overlay is dismissed, keep the existing compact inline
  "N sessions failed to resume — open to retry" line in `DeployPanel` (reuses current logic).

## Non-goals (YAGNI)

- No ETA / time-remaining. The backend exposes no progress signal — deploy phases are discrete
  and several underlying steps are single blocking calls (cf. `sdk-compaction-signals`: no
  ETA exists). A determinate bar over the 6 phases is the only honest progress.
- No live log stream.
- No minimize-while-running.

## Architecture

The current `DeployPanel` couples three concerns: the pending-changes banner, the trigger
button, and the live progress checklist. We split *trigger* from *display* and host the
display once at the app root.

### Components

1. **`lib/deployPhases.ts` (new)** — single source of truth for phase order and presentation,
   replacing the `PHASES` and `STATE_ICON` consts currently inlined in `DeployPanel`.
   ```ts
   export interface DeployPhaseMeta { key: string; label: string; description: string }
   export const DEPLOY_PHASES: DeployPhaseMeta[]  // preflight … resume, in order
   ```
   Friendly descriptions, e.g. `save → "Backing up live sessions"`, `update → "Pulling latest
   code"`, `restart → "Restarting service"`, `resume → "Restoring your sessions"`.
   Both the overlay and any other phase consumer import from here — no parallel phase lists.

2. **`lib/deployController.tsx` (new)** — a context provider wrapping `App`, modeled on the
   existing `SessionRuntimeProvider` pattern (the repo's one `createContext` precedent; no
   state library is in use). It owns the one piece of state the server cannot report — an
   ephemeral intent — plus the actions:
   ```ts
   type DeployIntent = 'idle' | 'starting' | 'held-success' | 'held-failure'
   interface DeployController {
     intent: DeployIntent
     start: () => void      // wraps useStartDeploy().mutate; flips intent → 'starting'
     dismiss: () => void    // clears a held terminal state → 'idle'
     startError: Error | null
   }
   ```
   This is what lets the two separately-mounted "Deploy now" buttons (DesktopShell + Dashboard)
   and the single root overlay agree. Everything else (phases, running, error, warnings,
   restore.failed) is read live from the existing `useDeployStatus()` 5s poll.

3. **`DeployOverlay.tsx` (new)** — mounted **once** at the App root, next to the existing
   `GlobalUpdateBanner` (`App.tsx`), so it is shell-agnostic (renders over both DesktopShell
   and the mobile routed views). Owns the takeover UI. Pure presentation: reads the controller
   intent + the deploy poll; calls `controller.dismiss()` on Close / auto-dismiss.

4. **`DeployPanel.tsx` (edited)** — keeps the "N changes pending deploy" banner, the commit
   list, and the "Deploy now" button. Its **inline phase checklist is removed** (the overlay
   now owns progress — no duplicate phase rendering). The button calls `controller.start()`.
   The existing compact failed-resume line stays as the post-dismiss fallback.

### Visibility rule

The overlay is visible when **any** of:
- `intent === 'starting'` (optimistic — appears instantly on click, before the first poll), OR
- `deploy.running === true`, OR
- `intent` is a held terminal state (`held-success` mid auto-dismiss, or `held-failure`).

## Overlay UI

Layout: `fixed inset-0 z-[60]`, dimmed `bg-black/60 backdrop-blur-sm`, centered card.
**Locked while running** — no backdrop click / Esc dismissal.

- **Header:** "Upgrading ccswitch" + subtitle "N changes · `targetSha` (short)".
- **Determinate progress bar:** `done / 6` phases, animated width transition.
- **Vertical stepper**, one row per `DEPLOY_PHASES` entry: status dot + label + description.
  - `done` → filled check
  - `active` (first non-done phase while running) → pulsing ring/spinner, row bold + highlighted
  - `pending` → hollow dot
  - `failed` → red ✗, row text red
- **Live status line** beneath the stepper: current human-readable message; inline amber
  `deploy.warnings`.
- **End states:**
  - **Success** (`!running && all phases done && !error && restore.failed empty`): swap card body
    to a centered "✓ Deployed" with the new short SHA; auto-dismiss after ~2s (`held-success`
    → `dismiss()`).
  - **Failure** (`deploy.error` set, or any phase `failed`, or `restore.failed` non-empty):
    red header + error text + failed-session deep links + **Close** button. Held open
    (`held-failure`) until the user closes; then `DeployPanel`'s inline failed-resume line
    carries the residual info.

### Reconnect awareness (daemon restart)

While the last observed state was `running` and the `useDeployStatus` query is currently
failing (React Query `isError` / `failureCount > 0`), the status line shows
**"Restarting service — reconnecting…"** with a spinner — never a red error. When the poll
recovers, normal rendering resumes. This is the single biggest reassurance win and the easiest
state to get wrong.

## Button feedback (`DeployPanel`)

- Add `hover:bg-amber-700`, `active:scale-95 transition`, and a focus ring — real press feel.
- **Optimistic open:** on click `controller.start()` sets `intent → 'starting'`, so the overlay
  appears immediately (preflight active, rest pending) without waiting for the 5s poll. The
  button shows an inline spinner + "Starting…" and is disabled while `start.isPending`.
- **Error surfacing:** if `start.mutate` errors (e.g. CI flipped green→red in the race window),
  the overlay shows that error held-open via `controller.startError` rather than silently
  vanishing.

## Data flow

```
DeployPanel "Deploy now" ──click──▶ controller.start()
        │                               │
        │                               ├─ intent = 'starting'  (optimistic)
        │                               └─ useStartDeploy().mutate()
        ▼                                       │
  (button: spinner "Starting…")                 ▼
                                         POST /deploy/start
App root: <DeployOverlay/> ◀── reads ── controller.intent
        ▲                          └──── useDeployStatus() poll (5s) ── deploy.running/phases/error
        │
   visible while starting | running | held-terminal
```

## Testing

- **`deployController`**: `start()` sets `intent='starting'` and calls the mutation;
  observing `running` then a clean terminal auto-transitions to `held-success` → `idle` via
  fake timers; `dismiss()` clears a `held-failure`.
- **`DeployOverlay`** (jsdom + fake timers): given server states, asserts active step, progress
  %, the reconnecting status line when the poll errors while running, success auto-dismiss, and
  failure held-open + Close. Includes the daemon-restart (poll-error-while-running) case.
- **`DeployPanel`**: existing enable/disable rules for "Deploy now" unchanged; click calls
  `controller.start`; inline failed-resume line still renders from `restore.failed`. Update the
  existing `DeployPanel.test.tsx` to drop the removed inline checklist assertions.
- Full `vitest run` + `tsc -b --force` + `eslint` (the real CI gates; `tsc --noEmit` is a
  false-green here).

## Affected files

- new: `web/src/lib/deployPhases.ts`
- new: `web/src/lib/deployController.tsx` (+ test)
- new: `web/src/components/DeployOverlay.tsx` (+ test)
- edit: `web/src/components/DeployPanel.tsx` (remove inline checklist, wire button to controller,
  press/hover/optimistic feedback) + `DeployPanel.test.tsx`
- edit: `web/src/App.tsx` (wrap in `DeployControllerProvider`, mount `<DeployOverlay/>` at root)
- no backend changes — all states derive from the existing `/deploy/status` contract.
