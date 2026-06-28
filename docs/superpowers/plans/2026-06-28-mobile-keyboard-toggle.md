# Mobile soft-keyboard toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a touch-only keyboard toggle to the virtual browser (noVNC) viewer that summons the OS soft keyboard and relays its keystrokes into the live session.

**Architecture:** A visually-hidden `<input>` proxy in `BrowserTab` summons the OS keyboard when focused; a ⌨ toggle button (shown only on coarse-pointer devices) focuses/blurs it. Keystrokes captured on the proxy are translated to X11 keysyms by a pure `keyRelay.ts` helper and pushed into noVNC via a new `VncClient.sendKey` → `rfb.sendKey`.

**Tech Stack:** React + TypeScript + Vite, `@novnc/novnc` RFB, Vitest + Testing Library (jsdom), Tailwind.

## Global Constraints

- Reuse the existing `useMediaQuery('(pointer: coarse)')` hook (`web/src/lib/useMediaQuery.ts`) for touch detection — do NOT add a parallel detection util. It returns `false` in jsdom, so tests must mock `window.matchMedia` to exercise the touch branch.
- `keyRelay.ts` MUST be self-contained: do NOT import any `@novnc/novnc/core/...` subpath — the package's `"exports": "./core/rfb.js"` seals all subpaths and the import will not resolve.
- Touch targets stay `min-h-[44px] min-w-[44px]`, styled to match the existing zoom-cluster buttons in `BrowserTab.tsx`.
- Every button keeps an `aria-label`; the toggle also exposes `aria-pressed` reflecting open/closed state.
- Frontend gates (run in `web/`): `vitest run`, `npx tsc -b --force` (the real type gate — `tsc --noEmit` checks nothing here), `eslint` with `--max-warnings 0`.
- X11 control keysyms: Enter (XK_Return) = `0xff0d`, Backspace (XK_BackSpace) = `0xff08`.

---

### Task 1: Expose `sendKey` on the noVNC wrapper

**Files:**
- Modify: `web/src/vnc/novnc.d.ts` (add `sendKey` to `RFBInstance`)
- Modify: `web/src/vnc/vncClient.ts` (add `sendKey` method)
- Test: `web/src/vnc/vncClient.test.ts` (create if absent; otherwise extend)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `VncClient.sendKey(keysym: number, code: string | null, down?: boolean): void` — passes through to `rfb.sendKey(keysym, code, down)`; no-ops when no RFB is connected. Task 3 calls this.

- [ ] **Step 1: Write the failing test**

Create `web/src/vnc/vncClient.test.ts` (or append the `describe` if the file already exists):

```ts
import { describe, it, expect, vi } from 'vitest'
import { VncClient } from './vncClient'

describe('VncClient.sendKey', () => {
  it('no-ops when no RFB is connected', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    // Never called connect(): rfb is null. Must not throw.
    expect(() => c.sendKey(0x41, null)).not.toThrow()
  })

  it('forwards keysym/code/down to the underlying rfb', () => {
    const c = new VncClient('ws://x/ws/vnc?session=s')
    const fake = { sendKey: vi.fn() }
    // Inject a fake RFB without opening a real socket.
    ;(c as unknown as { rfb: unknown }).rfb = fake
    c.sendKey(0xff0d, 'Enter', true)
    expect(fake.sendKey).toHaveBeenCalledWith(0xff0d, 'Enter', true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/vnc/vncClient.test.ts`
Expected: FAIL — `c.sendKey is not a function`.

- [ ] **Step 3: Add the type**

In `web/src/vnc/novnc.d.ts`, add to the `RFBInstance` interface, right after `blur(): void`:

```ts
    sendKey(keysym: number, code: string | null, down?: boolean): void
```

- [ ] **Step 4: Add the method**

In `web/src/vnc/vncClient.ts`, add after the `blur()` method (before `disconnect()`):

```ts
  /** Inject a key into the live session. With `down` omitted, noVNC sends a
   *  press+release pair. No-ops when no RFB is connected (e.g. reconnecting). */
  sendKey(keysym: number, code: string | null, down?: boolean) {
    this.rfb?.sendKey(keysym, code, down)
  }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd web && npx vitest run src/vnc/vncClient.test.ts`
Expected: PASS (both tests).

- [ ] **Step 6: Typecheck + lint**

