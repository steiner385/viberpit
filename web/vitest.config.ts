import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  // The Tailwind plugin is here so `import css from '@/index.css?inline'` in
  // tokenBridge.test.ts yields the *compiled* stylesheet — the same bridge check
  // the 2026-09-02 audit had to do by hand against web/dist.
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    // jsdom is the DEFAULT, not the only option. Constructing it costs ~1.1s per test
    // FILE, charged whether the file renders a component or tests a pure reducer — over
    // the full suite that was `environment: 409s` against `tests: 181s`, i.e. more time
    // spent building DOMs than running every assertion in the project.
    //
    // Files that touch no DOM opt out with `// @vitest-environment node` on line 1. That
    // set was derived empirically (run the suite under `--environment=node`; whatever
    // passes needs no DOM), NOT by grepping for `document`/`window` — a regex misses
    // things like `CloseEvent` in src/ws/client.test.ts, which reads as pure but is not.
    //
    // The default stays jsdom so a NEW test file is correct-but-slower rather than
    // mysteriously broken; opting in is a deliberate, per-file act.
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    exclude: ['node_modules', 'dist', 'e2e/**'],
    // Only index.css is processed (everything else stays an empty string, as before).
    // Anchored on `$` alone this never matches: Vitest's CSSEnablerPlugin tests
    // the *raw* module id, which for `import css from '@/index.css?inline'`
    // still carries the `?inline` query (e.g. `.../src/index.css?inline`), so
    // `$` never lines up and the css-disable plugin silently short-circuits the
    // import to `export default ""` regardless of index.css's content.
    css: { include: [/src\/index\.css(\?|$)/] },
  },
})
