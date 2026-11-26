/**
 * G14 (#1412) — prove the gates can FAIL.
 *
 * A check that has never been seen to go red is a decoration. Each test here
 * mounts a deliberate defect into the real, seeded app and asserts that the
 * matching helper from `support/` catches it — on every engine. The
 * expectations are inverted (`rejects`, `toContain`), so a run stays green while
 * it demonstrates the gate biting; the failing trace/screenshot evidence for the
 * issue is produced by `PW_SEED_DEFECT=1` runs of the real matrix (see
 * docs/audits/ui-conformance-scorecard.md).
 *
 * The a11y defects are injected into a scratch container and scanned with
 * `include`, so the app's own (classified) findings cannot mask or fake a hit.
 *
 * Two of the audit's analyzer findings are validated here rather than assumed:
 * that axe really evaluates modern colour functions (`oklch`, `color-mix`), and
 * what it does with a native `<input type="checkbox" role="switch">`. Both are
 * the construct Tailwind v4 and `PluginsSection` actually ship.
 */
import { test, expect, type Page } from '@playwright/test'
import { installSeededApi } from '../fixtures/seed'
import { scanA11y } from '../support/a11y'
import {
  expectInViewport, expectMinTarget, expectNoHorizontalOverflow, expectNoOverlap,
  silentlyClippedText, MIN_TARGET_AA_PX,
} from '../support/geometry'

test.use({ viewport: { width: 1024, height: 768 } })

/**
 * `overlay` pins the scratch node on top and on screen — what an axe scan needs (it skips
 * what it cannot see). Geometry defects stay in flow instead: a fixed box never counts
 * toward the document's scroll extent, so an overflow seeded that way could not be caught.
 */
async function mountDefect(page: Page, html: string, overlay = false): Promise<void> {
  await installSeededApi(page)
  await page.goto('/sessions')
  await expect(page.locator('main')).toHaveCount(1)
  await page.evaluate(([markup, pinned]) => {
    document.getElementById('seeded')?.remove() // a test may mount twice
    const scratch = document.createElement('div')
    scratch.id = 'seeded'
    if (pinned) scratch.setAttribute('style', 'position:fixed;top:0;left:0;z-index:2147483647;background:#fff;padding:8px')
    scratch.innerHTML = markup
    document.body.appendChild(scratch)
  }, [html, overlay] as const)
}

const ruleIds = async (page: Page): Promise<string[]> =>
  (await scanA11y(page, '#seeded')).all.violations.map((f) => f.id)

test.describe('geometry gates bite', () => {
  test('horizontal overflow is caught', async ({ page }) => {
    await mountDefect(page, '<div style="width:4000px;height:10px">wide</div>')
    await expect(expectNoHorizontalOverflow(page)).rejects.toThrow(/scrolls sideways/)
  })

  test('a 10px target is caught against the 24px floor', async ({ page }) => {
    await mountDefect(page, '<button id="tiny" style="width:10px;height:10px">x</button>')
    await expect(expectMinTarget(page.locator('#tiny'), MIN_TARGET_AA_PX)).rejects.toThrow(/needs 24/)
  })

  test('a control pushed off-screen is caught', async ({ page }) => {
    await mountDefect(page, '<button id="gone" style="position:fixed;left:2000px;top:10px">x</button>')
    await expect(expectInViewport(page, page.locator('#gone'))).rejects.toThrow(/past the right edge/)
  })

  test('overlapping regions are caught', async ({ page }) => {
    await mountDefect(page,
      '<div id="a" style="position:fixed;left:0;top:0;width:200px;height:200px"></div>'
      + '<div id="b" style="position:fixed;left:100px;top:100px;width:200px;height:200px"></div>')
    await expect(expectNoOverlap(page.locator('#a'), page.locator('#b'))).rejects.toThrow(/overlap/)
  })

  test('silently clipped text is caught, an ellipsis is not', async ({ page }) => {
    await mountDefect(page,
      '<div id="clip" style="width:40px;overflow:hidden;white-space:nowrap">a sentence far too long for the box</div>'
      + '<div id="ok" style="width:40px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">a sentence far too long</div>')
    const offenders = await silentlyClippedText(page, '#seeded')
    expect(offenders).toHaveLength(1)
    expect(offenders[0]).toContain('a sentence far too long for the box')
  })
})

test.describe('accessibility gate bites', () => {
  test('an unnamed button is caught', async ({ page }) => {
    await mountDefect(page, '<button></button>', true)
    expect(await ruleIds(page)).toContain('button-name')
  })

  test('an image with no alternative text is caught', async ({ page }) => {
    await mountDefect(page, '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">', true)
    expect(await ruleIds(page)).toContain('image-alt')
  })

  test('an unlabelled form control is caught', async ({ page }) => {
    await mountDefect(page, '<input type="text">', true)
    expect(await ruleIds(page)).toContain('label')
  })

  // The audit's "invalid modern-color" finding: does the analyzer even evaluate the
  // colour functions the stylesheet is built from? If a future axe stopped parsing
  // them, contrast would silently move from `violations` to `incomplete` — and this
  // is the test that notices.
  for (const [name, fg, bg] of [
    ['oklch()', 'oklch(0.85 0 0)', 'oklch(0.9 0 0)'],
    ['color-mix()', 'color-mix(in srgb, #ffffff 80%, #000000)', '#ffffff'],
    ['hex (control)', '#dddddd', '#ffffff'],
  ] as const) {
    test(`low contrast written as ${name} is caught as a contrast failure`, async ({ page }) => {
      await mountDefect(page, `<p style="color:${fg};background:${bg};font-size:14px">faint words</p>`, true)
      const { all } = await scanA11y(page, '#seeded')
      const asViolation = all.violations.some((f) => f.id === 'color-contrast')
      const asIncomplete = all.incomplete.some((f) => f.id === 'color-contrast')
      expect({ asViolation, asIncomplete }, `${name}: axe must DECIDE, not shrug`).toEqual({ asViolation: true, asIncomplete: false })
    })
  }

  // The audit's "native-checkbox-switch" finding. `PluginsSection` ships exactly this
  // markup; axe 4.x accepts the pairing, and still bites when the name is missing.
  test('a named native checkbox switch passes; an unnamed one is caught', async ({ page }) => {
    await mountDefect(page, '<label>Enable <input type="checkbox" role="switch" aria-label="Enable lint"></label>', true)
    expect(await ruleIds(page)).not.toContain('aria-allowed-role')
    expect(await ruleIds(page)).not.toContain('label')

    await mountDefect(page, '<input type="checkbox" role="switch">', true)
    expect(await ruleIds(page)).toContain('label')
  })
})