Run: `cd web && npx tsc -b --force && npx eslint src/vnc/vncClient.ts src/vnc/novnc.d.ts src/vnc/vncClient.test.ts --max-warnings 0`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add web/src/vnc/vncClient.ts web/src/vnc/novnc.d.ts web/src/vnc/vncClient.test.ts
git commit -m "feat(vnc): expose sendKey passthrough on VncClient"
```

---

### Task 2: Pure key-relay translation helper

**Files:**
- Create: `web/src/vnc/keyRelay.ts`
- Test: `web/src/vnc/keyRelay.test.ts`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces, for Task 3:
  - `type SendKey = (keysym: number, code: string | null, down?: boolean) => void`
  - `charToKeysym(codepoint: number): number`
  - `relayInput(text: string, send: SendKey): void`
  - `relayKeydown(e: Pick<KeyboardEvent, 'key' | 'code'>, send: SendKey): boolean` — returns `true` if it handled the key (caller should `preventDefault`).
  - Constants `XK_RETURN = 0xff0d`, `XK_BACKSPACE = 0xff08`.

> **Learning-mode handoff:** Step 3 implements `charToKeysym` — the one piece with real
> design judgment. The scaffolding, tests, and surrounding functions are provided; the
> ~3-line body of `charToKeysym` is for the user to write. See the note in Step 3.

- [ ] **Step 1: Write the failing tests**

Create `web/src/vnc/keyRelay.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest'
import { charToKeysym, relayInput, relayKeydown, XK_RETURN, XK_BACKSPACE } from './keyRelay'

describe('charToKeysym', () => {
  it('maps Latin-1 codepoints one-to-one', () => {
    expect(charToKeysym(0x41)).toBe(0x41) // 'A'
    expect(charToKeysym(0x20)).toBe(0x20) // space
    expect(charToKeysym(0xff)).toBe(0xff) // ÿ (upper Latin-1 boundary)
  })

  it('maps non-Latin-1 codepoints to the Unicode keysym form', () => {
    expect(charToKeysym(0x2603)).toBe(0x01000000 | 0x2603) // ☃ snowman
    expect(charToKeysym(0x1f600)).toBe(0x01000000 | 0x1f600) // 😀 (astral)
  })
})

describe('relayInput', () => {
  it('sends one key per character with down omitted (press+release)', () => {
    const send = vi.fn()
    relayInput('Hi', send)
    expect(send.mock.calls).toEqual([
      [0x48, null, undefined], // 'H'
      [0x69, null, undefined], // 'i'
    ])
  })

  it('iterates by code point so astral characters stay intact', () => {
    const send = vi.fn()
    relayInput('😀', send)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenCalledWith(0x01000000 | 0x1f600, null, undefined)
  })

  it('ignores empty input', () => {
    const send = vi.fn()
    relayInput('', send)
    expect(send).not.toHaveBeenCalled()
  })
})

