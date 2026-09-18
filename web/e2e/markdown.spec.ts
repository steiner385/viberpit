import { expect, test } from '@playwright/test'

for (const width of [390, 1440]) {
  test(`renders real Mermaid and rich Markdown at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const text = [
      '# Rich reply',
      '```mermaid\ngraph TD; A[Start] --> B[Finish]\n```',
      '```mermaid\nsequenceDiagram\nAlice->>Bob: Hello\n```',
      '```mermaid\nthis is not a diagram\n```',
      'Inline $x^2$ and a note[^1].',
      '| Feature | Works |\n| --- | ---: |\n| tables | yes |',
      '- [x] task',
      '<details><summary>More</summary><kbd>Enter</kbd></details>',
      '[^1]: Footnote content',
    ].join('\n\n')
    await page.route('**/api/**', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/sessions*', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/sessions/archived*', (route) => route.fulfill({ json: [{
      id: 'md-1', name: 'Markdown', account: 'alice', cwd: '/tmp', repo: 'o/r', engine: 'claude',
      engineSessionId: 'c-1', permissionMode: 'default',
      createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z',
    }] }))
    await page.route('**/api/sessions/*/transcript*', (route) => route.fulfill({ json: {
      events: [
        { type: 'turnStart', turnId: 'a0' },
        { type: 'textDelta', turnId: 'a0', text },
        { type: 'turnEnd', turnId: 'a0' },
      ], earliestTurn: 0, totalTurns: 1,
    } }))
    await page.routeWebSocket('**/ws', (ws) => ws.onMessage(() => {}))
    await page.goto('/sessions/archived/md-1')
    await expect(page.getByRole('heading', { name: 'Rich reply' })).toBeVisible()
    const diagrams = page.getByRole('img', { name: 'Mermaid diagram', exact: true })
    await expect(diagrams).toHaveCount(2)
    for (const diagram of await diagrams.all()) {
      await expect.poll(() => diagram.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
      expect(await diagram.evaluate((image) => image.getBoundingClientRect().width)).toBeLessThanOrEqual(width)
    }
    await expect(page.getByText('Unable to render diagram. Source is shown below.')).toBeVisible()
    await expect(page.locator('.katex')).toHaveCount(1)
    await expect(page.getByRole('table')).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Works' })).toHaveCSS('text-align', 'right')
    await expect(page.getByTestId('transcript-scroll').getByRole('checkbox')).toBeChecked()
    const footnote = page.locator('a[data-footnote-ref]')
    const href = await footnote.getAttribute('href')
    await footnote.click()
    await expect(page).toHaveURL(new RegExp(`${href}$`))
    await expect(page.locator(`[id="${href!.slice(1)}"]`)).toBeVisible()
  })
}
