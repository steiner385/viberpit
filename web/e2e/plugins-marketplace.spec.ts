/**
 * D-16c plugin marketplace in Settings › Plugins, at desktop and phone width: adding a
 * marketplace shows the exact source before an explicit confirm; an unconfirmed
 * marketplace shows its source verbatim; browse searches on the server; install opens
 * the consent card with the verbatim copy, posts `confirm: true`, and lists each host
 * (a failed host offers Retry); the kill switch hides install and add. Long untrusted
 * marketplace text never scrolls the page sideways at 390. Route-mocked: no backend.
 */
import { test, expect, type Page } from '@playwright/test'

const CONSENT =
  'Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust.'
const RESTART = 'Running sessions pick this up when they restart.'
const LONG = 'x'.repeat(400)

const MARKETS = [
  { name: 'acme', source: { source: 'github', repo: 'acme/plugins' }, autoUpdate: false,
    normalizedSource: 'github:acme/plugins', confirmedSource: 'github:acme/plugins', curation: 'confirmed' },
  { name: `handmade-${LONG.slice(0, 40)}`, source: { source: 'github', repo: 'someone/handmade' }, autoUpdate: false,
    normalizedSource: `github:someone/handmade${LONG.slice(0, 120)}`, confirmedSource: null, curation: 'unconfirmed' },
]
const INSTALLED = [{
  id: 'lint@acme', name: 'lint', marketplace: 'acme', enabled: true, version: '1.2.0', autoUpdate: true,
  description: `Lints things ${LONG}`, category: null,
  components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 1 }, tokenCost: null,
}]
const BROWSE = [
  { pluginId: 'hooky@acme', name: 'hooky', description: `Runs hooks ${LONG}`, marketplace: 'acme', version: '2.0.0',
    reviewed: false, components: { hooks: 2, mcpServers: 1, lspServers: 0, agents: 0, skills: 0 }, installed: false },
  { pluginId: 'reviewed-one@anthropic-plugin-directory', name: 'reviewed-one', description: 'From the directory',
    marketplace: 'anthropic-plugin-directory', version: null, reviewed: true, components: null, installed: false },
]

async function mockApi(page: Page, width: number, installEnabled = true) {
  await page.setViewportSize({ width, height: 900 })
  const posts: { url: string; body: unknown }[] = []
  const browseQueries: string[] = []
  let installCalls = 0
  await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/config', (route) => route.fulfill({ json: { repoRoot: '', adoptionEnabled: false } }))
  await page.route('**/api/daemon/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/api/library/plugins', (route) =>
    route.fulfill({ json: { plugins: INSTALLED, marketplaces: MARKETS, installEnabled } }))
  await page.route('**/api/library/plugins/browse**', (route) => {
    const q = new URL(route.request().url()).searchParams.get('q') ?? ''
    browseQueries.push(q)
    const rows = installEnabled ? BROWSE.filter((b) => b.name.includes(q)) : []
    return route.fulfill({ json: { plugins: rows, total: rows.length, offset: 0, limit: 20, installEnabled } })
  })
  await page.route('**/api/library/plugins/*/install', (route) => {
    posts.push({ url: 'install', body: route.request().postDataJSON() })
    installCalls += 1
    return route.fulfill({ json: installCalls === 1
      ? { op: 'install', ok: false, hosts: [
        { host: 'dobby', outcome: 'ok', version: '2.0.0' },
        { host: 'rocky', outcome: 'failed', failureCode: 'host_unreachable', message: 'rocky did not answer' },
      ] }
      : { op: 'install', ok: true, hosts: [
        { host: 'dobby', outcome: 'noop' }, { host: 'rocky', outcome: 'ok', version: '2.0.0' },
      ] } })
  })
  await page.route('**/api/library/marketplaces', (route) => {
    posts.push({ url: 'marketplaces', body: route.request().postDataJSON() })
    return route.fulfill({ json: { op: 'marketplace_add', ok: true, marketplaces: ['tools'],
      hosts: [{ host: 'dobby', outcome: 'ok', marketplace: 'tools' }, { host: 'rocky', outcome: 'ok', marketplace: 'tools' }] } })
  })
  return { posts, browseQueries }
}

