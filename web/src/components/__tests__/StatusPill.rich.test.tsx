import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { StatusPill } from '../UsageBadge'
import type { SessionStatus } from '@/api/types'

// Labels come from the single source of truth (statusTier.ts) — the StatusPill
// no longer carries its own status→label table.
describe('StatusPill rich session statuses', () => {
  it.each([
    ['in_progress', 'Working…'],
    ['waiting_input', 'Permission needed'],
    ['ready_review', 'Approve plan'],
    ['done', 'Your turn'],
    ['complete', 'Your turn'],
    ['idle', 'Ready'],
    ['needs_attention', 'Turn failed'],
  ])('renders %s as "%s"', (status, label) => {
    render(<StatusPill status={status as SessionStatus} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })
})
