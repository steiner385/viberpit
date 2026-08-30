import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { trackState, InitiativeTrack } from '../InitiativeTrack'

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
})
