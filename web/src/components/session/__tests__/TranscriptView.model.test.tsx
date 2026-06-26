import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { TranscriptView } from '../TranscriptView'
import type { Turn } from '@/session/reducer'

function turn(id: string, model: string | null): Turn {
  return { id, role: 'assistant', blocks: [{ kind: 'text', id: `${id}:t`, text: id }], model }
}

const base = {
  toolDetailMode: 'auto' as const,
  thinkingCollapsed: true,
  onAnswer: () => {},
}

describe('TranscriptView model switch divider', () => {
  it('shows a divider only when the model changes', () => {
    render(<TranscriptView {...base} turns={[turn('t1', 'claude-opus-4-8'), turn('t2', 'claude-haiku-4-5')]} />)
    expect(screen.getByRole('separator')).toBeInTheDocument()
    expect(screen.getByText(/Haiku 4\.5/)).toBeInTheDocument()
  })
  it('shows no divider when the model is unchanged', () => {
    render(<TranscriptView {...base} turns={[turn('t1', 'claude-opus-4-8'), turn('t2', 'claude-opus-4-8')]} />)
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
  })
  it('shows no divider on the first model-bearing turn', () => {
    render(<TranscriptView {...base} turns={[turn('t1', 'claude-opus-4-8')]} />)
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
  })
})
