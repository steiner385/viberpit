import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SubagentResult } from '../SubagentResult'

describe('SubagentResult', () => {
  it('renders the subagent final text as markdown (bold is emphasized, not literal)', () => {
    render(<SubagentResult payload={{ content: 'The fix is **defense in depth**.' }} />)
    const strong = screen.getByText('defense in depth')
    expect(strong.tagName).toBe('STRONG')
    // The markdown asterisks must not appear literally.
    expect(screen.queryByText(/\*\*defense/)).toBeNull()
  })

  it('joins a text-block-array final message', () => {
    render(<SubagentResult payload={{ content: [{ type: 'text', text: 'agent done' }] }} />)
    expect(screen.getByText('agent done')).toBeInTheDocument()
  })

  it('falls back to the generic renderer for structured (non-text) payloads', () => {
    render(<SubagentResult payload={{ summary: 'x', count: 2 }} />)
    expect(screen.getByText('summary')).toBeInTheDocument()
    expect(screen.getByText('count')).toBeInTheDocument()
  })
})
