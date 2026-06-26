import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const SESSION = { id: 's1', name: 'Alpha', accountName: 'acct', status: 'ready', updatedAt: null, subAgents: [], latestSuggestions: [], prs: [] } as unknown as Session

function props(over = {}) {
  return {
    session: SESSION, account: undefined,
    onOpen: vi.fn(), onRename: vi.fn(), onClose: vi.fn(), onDelete: vi.fn(),
    onQuickResume: vi.fn(), ...over,
  }
}

function fg() {
  const el = screen.getByTestId('card-swipe-fg')
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 300 }) // commit at 135px
  return el
}

afterEach(() => { vi.restoreAllMocks() })

describe('SessionCard — mobile swipe', () => {
  it('full left-swipe commits Delete (onDelete)', () => {
    const onDelete = vi.fn()
    render(<SessionCard {...props({ onDelete })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 100, clientY: 45 }) // dx -150
    fireEvent.pointerUp(window, { clientX: 100, clientY: 45 })
    expect(onDelete).toHaveBeenCalledWith('s1')
  })

  it('full right-swipe commits Archive (onClose)', () => {
    const onClose = vi.fn()
    render(<SessionCard {...props({ onClose })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 50, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 210, clientY: 45 }) // dx +160
    fireEvent.pointerUp(window, { clientX: 210, clientY: 45 })
    expect(onClose).toHaveBeenCalledWith('s1')
  })

  it('short left-swipe reveals a tappable Delete button', () => {
    const onDelete = vi.fn()
    render(<SessionCard {...props({ onDelete })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 170, clientY: 42 }) // dx -80: reveal
    fireEvent.pointerUp(window, { clientX: 170, clientY: 42 })
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Alpha' }))
    expect(onDelete).toHaveBeenCalledWith('s1')
  })

  it('a vertical drag neither commits nor opens the session', () => {
    const onOpen = vi.fn(), onClose = vi.fn(), onDelete = vi.fn()
    render(<SessionCard {...props({ onOpen, onClose, onDelete })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 200, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 205, clientY: 200 }) // vertical
    fireEvent.pointerUp(window, { clientX: 205, clientY: 200 })
    expect(onClose).not.toHaveBeenCalled()
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('a horizontal drag suppresses the trailing open-tap', () => {
    const onOpen = vi.fn()
    render(<SessionCard {...props({ onOpen })} />)
    const el = fg()
    fireEvent.pointerDown(el, { clientX: 200, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 168, clientY: 41 }) // small horizontal drag
    fireEvent.pointerUp(window, { clientX: 168, clientY: 41 })
    fireEvent.click(screen.getByRole('button', { name: /open session alpha/i }))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('does not render the swipe layer at desktop (lg) width', () => {
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: q.includes('min-width: 1024px'), media: q,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia
    render(<SessionCard {...props()} />)
    expect(screen.queryByTestId('card-swipe-fg')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete Alpha' })).toBeNull()
  })
})
