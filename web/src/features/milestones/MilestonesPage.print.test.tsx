// Export PDF (spec 2026-11-05 M6, §9 "Export"): the button prints the live view,
// a print-only stamp carries the time, and the print caps render their "+N more"
// in the DOM (marked print-only) so a test can see them without a print engine.
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { MilestonesPage } from './MilestonesPage'
import { PrintStamp } from './PrintStamp'
import { HOUSEHOLD, cond, ladderView, note } from './__fixtures__/ladder'

const q = vi.hoisted(() => ({
  milestones: { data: undefined as unknown, isLoading: false, isError: false, refetch: () => {} },
  project: { data: undefined as unknown },
}))
const { idle } = vi.hoisted(() => ({ idle: () => ({ mutate: () => {}, reset: () => {}, isPending: false, error: null }) }))
vi.mock('@/api/hooks', () => ({ useProject: () => q.project }))
vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useMilestones: () => q.milestones,
  useRefreshMilestones: idle,
  useAdvanceMilestone: idle,
  useSetStatusNote: idle,
  useTickCondition: idle,
  useAddNote: idle,
  useUpdateNote: idle,
}))

const mount = () => render(
  <MemoryRouter initialEntries={['/roadmap/kindash/milestones']}>
    <Routes><Route path="/roadmap/:productSlug/milestones" element={<MilestonesPage />} /></Routes>
  </MemoryRouter>,
)

