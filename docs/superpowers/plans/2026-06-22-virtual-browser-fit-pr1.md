# Virtual Browser Fit — PR1 (the fit story) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the session Browser tab fill its panel with a deterministic desktop layout (no letterbox/clipping), re-fit cleanly on resize, and offer a continuous-zoom affordance — all client-side over an unchanged VNC pipeline.

**Architecture:** Switch the server-side virtual display from a 5:4 `1280x1024` framebuffer to a fixed 16:10 `1280x800` one (one config value), and keep noVNC's `scaleViewport` as the base fit. Layer a user-controlled CSS-transform zoom on top of noVNC's canvas inside an `overflow:auto` pan container, with a `ResizeObserver` driving debounced re-fits. The framebuffer never resizes; the agent always sees a fixed desktop.

**Tech Stack:** Python FastAPI + pydantic/dataclass settings · React + TypeScript + Vite + Tailwind · `@novnc/novnc` ^1.7.0 · vitest + @testing-library/react · pytest.

## Global Constraints

- Fixed framebuffer is **`1280x800x24`** (16:10). Single source of truth = `server/config.py` `xvfb_resolution`; no new constant, no literal in `x11manager.py`.
- Zoom bounds: **min 1.0** (Fit, never zoom out past base), **max 3.0** (300%), **step 0.1** per `+`/`−`, continuous input clamped to `[1.0, 3.0]`. "Zoomed away from fit" threshold = zoom **> 1.02**.
- Status pill shows **current state** (`Fit` / `NNN%`), never the action. No binary `1:1` label anywhere.
- Every interactive control keeps a **≥44px touch target**. Canvas stays always-mounted.
- Eased re-fit transition MUST respect `prefers-reduced-motion: reduce` (snap instead).
- Off-16:10 residual margin uses the **panel background color**, not black.
- Top edge: zoom controls + pill grouped **top-right** as one cluster; **top-left reserved** for PR2's control-state indicator. No two top-edge elements overlap on a 390px panel.
- Web tests run in `web/` via `vitest run`; also keep `tsc --noEmit` and `eslint` green. Server tests via `pytest server/tests/`. A fresh worktree has no built env — run pytest through the main checkout's prebuilt venv and symlink `web/node_modules` (see CLAUDE.md Dev/testing).
- Commit after each task. Branch: `feat/virtual-browser-fit` (already created).

---

## File structure

- `server/config.py` — change the two `xvfb_resolution` defaults (pydantic Field + dataclass fallback).
- `server/tests/test_config_xvfb.py` — update the pinned default assertion.
- `server/tests/test_x11_manager.py` — add a guard that Xvfb's `-screen` geometry comes from the injected resolution (no hard-coded literal).
- `web/src/vnc/vncClient.ts` — keep base fit; add `refit()`.
- `web/src/vnc/vncClient.test.ts` — cover base fit + `refit()`.
- `web/src/vnc/useZoom.ts` (new) — headless zoom hook: state, clamping, keyboard/wheel/pinch handlers, pill label.
- `web/src/vnc/useZoom.test.ts` (new) — unit-test the hook's pure logic.
- `web/src/components/BrowserTab.tsx` — ResizeObserver + reduced-motion transition; mount the zoom wrapper, pill, Fit-reset, top-right cluster; empty-state de-stack + a11y.
- `web/src/components/__tests__/BrowserTab.test.tsx` — extend for re-fit, zoom UI, a11y, empty-state.

---

### Task 1: Server — fixed `1280x800x24` framebuffer

**Files:**
- Modify: `server/config.py:44` and `server/config.py:113-114`
- Test: `server/tests/test_config_xvfb.py:19`, `server/tests/test_x11_manager.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.xvfb_resolution == "1280x800x24"` by default; `X11Manager` continues to receive geometry via its `resolution=` param (unchanged signature).

- [ ] **Step 1: Update the pinned default test to the new value (failing test)**

In `server/tests/test_config_xvfb.py`, change the assertion on line 19:

```python
    assert s.xvfb_resolution == "1280x800x24"
```

