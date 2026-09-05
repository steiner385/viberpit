import '@testing-library/jest-dom'

// Everything below is DOM-only. It is guarded so a file that opts out of jsdom with
// `// @vitest-environment node` still gets a working setup instead of a
// `ReferenceError: HTMLElement is not defined` at import time.
//
// Why bother: constructing jsdom costs ~1.1s per test FILE and is charged whether the file
// renders a component or tests a pure reducer. Measured over the full suite, jsdom
// construction (`environment: 409s`) cost more than running all 3003 tests (`tests: 181s`).
const HAS_DOM = typeof HTMLElement !== 'undefined'

// Reset persistent browser storage between tests. Components that persist to
// localStorage (e.g. the per-session composer draft) would otherwise bleed
// state across tests reusing the same key — a test typing into a composer
// leaves a saved draft that a later "empty composer" test then hydrates.
afterEach(() => {
  if (HAS_DOM) localStorage.clear()
})

// Polyfill ResizeObserver for jsdom (not implemented by jsdom)
if (HAS_DOM && typeof ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as typeof globalThis & { ResizeObserver: typeof ResizeObserver }).ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}

// jsdom does no layout, so every element reports offsetWidth/offsetHeight === 0.
// @tanstack/react-virtual (TranscriptView) sizes its scroll viewport and rows
// from offsetHeight, so with 0 it would mount an empty window and nothing would
// render — breaking every test that asserts on transcript content. Give elements
// a fixed non-zero box so the virtualizer mounts a real window; true pixel-level
// windowing/stick-to-bottom is covered by e2e/transcript-virtualization.spec.ts.
// jsdom itself defines these as getters returning 0, so override unconditionally.
if (HAS_DOM) {
  for (const dim of ['offsetHeight', 'offsetWidth'] as const) {
    Object.defineProperty(HTMLElement.prototype, dim, {
      configurable: true,
      get() { return dim === 'offsetHeight' ? 600 : 800 },
    })
  }
}
