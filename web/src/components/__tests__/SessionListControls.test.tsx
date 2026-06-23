import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
      />,
    )
    await userEvent.selectOptions(screen.getByLabelText('Group by'), 'repo')
    expect(onGroupByChange).toHaveBeenCalledWith('repo')
    await userEvent.selectOptions(screen.getByLabelText('Sort by'), 'lastActive')
    expect(onSortByChange).toHaveBeenCalledWith('lastActive')
  })
})
