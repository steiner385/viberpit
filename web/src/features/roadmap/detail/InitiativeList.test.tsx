// `InitiativeList`'s `nest()`: a pure DFS over `parentInitiative`, not a
// one-level flatten. A chain deeper than program → phase must still emit
// every input exactly once, and a cyclic payload must not hang the render.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ini } from '../__fixtures__/map'
import { initiativeKey } from '../treeRows'
import { InitiativeList } from './InitiativeList'
import { ViewportProvider } from '@/shell/useViewport'

const mount = (initiatives: Parameters<typeof InitiativeList>[0]['initiatives']) =>
  render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <InitiativeList initiatives={initiatives} />
      </MemoryRouter>
    </ViewportProvider>,
  )

describe('InitiativeList — nest()', () => {
  it('a three-level chain renders all three, not just the root and its direct child', () => {
    const chain = [
      ini({ slug: 'a', title: 'Program A', projectSlug: 'kindash', hasChildren: true }),
      ini({ slug: 'b', title: 'Phase B', projectSlug: 'kindash', parentInitiative: 'a', hasChildren: true }),
      ini({ slug: 'c', title: 'Sub-phase C', projectSlug: 'kindash', parentInitiative: 'b' }),
    ]
    mount(chain)
    expect(screen.getByTestId(`row-${initiativeKey('a')}`)).toBeInTheDocument()
    expect(screen.getByTestId(`row-${initiativeKey('b')}`)).toBeInTheDocument()
    expect(screen.getByTestId(`row-${initiativeKey('c')}`)).toBeInTheDocument()
  })

  it('a cyclic payload (a duplicate slug pointing back up the chain) does not hang, and renders each slug once', () => {
    // A malformed payload: `a` is a genuine root, `b` is its phase, and a
    // SECOND object also slugged `a` claims to be `b`'s phase — closing a
    // cycle back to the root's own slug. Without the `seen` guard, walking
    // into that duplicate would look its slug back up in `byParent` and
    // recurse forever.
    const cyclic = [
      ini({ slug: 'a', title: 'A', projectSlug: 'kindash', hasChildren: true }),
      ini({ slug: 'b', title: 'B', projectSlug: 'kindash', parentInitiative: 'a', hasChildren: true }),
      ini({ slug: 'a', title: 'A (duplicate, cycles back)', projectSlug: 'kindash', parentInitiative: 'b' }),
    ]
    mount(cyclic)
    // Terminates (no hang) and each slug is walked at most once — the
    // duplicate `a` is dropped rather than looped on.
    expect(screen.getAllByTestId(`row-${initiativeKey('a')}`)).toHaveLength(1)
    expect(screen.getAllByTestId(`row-${initiativeKey('b')}`)).toHaveLength(1)
  })
})
