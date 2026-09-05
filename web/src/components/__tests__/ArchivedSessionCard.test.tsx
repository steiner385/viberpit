import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ArchivedSessionCard } from '../ArchivedSessionCard'
import { archivedSession } from '@/test/factories'

function props(over = {}) {
  return {
    session: archivedSession({ id: 'a1', name: 'Old project', account: 'alice', repo: 'cairnea/KinDash' }),
    onReview: vi.fn(),
    onUnarchive: vi.fn(),
    onDelete: vi.fn(),
    ...over,
  }
}

describe('ArchivedSessionCard', () => {
  it('shows the name, the repo and the account', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.getByText('Old project')).toBeInTheDocument()
    expect(screen.getByText('cairnea/KinDash')).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
  })

  it('falls back to a short id when the session has no name', () => {
    render(<ArchivedSessionCard {...props({ session: archivedSession({ id: 'abcdef123456', name: '' }) })} />)
    expect(screen.getByText('abcdef12')).toBeInTheDocument()
  })

  it('labels the archive time relatively, not as a raw locale string', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.getByTestId('archived-at').textContent).toMatch(/^Archived \d+[smhd] ago$/)
  })

  it('opens the read-only transcript from the card', () => {
    const onReview = vi.fn()
    render(<ArchivedSessionCard {...props({ onReview })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Review Old project' }))
    expect(onReview).toHaveBeenCalledWith('a1')
  })

  it('offers Review, Unarchive and Delete permanently in the overflow menu', async () => {
    const onUnarchive = vi.fn()
    render(<ArchivedSessionCard {...props({ onUnarchive })} />)
    await userEvent.click(screen.getByRole('button', { name: 'More actions for Old project' }))
    expect(screen.getByRole('menuitem', { name: 'Review' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Delete permanently' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('menuitem', { name: 'Unarchive' }))
    expect(onUnarchive).toHaveBeenCalledWith('a1')
  })

  it('commits Unarchive on a right-swipe and Delete on a left-swipe', () => {
    const onUnarchive = vi.fn()
    const onDelete = vi.fn()
    const p = props({ onUnarchive, onDelete })
    render(<ArchivedSessionCard {...p} />)
    const el = screen.getByTestId('card-swipe-fg')
    Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 300 })

    fireEvent.pointerDown(el, { clientX: 50, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 210, clientY: 45 })
    fireEvent.pointerUp(window, { clientX: 210, clientY: 45 })
    expect(onUnarchive).toHaveBeenCalledWith('a1')

    fireEvent.pointerDown(el, { clientX: 250, clientY: 40 })
    fireEvent.pointerMove(window, { clientX: 100, clientY: 45 })
    fireEvent.pointerUp(window, { clientX: 100, clientY: 45 })
    expect(onDelete).toHaveBeenCalledWith(p.session)
  })

  it('carries none of the live-session signals', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.queryByTestId('card-status-dot')).not.toBeInTheDocument()
    expect(screen.queryByTestId('card-model')).not.toBeInTheDocument()
    expect(screen.queryByTestId('card-prs')).not.toBeInTheDocument()
  })

  it('falls back to the cwd-derived repo when the session has no repo field', () => {
    render(<ArchivedSessionCard {...props({
      session: archivedSession({ repo: null, cwd: '/home/x/.ccswitch/worktrees/KinDash/ab12' }),
    })} />)
    // repoFromCwd takes the segment right after `worktrees/`, not the basename
    // (a meaningless hash) or the `.ccswitch` parent — see repoFromCwd.ts.
    expect(screen.getByText('KinDash')).toBeInTheDocument()
  })

  it('tags the account chip with the shared card-account testid', () => {
    render(<ArchivedSessionCard {...props()} />)
    expect(screen.getByTestId('card-account')).toHaveTextContent('alice')
  })

  describe('unarchiving', () => {
    it('shows the default enabled Unarchive label when not unarchiving', async () => {
      render(<ArchivedSessionCard {...props()} />)
      await userEvent.click(screen.getByRole('button', { name: 'More actions for Old project' }))
      const item = screen.getByRole('menuitem', { name: 'Unarchive' })
      expect(item).toBeEnabled()
    })

    it('shows a disabled "Restoring…" label while unarchiving', async () => {
      render(<ArchivedSessionCard {...props({ unarchiving: true })} />)
      await userEvent.click(screen.getByRole('button', { name: 'More actions for Old project' }))
      const item = screen.getByRole('menuitem', { name: 'Restoring…' })
      expect(item).toBeDisabled()
    })

    it('disables the swipe gesture while unarchiving, so a second swipe cannot double-submit', () => {
      // CardFrame only attaches the swipe layer (and its 'card-swipe-fg'
      // testid) when swipeEnabled — same pattern as the pending-session case
      // in SessionCardSwipe.test.tsx's "does not render the swipe layer".
      render(<ArchivedSessionCard {...props({ unarchiving: true })} />)
      expect(screen.queryByTestId('card-swipe-fg')).not.toBeInTheDocument()
      expect(screen.queryByTestId('swipe-panel-unarchive')).not.toBeInTheDocument()
      expect(screen.queryByTestId('swipe-panel-delete')).not.toBeInTheDocument()
    })
  })
})
