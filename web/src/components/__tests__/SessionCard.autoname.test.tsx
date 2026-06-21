import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import type { Session } from '@/api/types'

const base: Session = {
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'done',
  createdAt: '',
  updatedAt: '',
}

const noop = () => {}

describe('SessionCard auto-name affordance', () => {
  it('marks an auto-named session', () => {
    render(<SessionCard session={{ ...base, nameIsAuto: true }} account={undefined}
      onOpen={noop} onRename={noop} onClose={noop} />)
    expect(screen.getByText('Fix login bug')).toHaveAttribute('title', 'Auto-named by Claude')
  })

  it('does not mark a manually named session', () => {
    render(<SessionCard session={{ ...base, nameIsAuto: false }} account={undefined}
      onOpen={noop} onRename={noop} onClose={noop} />)
    expect(screen.getByText('Fix login bug')).not.toHaveAttribute('title')
  })
})