- [ ] **Step 2: Run it; verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_config_xvfb.py::test_xvfb_defaults -v`
Expected: FAIL — `assert '1280x1024x24' == '1280x800x24'`.

- [ ] **Step 3: Change both config defaults**

`server/config.py:44`:

```python
        xvfb_resolution: str = Field(default="1280x800x24", alias="CCSWITCH_XVFB_RESOLUTION")
```

`server/config.py:113-114`:

```python
        xvfb_resolution: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_XVFB_RESOLUTION", "1280x800x24")
        )
```

- [ ] **Step 4: Run; verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_config_xvfb.py -v`
Expected: PASS (both `test_xvfb_defaults` and `test_xvfb_env_override`).

- [ ] **Step 5: Add an x11 geometry guard (failing test)**

Append to `server/tests/test_x11_manager.py` (it already defines `FakeProc`, `DisplayAllocator`, `X11Manager` imports — reuse them; match the existing `fake_spawn` pattern that appends `argv`):

```python
async def test_xvfb_screen_geometry_comes_from_resolution(tmp_path):
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv); return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 1, 6100), resolution="1280x800x24",
                     max_browsers=1, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.ensure_display("sess-geom")
    xvfb_argv = next(a for a in spawned if a[0] == "Xvfb")
    assert "1280x800x24" in xvfb_argv, xvfb_argv
```

If the existing tests call a differently-named entry (e.g. `ensure_display` vs `attach`), mirror whatever the sibling tests in this file already use to provoke the Xvfb spawn.

- [ ] **Step 6: Run; verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_x11_manager.py -v`
Expected: PASS — proves `-screen 0 <resolution>` is wired from the injected value, so the config change actually reaches Xvfb and nobody hard-coded a literal.

- [ ] **Step 7: Commit**

```bash
git add server/config.py server/tests/test_config_xvfb.py server/tests/test_x11_manager.py
git commit -m "feat(browser): fixed 1280x800 (16:10) virtual display framebuffer

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: VncClient — base fit + `refit()`

**Files:**
- Modify: `web/src/vnc/vncClient.ts`
- Test: `web/src/vnc/vncClient.test.ts`

**Interfaces:**
- Consumes: `RFB` from `@novnc/novnc` (only `scaleViewport: boolean` + `addEventListener`).
- Produces: `VncClient.refit(): void` — re-applies base fit so noVNC recomputes its scale against the current mount size. (`setScale(fit)` is removed; the binary toggle is gone.)

- [ ] **Step 1: Write failing tests**

Replace `web/src/vnc/vncClient.test.ts` with a recording mock so we can assert on the RFB instance:

```typescript
// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi } from 'vitest'

let lastRfb: { scaleViewport: boolean; addEventListener: () => void }
vi.mock('@novnc/novnc', () => ({
  default: class {
    scaleViewport = false
    addEventListener = vi.fn()
    constructor() { lastRfb = this }
  },
}))

import { VncClient, classifyClose } from './vncClient'

describe('classifyClose', () => {
  it('maps distinct close codes to terminal states', () => {
    expect(classifyClose(4401)).toBe('disabled')
    expect(classifyClose(4404)).toBe('nodisplay')
  })
  it('treats other codes as reconnectable', () => {
    expect(classifyClose(1006)).toBe('reconnect')
  })
})

describe('VncClient fit', () => {
  it('enables scaleViewport (base fit) on connect', () => {
    const c = new VncClient('ws://x')
    c.connect(document.createElement('div'))
    expect(lastRfb.scaleViewport).toBe(true)
  })
  it('refit() leaves base fit enabled so noVNC recomputes scale', () => {
    const c = new VncClient('ws://x')
    c.connect(document.createElement('div'))
    lastRfb.scaleViewport = true
    c.refit()
    expect(lastRfb.scaleViewport).toBe(true)
  })
})
```

- [ ] **Step 2: Run; verify it fails**

Run (in `web/`): `npx vitest run src/vnc/vncClient.test.ts`
Expected: FAIL — `refit` is not a function / `setScale` referenced.

- [ ] **Step 3: Implement `refit()`, remove `setScale`**

