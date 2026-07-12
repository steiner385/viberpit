import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { PipelineView } from '../PipelineView'
import type { DashboardState } from '../types'

const state: DashboardState = {
  generatedAt: 'now', staleSince: null,
  repos: [{ repo: 'o/r', hasDeploy: false, queue: null, prs: [{
    repo: 'o/r', number: 1, title: 't', url: 'https://x/1',
    stage: { stage: 'ci', substate: null, percent: null, etaSeconds: null, etaRangeSeconds: null, overdue: false },
    queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
  }] }],
}
const wrap = (ui: ReactNode) => render(<MemoryRouter>{ui}</MemoryRouter>)

beforeEach(() => localStorage.clear())

describe('PipelineView blend edits', () => {
  it('kiosk renders tiles as non-interactive divs and rows non-expandable', () => {
    wrap(<PipelineView state={state} focusedRepo={null} kiosk />)
    // StatusStrip kiosk mode: tiles are not buttons (plotroom StatusStrip interactive=false)
    expect(screen.queryByRole('button', { name: /CI running/i })).toBeNull()
    // A count/label div still renders for the running bucket.
    expect(screen.getByText('CI running')).toBeInTheDocument()
  })
  it('persists repo collapse to localStorage (prdash.collapsed)', () => {
    wrap(<PipelineView state={state} focusedRepo={null} />)
    fireEvent.click(screen.getByRole('button', { name: /o\/r/ }))
    expect(JSON.parse(localStorage.getItem('prdash.collapsed')!)).toContain('o/r')
  })
  it('controlled filter: uses activeFilter prop and reports via onFilter', () => {
    const onFilter = vi.fn()
    wrap(<PipelineView state={state} focusedRepo={null} activeFilter={null} onFilter={onFilter} />)
    fireEvent.click(screen.getByRole('button', { name: /CI running/i }))
    expect(onFilter).toHaveBeenCalledWith('running')
  })
})
