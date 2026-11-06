/**
 * D-22 configurable session-branch prefix in a real browser, at desktop and phone width:
 * Settings › Sessions edits the prefix, a refused one (the server's 400
 * BRANCH_PREFIX_INVALID) is shown inline under the field with its reason, a valid one
 * is saved, and Reset sends null (back to the built-in). Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'

const COPY = 'New session branches start with this. ViberPit only ever deletes branches it recorded creating, so changing it never makes your own branches deletable.'
const REFUSED = 'feature/ is a common branch namespace; pick one of your own.'

function settings(branchPrefix: string | null) {
  return {
    defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null, defaultToolDetail: 'auto',
    autoArchiveEnabled: true, maxArchivedSessions: null, archiveHome: null, maxActiveSessions: null,
    modelApprovalAction: null, modelApprovalDenylist: null, branchPrefix,
    acceptedPermissionModes: ['interactive'],
    effectiveDefaults: {
      repoRoot: '/home/user/GitHub', archiveHome: null, modelApprovalAction: 'require_approval',
      modelApprovalDenylist: 'fable', globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
      branchPrefix: branchPrefix ?? 'viberpit/',
    },
    managed: [], about: { gitSha: null, hostName: 'edge' },
  }
}

async function mockSettings(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 })
  const puts: Record<string, unknown>[] = []
  let stored: string | null = null
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/settings', (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as Record<string, unknown>
      puts.push(body)
      if (body.branchPrefix === 'feature/') {
        return route.fulfill({ status: 400, json: { detail: { code: 'BRANCH_PREFIX_INVALID', reason: REFUSED } } })
      }
      stored = (body.branchPrefix as string | null | undefined) ?? null
    }
    return route.fulfill({ json: settings(stored) })
  })
  await page.goto('/settings/sessions')
  return puts
}

async function noSidewaysScroll(page: Page) {
  const doc = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }))
  expect(doc.sw).toBeLessThanOrEqual(doc.cw)
}

for (const width of [1440, 390]) {
  test(`${width}px: the branch prefix is edited, refused inline, saved and reset`, async ({ page }) => {
    const puts = await mockSettings(page, width)
    const field = page.getByRole('textbox', { name: 'Branch prefix' })
    await expect(field).toBeVisible()
    await expect(field).toHaveAttribute('placeholder', 'viberpit/')
    await expect(page.getByText(COPY)).toBeVisible()
    const save = page.getByRole('button', { name: 'Save' })

    // Refused: the reason sits under the field, and the field is described by it.
    await field.fill('feature/')
    await save.click()
    const reason = page.getByRole('alert').filter({ hasText: REFUSED })
    await expect(reason).toBeVisible()
    await expect(field).toHaveAccessibleDescription(new RegExp(REFUSED.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')))
    await expect(page.getByText('Not saved')).toHaveCount(0)
    await noSidewaysScroll(page)

    // Fixed and saved: the refusal goes away with the edit.
    await field.fill('agent/')
    await expect(reason).toHaveCount(0)
    await save.click()
    await expect.poll(() => puts.at(-1)?.branchPrefix).toBe('agent/')
    await expect(field).toHaveValue('agent/')

    // Reset: back to the built-in.
    await page.getByRole('button', { name: 'Reset branch prefix to default' }).click()
    await expect(field).toHaveValue('')
    await save.click()
    await expect.poll(() => puts.length).toBe(3)
    expect(puts.at(-1)?.branchPrefix).toBeNull()
    await noSidewaysScroll(page)
  })
}