In `web/src/vnc/vncClient.ts` replace the `setScale` line (`:30`) with:

```typescript
  /** Re-apply base fit so noVNC recomputes its scale against the current mount size.
   *  Toggling false→true forces the scaleViewport setter to recompute (it no-ops on
   *  an unchanged value). */
  refit() {
    if (!this.rfb) return
    this.rfb.scaleViewport = false
    this.rfb.scaleViewport = true
  }
```

Leave the `connect()` body (`this.rfb.scaleViewport = true`) unchanged.

- [ ] **Step 4: Run; verify it passes**

Run (in `web/`): `npx vitest run src/vnc/vncClient.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/vnc/vncClient.ts web/src/vnc/vncClient.test.ts
git commit -m "feat(browser): VncClient.refit() replaces binary scale toggle

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Zoom hook (`useZoom`) — pure state, clamping, label

**Files:**
- Create: `web/src/vnc/useZoom.ts`
- Test: `web/src/vnc/useZoom.test.ts`

**Interfaces:**
- Produces:
  ```typescript
  export const ZOOM_MIN = 1.0
  export const ZOOM_MAX = 3.0
  export const ZOOM_STEP = 0.1
  export const FIT_EPSILON = 1.02
  export function clampZoom(z: number): number            // clamp to [ZOOM_MIN, ZOOM_MAX]
  export function isZoomed(z: number): boolean            // z > FIT_EPSILON
  export function zoomLabel(z: number): string            // 'Fit' if !isZoomed, else `${Math.round(z*100)}%`
  export function useZoom(): {
    zoom: number
    zoomIn: () => void                                    // +ZOOM_STEP, clamped
    zoomOut: () => void                                   // -ZOOM_STEP, clamped
    setZoom: (z: number) => void                          // clamped
    resetFit: () => void                                  // → ZOOM_MIN
    zoomed: boolean
    label: string
  }
  ```
  Later tasks rely on these exact names.

- [ ] **Step 1: Write failing tests**

```typescript
// web/src/vnc/useZoom.test.ts
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { clampZoom, isZoomed, zoomLabel, useZoom, ZOOM_MIN, ZOOM_MAX } from './useZoom'

describe('zoom pure helpers', () => {
  it('clamps to [1,3]', () => {
    expect(clampZoom(0.2)).toBe(ZOOM_MIN)
    expect(clampZoom(9)).toBe(ZOOM_MAX)
    expect(clampZoom(1.4)).toBeCloseTo(1.4)
  })
  it('isZoomed uses the 1.02 epsilon', () => {
    expect(isZoomed(1.0)).toBe(false)
    expect(isZoomed(1.02)).toBe(false)
    expect(isZoomed(1.03)).toBe(true)
  })
  it('labels current state, never the action', () => {
    expect(zoomLabel(1.0)).toBe('Fit')
    expect(zoomLabel(1.4)).toBe('140%')
  })
})

describe('useZoom', () => {
  it('steps, clamps, and resets', () => {
    const { result } = renderHook(() => useZoom())
    expect(result.current.zoom).toBe(ZOOM_MIN)
    expect(result.current.zoomed).toBe(false)
    act(() => result.current.zoomIn())
    expect(result.current.zoom).toBeCloseTo(1.1)
    expect(result.current.label).toBe('110%')
    act(() => result.current.setZoom(99))
    expect(result.current.zoom).toBe(ZOOM_MAX)
    act(() => result.current.resetFit())
    expect(result.current.zoom).toBe(ZOOM_MIN)
    expect(result.current.zoomed).toBe(false)
  })
})
```

- [ ] **Step 2: Run; verify it fails**

Run (in `web/`): `npx vitest run src/vnc/useZoom.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the hook**

