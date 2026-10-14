// The product row's Needs-you badge counts only what a human alone can unblock;
// waiting work is said in quiet text, never badged (design 2026-10-14 §1).
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ProductCounts } from '@/api/roadmap'
import { ViewportProvider } from '@/shell/useViewport'
import { cap, ini, product } from '../__fixtures__/map'
import { TreeRowCard } from './TreeRowCard'

const counts = (over: Partial<ProductCounts> = {}): ProductCounts => ({
  inFlight: 0, blocked: 0, needsYou: 0, capabilities: 1, awaitingTriage: 0, needsHome: 0, ...over,
})
const WAITING = {
  blocked: 31, boardBlocked: 0, boardHeld: 0, parked: 28, epicOpen: 8, inProgress: 0, tracker: 0,
  notNeeded: 0, assigned: 0, offQueue: 0, done: 0, bot: 0, untrusted: 0, environment: 0,
}

function mount(p: ReturnType<typeof product>) {
  return render(
    <ViewportProvider kiosk={null}>
      <TreeRowCard
        row={{ kind: 'product', key: `product:${p.slug}`, depth: 0, product: p, expanded: true, hasChildren: false }}
        selected={false} focusable onOpen={() => {}} onToggle={() => {}} onOpenInbox={() => {}}
      />
    </ViewportProvider>,
  )
}

describe('TreeRowCard product row', () => {
  it('badges only needs-you work and says the waiting counters as text', () => {
    const p = product({
      slug: 'kindash', name: 'KinDash', tier: 'needs-you',
      counts: counts({ blocked: 2, waiting: WAITING }),
      capabilities: [cap({ slug: 'c', counts: { openIssues: 3, inFlight: 0, needsYou: 1, blocked: 2 } })],
      initiatives: [ini({ slug: 'stuck', projectSlug: 'kindash', tier: 'blocked' })],
    })
    mount(p)
    // 1 needs-you issue; the 2 blocked issues and the blocked initiative wait.
    expect(screen.getByText('Needs you (1)')).toBeInTheDocument()
    const summary = screen.getByTestId('waiting-summary')
    expect(summary).toHaveTextContent('Waiting: 31 blocked by a dependency · 28 parked · 8 epic with open sub-issues')
    expect(summary).toHaveAttribute('title', 'Waiting:\n31 blocked by a dependency\n28 parked\n8 epic with open sub-issues')
    // Quiet text, not a pill.
    expect(summary.className).not.toContain('rounded-pill')
  })

  it('says nothing about waiting when an older server sends no counters, or all are zero', () => {
    const { unmount } = mount(product({ slug: 'old', counts: counts() }))
    expect(screen.queryByTestId('waiting-summary')).toBeNull()
    unmount()
    mount(product({ slug: 'quiet', counts: counts({ waiting: { ...WAITING, blocked: 0, parked: 0, epicOpen: 0 } }) }))
    expect(screen.queryByTestId('waiting-summary')).toBeNull()
  })
})
