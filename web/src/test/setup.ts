import { vi } from 'vitest'
import '@testing-library/jest-dom'
import { configure } from '@testing-library/dom'

/**
 * `findBy*`/`waitFor` get 5s instead of testing-library's default 1s.
 *
 * The app code-splits its heavy surfaces (#1411): the Roadmap / Delivery /
 * Pipeline / Settings routes, the transcript markdown renderer, the VNC viewer.
 * In a browser that is a chunk fetch measured in tens of milliseconds. In vitest
 * there is no bundle — the "fetch" is a first-time module TRANSFORM of, say,
 * react-markdown plus highlight.js plus KaTeX, and how long that takes depends
 * on how many workers are competing for the machine. At 1s the suite was green
 * alone and red under `--pool=threads` load, which is a flake, not a signal:
 * the assertion was about what renders, and it was failing on how busy the box
 * was. 5s is still far below vitest's own per-test timeout, so a genuinely
 * never-arriving element still fails the test rather than hanging the run.
 */
configure({ asyncUtilTimeout: 5_000 })

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

// Standalone session components do not mount the application QueryClient. Tests of
// the actual personal-query boundary explicitly unmock this module.
vi.mock('@/api/quickReplies', () => ({
  useQuickReplies: () => ({ replies: [], statusText: 'Learning from your replies across sessions.' }),
}))
