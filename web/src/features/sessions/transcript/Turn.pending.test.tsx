/**
 * A pending user turn renders its bubble PLUS the stage note. Confirmed turns must
 * render exactly as before — the note is strictly additive.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Turn as TurnType } from '@/session/reducer'
import { Turn } from './Turn'

vi.mock('@/api/hooks', () => ({ useSessions: () => ({ data: [] }) }))

function renderTurn(turn: TurnType) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Turn turn={turn} isLastTurn toolDetailMode="auto" thinkingCollapsed={false} onAnswer={vi.fn()} />
    </QueryClientProvider>,
  )
}

const userTurn = (id: string, text: string, pending?: TurnType['pending']): TurnType => ({
  id, role: 'user', blocks: [{ kind: 'text', id: `${id}:u`, text }], ...(pending ? { pending } : {}),
})

describe('Turn — pending prompt', () => {
  it('renders the stage note under a pending user turn', () => {
    renderTurn(userTurn('pending:0', 'wake up', 'resuming'))
    expect(screen.getByText('wake up')).toBeInTheDocument()
    expect(screen.getByText(/waking session/i)).toBeInTheDocument()
  })

  it('renders no stage note on a confirmed user turn', () => {
    renderTurn(userTurn('real-1', 'already echoed'))
    expect(screen.getByText('already echoed')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('renders no stage note on an assistant turn', () => {
    renderTurn({ id: 'a1', role: 'assistant', blocks: [{ kind: 'text', id: 'a1:t', text: 'hi' }] })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
