import '@testing-library/jest-dom'

// Reset persistent browser storage between tests. Components that persist to
// localStorage (e.g. the per-session composer draft) would otherwise bleed
// state across tests reusing the same key — a test typing into a composer
// leaves a saved draft that a later "empty composer" test then hydrates.
afterEach(() => {
  localStorage.clear()
})

// Polyfill ResizeObserver for jsdom (not implemented by jsdom)
if (typeof ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as typeof globalThis & { ResizeObserver: typeof ResizeObserver }).ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}