beforeEach(() => {
  q.milestones = { data: ladderView(), isLoading: false, isError: false, refetch: () => {} }
  q.project = { data: { name: 'KinDash', repos: [{ id: 'r', repo: 'cairnea/KinDash', isPrimary: true }] } }
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('MilestonesPage export', () => {
  it('export button calls window.print', () => {
    const print = vi.spyOn(window, 'print').mockImplementation(() => {})
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Export PDF' }))
    expect(print).toHaveBeenCalledTimes(1)
  })

  it('print stamp renders product and a local timestamp', () => {
    const at = new Date('2026-11-05T15:04:00Z')
    vi.useFakeTimers({ now: at })
    const { container } = render(<PrintStamp product="KinDash" />)
    const local = at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    expect(container).toHaveTextContent(`Snapshot ${local} · KinDash · ViberPit`)
    // The browser's own header and footer may be off: the stamp is the page's time.
    expect(container.firstElementChild).toHaveAttribute('data-print', 'only')
  })

  it('print caps render +N more for leaves, decisions and asks', () => {
    const open = (n: number) => Array.from({ length: n }, (_, i) => ({
      ref: `cairnea/KinDash#${500 + i}`, title: `Open leaf ${i}`, url: null, state: 'not_started' as const, prs: [],
    }))
    const household = {
      ...HOUSEHOLD,
      leaves: { done: 2, inFlight: 0, notStarted: 9, total: 11, measuredAt: null, items: open(9) },
    }
    const decisions = Array.from({ length: 8 }, (_, i) => note({ id: `d${i}`, kind: 'decision', text: `Decision ${i}` }))
    const asks = Array.from({ length: 8 }, (_, i) => note({ id: `a${i}`, kind: 'ask', text: `Ask ${i}` }))
    const base = ladderView()
    q.milestones.data = ladderView({}, {
      milestones: [household, ...base.milestones.slice(1)],
      notes: { decisions, asks },
    })
    mount()

    // Leaves: 9 open, at most 4 in print.
    const card = screen.getByRole('listitem', { name: /Phase 0/ })
    const leavesMore = within(card).getByText('+5 more')
    expect(leavesMore).toHaveAttribute('data-print', 'only')
    expect(within(card).getByText('2 done')).toBeInTheDocument()
    // Decisions and asks: 8 each, at most 6 in print.
    for (const name of ['Needs you', 'Decisions']) {
      const panel = screen.getByRole('region', { name })
      const more = within(panel).getByText('+2 more')
      expect(more).toHaveAttribute('data-print', 'only')
    }
  })

  // print.css restates the light theme so a dark-mode print is light on white; a
  // retuned token in index.css must land there too.
  it('print.css restates the light tokens exactly as index.css declares them', () => {
    // Vitest runs with cwd at the web/ package root.
    const read = (p: string) => readFileSync(path.resolve(process.cwd(), p), 'utf-8')
    const indexCss = read('src/index.css')
    const printCss = read('src/features/milestones/print.css')
    const tokens = (css: string) => Object.fromEntries(
      [...css.matchAll(/--t-([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})\b/g)].map((m) => [m[1], m[2].toLowerCase()]),
    )
    const light = tokens(indexCss.slice(indexCss.indexOf(':root {'), indexCss.indexOf('@media (prefers-color-scheme: dark)')))
    const print = tokens(printCss)
    // Exactly the tokens a dark theme overrides: a new dark-only token must be
    // restated here too, or it would leak into the print.
    const darkStart = indexCss.indexOf(':root[data-theme="dark"] {')
    const dark = tokens(indexCss.slice(darkStart, indexCss.indexOf('}', darkStart)))
    expect(Object.keys(print).sort()).toEqual(Object.keys(dark).sort())
    for (const [k, v] of Object.entries(print)) expect(`--t-${k}: ${v}`).toBe(`--t-${k}: ${light[k]}`)
  })

  it('print puts a note\'s milestone before its text, so the clamp only cuts the text', () => {
    const long = 'x'.repeat(500)
    q.milestones.data = ladderView({}, { notes: {
      asks: [note({ id: 'a1', kind: 'ask', text: long, milestoneKey: 'household' })],
      decisions: [note({ id: 'd1', kind: 'decision', ref: 'D1', status: 'proposed', text: long })],
    } })
    mount()
    const ask = within(screen.getByRole('list', { name: 'Open asks' })).getByRole('listitem')
    const [askText, askWhere] = ask.querySelectorAll('p')
    // Print: a label ahead of the text; the trailing "where" line is screen-only.
    expect(askText.firstElementChild).toHaveTextContent('Phase 0 · Household gate —')
    expect(askText.firstElementChild).toHaveAttribute('data-print', 'only')
    expect(askWhere).toHaveAttribute('data-print', 'screen')
    expect(askWhere).toHaveTextContent('Phase 0 · Household gate')
    const decision = within(screen.getByRole('list', { name: 'Decisions' })).getByRole('listitem')
    expect(decision.querySelector('p')!.firstElementChild).toHaveTextContent('Whole ladder · Proposed —')
  })

  it('print caps the gate at 4, unmet first, then unknown, then met', () => {
    const gate = [
      cond({ key: 'g0', label: 'Met A', status: 'met' }),
      cond({ key: 'g1', label: 'Unmet A', status: 'unmet' }),
      cond({ key: 'g2', label: 'Met B', status: 'met' }),
      cond({ key: 'g3', label: 'Unknown A', status: 'unknown' }),
      cond({ key: 'g4', label: 'Met C', status: 'met' }),
      cond({ key: 'g5', label: 'Unmet B', status: 'unmet' }),
    ]
    q.milestones.data = ladderView({}, { milestones: [{ ...HOUSEHOLD, gate }, ...ladderView().milestones.slice(1)] })
    mount()
    const card = screen.getByRole('listitem', { name: /Phase 0/ })
    const rows = within(within(card).getByRole('list', { name: 'Gate' })).getAllByRole('listitem')
    // The screen keeps the definition order; print reorders by `--print-order`.
    expect(rows.map((r) => r.textContent?.match(/(Met|Unmet|Unknown) [A-C]/)?.[0]))
      .toEqual(['Met A', 'Unmet A', 'Met B', 'Unknown A', 'Met C', 'Unmet B'])
    expect(rows.map((r) => r.style.getPropertyValue('--print-order'))).toEqual(['3', '0', '4', '2', '5', '1'])
    expect(rows.map((r) => r.getAttribute('data-print'))).toEqual([null, null, 'hide', null, 'hide', null])
    expect(within(card).getByText('+2 more (2 met)')).toHaveAttribute('data-print', 'only')
  })
})
