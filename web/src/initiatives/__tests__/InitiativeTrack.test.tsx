import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { trackState, InitiativeTrack } from '../InitiativeTrack'
import type { InitiativeEpic } from '@/api/sdlc'

describe('trackState', () => {
  it('marks stages before current done, current active, rest pending', () => {
    const nodes = trackState('plan', 'active')
    // intake, design done; plan active; build, deploy, maintain, done pending
    expect(nodes.map((n) => n.status)).toEqual(
      ['done', 'done', 'active', 'pending', 'pending', 'pending', 'pending'])
    expect(nodes.map((n) => n.label)).toEqual(
      ['Intent', 'Spec', 'Plan', 'Build', 'Deploy', 'Maintain', 'Done'])
  })

  it('renders the current stage as parked when blocked', () => {
    expect(trackState('build', 'blocked')[3]!.status).toBe('parked')
  })

  it('renders the terminal stage as done, not active', () => {
    const nodes = trackState('done', 'done')
    expect(nodes[6]!.status).toBe('done')
    expect(nodes.every((n) => n.status === 'done')).toBe(true)
  })

  it('yields an all-pending track for an unknown stage', () => {
    expect(trackState('bogus', 'proposed').every((n) => n.status === 'pending')).toBe(true)
  })
})

describe('InitiativeTrack', () => {
  it('renders one labeled node per lifecycle stage', () => {
    const { container } = render(<InitiativeTrack stage="design" status="active" />)
    expect(container.querySelectorAll('.node')).toHaveLength(7)
    expect(container.querySelector('.node.active .node-label')?.textContent).toBe('Spec')
  })

  it('renders exactly as before when no epic is present', () => {
    const withEpic = render(<InitiativeTrack stage="build" status="active" />)
    const without = render(<InitiativeTrack stage="build" status="active" epic={null} />)
    expect(withEpic.container.innerHTML).toBe(without.container.innerHTML)
    expect(withEpic.container.querySelector('.node.active .node-label')?.textContent).toBe('Build')
    expect(withEpic.container.querySelector('a')).toBeNull()
  })

  it('shows the epic sub-issue count on the Build node and deep-links to GitHub', () => {
    const epic: InitiativeEpic = {
      ref: 'o/r#997', url: 'https://github.com/o/r/issues/997',
      total: 10, done: 3, blocked: 1, needsHuman: 0, children: [],
    }
    const { container } = render(<InitiativeTrack stage="build" status="active" epic={epic} />)
    expect(container.querySelector('.node.active .node-label')?.textContent).toBe('Build 3/10')
    const link = container.querySelector('a')
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute('href', 'https://github.com/o/r/issues/997')
  })

  it('exposes the epic deep link to keyboard and screen-reader users', () => {
    // Regression coverage for review round 1 IMPORTANT 2: the link must not be
    // both aria-hidden *and* pulled out of tab order (that combination made it
    // reachable by neither), and it needs its own accessible name since
    // "Build 3/10" alone doesn't say what activating it does.
    const epic: InitiativeEpic = {
      ref: 'o/r#997', url: 'https://github.com/o/r/issues/997',
      total: 10, done: 3, blocked: 1, needsHuman: 0, children: [],
    }
    const { container, getByRole } = render(
      <InitiativeTrack stage="build" status="active" epic={epic} />)

    // Findable via getByRole, which testing-library excludes for anything
    // under an aria-hidden ancestor by default — this fails if the wrapping
    // node span is aria-hidden again.
    const link = getByRole('link', { name: /view epic on GitHub/i })
    expect(link).toHaveAttribute('href', 'https://github.com/o/r/issues/997')
    // Not pulled out of the tab order.
    expect(link).not.toHaveAttribute('tabindex', '-1')
    // The Build node's own wrapper is no longer aria-hidden (every other node
    // still is — only the interactive one changes).
    const buildNode = container.querySelector('.node.active')
    expect(buildNode).not.toHaveAttribute('aria-hidden')
    const otherNodes = [...container.querySelectorAll('.node')].filter((n) => n !== buildNode)
    expect(otherNodes.every((n) => n.getAttribute('aria-hidden') === 'true')).toBe(true)

    // The container's aria-label also mentions the epic progress and the link.
    expect(container.querySelector('.track')?.getAttribute('aria-label')).toContain(
      'epic progress 3 of 10 done, view epic on GitHub')
  })
})
