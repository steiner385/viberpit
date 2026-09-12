import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { SessionCard } from '../SessionCard'
import { ViewportProvider } from '@/shell/useViewport'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'

const base: Session = session({
  id: '1',
  name: 'Fix login bug',
  accountName: 'work',
  status: 'done',
  createdAt: '',
  updatedAt: '',
})

const noop = () => {}

describe('SessionCard auto-name affordance', () => {
  it('marks an auto-named session', () => {
    render(
      <ViewportProvider kiosk={null}>
        <SessionCard session={{ ...base, nameIsAuto: true }} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop} />
      </ViewportProvider>,
    )
    expect(screen.getByText('Fix login bug')).toHaveAttribute('title', 'Auto-named by Claude')
  })

  it('does not mark a manually named session', () => {
    render(
      <ViewportProvider kiosk={null}>
        <SessionCard session={{ ...base, nameIsAuto: false }} account={undefined}
          onOpen={noop} onRename={noop} onClose={noop} onDelete={noop} onQuickResume={noop} />
      </ViewportProvider>,
    )
    expect(screen.getByText('Fix login bug')).not.toHaveAttribute('title')
  })
})
