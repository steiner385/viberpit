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

## Delivery: two PRs

Per UX sign-off, the letterbox/clipping bug is a constant, every-session defect with a
small, deterministic, self-contained fix — while the control-state rework is net-new
arbitration behavior with an unresolved backend contract (see PR2 gate). Coupling them
delays the obvious win and widens the blast radius of the risky one. So:

- **PR 1 — the fit story (ships first):** server resolution change · scale-to-fit re-fit ·
  continuous-zoom model · empty-state de-stack · **accessibility** · top-edge stacking. Fully
  shippable on its own and resolves the reported bug.
- **PR 2 — control-state arbitration:** the three-state agent/human control model, gated on
  resolving the exclusive-vs-advisory question below before frontend work starts.

The **zoom model stays in PR 1** — it is part of "look closer at the now-correct view" and
shares the same surface as the fit fix. Accessibility (a11y) and the top-edge stacking fix
also land in **PR 1**, since the zoom pill and the existing top-right button live there
regardless of when control-state lands.

## PR 1 — fit story

### Server (1 line)
- **`server/config.py`** — `xvfb_resolution` default `1280x1024x24` → **`1280x800x24`**.
  This is the single source of truth for display geometry; no new constant. `x11manager.py`
  already passes it straight to Xvfb's `-screen`, so no other server change is required.
  (Constitution VI: route through the existing setting, do not add a parallel one.)

### Fit & re-fit (`web/src/vnc/vncClient.ts`, `web/src/components/BrowserTab.tsx`)
- Keep `rfb.scaleViewport = true` as the **base fit** (default on mobile and desktop).
- Add a `ResizeObserver` on the canvas mount that triggers a **re-fit**, **debounced ~150ms**
  with an eased CSS transition, so panel resize / phone rotation does not snap or flicker.
  noVNC recomputes its scale against the current mount size.
- The eased transition MUST be disabled under `prefers-reduced-motion: reduce` (snap
  instantly instead).
- Off-16:10 panels leave a small residual margin on one axis; the margin MUST use the panel
  background color (not black), so it reads as neutral padding, not a mini-letterbox.

### Continuous-zoom model (replaces the binary `Fit ⇄ 1:1` toggle)
- Default = **Fit** (base scale only, user zoom = 1.0).
- User zoom layered on top of the base fit as a client transform:
  - **Touch:** pinch to zoom.
  - **Desktop:** `Ctrl`/`Cmd`+scroll and `+` / `−` keys, plus on-screen `+` / `−` buttons so
    the feature is reachable **keyboard-only** (scroll-zoom alone strands keyboard users).
- **Bounds & increments (numeric, so an engineer can't ship unbounded zoom or a flickering
  reset):**
  - Min zoom = **1.0** (Fit; never zoom out past base fit). Max zoom = **3.0** (300%).
  - Step = **0.1** per `+`/`−` press; pinch/scroll is continuous, clamped to `[1.0, 3.0]`.
  - "Zoomed away from fit" threshold = user zoom **> 1.02** (epsilon avoids the Fit control
    flickering in/out at the boundary).
- A **"Fit" reset control appears only when zoomed away from fit** (per the threshold above);
  activating it returns to base scale (user zoom → 1.0).
- The status pill shows the **current state** (`Fit` / `110%` / … / `300%`), never the action
  — fixing the live label bug at `BrowserTab.tsx:107` (`{fit ? '1:1' : 'Fit'}` labels the
  action, not the state).
- Zoom resets to Fit **only on the explicit Fit action**, never on an incidental panel resize
  (so resize does not fight an active zoom; when zoomed, re-fit recomputes base scale and
  preserves the user-zoom multiplier).
- Preserve the **≥44px touch target** on every control and the always-mounted-canvas pattern
  (overlays for transitional states), both correct today.

### Top-edge stacking (`BrowserTab.tsx`)
- The zoom pill, the `+`/`−` / Fit controls, and (in PR2) the control-state indicator are all
  top-anchored and would compete on a 390px column. PR1 MUST define a single coherent
  top-edge layout: zoom controls + pill grouped top-right (one cluster), leaving top-left
  free for PR2's control-state indicator. No two top-edge elements may overlap on a phone
  panel.

