import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from '../SessionCard'
import { QUICK_REPLIES } from '@/lib/quickReplies'
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

describe('SessionCard — quick-resume sparkle button', () => {
  it('with a suggestion: sparkle fires in place with the first suggestion', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['Run the tests'] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'Run the tests')
  })

  it('no suggestion: sparkle sends "continue"', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'continue')
  })

  it('detached "Ready" session with no suggestion: sparkle still sends "continue" (backend resumes it)', () => {
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

  it('renders the AI sparkle glyph (not the old play triangle) on the resume button', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume: noop })
    const resume = screen.getByRole('button', { name: /resume/i })
    expect(resume.querySelector('.lucide-sparkles')).toBeInTheDocument()
    expect(resume.querySelector('.lucide-play')).not.toBeInTheDocument()
  })

  // The dropdown now always offers the hard-coded quick replies, so it shows
  // whenever the sparkle does — even with zero backend suggestions.
  it('quick-replies dropdown is available with no backend suggestions', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    for (const reply of QUICK_REPLIES) {
      expect(screen.getByRole('menuitem', { name: reply })).toBeInTheDocument()
    }
  })

  it('clicking a canned quick reply sends its literal text', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'try again' }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'try again')
  })

  it('dropdown lists leftover dynamic suggestions above the canned list, separated by a divider', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['First step', 'Second step'] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    // The sparkle already sends 'First step'; the dropdown surfaces the rest.
    expect(screen.getByRole('menuitem', { name: 'Second step' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'First step' })).not.toBeInTheDocument()
    // Canned list still present below.
    expect(screen.getByRole('menuitem', { name: 'continue' })).toBeInTheDocument()
    // A divider separates the dynamic section from the canned section.
    expect(screen.getByRole('separator')).toBeInTheDocument()
  })

  it('no divider when there are no leftover dynamic suggestions', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['Only one'] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
  })

  // Regression: the dropdown menu shares the sessions-list stacking context with
  // the sticky group headers (GroupHeader uses `sticky top-0 z-10`). At an equal
  // z-index a later group's header would paint over an open menu from a card
  // above it. The menu must out-rank the header (z-20 > z-10).
  it('renders the quick-replies menu above the sticky group headers (z-20)', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['First step', 'Second step'] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    const menu = screen.getByRole('menu')
    expect(menu.className).toMatch(/\bz-20\b/)
    expect(menu.className).not.toMatch(/\bz-10\b/)
  })
})
