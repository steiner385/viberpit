import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { AssistantScope } from './AssistantScope'

vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: [{ id: 'x', name: 'Fix flaky test', status: 'in_progress' }] }),
}))

describe('AssistantScope with no context-bearing turn yet', () => {
  it('names the open session while idle: the first send sets the scope', () => {
    render(<AssistantScope current="x" scope={undefined} inFlight={false} />)
    expect(screen.getByText(/About:/)).toHaveTextContent('About: Fix flaky test')
  })

  it('claims no scope while a turn is running or queued', () => {
    // The running turn carried no session context, so the server holds no scope: saying
    // "About: <open session>" would claim a guard that is not in force.
    render(<AssistantScope current="x" scope={undefined} inFlight={true} />)
    expect(screen.queryByText(/About:/)).toBeNull()
    expect(screen.getByText(/Next message will be about/)).toHaveTextContent(
      'Next message will be about Fix flaky test')
  })

  it('still shows the server scope while in flight once a turn named one', () => {
    render(<AssistantScope current="x" scope="x" inFlight={true} />)
    expect(screen.getByText(/About:/)).toHaveTextContent('About: Fix flaky test')
    expect(screen.queryByText(/Next message/)).toBeNull()
  })
})
