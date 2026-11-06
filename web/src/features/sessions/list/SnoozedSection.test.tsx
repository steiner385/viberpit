import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { Session, SessionSnooze } from '@/api/types'
import { mkSession } from '@/test/factories'

const wake = vi.fn()
vi.mock('@/api/hooks', async (orig) => ({
  ...(await orig()),
  useWakeSession: () => ({ mutate: wake, isPending: false, variables: undefined }),
}))

import { SnoozedSection } from './SnoozedSection'

const snz = (over: Partial<SessionSnooze> = {}): SessionSnooze =>
  ({ at: '2026-11-06T00:00:00Z', until: null, reason: null, by: 'user', ...over })
const row = (id: string, snooze: SessionSnooze): Session => mkSession(id, { name: `sess-${id}`, snooze })

function expand() {
  fireEvent.click(screen.getByRole('button', { name: /Snoozed \(\d+\)/ }))
}

describe('SnoozedSection', () => {
  beforeEach(() => wake.mockClear())

  it('renders nothing when empty', () => {
    const { container } = render(<SnoozedSection sessions={[]} onOpen={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('is collapsed by default, with a count', () => {
    render(<SnoozedSection sessions={[row('a', snz())]} onOpen={() => {}} />)
    const header = screen.getByRole('button', { name: /Snoozed \(1\)/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('sess-a')).not.toBeInTheDocument()
  })

  it('shows wake time, reason and a self-snoozed hint once expanded', () => {
    render(
      <SnoozedSection
        sessions={[
          row('a', snz({ until: '2026-11-09T14:00:00Z', reason: 'waiting on PR #12', by: 'session' })),
          row('b', snz()),
        ]}
        onOpen={() => {}}
      />,
    )
    expand()
    const [a, b] = screen.getAllByRole('listitem')
    expect(a).toHaveTextContent(/until .*Nov/)
    expect(a).toHaveTextContent('self-snoozed')
    expect(a).toHaveTextContent('waiting on PR #12')
    expect(b).toHaveTextContent('until you wake it')
    expect(b).not.toHaveTextContent('self-snoozed')
  })

  it('opens a row and wakes it from the Wake button', () => {
    const onOpen = vi.fn()
    render(<SnoozedSection sessions={[row('a', snz())]} onOpen={onOpen} />)
    expand()
    fireEvent.click(screen.getByText('sess-a'))
    expect(onOpen).toHaveBeenCalledWith('a')
    fireEvent.click(screen.getByRole('button', { name: 'Wake sess-a' }))
    expect(wake).toHaveBeenCalledWith('a')
  })
})
