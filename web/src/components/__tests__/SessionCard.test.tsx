import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const SESSION = { id: 's1', name: 'Alpha', accountName: 'acct', status: 'ready', updatedAt: null, subAgents: [], latestSuggestions: [] } as unknown as Session

function props(over = {}) {
  return {
    session: SESSION, account: undefined, selected: true, listMode: true,
    onOpen: vi.fn(), onRename: vi.fn(), onClose: vi.fn(), onDelete: vi.fn(),
    onQuickResume: vi.fn(), onNavigate: vi.fn(), onExit: vi.fn(), ...over,
  }
}

async function press(key: string) {
  const user = userEvent.setup()
  screen.getByRole('button', { name: /open session alpha/i }).focus()
  await user.keyboard(key)
}

describe('SessionCard — list-mode keys', () => {
  it('c continues (resume), r renames, a archives, d deletes', async () => {
    const onQuickResume = vi.fn(), onRename = vi.fn(), onClose = vi.fn(), onDelete = vi.fn()
    render(<SessionCard {...props({ onQuickResume, onRename, onClose, onDelete })} />)
    await press('c'); expect(onQuickResume).toHaveBeenCalledWith('s1', 'continue')
    await press('r'); expect(onRename).toHaveBeenCalledWith('s1')
    await press('a'); expect(onClose).toHaveBeenCalledWith('s1')
    await press('{Backspace}'); expect(onClose).toHaveBeenCalledTimes(2)
    await press('d'); expect(onDelete).toHaveBeenCalledWith('s1')
    await press('{Delete}'); expect(onDelete).toHaveBeenCalledTimes(2)
  })

  it('Enter opens, arrows navigate, Esc exits', async () => {
    const onOpen = vi.fn(), onNavigate = vi.fn(), onExit = vi.fn()
    render(<SessionCard {...props({ onOpen, onNavigate, onExit })} />)
    await press('{Enter}'); expect(onOpen).toHaveBeenCalledWith('s1')
    await press('{ArrowDown}'); expect(onNavigate).toHaveBeenCalledWith('down')
    await press('{ArrowUp}'); expect(onNavigate).toHaveBeenCalledWith('up')
    await press('{Escape}'); expect(onExit).toHaveBeenCalled()
  })

  it('focuses the row when listMode + selected', () => {
    render(<SessionCard {...props({ listMode: true, selected: true })} />)
    expect(screen.getByRole('button', { name: /open session alpha/i })).toHaveFocus()
  })
})