async function noSidewaysScroll(page: Page) {
  const doc = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }))
  expect(doc.sw).toBeLessThanOrEqual(doc.cw)
}

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test('add a marketplace: the exact source shows before an explicit confirm', async ({ page }) => {
      const { posts } = await mockApi(page, width)
      await page.goto('/settings/plugins')
      const input = page.getByRole('textbox', { name: 'Marketplace source' })
      await input.fill('acme/tools')
      await page.getByRole('button', { name: 'Review source' }).click()
      const review = page.getByRole('group', { name: 'Review marketplace source' })
      await expect(review.locator('code')).toHaveText('acme/tools')
      await expect(review).toContainText('Every host fetches this source')
      expect(posts).toEqual([])
      await review.getByRole('button', { name: 'Confirm and add' }).click()
      await expect.poll(() => posts).toEqual([{ url: 'marketplaces', body: { source: 'acme/tools', confirm: true } }])
      await expect(page.getByRole('group', { name: 'Host results' })).toContainText('done on all 2 hosts')
      await noSidewaysScroll(page)
    })

    test('an unconfirmed marketplace shows its source verbatim with a Confirm', async ({ page }) => {
      await mockApi(page, width)
      await page.goto('/settings/plugins')
      const row = page.getByRole('listitem').filter({ hasText: 'Unconfirmed' })
      await expect(row).toContainText('github:someone/handmade')
      await expect(row.getByRole('button', { name: 'Confirm' })).toBeVisible()
      await noSidewaysScroll(page)
    })

    test('browse searches on the server; install shows the consent card and per-host results', async ({ page }) => {
      const { posts, browseQueries } = await mockApi(page, width)
      await page.goto('/settings/plugins')
      await expect(page.getByRole('listitem').filter({ hasText: 'reviewed-one' })).toContainText('Reviewed')
      await page.getByRole('searchbox', { name: 'Search plugins' }).fill('hook')
      await expect.poll(() => browseQueries).toContain('hook')
      await expect(page.getByText('reviewed-one')).toHaveCount(0)
      await noSidewaysScroll(page)

      await page.getByRole('button', { name: 'Install hooky' }).click()
      const dialog = page.getByRole('dialog', { name: 'Install plugin' })
      await expect(dialog).toContainText(CONSENT)
      await expect(dialog).toContainText('2 hooks · 1 MCP servers')
      await expect(dialog).toContainText('github:acme/plugins')
      expect(posts).toEqual([])
      await dialog.getByRole('button', { name: 'Install' }).click()
      await expect.poll(() => posts).toEqual([{ url: 'install', body: { confirm: true } }])
      const results = dialog.getByRole('group', { name: 'Host results' })
      await expect(results).toContainText('failed on 1 of 2 hosts')
      await expect(results).toContainText('host_unreachable')
      await expect(dialog).toContainText(RESTART)
      await noSidewaysScroll(page)
      await page.screenshot({ path: `/tmp/d16c-${width}.png`, fullPage: false })
      await results.getByRole('button', { name: 'Retry' }).click()
      await expect(results).toContainText('done on all 2 hosts')
      await dialog.getByRole('button', { name: 'Close' }).click()
      await expect(dialog).toHaveCount(0)
    })

    test('the kill switch hides install and add; enable and uninstall stay', async ({ page }) => {
      await mockApi(page, width, false)
      await page.goto('/settings/plugins')
      await expect(page.getByText(/Installing, updating and adding marketplaces is turned off/)).toBeVisible()
      await expect(page.getByRole('textbox', { name: 'Marketplace source' })).toHaveCount(0)
      await expect(page.getByRole('searchbox', { name: 'Search plugins' })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Update lint' })).toHaveCount(0)
      await expect(page.getByRole('switch', { name: 'Enable lint' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Uninstall lint' })).toBeVisible()
      await noSidewaysScroll(page)
    })
  })
}
