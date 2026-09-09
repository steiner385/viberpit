/**
 * Web app manifest — the document that makes ViberPit installable to a phone
 * home screen, which is what unlocks Web Push on iOS.
 *
 * A pure function rather than a static public/manifest.webmanifest for two
 * reasons: the brand may be spelled in exactly one place (see brand.ts), and a
 * pure function is unit-testable without building web/dist — which we avoid,
 * since a built dist changes server test behaviour.
 *
 * Emitted at build time by the manifest plugin in vite.config.ts.
 */
// Relative import, not the `@/` alias used elsewhere in src/: this module is
// also imported directly by vite.config.ts (see the manifest plugin there),
// and Vite's config loader bundles that file with esbuild before the
// `resolve.alias` from the config it is loading exists to apply — the alias
// resolves fine inside the app bundle and under vitest, but not here.
import { BRAND_NAME } from '../brand'
import { CANVAS } from '../shell/canvas'

export interface ManifestIcon {
  src: string
  sizes: string
  type: 'image/png'
  purpose?: 'any' | 'maskable'
}

export interface WebAppManifest {
  id: string
  name: string
  short_name: string
  start_url: string
  scope: string
  display: 'standalone'
  theme_color: string
  background_color: string
  icons: ManifestIcon[]
}

export function buildManifest(): WebAppManifest {
  return {
    id: '/',
    name: BRAND_NAME,
    short_name: BRAND_NAME,
    start_url: '/',
    scope: '/',
    display: 'standalone',
    // CANVAS.dark, not light: matches the dark `theme-color` meta in
    // index.html, so the installed app's splash and chrome do not flash a
    // different colour on launch.
    theme_color: CANVAS.dark,
    background_color: CANVAS.dark,
    icons: [
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512-maskable.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  }
}
