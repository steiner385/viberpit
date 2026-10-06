import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SessionCard } from './SessionCard'
const repliesState = vi.hoisted(() => ({ replies: ['continue', 'proceed', 'try again'], statusText: null as string | null }))
vi.mock('@/api/quickReplies', () => ({ useQuickReplies: () => repliesState }))
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

const noop = () => {}
// Default sparkle generator: resolves to one fresh suggestion.
const genOne = () => Promise.resolve(['Run the tests'])

const mkSession = (overrides: Partial<Session> = {}): Session =>
  session({
    id: 's1',
    name: 'Test Session',
    accountName: 'work',
    status: 'done',
    createdAt: '2026-06-22T11:00:00Z',
    updatedAt: '2026-06-22T11:58:00Z',
    ...overrides,
  })

function renderCard(props: {
  session: Session
  onQuickResume: (id: string, text?: string) => void
  onGenerateSuggestion?: (id: string) => Promise<string[]>
}) {
  return render(
    <ViewportProvider kiosk={null}>
      <SessionCard
        session={props.session}
        account={undefined}
        onOpen={noop}
        onRename={noop}
        onClose={noop}
        onDelete={noop}
        onQuickResume={props.onQuickResume}
        onGenerateSuggestion={props.onGenerateSuggestion ?? genOne}
      />
    </ViewportProvider>,
  )
}

