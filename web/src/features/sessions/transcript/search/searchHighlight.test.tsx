import { beforeAll, describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('@/api/assistant', () => ({
  useAssistantFeedback: vi.fn(() => ({ mutate: vi.fn() })),
}))
vi.mock('@/features/sessions/SessionRuntimeProvider', () => ({
  useSessionRuntimeContext: vi.fn(() => ({ sessionId: 'sess-1' })),
}))

import { Turn } from '../Turn'
import type { Turn as TurnType, Block } from '@/session/reducer'
import { prefetchMarkdown } from '../markdownChunk'
import { TranscriptSearchContext, MARK_CURRENT_ATTR, type TranscriptSearchState } from './TranscriptSearchContext'

beforeAll(() => prefetchMarkdown())

const noop = () => {}

function renderTurn(turn: TurnType, search: TranscriptSearchState | null) {
  return render(
    <TranscriptSearchContext.Provider value={search}>
      <Turn turn={turn} isLastTurn={false} toolDetailMode="all-open" thinkingCollapsed={false} onAnswer={noop} />
    </TranscriptSearchContext.Provider>,
  )
}

const marks = (root: HTMLElement) => Array.from(root.querySelectorAll('mark'))

describe('transcript search highlight', () => {
  it('marks every occurrence in a user prompt, case-insensitively', () => {
    const turn: TurnType = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:t', text: 'Deploy the Widget, then widget again' }] }
    const { container } = renderTurn(turn, { terms: ['widget'], currentTurnId: null })
    expect(marks(container).map((m) => m.textContent)).toEqual(['Widget', 'widget'])
    // No current hit in this turn → no stronger mark.
    expect(container.querySelector(`[${MARK_CURRENT_ATTR}]`)).toBeNull()
  })

  it('gives the current hit\'s first lead-term match the stronger style', () => {
    const turn: TurnType = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:t', text: 'alpha beta alpha' }] }
    const { container } = renderTurn(turn, { terms: ['alpha', 'beta'], currentTurnId: 'u1' })
    const current = container.querySelectorAll(`[${MARK_CURRENT_ATTR}]`)
    expect(current).toHaveLength(1)
    expect(current[0].textContent).toBe('alpha')
    expect(current[0]).toBe(marks(container)[0])
  })

  it('marks assistant markdown prose but never a fenced code block or inline code', async () => {
    const text = 'The **widget** works.\n\n```ts\nconst widget = 1\n```\n\nCall `widget` now.'
    const turn: TurnType = { id: 'a1', role: 'assistant', blocks: [{ kind: 'text', id: 'a1:t', text }] }
    const { container } = renderTurn(turn, { terms: ['widget'], currentTurnId: 'a1' })
    expect((await screen.findByText('widget', { selector: 'mark' })).tagName).toBe('MARK')
    const all = marks(container)
    expect(all).toHaveLength(1)
    expect(all[0].closest('strong')).not.toBeNull()
    expect(all[0]).toHaveAttribute(MARK_CURRENT_ATTR, 'true')
    for (const code of Array.from(container.querySelectorAll('code, pre'))) {
      expect(code.querySelector('mark')).toBeNull()
    }
  })

  it('never marks inside a tool block or thinking', () => {
    const blocks: Block[] = [
      { kind: 'thinking', id: 'a1:th', text: 'thinking about widget' },
      {
        kind: 'tool', id: 'a1:tool', toolId: 'a1:tool', name: 'Bash', toolKind: 'bash',
        input: { command: 'echo widget' }, result: { ok: true, payload: { stdout: 'widget' } },
      },
    ]
    const turn: TurnType = { id: 'a1', role: 'assistant', blocks }
    const { container } = renderTurn(turn, { terms: ['widget'], currentTurnId: 'a1' })
    expect(container.textContent).toContain('widget')
    expect(marks(container)).toHaveLength(0)
  })

  it('renders no marks when no search is open', () => {
    const turn: TurnType = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:t', text: 'widget' }] }
    const { container } = renderTurn(turn, null)
    expect(marks(container)).toHaveLength(0)
  })
})
