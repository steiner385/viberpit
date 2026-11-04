import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { StarterChips, FollowUpChips } from './StarterChips'

// A chip carries the drawer's context, like every drawer send (D-12).
const CTX = { route: '/sessions/s1', sessionId: 's1' }

const runtime = vi.hoisted(() => ({
  onPrompt: vi.fn(),
  state: { turns: [] as object[], latestSuggestions: [] as string[], status: { state: 'done' } },
}))
vi.mock('@/features/sessions/SessionRuntimeProvider', () => ({ useSessionRuntimeContext: () => runtime }))

beforeEach(() => {
  runtime.onPrompt.mockClear()
  runtime.state.turns = []
  runtime.state.latestSuggestions = []
})

describe('assistant prompt chip provenance', () => {
  it('marks a starter as suggested instead of learning it as a typed reply', () => {
    render(<StarterChips starters={['What needs my attention?']} contextProvider={() => CTX} />)
    fireEvent.click(screen.getByRole('button', { name: 'What needs my attention?' }))
    expect(runtime.onPrompt).toHaveBeenCalledWith('What needs my attention?', [], CTX, 'suggested')
  })

  it('marks a generated follow-up as suggested', () => {
    runtime.state.turns = [{}]
    runtime.state.latestSuggestions = ['What should I do next?']
    render(<FollowUpChips contextProvider={() => CTX} />)
    fireEvent.click(screen.getByRole('button', { name: 'What should I do next?' }))
    expect(runtime.onPrompt).toHaveBeenCalledWith('What should I do next?', [], CTX, 'suggested')
  })
})
