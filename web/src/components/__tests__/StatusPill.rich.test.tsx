import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { StatusPill } from '../UsageBadge'
import type { SessionStatus } from '@/api/types'

describe('StatusPill rich session statuses', () => {
  it.each([
    ['in_progress', 'In progress'],
    ['waiting_input', 'Waiting for input'],
    ['ready_review', 'Ready for review'],
    ['done', 'Done'],
  ])('renders %s as "%s"', (status, label) => {
    render(<StatusPill status={status as SessionStatus} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })
})
