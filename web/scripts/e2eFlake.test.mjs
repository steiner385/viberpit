// @vitest-environment node
// web/scripts/e2eFlake.test.mjs — rules of the E2E flake budget (#1412).
import { describe, it, expect } from 'vitest'

import {
  FLAKE_BUDGET, MIN_RUNS_FOR_VERDICT, flakeRate, formatReport, parseReport, tally, verdict,
} from './e2eFlake.mjs'

const test = (projectName, status) => ({ projectName, status })
const spec = (title, tests) => ({ title, tests })
const report = (...specs) => ({ suites: [{ title: 'file.spec.ts', specs: [], suites: [{ title: 'group', specs }] }] })

describe('parseReport', () => {
  it('flattens nested suites into one entry per project run, with the full title path', () => {
    const entries = parseReport(report(spec('a', [test('chromium', 'expected'), test('firefox', 'flaky')])))
    expect(entries).toEqual([
      { project: 'chromium', title: 'file.spec.ts › group › a', status: 'expected' },
      { project: 'firefox', title: 'file.spec.ts › group › a', status: 'flaky' },
    ])
  })

  it('refuses a status it does not know rather than counting it as clean', () => {
    expect(() => parseReport(report(spec('a', [test('chromium', 'timedOut')])))).toThrow(/unrecognised test status/)
  })

  it('tolerates an empty report', () => {
    expect(parseReport({})).toEqual([])
  })
})

describe('tally and flakeRate', () => {
  const entries = [
    { project: 'chromium', status: 'expected' },
    { project: 'chromium', status: 'flaky' },
    { project: 'firefox', status: 'expected' },
    { project: 'firefox', status: 'unexpected' },
    { project: 'firefox', status: 'skipped' },
  ]

  it('excludes skipped runs from the denominator', () => {
    const { byProject, total } = tally(entries)
    expect(byProject.firefox).toEqual({ runs: 2, flaky: 0, failed: 1, skipped: 1 })
    expect(total).toEqual({ runs: 4, flaky: 1, failed: 1, skipped: 1 })
  })

  it('keeps a hard failure OUT of the flake rate — it is a defect, not noise', () => {
    expect(flakeRate(tally(entries).total)).toBe(0.25)
  })

  it('has no rate for zero runs (0/0 is not 0%)', () => {
    expect(flakeRate({ runs: 0, flaky: 0 })).toBeNull()
  })
})

describe('verdict', () => {
  const bucket = (over) => ({ runs: MIN_RUNS_FOR_VERDICT, flaky: 0, failed: 0, skipped: 0, ...over })

  it('is "within" only with enough runs AND a rate under budget', () => {
    expect(verdict(bucket({}))).toBe('within')
    expect(verdict(bucket({ runs: 1000, flaky: 10 }))).toBe('within') // exactly 1%: not over
  })

  it('is "over" past the budget', () => {
    expect(verdict(bucket({ runs: 1000, flaky: 11 }))).toBe('over')
  })

  it('refuses a verdict on too few runs — 0 flakes in 20 runs proves nothing', () => {
    expect(verdict(bucket({ runs: MIN_RUNS_FOR_VERDICT - 1 }))).toBe('insufficient')
    expect(verdict(bucket({ runs: 0 }))).toBe('insufficient')
  })

  it('puts a real failure ahead of everything: fix the defect before measuring noise', () => {
    expect(verdict(bucket({ failed: 1 }))).toBe('failing')
    expect(verdict(bucket({ runs: 5, failed: 1 }))).toBe('failing')
  })

  it('honours an explicit budget and sample floor', () => {
    expect(verdict({ runs: 50, flaky: 1, failed: 0 }, { budget: 0.05, minRuns: 50 })).toBe('within')
    expect(FLAKE_BUDGET).toBe(0.01)
  })
})

describe('formatReport', () => {
  it('prints one row per project plus a total, with the verdict', () => {
    const text = formatReport(tally([
      { project: 'webkit', status: 'flaky' },
      { project: 'chromium', status: 'expected' },
    ]))
    const lines = text.split('\n')
    expect(lines[0]).toMatch(/^chromium\b.*rate\s+0\.00%.*insufficient/)
    expect(lines[1]).toMatch(/^webkit\b.*flaky\s+1.*rate\s+100\.00%/)
    expect(lines[2]).toMatch(/^ALL\b/)
  })
})
