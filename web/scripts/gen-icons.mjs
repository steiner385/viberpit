/**
 * Renders web/public/favicon.svg into the home-screen PNGs referenced by the
 * web app manifest (web/src/pwa/manifest.ts).
 *
 * Run MANUALLY after changing the mark, then commit the PNGs:
 *     (cd web && node scripts/gen-icons.mjs)
 *
 * Not part of `npm run build` — that would add an image-rendering step to every
 * CI build for assets that change roughly never.
 *
 * Chromium (via playwright-core, already a devDependency) is the renderer.
 * ImageMagick is NOT a substitute: without an rsvg-convert delegate it drops
 * the gradient-stroked signal lane and still exits 0.
 */
import { chromium } from 'playwright-core'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const svg = readFileSync(resolve(here, '../public/favicon.svg'), 'utf8')
const outDir = resolve(here, '../public/icons')

/** Android crops maskable icons to arbitrary shapes; only the centre 80% is
 *  guaranteed visible. Scale the mark to that safe zone and let the plate
 *  colour bleed to the edges. */
const MASKABLE_SAFE_ZONE = 0.8
const PLATE = '#0e0f14' // the favicon's rounded-plate fill

const targets = [
  { file: 'icon-192.png', size: 192, maskable: false },
  { file: 'icon-512.png', size: 512, maskable: false },
  { file: 'icon-512-maskable.png', size: 512, maskable: true },
]

const browser = await chromium.launch()
try {
  mkdirSync(outDir, { recursive: true })
  for (const { file, size, maskable } of targets) {
    const page = await browser.newPage({
      viewport: { width: size, height: size },
      deviceScaleFactor: 1,
    })
    const inner = maskable ? Math.round(size * MASKABLE_SAFE_ZONE) : size
    await page.setContent(`
      <style>
        html, body { margin: 0; padding: 0; }
        body {
          width: ${size}px; height: ${size}px;
          display: flex; align-items: center; justify-content: center;
          background: ${maskable ? PLATE : 'transparent'};
        }
        svg { display: block; width: ${inner}px; height: ${inner}px; }
      </style>
      ${svg}
    `)
    writeFileSync(resolve(outDir, file), await page.screenshot({ omitBackground: !maskable }))
    await page.close()
    console.log(`wrote ${file} (${size}x${size}${maskable ? ', maskable' : ''})`)
  }
} finally {
  await browser.close()
}
