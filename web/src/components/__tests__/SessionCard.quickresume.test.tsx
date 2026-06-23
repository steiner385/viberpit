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
  it('with a suggestion: play fires in place with the first suggestion', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['Run the tests'] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'Run the tests')
  })

  it('no suggestion: play sends "continue"', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'continue')
  })

  it('detached "Ready" session with no suggestion: play still sends "continue" (backend resumes it)', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'idle', detached: true, latestSuggestions: [] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'continue')
  })

  it('actively-working session: no resume button', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'in_progress', detached: false, latestSuggestions: [] }), onQuickResume })
    expect(screen.queryByRole('button', { name: /resume/i })).not.toBeInTheDocument()
  })
})
