// @vitest-environment node
// web/scripts/bundleBudget.test.mjs — rules of the lab bundle budget (#1411).
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { evaluate, formatReport, parseInitialAssets, ratchet, toKb } from './bundleBudget.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))

const HTML = `<!doctype html><html><head>
  <script type="module" crossorigin src="/assets/index-abc.js"></script>
  <link rel="modulepreload" crossorigin href="/assets/chunk-1.js">
  <link rel="modulepreload" crossorigin href="/assets/chunk-2.js">
  <link rel="prefetch" href="/assets/lazy-page.js">
  <link rel="preload" as="font" type="font/woff2" href="/assets/face.woff2">
  <link rel="preload" as="script" href="/assets/early.js">
  <link rel="stylesheet" crossorigin href="/assets/index-abc.css">
  <link rel="icon" type="image/png" href="/favicon.png">
</head><body><div id="root"></div></body></html>`

describe('parseInitialAssets', () => {
  it('finds the entry script, the preloads and the stylesheets', () => {
    const a = parseInitialAssets(HTML)
    expect(a.scripts).toEqual(['/assets/index-abc.js'])
    expect(a.modulepreloads).toEqual([
      '/assets/chunk-1.js', '/assets/chunk-2.js', '/assets/early.js',
    ])
    expect(a.stylesheets).toEqual(['/assets/index-abc.css'])
  })

  it('excludes prefetch — idle-time work is not the initial payload', () => {
    expect(parseInitialAssets(HTML).modulepreloads).not.toContain('/assets/lazy-page.js')
  })

  it('ignores non-asset links like the favicon and the manifest', () => {
    const a = parseInitialAssets(HTML)
    expect([...a.modulepreloads, ...a.stylesheets]).not.toContain('/favicon.png')
  })

  it('does not file a preloaded font under the JS payload', () => {
    // Real pre-paint weight, but not JS — counting it would make `initialJs`
    // mean two things at once.
    expect(parseInitialAssets(HTML).modulepreloads).not.toContain('/assets/face.woff2')
  })

  it('returns empty lists for an html with no assets rather than throwing', () => {
    expect(parseInitialAssets('<html></html>')).toEqual({
      scripts: [], modulepreloads: [], stylesheets: [],
    })
  })
})

describe('evaluate', () => {
  const budget = { ceilingsKb: { initialJs: 100 }, targetsKb: { initialJs: 80 } }

  it('passes under the ceiling', () => {
    const r = evaluate({ initialJs: 90 }, budget)
    expect(r.ok).toBe(true)
    expect(r.breaches).toEqual([])
    expect(r.rows[0].slackKb).toBe(10)
  })

  it('fails over the ceiling', () => {
    const r = evaluate({ initialJs: 100.1 }, budget)
    expect(r.ok).toBe(false)
    expect(r.breaches.map((b) => b.metric)).toEqual(['initialJs'])
  })

  it('exactly at the ceiling is within budget', () => {
    expect(evaluate({ initialJs: 100 }, budget).ok).toBe(true)
  })

  it('reports the target separately from the ceiling', () => {
    expect(evaluate({ initialJs: 90 }, budget).rows[0].meetsTarget).toBe(false)
    expect(evaluate({ initialJs: 70 }, budget).rows[0].meetsTarget).toBe(true)
  })

  it('a metric with no ceiling is reported but never gates', () => {
    const r = evaluate({ somethingNew: 9999 }, budget)
    expect(r.ok).toBe(true)
    expect(r.rows[0].ceiling).toBeNull()
  })
})

describe('ratchet', () => {
  it('lowers a ceiling when the build shrank, keeping a little headroom', () => {
    const { budget, lowered } = ratchet({ ceilingsKb: { initialJs: 300 } }, { initialJs: 200 })
    expect(budget.ceilingsKb.initialJs).toBe(210)
    expect(lowered).toEqual([{ metric: 'initialJs', from: 300, to: 210 }])
  })

  it('NEVER raises a ceiling, even when the build grew', () => {
    const { budget, lowered } = ratchet({ ceilingsKb: { initialJs: 100 } }, { initialJs: 400 })
    expect(budget.ceilingsKb.initialJs).toBe(100)
    expect(lowered).toEqual([])
  })

  it('does not raise a ceiling that headroom alone would push above it', () => {
    // 98 * 1.05 = 102.9 > 100, so there is nothing to bank.
    expect(ratchet({ ceilingsKb: { initialJs: 100 } }, { initialJs: 98 })
      .budget.ceilingsKb.initialJs).toBe(100)
  })

  it('leaves a metric with no ceiling alone rather than inventing one', () => {
    expect(ratchet({ ceilingsKb: {} }, { initialJs: 10 }).budget.ceilingsKb).toEqual({})
  })
})

describe('toKb', () => {
  it('rounds to one decimal', () => {
    expect(toKb(1024)).toBe(1)
    expect(toKb(1536)).toBe(1.5)
    expect(toKb(1587)).toBe(1.5)
  })
})

describe('formatReport', () => {
  it('names the overage so CI output says what to fix', () => {
    const out = formatReport(evaluate({ initialJs: 130 }, { ceilingsKb: { initialJs: 100 } }))
    expect(out).toContain('OVER by 30 kB')
  })
})

describe('the committed budget file', () => {
  const budget = JSON.parse(readFileSync(join(HERE, '..', 'bundle-budget.json'), 'utf8'))

  it('records #1411\'s proposed 250kB gzip target', () => {
    expect(budget.targetsKb.entryJs).toBe(250)
    expect(budget.targetsKb.initialJs).toBe(250)
  })

  it('has a ceiling for every gated metric', () => {
    for (const metric of ['entryJs', 'initialJs', 'initialCss']) {
      expect(typeof budget.ceilingsKb[metric]).toBe('number')
    }
  })

  // The ceilings exist to be lowered. If one ever drifts far above what the
  // build actually produces it has stopped gating anything — but a value here
  // cannot be checked without a build, so this only pins the invariant that the
  // entry ceiling stays under the target it is ratcheting toward.
  it('keeps the entry ceiling inside the target it is ratcheting toward', () => {
    expect(budget.ceilingsKb.entryJs).toBeLessThanOrEqual(budget.targetsKb.entryJs)
  })
})
