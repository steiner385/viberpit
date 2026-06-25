import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { SessionListControls } from '../SessionListControls'

describe('SessionListControls', () => {
  it('renders current selection and emits changes', async () => {
    const onGroupByChange = vi.fn()
    const onSortByChange = vi.fn()
    render(
      <SessionListControls
        groupBy="none"
        sortBy="status"
        onGroupByChange={onGroupByChange}
        onSortByChange={onSortByChange}
        searchQuery=""
        onSearchChange={() => {}}
      />,
    )
    await userEvent.selectOptions(screen.getByLabelText('Group by'), 'repo')
    expect(onGroupByChange).toHaveBeenCalledWith('repo')
    await userEvent.selectOptions(screen.getByLabelText('Sort by'), 'lastActive')
    expect(onSortByChange).toHaveBeenCalledWith('lastActive')
  })
})

function Harness() {
  const [q, setQ] = useState('')
  return (
    <SessionListControls
      groupBy="none" sortBy="status"
      onGroupByChange={() => {}} onSortByChange={() => {}}
      searchQuery={q} onSearchChange={setQ}
    />
  )
}

describe('SessionListControls injected options', () => {
  it('renders injected archived options', () => {
    render(
      <SessionListControls
        groupBy="account"
        sortBy="archived"
        onGroupByChange={() => {}}
        onSortByChange={() => {}}
        searchQuery=""
        onSearchChange={() => {}}
        groupOptions={[{ value: 'account', label: 'Account' }, { value: 'repo', label: 'Repo' }]}
        sortOptions={[{ value: 'archived', label: 'Archived date' }]}
      />,
    )
    expect(screen.getByRole('option', { name: 'Account' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Archived date' })).toBeInTheDocument()
  })
})

describe('SessionListControls search', () => {
  it('types into the search box and clears it', async () => {
    render(<Harness />)
    const box = screen.getByLabelText('Search sessions')
    await userEvent.type(box, 'auth')
    expect(box).toHaveValue('auth')
    await userEvent.click(screen.getByLabelText('Clear search'))
    expect(box).toHaveValue('')
  })

  it('hides the clear button when empty', () => {
    render(
      <SessionListControls
        groupBy="none" sortBy="status"
        onGroupByChange={() => {}} onSortByChange={() => {}}
        searchQuery="" onSearchChange={() => {}}
      />,
    )
    expect(screen.queryByLabelText('Clear search')).toBeNull()
  })
})
