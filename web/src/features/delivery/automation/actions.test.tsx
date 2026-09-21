// web/src/features/delivery/automation/actions.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AutomationActions } from './actions'

let items: unknown[] = []
vi.mock('@/api/hooks', () => ({
  useAutomationActionable: () => ({ data: items }),
  useAutomationTrigger: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))

describe('AutomationActions', () => {
  // #16348: PR Doctor's click path refuses "prior PR execution has not
  // stopped" while a predecessor session still holds the claim — a
  // status-only item (no `actions`) must render the reason as plain text,
  // never an inert button.
  it('renders status text instead of a button when actions is empty', () => {
    items = [{
      id: 'o/r#1', title: 'fix conflict', actions: [],
      status: 'Being worked by session sess-predecessor',
    }]
    render(<AutomationActions automationId="pr-doctor" enabled />)
    expect(screen.getByText('Being worked by session sess-predecessor')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders the action button, not status text, once a claim is free', () => {
    items = [{
      id: 'o/r#1', title: 'fix conflict',
      actions: [{ kind: 'merge_conflict', label: 'Resolve conflicts', style: 'risky' }],
    }]
    render(<AutomationActions automationId="pr-doctor" enabled />)
    expect(screen.getByRole('button', { name: 'Resolve conflicts' })).toBeInTheDocument()
  })
})