### Empty-state hint bug (`BrowserTab.tsx:127`)
- The "Open a browser" hint currently renders on **every** `connected` state, so it overlays
  a live browser view and can stack with the bottom caption on a narrow panel. Fix:
  1. **Always:** at most one bottom-pinned caption shows at a time (de-stack), so captions
     never overlap each other.
  2. **No-browser-open signal — resolve before frontend work, not during.** PR1's first task
     determines whether an explicit "no browser launched" signal exists (backend/x11 state)
     so the hint can hide once a browser is open. If one exists, gate on it. If none does,
     the degraded fallback is a **dismissible** hint (shown once, user can close it) rather
     than an always-on CTA overlaying live content — do **not** invent a blank-pixel
     heuristic, and do **not** leave an always-on CTA over a working page.

### Accessibility (PR 1)
- The live canvas is an opaque `<div tabIndex=0>` with no accessible name today — give it a
  clear `aria-label` (e.g. "Live browser view") so screen readers announce what it is.
- Status/zoom pill and (PR2) control-state use `aria-live="polite"` so transitions are
  announced non-visually, not only to sighted users.
- Focus management: when the empty-state button or any overlay mounts/unmounts, focus MUST
  NOT be trapped or lost (e.g. returning focus to the canvas when the "Open a browser" button
  disappears).
- `prefers-reduced-motion: reduce` disables the eased re-fit transition (also noted above).

## PR 2 — control-state arbitration (`BrowserTab.tsx`)

**Gate (resolve before frontend work):** the load-bearing question is whether human control
is **exclusive** (the human taking control pauses/blocks the agent — agent yields the cursor)
or **advisory** (both can send input, racing on one cursor). The current code only tracks
canvas `focus`; exclusive control is net-new behavior with a **backend contract implication**
(the engine must expose/honor a "human has control" state). PR2 does not start until this is
decided and the backend contract, if any, is specified.

- Collapse the separate amber "Agent is driving" badge (top-left) and "Tap to control"
  caption (bottom) into **one mutually-exclusive status indicator** (top-left, per the PR1
  layout) answering "can I touch this right now?":
  - **Agent driving** (human view is passive)
  - **Tap to take control**
  - **You're in control** (with an explicit, reversible way back to letting the agent drive)
- Taking/releasing control is **explicit and reversible** — no silent focus-steal on a shared
  autonomous surface — with the exact pause/yield semantics determined by the gate above.

## Error handling / fallbacks

- Panel aspect ≠ 16:10 → noVNC scales to fit with a *small* residual margin on one axis (far
  less than today's 5:4 banding), margin in panel bg color; deterministic.
- Disconnected / reconnecting / disabled / nodisplay → unchanged full-screen overlays.
- Test/storybook injection (`initialState`) path → unchanged; skips real connect.

## Testing

- **Server (pytest):** assert `x11manager` builds Xvfb with `1280x800x24` from the settings
  default (geometry comes from config, no hard-coded literal in the manager).
- **Frontend (vitest) — PR1:**
  - `VncClient` sets `scaleViewport = true` on connect.
  - `ResizeObserver` change triggers a (debounced) re-fit call.
  - Zoom: pill reflects **current** zoom (`Fit` / `NNN%`); clamps to `[1.0, 3.0]`; `+`/`−`
    step 0.1; Fit-reset appears only when zoom > 1.02; explicit Fit resets to 1.0; a re-fit
    while zoomed preserves the user-zoom multiplier.
  - Canvas has an `aria-label`; reduced-motion path skips the transition.
  - Empty-state: at most one bottom caption at a time; hint hidden when a browser is open
    (or dismissible if no signal exists).
- **Frontend (vitest) — PR2:**
  - Control-state indicator renders exactly one of {agent-driving, tap-to-control,
    in-control} for given props (mutually exclusive) and is `aria-live`.
- **Manual:**
  - PR1: desktop panel and narrow (phone-width) panel — no letterbox, readable desktop
    layout, smooth (and reduced-motion-respecting) re-fit on resize, zoom + Fit reset,
    keyboard-only zoom reach, empty-state no longer stacks/overlays.
  - PR2: exercise the **agent→human→agent control hand-off transition** (not just static
    state), confirming no silent steal and correct pause/yield per the gate decision.

## Out of scope

- Dynamic remote framebuffer resize / `x11vnc -xrandr` / RandR (explicitly rejected above).
- Changing the WebSocket relay, auth, or session lifecycle.
- A per-session "mobile vs desktop viewport" chooser (could be a future follow-up; default
  desktop is deterministic and sufficient now).
