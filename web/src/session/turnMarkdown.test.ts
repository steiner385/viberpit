import { describe, it, expect } from 'vitest'
import { turnMarkdown } from './turnMarkdown'
import type { Turn } from './reducer'

describe('turnMarkdown', () => {
  it('joins assistant text blocks around a tool with a blank line', () => {
    const t: Turn = {
      id: 't', role: 'assistant',
      blocks: [
        { kind: 'text', id: '1', text: 'a' },
        { kind: 'tool', id: '2', toolId: '2', name: 'Read', toolKind: 'read', input: {} },
        { kind: 'thinking', id: '3', text: 'hmm' },
        { kind: 'text', id: '4', text: 'b' },
      ],
    }
    expect(turnMarkdown(t)).toBe('a\n\nb')
  })
  it('returns a user prompt text', () => {
    expect(turnMarkdown({ id: 'u', role: 'user', blocks: [{ kind: 'text', id: '1', text: 'hello' }] })).toBe('hello')
  })
  it('is null for a tools-only assistant turn and an image-only user turn', () => {
    expect(turnMarkdown({
      id: 't', role: 'assistant',
      blocks: [{ kind: 'tool', id: '2', toolId: '2', name: 'Read', toolKind: 'read', input: {} }],
    })).toBeNull()
    expect(turnMarkdown({
      id: 'u', role: 'user', blocks: [{ kind: 'image', id: 'i', images: [] }],
    })).toBeNull()
  })
})