describe('relayKeydown', () => {
  it('handles Enter as XK_Return and reports handled', () => {
    const send = vi.fn()
    expect(relayKeydown({ key: 'Enter', code: 'Enter' }, send)).toBe(true)
    expect(send).toHaveBeenCalledWith(XK_RETURN, 'Enter', undefined)
  })

  it('handles Backspace as XK_BackSpace and reports handled', () => {
    const send = vi.fn()
    expect(relayKeydown({ key: 'Backspace', code: 'Backspace' }, send)).toBe(true)
    expect(send).toHaveBeenCalledWith(XK_BACKSPACE, 'Backspace', undefined)
  })

  it('ignores printable keys (those arrive via relayInput) and reports unhandled', () => {
    const send = vi.fn()
    expect(relayKeydown({ key: 'a', code: 'KeyA' }, send)).toBe(false)
    expect(send).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/vnc/keyRelay.test.ts`
Expected: FAIL — cannot resolve `./keyRelay`.

- [ ] **Step 3: Create the helper (user writes `charToKeysym`)**

Create `web/src/vnc/keyRelay.ts`. Write everything EXCEPT the body of `charToKeysym`, which is the learning-mode contribution:

```ts
// Pure translation from DOM keyboard signals to noVNC sendKey() calls.
// Self-contained ON PURPOSE: @novnc/novnc's "exports" seals every subpath, so
// noVNC's own keysymdef table cannot be imported here.

export type SendKey = (keysym: number, code: string | null, down?: boolean) => void

// X11 keysyms for the control keys a soft keyboard emits via keydown.
export const XK_RETURN = 0xff0d
export const XK_BACKSPACE = 0xff08

/**
 * Translate a Unicode code point to an X11 keysym, reproducing noVNC's lookup()
 * rule (we can't import theirs):
 *   - Latin-1 (0x20..0xff): the keysym IS the code point.
 *   - everything else: the Unicode keysym form, 0x01000000 | codePoint.
 * (The legacy non-Unicode keysym table is intentionally skipped — YAGNI for
 *  mobile text entry; modern X/VNC accept the Unicode form.)
 *
 * TODO(learning-mode): implement the ~3-line body per the rule above.
 */
export function charToKeysym(codepoint: number): number {
  throw new Error('not implemented')
}

/** Relay typed text: one press+release per character (down omitted). */
export function relayInput(text: string, send: SendKey): void {
  for (const ch of text) {
    const cp = ch.codePointAt(0)
    if (cp === undefined) continue
    send(charToKeysym(cp), null)
  }
}

/** Relay control keys from keydown. Returns true if handled (caller preventDefaults). */
export function relayKeydown(e: Pick<KeyboardEvent, 'key' | 'code'>, send: SendKey): boolean {
  if (e.key === 'Enter') {
    send(XK_RETURN, e.code)
    return true
  }
  if (e.key === 'Backspace') {
    send(XK_BACKSPACE, e.code)
    return true
  }
  return false
}
```

> **User contribution — implement `charToKeysym`.** Replace the `throw` with the rule in
> the docstring. Two decisions are baked into the design and worth understanding as you
> write it: (1) Latin-1 passes through unchanged because X11 keysyms for that range equal
> the code point; (2) the `0x01000000 | cp` fallback is what makes arbitrary Unicode
> (emoji, CJK) typeable without a giant table. The `relayInput` loop already iterates by
> code point, so `charToKeysym` only ever sees a single complete code point.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/vnc/keyRelay.test.ts`
Expected: PASS (all groups). If `charToKeysym` tests fail, the user's implementation needs adjusting against the rule.

- [ ] **Step 5: Typecheck + lint**

Run: `cd web && npx tsc -b --force && npx eslint src/vnc/keyRelay.ts src/vnc/keyRelay.test.ts --max-warnings 0`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/vnc/keyRelay.ts web/src/vnc/keyRelay.test.ts
git commit -m "feat(vnc): add pure key-relay translation helper"
```

---

### Task 3: Keyboard toggle + hidden input in BrowserTab

**Files:**
- Modify: `web/src/components/BrowserTab.tsx`
- Test: `web/src/components/BrowserTab.test.tsx` (create if absent; otherwise extend)

**Interfaces:**
- Consumes: `VncClient.sendKey` (Task 1); `relayInput`, `relayKeydown`, `SendKey` (Task 2); `useMediaQuery` (existing).
- Produces: user-facing toggle; no exported API.

- [ ] **Step 1: Write the failing tests**

Create/extend `web/src/components/BrowserTab.test.tsx`. These tests render with an injected `initialState` so no real socket opens (see `BrowserTab.tsx:50`), and stub `window.matchMedia` to drive the coarse-pointer branch:

```tsx
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { BrowserTab } from './BrowserTab'

function setCoarsePointer(coarse: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('coarse') ? coarse : false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  }))
}

afterEach(() => vi.unstubAllGlobals())

describe('BrowserTab keyboard toggle', () => {
  it('hides the toggle on fine-pointer (desktop) devices', () => {
    setCoarsePointer(false)
    render(<BrowserTab sessionId="s" statusState="idle" initialState="connected" />)
    expect(screen.queryByRole('button', { name: /keyboard/i })).toBeNull()
  })

  it('shows the toggle on coarse-pointer (touch) devices and reflects aria-pressed', () => {
    setCoarsePointer(true)
    render(<BrowserTab sessionId="s" statusState="idle" initialState="connected" />)
    const btn = screen.getByRole('button', { name: /keyboard/i })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('relays typed text from the hidden input through to the session', () => {
    setCoarsePointer(true)
    render(<BrowserTab sessionId="s" statusState="idle" initialState="connected" />)
    fireEvent.click(screen.getByRole('button', { name: /keyboard/i }))
    const proxy = screen.getByLabelText('Keyboard input') as HTMLInputElement
    // With initialState injected, clientRef is null, so sendKey path must not throw.
    proxy.value = 'a'
    expect(() => fireEvent.input(proxy)).not.toThrow()
    // Proxy is cleared after each input so it never accumulates / composes.
    expect(proxy.value).toBe('')
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components/BrowserTab.test.tsx`
Expected: FAIL — no button named "keyboard", no element labelled "Keyboard input".

- [ ] **Step 3: Wire imports and state**

In `web/src/components/BrowserTab.tsx`, update the top imports:

```tsx
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { VncClient, type VncState } from '../vnc/vncClient'
import { useZoom } from '../vnc/useZoom'
import { statusTier } from '../session/statusTier'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { relayInput, relayKeydown } from '../vnc/keyRelay'
```

Inside the component, after the existing `const [openError, …]` state (around line 34), add:

```tsx
  const isTouch = useMediaQuery('(pointer: coarse)')
  const [keyboardOpen, setKeyboardOpen] = useState(false)
  const hiddenInputRef = useRef<HTMLInputElement>(null)

  // Push a key into the live session; no-ops when not connected.
  const send = (keysym: number, code: string | null, down?: boolean) =>
    clientRef.current?.sendKey(keysym, code, down)

  function toggleKeyboard() {
    const next = !keyboardOpen
    setKeyboardOpen(next)
    if (next) hiddenInputRef.current?.focus()
    else hiddenInputRef.current?.blur()
  }
```

- [ ] **Step 4: Add the hidden input proxy**

In `BrowserTab.tsx`, add the proxy as the first child inside the outer `return`'s root `<div className="relative h-full w-full overflow-hidden">` (immediately after that opening tag, before the pan container comment). The off-screen positioning summons the OS keyboard without showing a visible field:

```tsx
      {/* Hidden editable proxy: focusing it summons the OS soft keyboard on touch
          devices (a <canvas> never does). Keystrokes relay into noVNC. Kept empty
          so it never enters an IME composition state. */}
      <input
        ref={hiddenInputRef}
        aria-label="Keyboard input"
        className="absolute h-px w-px opacity-0"
        style={{ left: -9999, top: 0 }}
        autoCapitalize="none"
        autoCorrect="off"
        autoComplete="off"
        spellCheck={false}
        onInput={(e) => {
          relayInput(e.currentTarget.value, send)
          e.currentTarget.value = ''
        }}
        onKeyDown={(e) => {
          if (relayKeydown(e, send)) e.preventDefault()
        }}
        onBlur={() => setKeyboardOpen(false)}
      />
```

- [ ] **Step 5: Add the toggle button to the zoom cluster**

In `BrowserTab.tsx`, inside the zoom cluster `<div className="absolute right-2 top-2 flex items-center gap-1">`, add as the first child (before the "Zoom out" button), gated on touch:

```tsx
        {isTouch && (
          <button
            aria-label={keyboardOpen ? 'Hide keyboard' : 'Show keyboard'}
            aria-pressed={keyboardOpen}
            className={cn(
              'min-h-[44px] min-w-[44px]',
              'rounded border border-border bg-background/80 px-2 text-base leading-none',
              keyboardOpen && 'bg-primary text-primary-foreground',
            )}
            onClick={toggleKeyboard}
          >
            ⌨
          </button>
        )}
```

- [ ] **Step 6: Run the component tests to verify they pass**

Run: `cd web && npx vitest run src/components/BrowserTab.test.tsx`
Expected: PASS (all three).

- [ ] **Step 7: Full frontend gates**

Run: `cd web && npx vitest run && npx tsc -b --force && npx eslint src --max-warnings 0`
Expected: whole suite green, no type errors, no lint warnings.

- [ ] **Step 8: Commit**

```bash
git add web/src/components/BrowserTab.tsx web/src/components/BrowserTab.test.tsx
git commit -m "feat(web): touch keyboard toggle for virtual browser sessions"
```

---

## Notes for the reviewer / manual verification

- **Manual mobile check (cannot be unit-tested):** on a phone, open a session's Browser tab,
  tap ⌨, confirm the OS keyboard appears and typed characters land in the virtual browser;
  confirm Enter submits and Backspace deletes; tap ⌨ again (or dismiss the keyboard) and
  confirm it closes and `aria-pressed` returns to false.
- **Known caveat (out of scope):** some Android keyboards deliver Backspace via a
  `beforeinput`/`deleteContentBackward` event rather than a `keydown` — if field reports show
  Backspace not working on a specific keyboard, that is the follow-up, not a regression here.
- **No backend changes** — keystrokes ride the existing `/ws/vnc` RFB channel.