```typescript
// web/src/vnc/useZoom.ts
import { useCallback, useState } from 'react'

export const ZOOM_MIN = 1.0
export const ZOOM_MAX = 3.0
export const ZOOM_STEP = 0.1
export const FIT_EPSILON = 1.02

export function clampZoom(z: number): number {
  if (Number.isNaN(z)) return ZOOM_MIN
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z))
}
export function isZoomed(z: number): boolean { return z > FIT_EPSILON }
export function zoomLabel(z: number): string {
  return isZoomed(z) ? `${Math.round(z * 100)}%` : 'Fit'
}

export function useZoom() {
  const [zoom, setZoomState] = useState(ZOOM_MIN)
  const setZoom = useCallback((z: number) => setZoomState(clampZoom(z)), [])
  const zoomIn = useCallback(() => setZoomState((z) => clampZoom(z + ZOOM_STEP)), [])
  const zoomOut = useCallback(() => setZoomState((z) => clampZoom(z - ZOOM_STEP)), [])
  const resetFit = useCallback(() => setZoomState(ZOOM_MIN), [])
  return { zoom, zoomIn, zoomOut, setZoom, resetFit, zoomed: isZoomed(zoom), label: zoomLabel(zoom) }
}
```

- [ ] **Step 4: Run; verify it passes**

Run (in `web/`): `npx vitest run src/vnc/useZoom.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/vnc/useZoom.ts web/src/vnc/useZoom.test.ts
git commit -m "feat(browser): useZoom hook — bounded continuous zoom state + labels

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: BrowserTab — zoom wrapper, pan, gestures, pill, Fit-reset, top-right cluster

**Files:**
- Modify: `web/src/components/BrowserTab.tsx`
- Test: `web/src/components/__tests__/BrowserTab.test.tsx`

**Interfaces:**
- Consumes: `useZoom`, `clientRef.current` (`VncClient`).
- Produces: the visible zoom UI. The pan container wraps the existing noVNC mount.

Implementation notes for the executor:
- noVNC owns the mount and sizes its canvas via `scaleViewport`. Put the existing `canvasRef` div inside a new **pan container** (`overflow-auto`, fills the panel). Apply the user zoom as `transform: scale(zoom)` with `transform-origin: 0 0` on an inner zoom layer that also sets `width: ${zoom*100}% ; height: ${zoom*100}%` so the `overflow-auto` parent produces real scrollbars to pan. At `zoom===1` the layer is exactly the panel size (no scroll) and noVNC fits as before.
- Gestures: `Ctrl`/`Cmd`+wheel → `setZoom(zoom * (1 - e.deltaY*0.001))` (preventDefault); `+`/`=` → `zoomIn`, `-` → `zoomOut` in the existing `onKeyDown`; two-pointer pinch → track the two active pointers' distance ratio and `setZoom`. Keep the existing Escape handling.
- The `ResizeObserver` (Task 5) and the zoom transform are independent: re-fit recomputes noVNC's base scale; the user-zoom multiplier on the wrapper is preserved across re-fit (do **not** reset zoom on resize).

- [ ] **Step 1: Write failing tests**

Add to `web/src/components/__tests__/BrowserTab.test.tsx` (and add `refit` to the existing `VncClient` mock so later tasks compile):

```typescript
import { fireEvent } from '@testing-library/react'
// ...extend the existing vi.mock VncClient class with: refit() {}

  it('defaults to Fit and hides the Fit-reset control until zoomed', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.getByText('Fit')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reset to fit/i })).toBeNull()
  })

  it('zoom-in updates the pill to a percentage and reveals Fit-reset', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    fireEvent.click(screen.getByRole('button', { name: /zoom in/i }))
    expect(screen.getByText('110%')).toBeInTheDocument()
    const reset = screen.getByRole('button', { name: /reset to fit/i })
    fireEvent.click(reset)
    expect(screen.getByText('Fit')).toBeInTheDocument()
  })

  it('never renders a "1:1" label', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.queryByText('1:1')).toBeNull()
  })
```

- [ ] **Step 2: Run; verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/BrowserTab.test.tsx`
Expected: FAIL — no zoom-in button / pill.

- [ ] **Step 3: Implement the zoom UI**

In `BrowserTab.tsx`: import `useZoom`; call it. Remove the old `fit` state and the `Fit/1:1` button (`:23`, `:95-108`). Wrap the noVNC `canvasRef` div in a pan container + zoom layer:

