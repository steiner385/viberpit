import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { expect, test as base } from '@playwright/test'
import type { SessionEvent } from '../src/session/events'
import { liveFrame, replayFrame, wire } from './fixtures/wsFrames'

// Override locally with a real screenshot without copying another repo's fixtures.
const fixture = process.env.VIBERPIT_IMAGE_FIXTURE || 'public/brand/viberpit-mascot.png'
const sid = 'image-session'
const url = '/api/attachments/11111111-1111-4111-8111-111111111111'
// Downloads must use real HTTP: Chromium cancels route.fulfill downloads.
// Serve fixture bytes with the attachment endpoint's headers, proxying static
// assets to the built preview. Other API/WS traffic remains protocol fixtures.
const test = base.extend<{ imageHost: { origin: string; png: Buffer } }>({
  imageHost: async ({ baseURL }, runFixture) => {
    const png = await readFile(fixture)
    const server = createServer(async (req, res) => {
      if (req.url === url) {
        res.writeHead(200, { 'Content-Type': 'image/png', 'Content-Length': png.length,
          'Content-Disposition': 'inline', 'Cache-Control': 'private' })
        res.end(png)
        return
      }
      try {
        const response = await fetch(new URL(req.url || '/', baseURL))
        res.writeHead(response.status, { 'Content-Type': response.headers.get('content-type') || 'application/octet-stream' })
        res.end(Buffer.from(await response.arrayBuffer()))
      } catch {
        res.writeHead(502)
        res.end()
      }
    })
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('Fixture server has no port')
    try {
      await runFixture({ origin: `http://127.0.0.1:${address.port}`, png })
    } finally {
      server.closeAllConnections()
      await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
    }
  },
})
const events: SessionEvent[] = [
  { type: 'turnStart', turnId: 'images' },
  { type: 'toolCall', turnId: 'images', toolId: 'shot', name: 'Read', toolKind: 'read', input: { file_path: '/workspace/screenshot.png' } },
  { type: 'toolResult', toolId: 'shot', ok: true, payload: { content: [{ type: 'image', source: { type: 'url', url } }] } },
  { type: 'textDelta', turnId: 'images', text: 'Screenshot ready.\n\n![Workspace screenshot](/workspace/screenshot.png)' },
  { type: 'turnEnd', turnId: 'images' },
]

for (const width of [390, 1440]) {
  test(`tool images remain visible live and after reload at ${width}px`, async ({ page, imageHost }) => {
    await page.setViewportSize({ width, height: 1000 })
    const { png } = imageHost
    await page.context().route('**/api/**', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/sessions', (route) => route.fulfill({ json: [{
      id: sid, name: 'Image repair', accountName: 'alice', engine: 'codex', status: 'working',
      createdAt: '2026-10-07T10:00:00Z', updatedAt: '2026-10-07T10:00:00Z',
    }] }))
    await page.context().route(`**${url}`, (route) => route.continue())
    let attachCount = 0
    await page.routeWebSocket('**/ws', (ws) => ws.onMessage((msg) => {
      if (JSON.parse(msg.toString()).type !== 'attach') return
      attachCount++
      if (attachCount === 1) {
        ws.send(wire(replayFrame(sid, [], { totalTurns: 0 })))
        ws.send(wire(liveFrame(sid, 0, events)))
      } else ws.send(wire(replayFrame(sid, events, { totalTurns: 1 })))
    }))
    const localRequests: string[] = []
    page.on('request', (req) => { if (req.url().includes('/workspace/')) localRequests.push(req.url()) })
    await page.goto(imageHost.origin)
    await page.getByRole('button', { name: /open session image repair/i }).click()
    const image = page.locator(`img[src="${url}"]`)
    const verifyImage = async () => {
      await expect(image).toBeVisible()
      await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0)
      expect(await image.evaluate((el) => el.getBoundingClientRect().width)).toBeLessThanOrEqual(width)
    }
    await verifyImage()
    await expect(page.getByText(/share_artifact/)).toBeVisible()
    expect(localRequests).toEqual([])
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('link', { name: /download/i }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBeTruthy()
    const downloadedPath = await download.path()
    expect(downloadedPath).not.toBeNull()
    expect(await readFile(downloadedPath!)).toEqual(png)
    const popupPromise = page.waitForEvent('popup')
    await page.getByRole('link', { name: /open full size/i }).click()
    const popup = await popupPromise
    await expect(popup).toHaveURL(new RegExp(url))
    await expect.poll(() => popup.locator('img').evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0)
    await popup.close()
    await page.reload()
    await verifyImage()
    await expect(page.getByText(/share_artifact/)).toBeVisible()
    expect(attachCount).toBeGreaterThan(1)
    await page.screenshot({ path: `test-results/session-images-${width}.png`, fullPage: true })
  })
}

for (const width of [390, 1440]) {
  test(`missing and malformed images have visible fallbacks at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/sessions/archived*', (route) => route.fulfill({ json: [{
      id: sid, name: 'Unavailable images', account: 'alice', cwd: '/tmp', engine: 'codex',
      createdAt: '2026-10-07T10:00:00Z', archivedAt: '2026-10-07T11:00:00Z',
    }] }))
    await page.route(`**${url}`, (route) => route.fulfill({ status: 404 }))
    await page.route('**/api/sessions/*/transcript*', (route) => route.fulfill({ json: {
      events: [events[0], events[1], { ...events[2], payload: { content: [
        { type: 'image', source: { type: 'url', url } },
        { type: 'image' },
        { type: 'image', source: { type: 'url', url: 'javascript:alert(1)' } },
      ] } }, events[4]], earliestTurn: 0, totalTurns: 1,
    } }))
    await page.routeWebSocket('**/ws', (ws) => ws.onMessage(() => {}))
    await page.goto(`/sessions/archived/${sid}`)
    const fallback = page.getByText(/Image unavailable/)
    await expect(fallback).toHaveCount(3)
    for (const el of await fallback.all()) {
      await expect(el).toBeVisible()
      expect(await el.evaluate((node) => node.getBoundingClientRect().width)).toBeLessThanOrEqual(width)
    }
    await expect(page.locator(`img[src="${url}"]`)).toHaveCount(0)
    await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0)
  })
}
