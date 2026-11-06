import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const wake = vi.fn()
vi.mock('@/api/hooks', async (orig) => ({
  ...(await orig()),
  useWakeSession: () => ({ mutate: wake, isPending: false }),
}))

import { SnoozeBanner } from './SnoozeBanner'

describe('SnoozeBanner', () => {
  it('says until when and why, and wakes the session', () => {
    render(
      <SnoozeBanner
        sessionId="s1"
        snooze={{ at: '2026-11-06T00:00:00Z', until: null, reason: 'deploy soak', by: 'session' }}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Snoozed until you wake it · self-snoozed · deploy soak')
    fireEvent.click(screen.getByRole('button', { name: 'Wake' }))
    expect(wake).toHaveBeenCalledWith('s1')
  })
})
