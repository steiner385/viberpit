/**
 * G14 (#1412) — the representative cross-browser matrix.
 *
 * Every top-level route × four layout modes, run on Chromium, Firefox and WebKit
 * (`playwright.config.ts` points the two extra engines at this directory). Each
 * cell asserts what a real engine laid out, not what the DOM claims:
 *
 *   - the page has its one `<main>` and its sections nav, and does not scroll sideways;
 *   - on a touch mode the section controls meet the app's 44px floor;
 *   - axe finds nothing unclassified (support/a11y.ts — no blanket rule disables);
 *   - a screenshot is attached, so the CI artifact IS the visual evidence.
 *
 * The backend is the seeded, frozen-clock one in `fixtures/seed.ts`: nothing real
 * can reach a screenshot, a trace or an axe report, and the same input reaches
 * every engine.
 *
 * `isMobile` is deliberately NOT used: Firefox does not implement it, and the
 * layout keys off viewport width and pointer capability, both of which
 * `viewport` + `hasTouch` set on every engine.
 */
import { test, expect } from '@playwright/test'
import { installSeededApi } from '../fixtures/seed'
import { expectNoA11yViolations } from '../support/a11y'
import { publishEvidence } from '../support/evidence'
import {
  expectInViewport, expectMinTarget, expectNoHorizontalOverflow, expectNoOverlap,
  MIN_TARGET_AA_PX, MIN_TARGET_TOUCH_PX,
} from '../support/geometry'

interface Mode {
  name: string
  viewport: { width: number; height: number }
  /** A coarse pointer: the app's touch floor applies, hover-only chrome is absent. */
  touch: boolean
}

const MODES: readonly Mode[] = [
  { name: 'desktop', viewport: { width: 1440, height: 900 }, touch: false },
  { name: 'tablet-landscape', viewport: { width: 1024, height: 768 }, touch: true },
  { name: 'phone-portrait', viewport: { width: 390, height: 844 }, touch: true },
  { name: 'phone-landscape', viewport: { width: 844, height: 390 }, touch: true },
]

const ROUTES = [
  { path: '/sessions', name: 'Sessions' },
  { path: '/pipeline', name: 'Pipeline' },
  { path: '/roadmap', name: 'Roadmap' },
  { path: '/delivery', name: 'Delivery' },
  { path: '/settings/general', name: 'Settings' },
] as const

for (const mode of MODES) {
  test.describe(`${mode.name} (${mode.viewport.width}×${mode.viewport.height})`, () => {
    test.use({ viewport: mode.viewport, hasTouch: mode.touch })

    for (const route of ROUTES) {
      test(`${route.name}: structure, geometry and a11y`, async ({ page }, testInfo) => {
        await installSeededApi(page)
        await page.goto(route.path)

        // The shell is up: one content landmark and a way to reach the other sections.
        await expect(page.locator('main')).toHaveCount(1)
        // Settled, not just mounted: the pages are lazy chunks plus queries, and a scan of
        // a skeleton passes for the wrong reason (and fails differently on a slower engine).
        await expect(page.locator('[aria-busy="true"]')).toHaveCount(0)
        if (route.path === '/sessions') await expect(page.getByText('Refactor the importer')).toBeVisible()
        const sections = page.getByRole('navigation', { name: 'Sections', exact: true })
        await expect(sections).toBeVisible()

        await expectNoHorizontalOverflow(page)
        await expectInViewport(page, sections, 'sections nav')

        // Touch: every section control is a 44px target. Desktop keeps the dense rail
        // but still clears the WCAG 2.2 AA floor.
        const floor = mode.touch ? MIN_TARGET_TOUCH_PX : MIN_TARGET_AA_PX
        for (const link of await sections.getByRole('link').all()) {
          await expectMinTarget(link, floor, `section link "${(await link.innerText()).trim() || 'icon'}"`)
        }

        await expectNoA11yViolations(page, testInfo)
        await publishEvidence(testInfo, `${route.name}-${mode.name}.png`,
          await page.screenshot({ animations: 'disabled' }), 'image/png')
      })
    }
  })
}

test.describe('Sessions: the open-session hand-off', () => {
  test('phone portrait: list → session → back, one band at a time', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await installSeededApi(page)
    await page.goto('/sessions')

    const open = page.getByRole('button', { name: /open session refactor the importer/i })
    await open.click()
    await expect(page.getByRole('button', { name: /back to session list/i })).toBeVisible()
    await expect(open).toHaveCount(0) // the list gives up the band
    await expectNoHorizontalOverflow(page)

    await page.getByRole('button', { name: /back to session list/i }).click()
    await expect(open).toBeVisible()
  })

  test('desktop: the list and the open session share the screen without overlapping', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installSeededApi(page)
    await page.goto('/sessions')
    await page.getByRole('button', { name: /open session refactor the importer/i }).click()

    const list = page.locator('[data-region="app.list"]')
    const main = page.locator('[data-region="app.main"]')
    await expect(list).toBeVisible()
    await expect(main).toBeVisible()
    await expectNoOverlap(list, main, 'session list vs session pane')
    await expectNoHorizontalOverflow(page)
  })
})

test.describe('Confirmed gaps, seeded as regressions', () => {
  // The per-card "Quick replies" chevron was 20px wide on a hover-capable pointer —
  // under WCAG 2.2 SC 2.5.8's 24px. Found by this gate's first axe run (#1412).
  test('desktop: the Quick-replies chevron meets the 24px target floor', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installSeededApi(page)
    await page.goto('/sessions')
    const chevron = page.getByRole('button', { name: /^Quick replies for session/ }).first()
    await expect(chevron).toBeVisible()
    await expectMinTarget(chevron, MIN_TARGET_AA_PX, 'Quick replies chevron')
  })

  // De-emphasised text was dimmed with `opacity-50/60`, which lands at 3.2–4.3:1 on the
  // light surface (needs 4.5:1). Both Settings sections now use the `ink-muted` token.
  // Each section is scanned only once its own intro copy (the dimmed text under test)
  // has rendered: the page is a lazy chunk plus a query, and a scan of the "Loading…"
  // state would pass for the wrong reason on a fast engine.
  const SECTION_COPY = {
    plugins: /Plugins are shared across all accounts/,
    mcp: /MCP servers are shared across accounts/,
  } as const
  for (const section of ['plugins', 'mcp'] as const) {
    test(`settings/${section}: de-emphasised text keeps 4.5:1 contrast`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await installSeededApi(page)
      await page.goto(`/settings/${section}`)
      await expect(page.getByText(SECTION_COPY[section])).toBeVisible()
      await expectNoA11yViolations(page, testInfo)
    })
  }
})
