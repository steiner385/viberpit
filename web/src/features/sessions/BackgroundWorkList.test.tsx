import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { BackgroundWorkList } from './BackgroundWorkList'
import type { BackgroundWorkRow } from '@/session/selectors'

const mutate = vi.fn()
vi.mock('@/api/hooks', () => ({ useStopSubAgent: () => ({ mutate, isPending: false }) }))
afterEach(() => { cleanup(); mutate.mockReset() })

const row = (over: Partial<BackgroundWorkRow> = {}): BackgroundWorkRow =>
  ({ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', startedAt: Date.now() - 65_000, live: true, ...over })

describe('BackgroundWorkList', () => {
  it('renders kind, status, description and elapsed for a running shell', async () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [row()], wakeup: false, wakeupAt: null }} />)
    const list = screen.getByRole('list', { name: 'Background work' })
    expect(list).toHaveTextContent('shell'); expect(list).toHaveTextContent('running'); expect(list).toHaveTextContent('pytest -q')
    await waitFor(() => expect(list).toHaveTextContent(/1m \d+s/))
  })
  it('Stop acts only on a live running row and posts the tool-use id', () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [row(), row({ toolUseId: 'tu2', live: false }), row({ toolUseId: 'tu3', status: 'completed' })], wakeup: false, wakeupAt: null }} />)
    const stops = screen.getAllByRole('button', { name: /^Stop shell/ })
    expect(stops).toHaveLength(1)
    fireEvent.click(stops[0])
    expect(mutate).toHaveBeenCalledWith({ id: 's1', toolUseId: 'tu1' })
  })
  it('shows an armed wakeup with no fire time', () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [], wakeup: true, wakeupAt: null }} />)
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent(/wakeup.*armed/)
  })
  it('counts down to a timed wakeup', async () => {
    render(<BackgroundWorkList sessionId="s1" work={{ rows: [], wakeup: true, wakeupAt: Date.now() + 125_000 }} />)
    await waitFor(() => expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent(/fires in 2m \d+s/))
  })
})
