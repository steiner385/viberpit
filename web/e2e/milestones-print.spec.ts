/**
 * Release milestones Export PDF (spec 2026-11-05 M6, §9 "Export"): the page
 * prints as ONE landscape Letter page — for the KinDash fixture with notes and
 * many open leaves, and for a 6-milestone ladder. Content past the print caps is
 * truncated, never paginated.
 *
 * The page count is the number of `/Type /Page` objects in the PDF Chromium
 * writes (not `/Pages`, the tree node). The KinDash PDF is saved to
 * `test-results/milestones-print.pdf` for the visual check.
 *
 * Run: npx playwright test e2e/milestones-print.spec.ts
 */
import { test, expect, type Page } from '@playwright/test'
import { LADDER, ago, cond, interceptApi, leaves, milestone, note } from './fixtures/milestones'

const LONG = 'and a long condition label that keeps going well past two lines on a narrow print card, so it must clamp'

/** The fixture, extended: long labels, 9 + 12 open leaves, 8 decisions and 8 asks. */
const KINDASH = (() => {
  const v = structuredClone(LADDER)
  const [household, friendly] = v.milestones
  household.gate = household.gate.map((c) => ({ ...c, label: `${c.label} ${LONG}` }))
  household.leaves = leaves(17, 3, 6)
  friendly.leaves = leaves(0, 2, 10)
  v.numbers = [
    ...v.numbers,
    cond({ key: 'leaves', label: 'phase leaves closed', kind: 'ladder_leaves', status: 'info', value: { done: 17, total: 27 }, measuredAt: ago(240) }),
    cond({ key: 'outside', label: 'outside families on the live site', status: 'unknown' }),
    cond({ key: 'defects', label: 'of recent defects found by CI (%)', status: 'info', value: { n: 40 }, measuredAt: ago(2 * 86400) }),
  ]
  v.notes = {
    decisions: Array.from({ length: 8 }, (_, i) => note({
      id: `d${i}`, kind: 'decision', ref: `D${i}`, status: i === 7 ? 'proposed' : 'locked',
      text: `Decision ${i}: lifetime free PLUS for friendly families, founder pricing held for a year`,
    })),
    asks: Array.from({ length: 8 }, (_, i) => note({
      id: `a${i}`, kind: 'ask', status: 'open', milestoneKey: i % 2 ? 'household' : null,
      text: `Ask ${i}: confirm the DPA with the payment processor before the friendly cohort`,
    })),
  }
  v.ladder.asksOpen = 8
  return v
})()

/** Six rungs, each with a full gate and open leaves. */
const SIX = (() => {
  const v = structuredClone(KINDASH)
  const extra = [4, 5].map((n) => milestone(`phase-${n}`, `Phase ${n}`, `Expansion ${n}`, 'upcoming', {
    gate: [cond({ key: 'g1', label: `Expansion ${n} gate ${LONG}` }), cond({ key: 'g2', label: 'Zero open P0' })],
    gateSummary: { met: 0, total: 2, unknown: 2, ready: false },
    leaves: leaves(1, 2, 7),
  }))
  v.milestones = [...v.milestones, ...extra] as typeof v.milestones
  return v
})()

/** Letter landscape less the 0.4in `@page` margins, in CSS px (96/in): 10.2in × 7.7in. */
const PRINTABLE = { width: 979, height: 739 }

/** Prints `ladder` and returns the PDF's page count. One page alone proves nothing
 *  if the app's inner scroll pane clipped the rest, so it first lays the page out
 *  at the printable size in print media and checks that the content — through the
 *  last panel — ends inside it. */
async function printedPages(page: Page, ladder: unknown, save?: string): Promise<number> {
  await interceptApi(page, ladder)
  await page.goto('/roadmap/kindash/milestones')
  await expect(page.getByRole('region', { name: 'Where we are' })).toContainText('You are here')
  await page.emulateMedia({ media: 'print' })
  await page.setViewportSize(PRINTABLE)
  const decisions = page.getByRole('region', { name: 'Decisions' })
  await expect(decisions).toBeVisible()
  const fit = await page.evaluate(() => {
    const root = document.querySelector('[data-milestones-print]')!
    const last = [...document.querySelectorAll('[data-milestones-print] section')].at(-1)!
    return {
      bottom: Math.max(root.getBoundingClientRect().bottom, last.getBoundingClientRect().bottom),
      docHeight: document.documentElement.scrollHeight,
    }
  })
  expect(fit.bottom).toBeLessThanOrEqual(PRINTABLE.height)
  expect(fit.docHeight).toBeLessThanOrEqual(PRINTABLE.height)
  const pdf = await page.pdf({ format: 'Letter', landscape: true, printBackground: true, ...(save ? { path: save } : {}) })
  return (pdf.toString('latin1').match(/\/Type\s*\/Page[^s]/g) ?? []).length
}

test.describe('Milestones — Export PDF', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('the KinDash fixture prints on one landscape Letter page', async ({ page }) => {
    expect(await printedPages(page, KINDASH, 'test-results/milestones-print.pdf')).toBe(1)
  })

  test('a 6-milestone ladder prints on one page', async ({ page }) => {
    expect(await printedPages(page, SIX)).toBe(1)
  })

  test('print hides the shell and every button, and shows the stamp and caps', async ({ page }) => {
    await interceptApi(page, KINDASH)
    await page.goto('/roadmap/kindash/milestones')
    await expect(page.getByRole('button', { name: 'Export PDF' })).toBeVisible()
    const rail = page.locator('[data-region="app.rail"]')
    await expect(rail).toBeVisible()
    await page.emulateMedia({ media: 'print' })
    await expect(page.getByRole('button', { name: 'Export PDF' })).toBeHidden()
    await expect(page.getByRole('button', { name: 'Refresh' })).toBeHidden()
    await expect(page.getByText(/^Snapshot .+ · KinDash · ViberPit$/)).toBeVisible()
    const card = page.getByRole('listitem', { name: /Phase 0/ })
    await expect(card.getByText('+5 more')).toBeVisible()
    await expect(page.getByRole('region', { name: 'Decisions' }).getByText('+2 more')).toBeVisible()
    await expect(page.getByRole('region', { name: 'Needs you' }).getByText('+2 more')).toBeVisible()
    await expect(rail).toBeHidden()
  })
})
