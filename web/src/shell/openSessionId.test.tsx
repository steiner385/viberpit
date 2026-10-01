import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_SESSION_ROUTE, ARCHIVED_PATH, SESSIONS_PATH } from '@/session/sessionNavState'
import {
  CAPABILITY_ROUTE, INITIATIVE_ROUTE, PRODUCT_ROUTE, ROADMAP_PATH,
} from '@/features/roadmap/roadmapNavState'
import {
  AUTOMATION_ROUTE, CAPACITY_ROUTE, CI_REPO_ROUTE, DELIVERY_PATH,
} from '@/features/delivery/deliveryNavState'
import type { OpenItem } from './openItems'
import { useOpenItem, useOpenSessionId } from './openSessionId'

/** One line per `OpenItem` kind. Exhaustive on purpose: a new kind that this
 *  probe cannot describe fails to compile rather than reading as `none`. */
function describeItem(item: OpenItem | null): string {
  if (!item) return 'none'
  switch (item.kind) {
    case 'session':
    case 'archived': return `${item.kind}:${item.id}`
    case 'product': return `product:${item.productSlug}`
    case 'capability': return `capability:${item.productSlug}/${item.capSlug}`
    case 'initiative': return `initiative:${item.initiativeSlug}`
    case 'proposal': return `proposal:${item.proposalId}`
    case 'portfolio': return 'portfolio'
    case 'automation': return `automation:${item.automationId}`
    case 'ciRepo': return `ciRepo:${item.repo}`
    case 'capacity': return 'capacity'
  }
}

function Probe() {
  const item = useOpenItem()
  const sessionId = useOpenSessionId()
  return <div data-testid="probe">{`${describeItem(item)}|${sessionId ?? '-'}`}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {[
          SESSIONS_PATH, SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_PATH, ARCHIVED_SESSION_ROUTE,
          '/pipeline',
          ROADMAP_PATH, PRODUCT_ROUTE, CAPABILITY_ROUTE, INITIATIVE_ROUTE,
          DELIVERY_PATH, AUTOMATION_ROUTE, CI_REPO_ROUTE, CAPACITY_ROUTE,
        ].map((p) => (
          <Route key={p} path={p} element={<Probe />} />
        ))}
      </Routes>
    </MemoryRouter>,
  )
}

describe('useOpenItem', () => {
  it.each([
    ['/sessions', 'none|-'],
    ['/sessions/abc', 'session:abc|abc'],
    ['/sessions/abc/agents/x', 'session:abc|abc'],
    ['/sessions/archived', 'none|-'],
    ['/pipeline', 'none|-'],
    // Roadmap detail routes report an open item and NEVER an open session: the
    // resolver discriminates on `productSlug`/`capSlug`/`initiativeSlug`, so no
    // roadmap URL can attach a live session runtime.
    // Bare `/roadmap` is the portfolio PAGE, open in its own right (it holds a
    // phone's band); a list scope is nothing open. Neither is a session.
    ['/roadmap', 'portfolio|-'],
    ['/roadmap?scope=map', 'none|-'],
    ['/roadmap/kindash', 'product:kindash|-'],
    ['/roadmap/kindash/capabilities/tasks', 'capability:kindash/tasks|-'],
    ['/initiatives/views', 'initiative:views|-'],
    ['/roadmap?scope=inbox&proposal=p1', 'proposal:p1|-'],
    // Delivery detail routes, same property: the resolver discriminates on
    // `automationId` / `owner`+`repo`, so no Delivery URL can attach a live
    // session runtime either.
    ['/delivery', 'none|-'],
    ['/delivery/automations/pr-doctor', 'automation:pr-doctor|-'],
    ['/delivery/ci/o/r', 'ciRepo:o/r|-'],
    // The Capacity pane as an item (2026-10-01 Delivery re-critique, P0); an
    // unknown pane name is not an item.
    ['/delivery/capacity/governor', 'capacity|-'],
    ['/delivery/capacity/bogus', 'none|-'],
  ])('%s → %s', (path, expected) => {
    renderAt(path)
    expect(screen.getByTestId('probe')).toHaveTextContent(expected)
  })

  it('reports an archived transcript as open, but never as an open SESSION', () => {
    // Both halves matter: the shell must know something is open (so a phone
    // shows the reader instead of the list), and PageHost must not attach a
    // live runtime to it.
    renderAt('/sessions/archived/abc')
    expect(screen.getByTestId('probe')).toHaveTextContent('archived:abc|-')
  })
})
