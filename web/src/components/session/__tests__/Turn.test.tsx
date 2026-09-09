import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Turn } from '../Turn'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import type { Turn as TurnType, Block, TranscriptState } from '@/session/reducer'

function tool(id: string): Block {
  return {
    kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read',
    input: { file_path: `/f/${id}` }, result: { ok: true, payload: { content: 'x' } },
  }
}

function makeTurn(blocks: Block[]): TurnType {
  return { id: 'turn-1', role: 'assistant', blocks }
}

const noop = () => {}

describe('Turn tool grouping', () => {
  it('renders one line per completed tool — no rollup — when NOT the last turn', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        isLastTurn={false}
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    const rows = screen.getAllByRole('button', { name: /tool details/i })
    expect(rows).toHaveLength(4)
    // Auto + not live tail → every result stays collapsed.
    for (const row of rows) expect(row).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the tool run expanded when this IS the last turn (live tail)', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('renders text blocks between separate tool groups in order', () => {
    const blocks: Block[] = [tool('a'), { kind: 'text', id: 't', text: 'hello world' }, tool('b'), tool('c')]
    render(
      <Turn
        turn={makeTurn(blocks)}
        isLastTurn={false}
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.getByText('hello world')).toBeInTheDocument()
  })
})

describe('Turn rendering edge cases', () => {
  it('renders nothing for a turn with no blocks (no stray padded gap)', () => {
    const { container } = render(
      <Turn
        turn={makeTurn([])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the open assistant block as raw text while it is streaming', () => {
    const blocks: Block[] = [{ kind: 'text', id: 't1', text: '**bold**' }]
    render(
      <Turn
        turn={makeTurn(blocks)}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        streamingBlockId="t1"
        onAnswer={noop}
      />,
    )
    expect(screen.getByText('**bold**')).toBeInTheDocument()
    expect(document.querySelector('strong')).toBeNull()
  })

  it('renders a finalized (non-streaming) assistant block as markdown', () => {
    const blocks: Block[] = [{ kind: 'text', id: 't1', text: '**bold**' }]
    render(
      <Turn
        turn={makeTurn(blocks)}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        streamingBlockId={null}
        onAnswer={noop}
      />,
    )
    expect(document.querySelector('strong')).not.toBeNull()
  })
})

describe('Turn user-vs-agent styling', () => {
  // The one visual contract the transcript makes (rich-session-rendering design):
  // a human prompt is a right-aligned `UserBubble`; an agent response is plain
  // full-width markdown. `bg-blue-50` is the bubble's distinguishing mark.
  const bubbleAround = (el: HTMLElement | null) => el?.closest('.bg-blue-50') ?? null

  it('renders a USER turn text block in a bubble', () => {
    render(
      <Turn
        turn={{ id: 't', role: 'user', blocks: [{ kind: 'text', id: 'b', text: 'are we done?' }] }}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
      />,
    )
    expect(bubbleAround(screen.getByText('are we done?'))).not.toBeNull()
  })

  it('renders an AGENT turn text block as markdown with no bubble', () => {
    render(
      <Turn
        turn={makeTurn([{ kind: 'text', id: 'b', text: 'Not yet.' }])}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
      />,
    )
    const text = screen.getByText('Not yet.')
    expect(bubbleAround(text)).toBeNull()
    expect(text.closest('.markdown')).not.toBeNull()
  })

  it('keeps agent styling when the engine reuses ONE turn id for prompt and reply', () => {
    // The Codex shape end to end: `user`, `turnStart` and `textDelta` all carry the
    // same native turn id. Before the reducer's role-scoped lookup, the agent's text
    // was appended into the user's turn and drew as a UserBubble — indistinguishable
    // from something the human typed.
    const events = [
      { type: 'user', turnId: 'native-1', text: 'are we completely done?' },
      { type: 'turnStart', turnId: 'native-1' },
      { type: 'textDelta', turnId: 'native-1', text: 'Not yet — the fix is implemented.' },
    ]
    const state = events.reduce<TranscriptState>(
      (s, ev) => transcriptReducer(s, ev as Parameters<typeof transcriptReducer>[1]),
      initialTranscript,
    )

    render(
      <>
        {state.turns.map((t) => (
          <Turn key={t.id} turn={t} isLastTurn={false} toolDetailMode="auto"
            thinkingCollapsed={true} streamingBlockId={null} onAnswer={noop} />
        ))}
      </>,
    )

    expect(bubbleAround(screen.getByText('are we completely done?'))).not.toBeNull()
    expect(bubbleAround(screen.getByText('Not yet — the fix is implemented.'))).toBeNull()
    // Exactly one bubble on screen — the prompt.
    expect(document.querySelectorAll('.bg-blue-50')).toHaveLength(1)
  })
})

describe('Turn question rendering', () => {
  const baseQ = {
    kind: 'question' as const,
    id: 'q1',
    questionId: 'q1',
    header: 'Layout',
    question: 'Two-pane?',
    options: [{ label: 'Yes' }],
    multiSelect: false,
  }

  it('does not render a LIVE question inline (it is pinned elsewhere)', () => {
    const { container } = render(
      <Turn
        turn={makeTurn([{ ...baseQ, live: true }])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByText('Two-pane?')).toBeNull()
    // No stray empty wrapper div either
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a historical (non-live) question inline, read-only', () => {
    render(
      <Turn
        turn={makeTurn([{ ...baseQ, live: false }])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={vi.fn()}
      />,
    )
    expect(screen.getByText('Two-pane?')).toBeInTheDocument()
    // Read-only: no interactive radio inputs
    expect(screen.queryByRole('radio')).toBeNull()
  })
})
