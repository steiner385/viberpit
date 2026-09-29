import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SessionMeta } from './SessionMeta'

// 2026-09-29 re-critique (P2): on a phone the meta strip wrapped three rows
// deep (114px) above the transcript. In a narrow pane the identity/settings
// readouts fold behind "Details"; the live status, export, actions and the
// sub-agent toggle stay on the visible row. The fold keys on the strip's own
// width (`@container` + `@max-md:`), the same mechanism as the list controls.
// jsdom applies no CSS, so this pins the contract (aria wiring, the variant
// classes, what is inside vs outside the fold), not the layout.
function renderMeta() {
  return render(
    <MemoryRouter>
      <SessionMeta
        sessionId="s1" account="alice" host="rocky" model={null} mode="default"
        contextPct={81} quota={[{ key: 'session', label: 'session', pct: 4, resetsAt: null, durationSeconds: 18000 }]}
        subAgents={[]} statusSlot={<span data-testid="status-slot">Working…</span>}
        exportSlot={<button type="button">Export</button>}
        actionsSlot={<button type="button">Archive</button>}
        models={[]} accounts={[{ name: 'alice' }]} accountPinned={false}
        onSelectModel={vi.fn()} onSelectAccount={vi.fn()} onSetMode={vi.fn()}
      />
    </MemoryRouter>,
  )
}

describe('SessionMeta — narrow-strip fold', () => {
  it('folds the identity and settings readouts behind a Details toggle, collapsed by default', async () => {
    const { container } = renderMeta()
    expect(container.querySelector('.\\@container')).not.toBeNull()
    // Distinct name: the transcript's tool cards carry their own "Details" toggles.
    const toggle = screen.getByRole('button', { name: 'Session details' })
    expect(toggle).toHaveTextContent('Details')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveClass('hidden', '@max-md:inline-flex')
    const fold = document.getElementById(toggle.getAttribute('aria-controls')!)!
    expect(fold).toHaveClass('contents', '@max-md:hidden')
    // Inside the fold: the readouts and settings.
    expect(within(fold).getByLabelText('Switch account')).toBeInTheDocument()
    expect(within(fold).getByLabelText('Select model')).toBeInTheDocument()
    expect(within(fold).getByLabelText('Runner host')).toBeInTheDocument()
    expect(within(fold).getByLabelText('Remaining context')).toBeInTheDocument()
    expect(within(fold).getByLabelText('Account quota')).toBeInTheDocument()
    expect(within(fold).getByRole('switch', { name: 'Plan mode' })).toBeInTheDocument()
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(fold).not.toHaveClass('@max-md:hidden')
  })

  it('keeps the live status, export, actions and sub-agent toggle OUTSIDE the fold', () => {
    renderMeta()
    const fold = screen.getByTestId('session-meta-details')
    expect(within(fold).queryByTestId('status-slot')).toBeNull()
    expect(within(fold).queryByRole('button', { name: 'Export' })).toBeNull()
    expect(within(fold).queryByRole('button', { name: 'Archive' })).toBeNull()
    expect(screen.getByTestId('status-slot')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Archive' })).toBeInTheDocument()
  })
})
