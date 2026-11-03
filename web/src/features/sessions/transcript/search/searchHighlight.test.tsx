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
import { TranscriptSearchContext, type TranscriptSearchState } from './TranscriptSearchContext'
import { renderKit } from './searchRender'
import { MARK_CURRENT_ATTR } from './searchMarks'

beforeAll(() => prefetchMarkdown())

const noop = () => {}

type Search = { terms: string[]; current: TranscriptSearchState['current'] }

function renderTurn(turn: TurnType, s: Search | null) {
  const search: TranscriptSearchState | null = s && { ...s, kit: renderKit }
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
    const { container } = renderTurn(turn, { terms: ['widget'], current: null })
    expect(marks(container).map((m) => m.textContent)).toEqual(['Widget', 'widget'])
    // No current hit in this turn → no stronger mark.
    expect(container.querySelector(`[${MARK_CURRENT_ATTR}]`)).toBeNull()
  })

  it('gives the current hit\'s first lead-term match the stronger style', () => {
    const turn: TurnType = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:t', text: 'alpha beta alpha' }] }
    const { container } = renderTurn(turn, { terms: ['alpha', 'beta'], current: { turnId: 'u1', lead: 0 } })
    const current = container.querySelectorAll(`[${MARK_CURRENT_ATTR}]`)
    expect(current).toHaveLength(1)
    expect(current[0].textContent).toBe('alpha')
    expect(current[0]).toBe(marks(container)[0])
  })

  it('marks assistant markdown prose but never a fenced code block or inline code', async () => {
    const text = 'The **widget** works.\n\n```ts\nconst widget = 1\n```\n\nCall `widget` now.'
    const turn: TurnType = { id: 'a1', role: 'assistant', blocks: [{ kind: 'text', id: 'a1:t', text }] }
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'a1', lead: 0 } })
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
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'a1', lead: 0 } })
    expect(container.textContent).toContain('widget')
    expect(marks(container)).toHaveLength(0)
  })

  it('the current hit is the turn\'s k-th lead run, counted across its prose blocks', () => {
    // Two user text blocks: "widget" twice, then once. Lead 2 is the third — in block 2.
    const turn: TurnType = {
      id: 'u1', role: 'user', blocks: [
        { kind: 'text', id: 'u1:a', text: 'one widget, two widget' },
        { kind: 'text', id: 'u1:b', text: 'three widget' },
      ],
    }
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'u1', lead: 2 } })
    const all = marks(container)
    expect(all).toHaveLength(3)
    const current = container.querySelectorAll(`[${MARK_CURRENT_ATTR}]`)
    expect(current).toHaveLength(1)
    expect(current[0]).toBe(all[2])
  })

  it('in markdown the k-th lead run is current too', async () => {
    const turn: TurnType = { id: 'a1', role: 'assistant', blocks: [{ kind: 'text', id: 'a1:t', text: 'A widget, then *another widget*.' }] }
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'a1', lead: 1 } })
    await screen.findAllByText('widget', { selector: 'mark' })
    const all = marks(container)
    expect(all).toHaveLength(2)
    expect(all[1]).toHaveAttribute(MARK_CURRENT_ATTR, 'true')
    expect(all[0]).not.toHaveAttribute(MARK_CURRENT_ATTR)
  })

  it('a block whose only occurrence is inside code gets no current hit — the next block does', async () => {
    const turn: TurnType = {
      id: 'a1', role: 'assistant', blocks: [
        { kind: 'text', id: 'a1:a', text: 'Call `widget` here.' },
        { kind: 'text', id: 'a1:b', text: 'Then the widget works.' },
      ],
    }
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'a1', lead: 0 } })
    const current = await screen.findByText('widget', { selector: `mark[${MARK_CURRENT_ATTR}]` })
    expect(current.closest('p')?.textContent).toBe('Then the widget works.')
    expect(container.querySelectorAll(`[${MARK_CURRENT_ATTR}]`)).toHaveLength(1)
  })

  it('a current lead past the last rendered occurrence falls back to the last one (markdown)', async () => {
    const turn: TurnType = { id: 'a1', role: 'assistant', blocks: [{ kind: 'text', id: 'a1:t', text: 'A widget and a widget.' }] }
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'a1', lead: 5 } })
    await screen.findAllByText('widget', { selector: 'mark' })
    const all = marks(container)
    expect(all[1]).toHaveAttribute(MARK_CURRENT_ATTR, 'true')
    expect(container.querySelectorAll(`[${MARK_CURRENT_ATTR}]`)).toHaveLength(1)
  })

  it('a current lead past the last occurrence falls back to the last one (plain text)', () => {
    const turn: TurnType = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:t', text: 'one widget, two widget' }] }
    const { container } = renderTurn(turn, { terms: ['widget'], current: { turnId: 'u1', lead: 9 } })
    const all = marks(container)
    expect(all[1]).toHaveAttribute(MARK_CURRENT_ATTR, 'true')
    expect(container.querySelectorAll(`[${MARK_CURRENT_ATTR}]`)).toHaveLength(1)
  })

  it('renders no marks when no search is open', () => {
    const turn: TurnType = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:t', text: 'widget' }] }
    const { container } = renderTurn(turn, null)
    expect(marks(container)).toHaveLength(0)
  })
})
