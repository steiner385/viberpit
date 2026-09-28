// web/src/features/delivery/automation/actions.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByText('Being worked by session sess-predecessor')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders the action button, not status text, once a claim is free', () => {
    items = [{
      id: 'o/r#1', title: 'fix conflict',
      actions: [{ kind: 'merge_conflict', label: 'Resolve conflicts', style: 'risky' }],
    }]
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByRole('button', { name: 'Resolve conflicts' })).toBeInTheDocument()
  })

  it('preview: three rows, a "See all N" button that hands off, and nothing at all when empty', () => {
    const onSeeAll = vi.fn()
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [] , status: 'waiting' }))
    render(<AutomationActions automationId="pr-doctor" enabled variant="preview" onSeeAll={onSeeAll} />)
    expect(screen.getByRole('heading', { name: 'Actionable items' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
    expect(screen.queryByText('item 4')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'See all 5' }))
    expect(onSeeAll).toHaveBeenCalledTimes(1)
  })

  it('preview: no "See all" button at all when no onSeeAll is given, even past PREVIEW_ROWS (F6)', () => {
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [], status: 'waiting' }))
    render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(screen.queryByRole('button', { name: /See all/ })).toBeNull()
  })

  it('preview: no "See all" when the list fits, and null when it is empty', () => {
    items = [{ id: 'o/r#1', title: 'only', actions: [], status: 'waiting' }]
    const { unmount } = render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(screen.queryByRole('button', { name: /See all/ })).toBeNull()
    unmount()
    items = []
    const { container } = render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('full: every row, no heading, no scroll box, and a sentence when empty', () => {
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [], status: 'waiting' }))
    const { unmount, container } = render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getAllByRole('listitem')).toHaveLength(5)
    expect(screen.queryByRole('heading')).toBeNull()
    expect(container.querySelector('.overflow-y-auto')).toBeNull()
    unmount()
    items = []
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByText('Nothing needs you.')).toBeInTheDocument()
  })
})