```tsx
const { zoom, zoomIn, zoomOut, resetFit, zoomed, label } = useZoom()
// ...
<div className="relative h-full w-full overflow-hidden">
  <div className="h-full w-full overflow-auto">{/* pan container */}
    <div
      style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%`,
               transform: `scale(${zoom})`, transformOrigin: '0 0' }}
    >
      <div ref={canvasRef} className="h-full w-full" tabIndex={0}
           aria-label="Live browser view"
           onWheel={(e) => { if (e.ctrlKey || e.metaKey) { e.preventDefault();
             /* setZoom(zoom * (1 - e.deltaY * 0.001)) via useZoom.setZoom */ } }}
           onFocus={…} onBlur={…} onKeyDown={(e) => {
             if (e.key === '+' || e.key === '=') zoomIn()
             else if (e.key === '-') zoomOut()
             else if (e.key === 'Escape') { e.stopPropagation(); (e.currentTarget as HTMLElement).blur() }
           }} />
    </div>
  </div>

  {/* top-right zoom cluster — one group, ≥44px targets */}
  <div className="absolute right-2 top-2 flex items-center gap-1">
    <button aria-label="Zoom out" className="min-h-[44px] min-w-[44px] …" onClick={zoomOut}>−</button>
    <span aria-live="polite" className="rounded bg-background/80 px-2 text-xs">{label}</span>
    <button aria-label="Zoom in" className="min-h-[44px] min-w-[44px] …" onClick={zoomIn}>+</button>
    {zoomed && (
      <button aria-label="Reset to fit" className="min-h-[44px] min-w-[44px] …" onClick={resetFit}>Fit</button>
    )}
  </div>
  …
```

Wire `onWheel`'s zoom to `setZoom` from the hook (kept out of the snippet brevity, but required). Add the two-pointer pinch handler on the pan container using `onPointerDown/Move/Up` tracking a `Map` of pointerId→point and applying the distance ratio to `setZoom`. Keep `transition-transform` on the zoom layer for smoothness (reduced-motion handled in Task 5).

- [ ] **Step 4: Run; verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/BrowserTab.test.tsx`
Expected: PASS, including the existing Escape test (unchanged handler).

- [ ] **Step 5: Typecheck + lint**

Run (in `web/`): `npx tsc --noEmit && npx eslint src/components/BrowserTab.tsx src/vnc/useZoom.ts`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/BrowserTab.tsx web/src/components/__tests__/BrowserTab.test.tsx
git commit -m "feat(browser): continuous zoom UI replaces Fit/1:1 toggle

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: BrowserTab — debounced re-fit + reduced-motion transition

**Files:**
- Modify: `web/src/components/BrowserTab.tsx`
- Test: `web/src/components/__tests__/BrowserTab.test.tsx`

**Interfaces:**
- Consumes: `clientRef.current.refit()` (Task 2), the pan container ref.
- Produces: re-fit on panel resize; eased transform unless reduced-motion.

- [ ] **Step 1: Write failing test (ResizeObserver drives refit)**

Add to the BrowserTab test. Override the global `ResizeObserver` with a capturing mock so the test can fire it, and have the `VncClient` mock expose a shared `refit` spy:

```typescript
it('re-fits the VNC view when the panel resizes', () => {
  const cbs: Array<() => void> = []
  ;(globalThis as any).ResizeObserver = class {
    constructor(cb: () => void) { cbs.push(cb) }
    observe() {}; unobserve() {}; disconnect() {}
  }
  render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
  expect(cbs.length).toBeGreaterThan(0)
  act(() => cbs[0]())            // simulate a panel resize
  expect(refitSpy).toHaveBeenCalled()   // refitSpy is the mock VncClient.refit
})
```

