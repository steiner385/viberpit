/**
 * The app's canvas colours — light and dark. Single source of truth: also
 * referenced by the two <meta name="theme-color"> entries in index.html, the
 * --t-canvas values in index.css, and the web app manifest's theme_color /
 * background_color (web/src/pwa/manifest.ts).
 *
 * Kept in its own DOM-free module (no React, no `@/*` alias) because
 * web/src/pwa/manifest.ts is imported by vite.config.ts under the node-scope
 * tsconfig project, which has no DOM lib and no alias mapping.
 */
export const CANVAS = { light: '#ffffff', dark: '#16171d' } as const
