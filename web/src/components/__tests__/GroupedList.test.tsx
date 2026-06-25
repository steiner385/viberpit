import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GroupedList } from '../GroupedList'
import type { SessionGroup } from '@/session/ordering'

interface Row { id: string }
const groups: SessionGroup<Row>[] = [
  { key: 'x', label: 'X', count: 1, sessions: [{ id: 'a' }] },
  { key: 'y', label: 'Y', count: 1, sessions: [{ id: 'b' }] },
]

describe('GroupedList', () => {
  it('renders a header per labelled group and its rows', () => {
    render(<GroupedList groups={groups} renderRow={(r) => <div key={r.id}>row-{r.id}</div>} />)
    expect(screen.getByText('X')).toBeInTheDocument()
    expect(screen.getByText('row-a')).toBeInTheDocument()
    expect(screen.getByText('row-b')).toBeInTheDocument()
  })

  it('hides rows of a collapsed group', () => {
    render(
      <GroupedList
        groups={groups}
        renderRow={(r) => <div key={r.id}>row-{r.id}</div>}
        collapsedKeys={new Set(['x'])}
      />,
    )
    expect(screen.queryByText('row-a')).not.toBeInTheDocument()
    expect(screen.getByText('row-b')).toBeInTheDocument()
  })
})
