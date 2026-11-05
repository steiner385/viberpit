/**
 * Release milestones (spec 2026-11-05 §9) — E2E for the page at
 * `/roadmap/:productSlug/milestones`: the read-only ladder, ticking a manual
 * condition, and the override dialog round trip (writes answer with the full view).
 *
 * Same harness as `roadmap.spec.ts`: `npm run build && npm run preview`
 * (Playwright's `webServer` starts it), no backend, every `/api/*` call
 * intercepted. The ladder fixture and the API mock live in
 * `e2e/fixtures/milestones.ts` (shared with `milestones-print.spec.ts`).
 *
 * Run: npx playwright test e2e/milestones.spec.ts
 */
import { test, expect } from '@playwright/test'
import { LADDER, after, ago, interceptApi } from './fixtures/milestones'

for (const [label, width] of [['desktop', 1440], ['phone', 390]] as const) {
  test.describe(`Milestones — ${label}`, () => {
    test.use({ viewport: { width, height: 900 } })

    test('renders the header strip and four cards, the current one marked', async ({ page }) => {
      await interceptApi(page)
      await page.goto('/roadmap/kindash/milestones')
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('Milestones')
      const strip = page.getByRole('region', { name: 'Where we are' })
      await expect(strip).toContainText('You are here: Phase 0 · Household gate')
      await expect(strip).toContainText('1 of 4 gate conditions met · 1 unknown')
      await expect(strip).toContainText('1 in flight')
      const cards = page.getByRole('list', { name: 'Milestones' }).locator(':scope > li')
      await expect(cards).toHaveCount(4)
      await expect(cards.first()).toHaveAttribute('aria-current', 'step')
      await expect(cards.first()).toContainText('293 → 0')
      // Nothing scrolls sideways at either width.
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
      expect(overflow).toBeLessThanOrEqual(0)
    })
  })
}

test('no ladder: the EmptyState names the definition file', async ({ page }) => {
  await interceptApi(page, { ladder: null })
  await page.goto('/roadmap/kindash/milestones')
  await expect(page.getByText('docs/product/milestones.yaml')).toBeVisible()
})

test.describe('Milestones — manage', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('ticks a manual condition with evidence; the answer replaces the view', async ({ page }) => {
    const seen: unknown[] = []
    const ticked = after((v) => {
      Object.assign(v.milestones[1].gate[0], {
        status: 'met', value: { met: true }, manualBy: 'tony', manualEvidence: 'call notes 11/04', measuredAt: ago(1),
      })
    })
    await interceptApi(page, LADDER, [
      { method: 'PUT', path: '/api/pm/projects/kindash/milestones/friendly/conditions/manual', json: ticked },
    ], seen)
    await page.goto('/roadmap/kindash/milestones')
    const friendly = page.getByRole('listitem', { name: /Phase 1/ })
    await friendly.getByRole('button', { name: 'Tick: A manual check' }).click()
    const dialog = page.getByRole('dialog', { name: 'Tick condition' })
    await dialog.getByLabel(/Reading/).selectOption('met')
    await dialog.getByLabel(/Evidence/).fill('call notes 11/04')
    await dialog.getByRole('button', { name: 'Save tick' }).click()
    await expect(dialog).toBeHidden()
    expect(seen).toEqual([{ met: true, evidence: 'call notes 11/04', url: null }])
    await expect(friendly).toContainText('by tony · call notes 11/04')
    await expect(friendly.getByRole('button', { name: 'Update: A manual check' })).toBeVisible()
  })

  test('override dialog round trip: lists what is unmet, needs a reason, advances', async ({ page }) => {
    const seen: unknown[] = []
    const advanced = after((v) => {
      v.ladder.currentKey = 'friendly'
      Object.assign(v.milestones[0], {
        state: 'done', exitedAt: ago(1),
        exitOverride: { by: 'tony', reason: 'owner accepted the nightly risk', at: ago(1), unmet: [] },
      })
      v.milestones[1].state = 'current'
    })
    await interceptApi(page, LADDER, [
      { method: 'POST', path: '/api/pm/projects/kindash/milestones/advance', json: advanced },
    ], seen)
    await page.goto('/roadmap/kindash/milestones')
    await page.getByRole('button', { name: 'Advance with override…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Advance with override' })
    await expect(dialog.getByRole('list', { name: 'Not met yet' }).getByRole('listitem')).toHaveCount(3)
    const submit = dialog.getByRole('button', { name: 'Advance with override' })
    await dialog.getByLabel(/Reason/).fill('too short')
    await expect(submit).toBeDisabled()
    await dialog.getByLabel(/Reason/).fill('owner accepted the nightly risk')
    await submit.click()
    await expect(dialog).toBeHidden()
    expect(seen).toEqual([{ expectedCurrent: 'household', overrideReason: 'owner accepted the nightly risk' }])
    await expect(page.getByText('Advanced by override: owner accepted the nightly risk')).toBeVisible()
    await expect(page.getByRole('listitem', { name: /Phase 1/ })).toHaveAttribute('aria-current', 'step')
  })
})

test.describe('Milestones — a concurrent advance', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('a 409 stale_current refetches the ladder and the page says someone else advanced it', async ({ page }) => {
    const moved = after((v) => {
      v.ladder.currentKey = 'friendly'
      Object.assign(v.milestones[0], { state: 'done', exitedAt: ago(5) })
      v.milestones[1].state = 'current'
    })
    await interceptApi(page, LADDER, [
      { method: 'POST', path: '/api/pm/projects/kindash/milestones/advance', status: 409,
        json: { detail: { error: 'stale_current', current: 'friendly' } } },
    ])
    // Registered last, so it wins for the GET: the first read is the old ladder,
    // every later one (the refetch) the ladder someone else moved.
    let reads = 0
    await page.route('**/api/pm/projects/kindash/milestones', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      reads += 1
      return route.fulfill({ json: reads === 1 ? LADDER : moved })
    })
    await page.goto('/roadmap/kindash/milestones')
    await page.getByRole('button', { name: 'Advance with override…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Advance with override' })
    await dialog.getByLabel(/Reason/).fill('owner accepted the nightly risk')
    await dialog.getByRole('button', { name: 'Advance with override' }).click()
    await expect(page.getByRole('listitem', { name: /Phase 1/ })).toHaveAttribute('aria-current', 'step')
    await expect(dialog).toBeHidden()
    await expect(page.getByRole('region', { name: 'Where we are' }).getByRole('alert'))
      .toHaveText('Someone else advanced this ladder')
  })
})

test.describe('Milestones — manage on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('a refused override says only the owner can override; the dialog fits the screen', async ({ page }) => {
    await interceptApi(page, LADDER, [
      { method: 'POST', path: '/api/pm/projects/kindash/milestones/advance', status: 403,
        json: { detail: { error: 'override_requires_owner' } } },
    ])
    await page.goto('/roadmap/kindash/milestones')
    await page.getByRole('button', { name: 'Advance with override…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Advance with override' })
    await dialog.getByLabel(/Reason/).fill('owner accepted the nightly risk')
    await dialog.getByRole('button', { name: 'Advance with override' }).click()
    await expect(dialog.getByRole('alert')).toHaveText('Only the owner can override from the ViberPit app')
    const box = await dialog.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(390)
  })
})
