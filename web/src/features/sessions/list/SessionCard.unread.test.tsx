import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { SessionCard } from './SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'done',
  createdAt: '2026-06-22T11:00:00Z',
  updatedAt: '2026-06-22T11:58:00Z',
})

const noop = () => {}

function renderCard(s: Session, onSetUnread?: (id: string, unread: boolean) => void) {
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <SessionCard session={s} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop}
          onSetUnread={onSetUnread} />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: /more actions for session/i }))
}

describe('SessionCard unread state (D-10)', () => {
  it('shows an unread session title in bold and says "unread" in its accessible name', () => {
    renderCard({ ...base, unread: true })
    expect(screen.getByText('Fix login bug')).toHaveClass('font-bold')
    expect(screen.getByRole('button', { name: /open session fix login bug/i }))
      .toHaveAccessibleName(expect.stringMatching(/unread/i))
  })

  it('keeps a read session title out of bold and "unread" out of its accessible name', () => {
    renderCard({ ...base, unread: false })
    expect(screen.getByText('Fix login bug')).not.toHaveClass('font-bold')
    expect(screen.getByRole('button', { name: /open session fix login bug/i }))
      .not.toHaveAccessibleName(expect.stringMatching(/unread/i))
  })

  it('treats an absent flag as read', () => {
    renderCard(base)
    expect(screen.getByText('Fix login bug')).not.toHaveClass('font-bold')
  })

  it('offers "Mark as read" on an unread card and asks for read on click', () => {
    const onSetUnread = vi.fn()
    renderCard({ ...base, unread: true }, onSetUnread)
    openMenu()
    const item = screen.getByRole('menuitem', { name: 'Mark as read' })
    expect(screen.queryByRole('menuitem', { name: 'Mark as unread' })).not.toBeInTheDocument()
    fireEvent.click(item)
    expect(onSetUnread).toHaveBeenCalledWith('1', false)
  })

  it('offers "Mark as unread" on a read card and asks for unread on click', () => {
    const onSetUnread = vi.fn()
    renderCard({ ...base, unread: false }, onSetUnread)
    openMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Mark as unread' }))
    expect(onSetUnread).toHaveBeenCalledWith('1', true)
  })

  it('has no read/unread item when no handler is wired', () => {
    renderCard({ ...base, unread: true })
    openMenu()
    expect(screen.queryByRole('menuitem', { name: /mark as (un)?read/i })).not.toBeInTheDocument()
  })

  it('leaves the amber needs-you status pill unchanged', () => {
    const needsYou: Session = { ...base, status: 'waiting_question' }
    const { unmount } = renderCard({ ...needsYou, unread: false })
    const readPill = screen.getByLabelText(/^Status: /)
    const readText = readPill.textContent
    const readClass = readPill.className
    expect(readClass).toContain('text-warning')
    unmount()
    renderCard({ ...needsYou, unread: true })
    const unreadPill = screen.getByLabelText(/^Status: /)
    expect(unreadPill.textContent).toBe(readText)
    expect(unreadPill.className).toBe(readClass)
  })
})
