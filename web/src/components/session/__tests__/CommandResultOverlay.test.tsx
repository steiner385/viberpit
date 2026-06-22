// web/src/components/session/__tests__/CommandResultOverlay.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CommandResultOverlay } from '@/components/session/CommandResultOverlay'
import type { Command } from '@/session/events'

const CMDS: Command[] = [{ name: 'help', description: 'List commands', argumentHint: null, kind: 'client' }]

describe('CommandResultOverlay', () => {
  it('renders the help list', () => {
    render(<CommandResultOverlay result={{ render: 'help', data: null }} commands={CMDS} onClose={vi.fn()} />)
    expect(screen.getByText('help')).toBeInTheDocument()
    expect(screen.getByText('List commands')).toBeInTheDocument()
  })

  it('renders context usage', () => {
    render(<CommandResultOverlay result={{ render: 'context', data: { percentage: 42, totalTokens: 1000, maxTokens: 2000 } }} commands={CMDS} onClose={vi.fn()} />)
    expect(screen.getByText(/42/)).toBeInTheDocument()
  })

  it('renders nothing when result is null', () => {
    const { container } = render(<CommandResultOverlay result={null} commands={CMDS} onClose={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