describe('SessionCard — quick-resume sparkle button', () => {
  it('sparkle ALWAYS asks the LLM and resumes with the top freshly-generated suggestion', async () => {
    const onQuickResume = vi.fn()
    // A stale cached suggestion must be ignored — the sparkle generates fresh.
    const onGenerateSuggestion = vi.fn().mockResolvedValue(['Generated next step', 'Another'])
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['stale cached'] }), onQuickResume, onGenerateSuggestion })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    await waitFor(() => expect(onGenerateSuggestion).toHaveBeenCalledWith('s1'))
    await waitFor(() => expect(onQuickResume).toHaveBeenCalledWith('s1', 'Generated next step', 'suggested'))
  })

  it('empty generation: sparkle resumes with nothing — never a hard-coded "continue"', async () => {
    const onQuickResume = vi.fn()
    const onGenerateSuggestion = vi.fn().mockResolvedValue([])
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume, onGenerateSuggestion })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    await waitFor(() => expect(onGenerateSuggestion).toHaveBeenCalled())
    expect(onQuickResume).not.toHaveBeenCalled()
  })

  it('detached "Ready" session: sparkle still generates (backend resumes from transcript)', async () => {
    const onQuickResume = vi.fn()
    const onGenerateSuggestion = vi.fn().mockResolvedValue(['Pick up where you left off'])
    renderCard({ session: mkSession({ status: 'idle', detached: true, latestSuggestions: [] }), onQuickResume, onGenerateSuggestion })
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))
    await waitFor(() => expect(onQuickResume).toHaveBeenCalledWith('s1', 'Pick up where you left off', 'suggested'))
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

  // Quick replies live behind ✦'s own chevron (2026-09-29 critique, P1): a reply
  // is a SEND, and the unified ⋯ menu had four of them one row above Delete.
  // Always available — even with zero backend suggestions — whenever the
  // session is resumable.
  it('learned quick replies are available in the Quick-replies menu with no backend suggestions', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    for (const reply of repliesState.replies) {
      expect(screen.getByRole('menuitem', { name: reply })).toBeInTheDocument()
    }
    // The menu says what a row does before the row is tapped.
    expect(screen.getByRole('menu')).toHaveTextContent(/send a reply/i)
  })

  it('clicking a learned quick reply sends its literal text', () => {
    const onQuickResume = vi.fn()
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'try again' }))
    expect(onQuickResume).toHaveBeenCalledWith('s1', 'try again', 'suggested')
  })

  it('lists all cached dynamic suggestions above the learned list, sections divided', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['First step', 'Second step'] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    // The sparkle generates fresh, so the menu surfaces ALL cached suggestions.
    expect(screen.getByRole('menuitem', { name: 'First step' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Second step' })).toBeInTheDocument()
    // Canned list still present below.
    expect(screen.getByRole('menuitem', { name: 'continue' })).toBeInTheDocument()
    // One divider: suggestions | learned replies.
    expect(screen.getAllByRole('separator')).toHaveLength(1)
  })

  it('no divider when there are no cached suggestions', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    expect(screen.queryAllByRole('separator')).toHaveLength(0)
  })

  it('the ⋯ menu is management only — no one-tap send shares a list with Delete', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['First step'] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    const items = screen.getAllByRole('menuitem').map((el) => el.textContent?.trim())
    expect(items).toEqual(['Rename', 'Archive', 'Delete'])
    expect(screen.queryByRole('menuitem', { name: 'First step' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'continue' })).toBeNull()
  })

  it('offers no reply menu while the session is busy (nothing to resume)', () => {
    renderCard({ session: mkSession({ status: 'in_progress', detached: false, latestSuggestions: ['First step'] }), onQuickResume: noop })
    expect(screen.queryByRole('button', { name: /quick replies/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    expect(screen.queryByRole('menuitem', { name: 'First step' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'continue' })).not.toBeInTheDocument()
    // Management actions remain reachable mid-turn.
    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
  })

  // 2026-09-29 re-critique, P1: the send affordances were gated on the `busy`
  // tier only, so a session parked in the background (`waiting_external` — "I'm
  // waiting for the deploy…") still offered ✦, the quick replies and `c`: one tap
  // sent a generated prompt into an agent that was deliberately waiting.
  describe('no sends into a waiting session', () => {
    it('a waiting_external session has no Resume button and no quick-replies menu; ⋯ stays', () => {
      renderCard({ session: mkSession({ status: 'waiting_external', latestSuggestions: ['First step'] }), onQuickResume: noop })
      expect(screen.queryByRole('button', { name: /resume/i })).toBeNull()
      expect(screen.queryByRole('button', { name: /quick replies/i })).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
      expect(screen.getByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
    })

    it('an unreachable session (dead host) offers no send either — the daemon cannot honour it', () => {
      renderCard({ session: mkSession({ status: 'done', liveness: 'dead' }), onQuickResume: noop })
      expect(screen.queryByRole('button', { name: /resume/i })).toBeNull()
    })

    it('a waiting session ignores the `c` list-mode shortcut', async () => {
      const onQuickResume = vi.fn()
      const onGenerateSuggestion = vi.fn().mockResolvedValue(['Generated'])
      render(
        <ViewportProvider kiosk={null}>
          <SessionCard
            session={mkSession({ status: 'waiting_external' })}
            account={undefined} selected listMode
            onOpen={noop} onRename={noop} onClose={noop} onDelete={noop}
            onQuickResume={onQuickResume} onGenerateSuggestion={onGenerateSuggestion}
          />
        </ViewportProvider>,
      )
      fireEvent.keyDown(screen.getByRole('button', { name: /open session/i }), { key: 'c' })
      await new Promise((r) => setTimeout(r, 0))
      expect(onGenerateSuggestion).not.toHaveBeenCalled()
      expect(onQuickResume).not.toHaveBeenCalled()
    })
  })

  // On a coarse pointer a bare ✦ is 44px of one-tap send sitting 8px from the
  // title tap. There, ✦ opens the reply menu instead, with "Generate next step"
  // as its first row — a send is always a second, named tap.
  describe('coarse pointer: ✦ is the menu, not the send', () => {
    const realMatchMedia = window.matchMedia
    beforeEach(() => {
      Object.defineProperty(window, 'matchMedia', {
        writable: true, configurable: true,
        value: vi.fn().mockImplementation((query: string) => ({
          matches: query === '(pointer: coarse)', media: query,
          addEventListener: vi.fn(), removeEventListener: vi.fn(),
          addListener: vi.fn(), removeListener: vi.fn(), onchange: null, dispatchEvent: vi.fn(),
        })),
      })
    })
    afterEach(() => {
      Object.defineProperty(window, 'matchMedia', { writable: true, configurable: true, value: realMatchMedia })
    })

    it('tapping ✦ opens the reply menu instead of sending; its first row generates', async () => {
      const onQuickResume = vi.fn()
      const onGenerateSuggestion = vi.fn().mockResolvedValue(['Generated next step'])
      renderCard({ session: mkSession({ status: 'done', latestSuggestions: ['Cached one'] }), onQuickResume, onGenerateSuggestion })
      const resume = screen.getByRole('button', { name: /resume session/i })
      expect(resume).toHaveAttribute('aria-haspopup', 'menu')
      // No separate chevron: ✦ is the one trigger on touch.
      expect(screen.queryByRole('button', { name: /quick replies for/i })).toBeNull()
      fireEvent.click(resume)
      expect(onGenerateSuggestion).not.toHaveBeenCalled()
      const items = screen.getAllByRole('menuitem').map((el) => el.textContent?.trim())
      expect(items[0]).toMatch(/generate next step/i)
      expect(items).toContain('Cached one')
      expect(items).toContain('continue')
      fireEvent.click(screen.getByRole('menuitem', { name: /generate next step/i }))
      await waitFor(() => expect(onQuickResume).toHaveBeenCalledWith('s1', 'Generated next step', 'suggested'))
    })
  })

  it('menu rows carry the 44px coarse-pointer floor', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: [] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    for (const item of screen.getAllByRole('menuitem')) expect(item).toHaveClass('min-h-11')
  })

  // Regression: the menu shares the sessions-list stacking context with the
  // sticky group headers (GroupHeader uses `sticky top-0 z-raised`). At an
  // equal z-index a later group's header would paint over an open menu from a
  // card above it. The menu must out-rank the header (z-sticky > z-raised).
  it('renders the ⋯ menu above the sticky group headers (z-sticky)', () => {
    renderCard({ session: mkSession({ status: 'done', detached: false, latestSuggestions: ['First step', 'Second step'] }), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    const menu = screen.getByRole('menu')
    expect(menu.className).toMatch(/\bz-sticky\b/)
    expect(menu.className).not.toMatch(/\bz-raised\b/)
  })
})

it.each(['Loading your replies…', 'Learning from your replies across sessions.', 'Quick replies unavailable. Try again later.'])('shows %s without canned fallback', (statusText) => {
  const previous = repliesState.replies
  repliesState.replies = []
  repliesState.statusText = statusText
  try {
    renderCard({ session: mkSession(), onQuickResume: noop })
    fireEvent.click(screen.getByRole('button', { name: /quick replies/i }))
    expect(screen.getByRole('status')).toHaveTextContent(statusText)
    expect(screen.queryByRole('menuitem', { name: 'continue' })).toBeNull()
  } finally {
    repliesState.replies = previous
    repliesState.statusText = null
  }
})
