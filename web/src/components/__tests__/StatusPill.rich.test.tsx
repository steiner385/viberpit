import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { StatusPill } from '../UsageBadge'
import type { SessionStatus } from '@/api/types'

describe('StatusPill rich session statuses', () => {
  it.each([
    ['in_progress', 'Working…'],
    ['waiting_input', 'Permission needed'],
    ['waiting_question', 'Answer needed'],
    ['ready_review', 'Approve plan'],
    ['done', 'Your turn'],
    ['needs_attention', 'Turn failed'],
  ])('renders %s as "%s"', (status, label) => {
    render(<StatusPill status={status as SessionStatus} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })
})
