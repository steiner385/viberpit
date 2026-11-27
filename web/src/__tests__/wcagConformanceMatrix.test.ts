// @vitest-environment node
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

/**
 * Keeps `docs/audits/wcag-2.2-aa-conformance.md` honest (#1416, G18).
 *
 * A conformance record rots in three ways, and each is a test here: a criterion
 * silently goes missing, a "Gap" loses the issue that tracks it, or the evidence
 * column points at a file that was since moved or deleted — at which point the row
 * claims a control that no longer exists.
 *
 * It does NOT judge whether a status is true. That is what the tests the rows name
 * are for, and what §3 of the record (a person, a screen reader) is for.
 */
// Vitest runs with cwd at the web/ package root.
const repoRoot = path.resolve(process.cwd(), '..')
const record = readFileSync(path.join(repoRoot, 'docs/audits/wcag-2.2-aa-conformance.md'), 'utf-8')

/** Every WCAG 2.2 Level A and AA success criterion (4.1.1 Parsing was removed in 2.2). */
const WCAG_22_A_AA: Record<string, 'A' | 'AA'> = {
  '1.1.1': 'A',
  '1.2.1': 'A', '1.2.2': 'A', '1.2.3': 'A', '1.2.4': 'AA', '1.2.5': 'AA',
  '1.3.1': 'A', '1.3.2': 'A', '1.3.3': 'A', '1.3.4': 'AA', '1.3.5': 'AA',
  '1.4.1': 'A', '1.4.2': 'A', '1.4.3': 'AA', '1.4.4': 'AA', '1.4.5': 'AA',
  '1.4.10': 'AA', '1.4.11': 'AA', '1.4.12': 'AA', '1.4.13': 'AA',
  '2.1.1': 'A', '2.1.2': 'A', '2.1.4': 'A',
  '2.2.1': 'A', '2.2.2': 'A',
  '2.3.1': 'A',
  '2.4.1': 'A', '2.4.2': 'A', '2.4.3': 'A', '2.4.4': 'A', '2.4.5': 'AA', '2.4.6': 'AA', '2.4.7': 'AA', '2.4.11': 'AA',
  '2.5.1': 'A', '2.5.2': 'A', '2.5.3': 'A', '2.5.4': 'A', '2.5.7': 'AA', '2.5.8': 'AA',
  '3.1.1': 'A', '3.1.2': 'AA',
  '3.2.1': 'A', '3.2.2': 'A', '3.2.3': 'AA', '3.2.4': 'AA', '3.2.6': 'A',
  '3.3.1': 'A', '3.3.2': 'A', '3.3.3': 'AA', '3.3.4': 'AA', '3.3.7': 'A', '3.3.8': 'AA',
  '4.1.2': 'A', '4.1.3': 'AA',
}

const STATUSES = ['Automated', 'Partly automated', 'Inspected', 'Gap', 'Not verified', 'N/A'] as const

interface Row { sc: string; level: string; status: string; evidence: string; at: string }

const rows: Row[] = record.split('\n')
  .map((line) => line.split('|').map((cell) => cell.trim()))
  .filter((cells) => /^\d\.\d\.\d+$/.test(cells[1] ?? ''))
  .map((cells) => ({ sc: cells[1], level: cells[3], status: cells[4], evidence: cells[5], at: cells[6] }))

describe('WCAG 2.2 AA conformance record', () => {
  it('has exactly one row per Level A and AA criterion, at the right level', () => {
    expect(rows.map((r) => r.sc)).toEqual(Object.keys(WCAG_22_A_AA))
    expect(rows.filter((r) => r.level !== WCAG_22_A_AA[r.sc]).map((r) => r.sc)).toEqual([])
  })

  it('uses only the statuses its own legend defines', () => {
    expect(rows.filter((r) => !(STATUSES as readonly string[]).includes(r.status)).map((r) => `${r.sc}: ${r.status}`)).toEqual([])
    for (const status of STATUSES) expect(record).toContain(`| **${status}** |`)
  })

  it('tracks every gap with an issue', () => {
    const gaps = rows.filter((r) => r.status === 'Gap')
    expect(gaps.filter((r) => !/^#\d+\./.test(r.evidence)).map((r) => r.sc)).toEqual([])
  })

  it('says for every row whether assistive technology is needed', () => {
    expect(rows.filter((r) => r.at !== 'needed' && r.at !== '—').map((r) => r.sc)).toEqual([])
  })

  it('points only at evidence files that exist', () => {
    const cited = [...record.matchAll(/`((?:web|docs|server|src)\/[^`\s]+)`/g)].map((m) => m[1])
    expect(cited.length).toBeGreaterThan(20)
    expect([...new Set(cited)].filter((p) => !existsSync(path.join(repoRoot, p)))).toEqual([])
  })

  it('states totals that match its own rows', () => {
    const count = (status: string) => rows.filter((r) => r.status === status).length
    const totals = `${rows.length} criteria: ${count('Automated')} automated, ${count('Partly automated')} partly automated, `
      + `${count('Inspected')} inspected, ${count('Gap')} with a known gap, ${count('Not verified')} not verified, ${count('N/A')} not applicable.`
    expect(record).toContain(totals)
    expect(record).toContain(`${rows.filter((r) => r.at === 'needed').length} need assistive technology or a device to confirm`)
  })
})
