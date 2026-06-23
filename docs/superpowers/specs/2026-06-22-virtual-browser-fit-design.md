# Virtual Browser Fit — Design (v2)

**Date:** 2026-06-22
**Feature area:** 001-ccswitch-web · Browser tab (virtual browser / VNC view)
**Status:** Approved for planning

## Problem

The session **Browser** tab streams a live Chromium running in a server-side virtual X
display (Xvfb → x11vnc → WebSocket relay → noVNC). Today the view fits the panel badly:
a wide black letterbox band on one side, content shifted right and **clipped off the right
edge**.

Root cause: the framebuffer is a fixed **5:4** resolution (`1280x1024x24`, set in
`server/config.py`) while the Browser panel is roughly **16:9 / 16:10**. noVNC's
`scaleViewport` preserves aspect ratio, so a 5:4 source pillarboxes inside a wide panel.
The clipped-right appearance is the aspect mismatch compounded by a stale container
measurement (noVNC measures its mount once on connect; there is no re-fit on panel resize).

## Goal

The Browser view fills the panel with no letterbox bars, renders a familiar **desktop**
layout, re-fits cleanly when the panel resizes, and gives the human a natural way to look
closer — without the agent's view of the page depending on who is watching.

## Decision: fixed framebuffer, scale to fit (NOT dynamic resize)

A dynamic-resize approach (framebuffer chases the panel via noVNC `resizeSession` +
`x11vnc -xrandr`) was considered and **rejected** after two independent UX reviews.

Rejected because:

- **Non-determinism.** If the framebuffer matches the panel, *who is watching changes what
  the agent sees and clicks* — a 390px phone makes Chromium serve cramped mobile
  breakpoints; the same agent task renders differently per device. Observing the session
  would mutate the agent's environment.
- **Risk/cost.** It needs an unproven mechanism: x11vnc 0.9.16 honoring client
  `SetDesktopSize`, and Chromium reflowing on a RandR resize **with no window manager**
  running (a maximized Chromium may latch its startup size). Worst case pulls in a WM.

Chosen instead: **the remote browser is a fixed desktop machine; the panel is a window onto
it.** Render a fixed **`1280x800`** (16:10) framebuffer and **CSS-scale-to-fit** the panel.
This kills the letterbox (16:10 fits a wide panel far better than 5:4), is deterministic,
and is a near-trivial server change (one resolution constant) with the rest in the frontend.

`1280x800` chosen over `1440x900`: it is a real common laptop logical resolution, renders
desktop breakpoints everywhere, and does not produce tiny text when scaled down into a
~390px phone column.

## Architecture & data flow

```
Xvfb :N  -screen 0 1280x800x24   (fixed; was 1280x1024x24)
  └─ Chromium --start-maximized   → fills the fixed 1280x800 root
      └─ x11vnc (unchanged)       → RFB stream, framebuffer always 1280x800
          └─ WS relay (unchanged) → /ws/vnc
              └─ noVNC RFB         → scaleViewport=true: CSS-scale the 1280x800
                                     source into the panel, no letterbox
                  └─ ResizeObserver on the mount → debounced re-fit on panel/rotation
                  └─ user zoom (pinch / scroll / +-) layered on top of the base fit
```

The framebuffer never changes size. Fit is purely client-side CSS scaling. No x11vnc flag
change, no RandR, no spike, no window manager.

## Components & changes

### Server (1 line)
- **`server/config.py`** — `xvfb_resolution` default `1280x1024x24` → **`1280x800x24`**.
  This is the single source of truth for display geometry; no new constant. `x11manager.py`
  already passes it straight to Xvfb's `-screen`, so no other server change is required.
  (Constitution VI: route through the existing setting, do not add a parallel one.)

### Frontend — fit & zoom (`web/src/components/BrowserTab.tsx`, `web/src/vnc/vncClient.ts`)
- Keep `rfb.scaleViewport = true` as the **base fit** (default on mobile and desktop).
- Add a `ResizeObserver` on the canvas mount that triggers a **re-fit**, **debounced
  ~150ms** with an eased CSS transition, so panel resize / phone rotation does not snap or
  flicker. noVNC recomputes its scale against the current mount size.