(Define `refitSpy = vi.fn()` at module scope and reference it from the `VncClient` mock's `refit`.)

- [ ] **Step 2: Run; verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/BrowserTab.test.tsx`
Expected: FAIL — `refitSpy` not called (no ResizeObserver yet).

- [ ] **Step 3: Implement the ResizeObserver + debounce + reduced-motion**

In `BrowserTab.tsx`, add an effect on the pan-container ref:

```tsx
useEffect(() => {
  const el = panRef.current
  if (!el) return
  let t: ReturnType<typeof setTimeout> | undefined
  const ro = new ResizeObserver(() => {
    clearTimeout(t)
    t = setTimeout(() => clientRef.current?.refit(), 150)  // debounce ~150ms
  })
  ro.observe(el)
  return () => { clearTimeout(t); ro.disconnect() }
}, [])
```

Make the eased transform respect reduced-motion. Add a `motion-reduce:transition-none transition-transform` class on the zoom layer (Tailwind's `motion-reduce:` variant already keys off `prefers-reduced-motion: reduce`), so no JS matchMedia is needed.

> Note: the debounce test above fires the observer synchronously; if jsdom's fake timers aren't enabled, call `clientRef.current?.refit()` directly in addition to the debounced path is NOT allowed — instead the test uses `vi.useFakeTimers()` + `vi.runAllTimers()` around the `act(() => cbs[0]())`. Add those two lines to the test.

- [ ] **Step 4: Run; verify it passes**

Run (in `web/`): `npx vitest run src/components/__tests__/BrowserTab.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/BrowserTab.tsx web/src/components/__tests__/BrowserTab.test.tsx
git commit -m "feat(browser): debounced re-fit on panel resize, reduced-motion aware

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: BrowserTab — empty-state de-stack, no-browser gating, a11y polish

**Files:**
- Modify: `web/src/components/BrowserTab.tsx`
- Test: `web/src/components/__tests__/BrowserTab.test.tsx`
- Investigate: `server/sessions/x11manager.py`, `server/app.py`, session status payload feeding the frontend.

**Interfaces:**
- Consumes: existing `state` + `statusState` + (if found) a no-browser-open signal.
- Produces: at most one bottom caption; canvas `aria-label`; `aria-live` status; focus returned to canvas when the empty-state button unmounts.

- [ ] **Step 1: Investigate the no-browser-open signal**

Grep for any existing signal the frontend already receives that says a browser is/ isn't open:

Run: `grep -rni "browser" server/app.py server/sessions/x11manager.py web/src | grep -iv test | grep -iE "open|launched|running|active|hasBrowser"`

- If a reliable signal exists (e.g. a session-status field), thread it into `BrowserTab` props and gate the "Open a browser" hint on it.
- If none exists, implement the **dismissible fallback**: the hint shows once per mount with a close affordance; once dismissed it stays hidden. Do NOT add a blank-pixel heuristic; do NOT leave an always-on CTA over a live page.

Record which path you took in the commit message.

- [ ] **Step 2: Write failing tests**

```typescript
it('shows at most one bottom caption at a time', () => {
  render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
  // "Tap to control" and the empty-state hint must not both be visible.
  const tap = screen.queryByText(/tap to control/i)
  const empty = screen.queryByText(/hasn.t opened a browser yet/i)
  expect([tap, empty].filter(Boolean).length).toBeLessThanOrEqual(1)
})

it('labels the live canvas for screen readers', () => {
  render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
  expect(screen.getByLabelText(/live browser view/i)).toBeInTheDocument()
})
```

If you took the dismissible-fallback path, also:

```typescript
it('hides the empty-state hint after dismiss', () => {
  render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
  fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
  expect(screen.queryByText(/hasn.t opened a browser yet/i)).toBeNull()
})
```

- [ ] **Step 3: Run; verify it fails**

Run (in `web/`): `npx vitest run src/components/__tests__/BrowserTab.test.tsx`
Expected: FAIL on the new assertions.

- [ ] **Step 4: Implement**

- Gate the bottom captions so only one renders: when the empty-state hint is shown, suppress the "Tap to control · pinch to zoom" caption (and vice-versa).
- Add `aria-label="Live browser view"` to the canvas div (already added in Task 4 — keep it; assert here).
- Add `aria-live="polite"` to the status pill (Task 4) — assert it survives.
- On the empty-state branch: gate on the signal from Step 1, or render the dismissible variant with a `Dismiss` button (≥44px) that flips a local `dismissed` state. When the "Open a browser" button unmounts, move focus back to the canvas (`canvasRef.current?.focus()`).

- [ ] **Step 5: Run full web suite + typecheck + lint**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src`
Expected: all PASS / no errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/BrowserTab.tsx web/src/components/__tests__/BrowserTab.test.tsx
git commit -m "feat(browser): de-stack captions, gate empty-state hint, a11y labels

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Manual verification + PR

**Files:** none (verification + integration).

- [ ] **Step 1: Run the app and load the Browser tab**

Use the `run` skill / project launch. With a session's Browser tab open on a wide desktop panel and a narrow (phone-width, ~390px) panel, confirm:
- no letterbox bands, readable desktop layout (1280×800 source);
- smooth re-fit when the panel/window resizes; with OS "reduce motion" on, the re-fit snaps (no eased transform);
- zoom via `+`/`−` buttons, `Ctrl/⌘`+scroll, `+`/`-` keys, and pinch on a touch device; pill shows `Fit`/`NNN%`; Fit-reset appears only when zoomed and returns to Fit;
- panning works (scroll) when zoomed; an active zoom is preserved across a panel resize;
- the empty-state hint no longer overlays a live page and no two bottom captions stack;
- keyboard-only: Tab reaches the zoom controls and Fit-reset.

- [ ] **Step 2: Full server suite (regression)**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (no `CCSWITCH_TAILNET_ALLOWLIST`/xvfb env surprises — see CLAUDE.md; use `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1` and unset `CCSWITCH_ENABLE_XVFB` if the live shell set it).

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/virtual-browser-fit
gh pr create --title "Browser tab: fixed 1280x800 framebuffer + scale-to-fit + zoom" \
  --body "$(cat <<'EOF'
Fixes the letterbox/clipping on the session Browser tab by switching the virtual
display to a fixed 16:10 1280x800 framebuffer (deterministic desktop layout) and
fitting it to the panel client-side, plus a continuous-zoom affordance replacing the
confusing Fit/1:1 toggle.

Design + rationale (incl. why dynamic-resize was rejected): docs/superpowers/specs/2026-06-22-virtual-browser-fit-design.md
Plan: docs/superpowers/plans/2026-06-22-virtual-browser-fit-pr1.md

Control-state arbitration (agent vs human) is intentionally deferred to PR2, gated on
the exclusive-vs-advisory decision recorded in the spec.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

(Per CLAUDE.md: open non-draft and arm auto-merge; required checks `lint-and-test` + `frontend` must go green.)

---

## Self-review

**Spec coverage (PR1 scope):**
- Server `1280x800x24` (single source of truth) → Task 1. ✓
- Base fit + debounced re-fit + reduced-motion → Tasks 2, 5. ✓
- Residual margin = panel bg → carried as a Global Constraint + manual check (the `overflow` container inherits panel bg; no black canvas). ✓
- Continuous zoom: bounds 1.0–3.0, step 0.1, threshold 1.02, pill=current-state, keyboard reach, pinch, ctrl-scroll, Fit-reset-only-when-zoomed, zoom preserved across re-fit → Tasks 3, 4, 5. ✓
- Top-edge single cluster, ≥44px targets → Task 4 (constraint). ✓
- Empty-state de-stack + no-browser gating w/ dismissible fallback → Task 6. ✓
- A11y: canvas aria-label, aria-live status, focus management, reduced-motion → Tasks 4–6. ✓
- PR2 (control-state) explicitly out of this plan → noted in Task 7 PR body. ✓

**Placeholder scan:** Task 4's JSX uses `…` only for unchanged existing handlers (`onFocus`/`onBlur` already in the file) and for the `onWheel`→`setZoom` wiring, which is described in prose with the exact formula; acceptable since the surrounding code is shown and the formula is given. No "TBD/handle edge cases" placeholders.

**Type consistency:** `refit()` (Task 2) used in Tasks 5–6; `useZoom` returns `{ zoom, zoomIn, zoomOut, setZoom, resetFit, zoomed, label }` (Task 3) consumed verbatim in Task 4; `zoomLabel` strings (`Fit`/`NNN%`) asserted consistently. ✓
