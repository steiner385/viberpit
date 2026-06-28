# Mobile soft-keyboard toggle for virtual X (browser) sessions

**Date:** 2026-06-28
**Status:** Approved design — ready for implementation plan
**Feature area:** `web/` frontend — virtual browser / noVNC viewer (`BrowserTab`)

## Problem

On touch devices, focusing the noVNC `<canvas>` in `BrowserTab.tsx` never summons the
OS soft keyboard, because mobile keyboards only appear for *editable* DOM elements
(`<input>` / `<textarea>` / `contentEditable`). The virtual X session renders into a
`<canvas>`, so today a phone user can tap to control and pinch to zoom, but **cannot
type into the virtual X session at all**. The "Tap to control · pinch to zoom" caption
is only half-true on mobile.

noVNC's RFB can inject keystrokes via `sendKey(keysym, code, down)`, but that method is
not exposed by our `VncClient` wrapper or typed in `novnc.d.ts` (both currently expose
only `focus`/`blur`).

## Goal

Add a keyboard toggle button to the virtual browser viewer that, on touch devices,
summons the OS soft keyboard and relays its keystrokes into the live noVNC session.

Decisions locked during brainstorming:
- **Goal:** summon the *native* OS soft keyboard (not a custom on-screen key grid).
- **Visibility:** show the toggle only on coarse-pointer / touch devices
  (`matchMedia('(pointer: coarse)')`). Desktop is unchanged — canvas focus already works.
- **Capture:** hybrid — printable text from the `input`/`beforeinput` event (robust
  against Android/GBoard's `keyCode=229` IME sentinel), control keys (Enter, Backspace)
  from `keydown`.

## Architecture

A **hidden editable proxy + a toggle button** inside `BrowserTab`:

- A visually-hidden `<input>` sits in `BrowserTab`. Focusing it summons the OS keyboard;
  blurring dismisses it.
- A **⌨ toggle button** in the existing top-right zoom cluster focuses/blurs that proxy
  and reflects open/closed state via `aria-pressed`.
- Keystrokes captured on the proxy are translated to X11 keysyms and pushed into the live
  session via a new `VncClient.sendKey(...)` → `rfb.sendKey(...)`.
- The button only renders on coarse-pointer devices.

## Components & boundaries

| Unit | Responsibility | Interface |
|---|---|---|
| `VncClient.sendKey` (`web/src/vnc/vncClient.ts`) | Thin pass-through to `rfb.sendKey`; no-op when `rfb` is null | `sendKey(keysym: number, code: string \| null, down?: boolean): void` |
| `novnc.d.ts` | Type the real RFB `sendKey` we now call | add `sendKey(keysym: number, code: string \| null, down?: boolean): void` to `RFBInstance` |
| touch detection (`web/src/lib/`) | Detect coarse-pointer device | a `boolean`; reuse `useMediaQuery('(pointer: coarse)')` if it fits, else a small `usePointerCoarse()` hook |
| key-relay helper (`web/src/vnc/keyRelay.ts`) | **Translate an input/keydown event → sendKey call(s)** | `relayInput(text: string, send): void`, `relayKeydown(e: KeyboardEvent, send): boolean` |
| `BrowserTab` toggle + hidden input | UI: button, proxy element, wiring, focus management | local `keyboardOpen` state |

`send` is the callback `(keysym: number, code: string | null, down?: boolean) => void`
wired to `VncClient.sendKey`.

The translation helper (`keyRelay.ts`) is the one piece with real design judgment
(Unicode→keysym mapping, which special keys a soft keyboard emits, discrete down/up
sequencing), so it is isolated as a pure module — independently unit-testable in jsdom
without a real keyboard, mirroring the existing pure `classifyClose` export in
`vncClient.ts`.

`keyRelay.ts` is **self-contained** — it does NOT import noVNC's internal keysym tables.
The `@novnc/novnc` package declares `"exports": "./core/rfb.js"` (a single string), which
seals every subpath, so `@novnc/novnc/core/input/keysymdef.js` is not importable. The
helper reproduces noVNC's `lookup()` rule directly: a Latin-1 codepoint (`0x20`–`0xFF`)
maps one-to-one to itself; any other codepoint maps to the Unicode keysym form
`0x01000000 | codepoint`. (The legacy non-Unicode keysym table is intentionally skipped —
acceptable YAGNI for mobile text entry; modern X/VNC accept the Unicode form.) Control
keysyms are local constants: Enter = `0xFF0D` (XK_Return), Backspace = `0xFF08`
(XK_BackSpace). `rfb.sendKey(keysym, code)` with `down` omitted auto-sends a press+release
pair, so printable chars need a single call.

## Data flow

```
user taps ⌨  → keyboardOpen=true → hiddenInput.focus() → OS keyboard appears
user types   → 'beforeinput'/'input' on hiddenInput → relayInput(text, send)
                                                     → VncClient.sendKey → rfb.sendKey → session
Enter/Backspace → 'keydown' on hiddenInput → relayKeydown(e, send) → sendKey(0xFF0D / 0xFF08)
hiddenInput value is cleared after each input so it never grows / never composes
user taps ⌨ again (or blur) → keyboardOpen=false → hiddenInput.blur() → keyboard dismisses
```

The hybrid capture: printable text from `input`/`beforeinput` (immune to Android's
`keyCode=229`), control keys from `keydown`.

## Error handling & edge cases

- **No live session / disconnected:** `sendKey` no-ops when `rfb` is null (same guard as
  `focus`/`blur`). Typing while reconnecting is silently dropped, not a crash.
- **Agent is driving:** keystrokes still relay (matches today's canvas behavior — input
  is not locked). Decision: keep unlocked.
- **Desktop:** button hidden via coarse-pointer gate; nothing changes — canvas focus keeps
  working as today.
- **Existing canvas keydown** (zoom `+`/`-`, `Esc`-to-blur) is untouched; the proxy is a
  separate element from the canvas.
- **Proxy never accumulates text:** its value is cleared after each `input` so it does not
  grow and does not enter a composition state.

## Testing

- `web/src/vnc/keyRelay.test.ts` — pure unit tests of translation:
  - a printable char → correct keysym + a down then up pair (or single `sendKey` call per
    the helper's chosen contract);
  - Enter `keydown` → `sendKey(0xFF0D, …)`; Backspace `keydown` → `sendKey(0xFF08, …)`;
  - empty string / IME-noise input → no `sendKey` calls.
- `web/src/components/BrowserTab.test.tsx`:
  - toggle button **absent** when `matchMedia('(pointer: coarse)')` is false, **present**
    when true (mock `matchMedia`);
  - clicking the toggle focuses the hidden input and flips `aria-pressed`;
  - an `input` event on the proxy invokes `VncClient.sendKey`.

## Out of scope (YAGNI)

- Custom on-screen key grid / special-key row (Esc, Tab, Ctrl, arrows). Native keyboard only.
- Programmatic key chords / modifier combos beyond what the OS keyboard emits.
- Desktop changes — desktop keyboard already works via canvas focus.

## Files touched

- `web/src/vnc/vncClient.ts` — add `sendKey`.
- `web/src/vnc/novnc.d.ts` — type `sendKey`.
- `web/src/vnc/keyRelay.ts` — **new** pure translation helper.
- `web/src/vnc/keyRelay.test.ts` — **new** unit tests.
- `web/src/lib/` — touch detection (reuse `useMediaQuery` or small new hook).
- `web/src/components/BrowserTab.tsx` — toggle button, hidden input, wiring.
- `web/src/components/BrowserTab.test.tsx` — component tests (new or extended).
