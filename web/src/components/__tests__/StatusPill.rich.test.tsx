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

describe('StatusPill pulse behavior', () => {
  it('ended pill does NOT have animate-pulse', () => {
    render(<StatusPill status={'ended' as SessionStatus} />)
    const pill = screen.getByRole('generic', { name: 'Status: Ended' })
    expect(pill.className).not.toContain('animate-pulse')
  })

  it('needs_attention pill DOES have motion-safe:animate-pulse', () => {
    render(<StatusPill status={'needs_attention' as SessionStatus} />)
    const pill = screen.getByRole('generic', { name: 'Status: Turn failed' })
    expect(pill.className).toContain('motion-safe:animate-pulse')
  })
})
