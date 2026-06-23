import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const noop = () => {}

function mkSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 's1',
    name: 'Test Session',
    accountName: 'work',
    status: 'done',
    createdAt: '2026-06-22T11:00:00Z',
    updatedAt: '2026-06-22T11:58:00Z',
    ...overrides,
  }
}

function renderCard(props: {
  session: Session
  onQuickResume: (id: string, text?: string) => void
}) {
  return render(
    <SessionCard
      session={props.session}
      account={undefined}
      onOpen={noop}
      onRename={noop}
      onClose={noop}
      onDelete={noop}
      onQuickResume={props.onQuickResume}
    />,
  )
}

describe('SessionCard — quick-resume play button', () => {
  it('live session with a suggestion: play fires in place with the first suggestion', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['Run the tests'] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'Run the tests')
  })

  it('no suggestion: play calls onQuickResume with no text (caller navigates)', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', undefined)
  })

  it('detached session with a suggestion: play passes no text (navigate to resume)', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: true, latestSuggestions: ['x'] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', undefined)
  })
})
