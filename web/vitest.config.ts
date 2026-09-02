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
