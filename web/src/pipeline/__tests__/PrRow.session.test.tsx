// ccswitch blend test (spec §6): PrRow session label + Fix action. Not a plotroom port.
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ApiBaseProvider } from '../ApiBaseContext'
import { PrRow } from '../PrRow'
import type { PrView } from '../types'

const pr = (over: Partial<PrView> = {}): PrView => ({
  repo: 'o/r', number: 7, title: 'fix auth', url: 'https://x/pull/7',
  stage: { stage: 'ci', substate: null, percent: null, etaSeconds: null,
           etaRangeSeconds: null, overdue: false },
  queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
  session: { id: 's1', name: 'fix-auth', fixable: false },
  ...over,
})

const wrap = (ui: React.ReactNode) => render(
  <MemoryRouter><ApiBaseProvider base="/api/pipeline">{ui}</ApiBaseProvider></MemoryRouter>)

describe('PrRow ccswitch extras', () => {
  it('shows the session label linking to the session', () => {
    wrap(<PrRow pr={pr()} hasDeploy={false} queueCulprit={null} />)
    const link = screen.getByRole('link', { name: /fix-auth/ })
    expect(link).toHaveAttribute('href', '/sessions/s1')
  })
  it('shows Fix only when fixable, and calls onFixPr', () => {
    const onFix = vi.fn()
    wrap(<PrRow pr={pr({ session: { id: 's1', name: 'fix-auth', fixable: true } })}
                hasDeploy={false} queueCulprit={null} onFixPr={onFix} />)
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    expect(onFix).toHaveBeenCalledWith('s1', 7)
  })
  it('renders no session UI when session is null', () => {
    wrap(<PrRow pr={pr({ session: null })} hasDeploy={false} queueCulprit={null} />)
    expect(screen.queryByRole('link', { name: /fix-auth/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Fix' })).toBeNull()
  })
})
