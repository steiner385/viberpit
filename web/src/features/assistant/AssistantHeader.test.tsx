import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ApiRequestError } from '@/api/client'
import { AssistantHeader } from './AssistantHeader'

const setModel = vi.fn()
const state = {
  status: { enabled: true, sessionId: 's1' as string | null, model: 'sonnet' },
  setModel: { isError: false, error: null as ApiRequestError | null, isPending: false },
}

vi.mock('@/api/assistant', () => ({
  useAssistantStatus: () => ({ data: state.status }),
  useResetAssistant: () => ({ mutateAsync: vi.fn() }),
  useSetAssistantModel: () => ({ mutateAsync: setModel, ...state.setModel }),
}))
vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [{ id: 'sonnet', label: 'Sonnet' }, { id: 'opus', label: 'Opus' }] }),
}))

beforeEach(() => {
  setModel.mockReset()
  state.status = { enabled: true, sessionId: 's1', model: 'sonnet' }
  state.setModel = { isError: false, error: null, isPending: false }
})

describe('AssistantHeader model picker', () => {
  it('posts the chosen model for the assistant session', async () => {
    render(<AssistantHeader />)
    fireEvent.change(screen.getByLabelText('Assistant model'), { target: { value: 'opus' } })
    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith({ sessionId: 's1', model: 'opus' }))
  })

  it('shows the current model as the selected option', () => {
    render(<AssistantHeader />)
    expect(screen.getByLabelText<HTMLSelectElement>('Assistant model').value).toBe('sonnet')
  })

  it('falls back to a read-only label before a session exists', () => {
    state.status = { enabled: true, sessionId: null, model: 'sonnet' }
    render(<AssistantHeader />)
    // Nothing to switch yet — a picker here would post to /sessions/null/model.
    expect(screen.queryByLabelText('Assistant model')).toBeNull()
    expect(screen.getByText('Sonnet')).toBeTruthy()
  })

  it('explains a gated model rather than showing a generic failure', () => {
    state.setModel = {
      isError: true, isPending: false,
      // The real envelope from POST /sessions/{id}/model — `detail`, not `error`
      // (server/api/sessions.py::_action_error_response).
      error: new ApiRequestError(400, { detail: { code: 'MODEL_GATED', message: 'nope' } }),
    }
    render(<AssistantHeader />)
    expect(screen.getByRole('alert').textContent).toContain('needs approval')
  })

  it('falls back to a generic message for any other failure', () => {
    state.setModel = {
      isError: true, isPending: false,
      error: new ApiRequestError(500, {}),
    }
    render(<AssistantHeader />)
    expect(screen.getByRole('alert').textContent).toContain('Couldn’t switch model')
  })
})
