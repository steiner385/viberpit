import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

const base: Session = session({
  id: 'pending-1', name: 'My new session', accountName: '', status: 'in_progress',
  createdAt: '2026-06-23T00:00:00Z', pending: true,
})

describe('SessionCard pending placeholder', () => {
  it('shows a Starting… label and does not fire onOpen when clicked', () => {
    const onOpen = vi.fn()
    render(<SessionCard session={base} account={undefined} onOpen={onOpen} onRename={vi.fn()} onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()} />)
    expect(screen.getByText('My new session')).toBeInTheDocument()
    expect(screen.getByText(/starting/i)).toBeInTheDocument()
    fireEvent.click(screen.getByText('My new session'))
    expect(onOpen).not.toHaveBeenCalled()
  })
})