- **Replace the binary `Fit ⇄ 1:1` toggle with a continuous-zoom model:**
  - Default = Fit (base scale only).
  - User zoom: pinch (touch) / scroll-or-`+`/`−` (desktop), layered on top of the base fit
    as a client transform.
  - A **"Fit" reset control appears only once the user has zoomed away from fit**; tapping
    it returns to base scale.
  - The status pill shows the **current state** (`Fit` / `100%` / e.g. `140%`), never the
    action — fixing the live label bug at `BrowserTab.tsx:107`
    (`{fit ? '1:1' : 'Fit'}` labels the action, not the state).
  - Pinch/zoom resets to Fit **only on the explicit Fit action**, never on an incidental
    panel resize (so resize does not fight an active zoom).
- Preserve the **≥44px touch target** and the always-mounted-canvas pattern (overlays for
  transitional states), both of which are correct today.

### Frontend — control-state clarity (`BrowserTab.tsx`)
- Collapse the separate amber "Agent is driving" badge (top-left) and "Tap to control"
  caption (bottom) into **one mutually-exclusive status indicator** answering "can I touch
  this right now?":
  - **Agent driving** (human view is passive)
  - **Tap to take control**
  - **You're in control** (with a way back to letting the agent drive)
- Taking/releasing control is **explicit and reversible** — no silent focus-steal on a
  shared autonomous surface. (Today `focused` only tracks canvas focus, not agent-vs-human
  arbitration.)

### Frontend — empty-state hint bug (`BrowserTab.tsx:127`)
- The "Open a browser" hint currently renders on **every** `connected` state, so it overlays
  a live browser view and can stack with the bottom "Tap to control" caption on a narrow
  panel. Fix in two steps:
  1. **Always:** ensure at most one bottom-pinned caption shows at a time (de-stack), so the
     two hints never overlap.
  2. **If a no-browser-open signal is available** (an explicit backend/x11 "no browser
     launched" state, or a reliable blank-framebuffer indicator), gate the hint on it so it
     hides once a browser is open. The plan's first frontend task must confirm whether such
     a signal exists; if none does, ship step 1 only and leave gating as a noted follow-up
     rather than inventing a fragile blank-pixel heuristic.

## Error handling / fallbacks

- Panel aspect ≠ 16:10 → noVNC scales to fit with a *small* residual margin on one axis
  (far less than today's 5:4 banding); acceptable and deterministic.
- Disconnected / reconnecting / disabled / nodisplay → unchanged full-screen overlays.
- Test/storybook injection (`initialState`) path → unchanged; skips real connect.

## Testing

- **Server (pytest):** assert `x11manager` builds Xvfb with `1280x800x24` from the settings
  default (geometry comes from config, no hard-coded literal in the manager).
- **Frontend (vitest):**
  - `VncClient` sets `scaleViewport = true` on connect.
  - `ResizeObserver` change triggers a (debounced) re-fit call.
  - Zoom state: status pill reflects **current** zoom (`Fit` / `100%` / `NNN%`), Fit-reset
    control appears only when zoomed, and an explicit Fit resets zoom.
  - Control-state indicator renders the correct one of {agent-driving, tap-to-control,
    in-control} for the given props and is mutually exclusive.
  - Empty-state hint renders only when no browser is open.
- **Manual:** load the Browser tab on a desktop panel and a narrow (phone-width) panel;
  confirm no letterbox, readable desktop layout, smooth re-fit on resize, zoom + Fit reset,
  and the empty-state hint no longer stacks.

## Out of scope

- Dynamic remote framebuffer resize / `x11vnc -xrandr` / RandR (explicitly rejected above).
- Changing the WebSocket relay, auth, or session lifecycle.
- A per-session "mobile vs desktop viewport" chooser (could be a future follow-up; default
  desktop is deterministic and sufficient now).
