import { beforeAll, describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const feedbackMutate = vi.fn()
vi.mock('@/api/assistant', () => ({
  useAssistantFeedback: vi.fn(() => ({ mutate: feedbackMutate })),
}))
vi.mock('@/features/sessions/SessionRuntimeProvider', () => ({
  useSessionRuntimeContext: vi.fn(() => ({ sessionId: 'sess-1' })),
}))

import { Turn } from './Turn'
import { transcriptReducer, initialTranscript } from '@/session/reducer'
import type { Turn as TurnType, Block, TranscriptState } from '@/session/reducer'
import { prefetchMarkdown } from './markdownChunk'

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


// The markdown renderer is loaded lazily (#1411). In the browser that is a chunk
// fetch; in vitest it is a module TRANSFORM of react-markdown + highlight.js +
// KaTeX, which under a loaded worker can take longer than findBy's default
// timeout. Warming it once here means these tests assert on rendering, not on
// how busy the machine is — the same call the transcript makes on mount.
beforeAll(() => prefetchMarkdown())

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

  it('renders the open assistant block as Markdown while it is streaming', async () => {
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
    expect(await screen.findByText('bold')).toBeInTheDocument()
    expect(document.querySelector('strong')).not.toBeNull()
  })

  it('renders a finalized (non-streaming) assistant block as markdown', async () => {
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
    await screen.findByText('bold')
    expect(document.querySelector('strong')).not.toBeNull()
  })
})

describe('Turn user-vs-agent styling', () => {
  // The one visual contract the transcript makes (rich-session-rendering design):
  // a human prompt is a right-aligned `UserBubble`; an agent response is plain
  // full-width markdown. `bg-accent-wash` is the bubble's distinguishing mark.
  const bubbleAround = (el: HTMLElement | null) => el?.closest('.bg-accent-wash') ?? null

  it('renders a USER turn text block in a bubble', () => {
    render(
      <Turn
        turn={{ id: 't', role: 'user', blocks: [{ kind: 'text', id: 'b', text: 'are we done?' }] }}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
      />,
    )
    expect(bubbleAround(screen.getByText('are we done?'))).not.toBeNull()
  })

  // `<Markdown>` defers its renderer (#1411): the `.markdown` wrapper only exists
  // once the chunk lands, so this awaits the rendered form.
  it('renders an AGENT turn text block as markdown with no bubble', async () => {
    render(
      <Turn
        turn={makeTurn([{ kind: 'text', id: 'b', text: 'Not yet.' }])}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
      />,
    )
    const text = await screen.findByText('Not yet.')
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
    expect(document.querySelectorAll('.bg-accent-wash')).toHaveLength(1)
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

describe('Turn feedback (assistant drawer only)', () => {
  it('renders TurnFeedback for an assistant-role turn in the assistant variant', () => {
    render(
      <Turn
        turn={makeTurn([{ kind: 'text', id: 'b', text: 'Here you go.' }])}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
        variant="assistant"
      />,
    )
    expect(screen.getByRole('button', { name: 'Helpful' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Not helpful' })).toBeInTheDocument()
  })

  it('does not render TurnFeedback for a USER turn even in the assistant variant', () => {
    render(
      <Turn
        turn={{ id: 't', role: 'user', blocks: [{ kind: 'text', id: 'b', text: 'hi' }] }}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
        variant="assistant"
      />,
    )
    expect(screen.queryByRole('button', { name: 'Helpful' })).toBeNull()
  })

  it('does not render TurnFeedback outside the assistant variant', () => {
    render(
      <Turn
        turn={makeTurn([{ kind: 'text', id: 'b', text: 'Here you go.' }])}
        isLastTurn toolDetailMode="auto" thinkingCollapsed={true} onAnswer={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Helpful' })).toBeNull()
  })
})

describe('Turn share_artifact rendering', () => {
  const artifact: Block = {
    kind: 'tool', id: 's', toolId: 's', name: 'mcp__viberpit__share_artifact', toolKind: 'app_action',
    input: { kind: 'url', value: 'https://g.h/o/n/pull/7', label: 'PR #7' },
    result: { ok: true, payload: { content: [{ type: 'text', text: '{"data":{}}' }] } },
  }

  it('renders a shared artifact as an always-visible card, never a collapsed tool row', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), artifact, tool('b')])}
        isLastTurn={false}
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.getByRole('link', { name: 'https://g.h/o/n/pull/7' })).toHaveAttribute('href', 'https://g.h/o/n/pull/7')
    expect(screen.getByText('PR #7')).toBeInTheDocument()
    // Only the two Reads are collapsible tool rows; the artifact has no details toggle.
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(2)
  })
})

describe('Turn under a transcript view mode', () => {
  const thinking = (id: string, text: string): Block => ({ kind: 'thinking', id, text })
  const toggles = () => screen.getAllByRole('button', { name: /^thinking$/i })

  it('a thinking block appended while streaming under Thinking renders open', () => {
    const first = makeTurn([thinking('th-1', 'first')])
    const { rerender } = render(
      <Turn turn={first} isLastTurn toolDetailMode="auto" thinkingCollapsed={false} viewModeEpoch={1} onAnswer={noop} />,
    )
    const grown = makeTurn([thinking('th-1', 'first'), thinking('th-2', 'second')])
    rerender(
      <Turn turn={grown} isLastTurn toolDetailMode="auto" thinkingCollapsed={false} viewModeEpoch={1}
        streamingBlockId="th-2" onAnswer={noop} />,
    )
    expect(toggles()).toHaveLength(2)
    for (const t of toggles()) expect(t).toHaveAttribute('aria-expanded', 'true')
  })

  it('a new epoch re-applies the mode default to a block the user toggled', async () => {
    const user = userEvent.setup()
    const turn = makeTurn([thinking('th-1', 'first')])
    const { rerender } = render(
      <Turn turn={turn} isLastTurn toolDetailMode="auto" thinkingCollapsed={false} viewModeEpoch={1} onAnswer={noop} />,
    )
    await user.click(toggles()[0])
    expect(toggles()[0]).toHaveAttribute('aria-expanded', 'false')
    rerender(
      <Turn turn={turn} isLastTurn toolDetailMode="all-open" thinkingCollapsed={false} viewModeEpoch={2} onAnswer={noop} />,
    )
    expect(toggles()[0]).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('Turn message actions', () => {
  it('renders one Message actions trigger per turn', () => {
    render(
      <Turn
        turn={makeTurn([
          { kind: 'text', id: 'a', text: 'one' }, tool('t1'), { kind: 'text', id: 'b', text: 'two' },
        ])}
        isLastTurn={false} toolDetailMode="auto" thinkingCollapsed onAnswer={noop}
      />,
    )
    expect(screen.getAllByRole('button', { name: /^Message actions/ })).toHaveLength(1)
  })

  it('names the trigger by role: "your prompt" for a user turn, "reply" for an assistant turn', () => {
    const { unmount } = render(
      <Turn turn={{ id: 'u', role: 'user', blocks: [{ kind: 'text', id: 'a', text: 'hi' }] }}
        isLastTurn={false} toolDetailMode="auto" thinkingCollapsed onAnswer={noop} />,
    )
    expect(screen.getByRole('button', { name: /^Message actions/ })).toHaveAccessibleName('Message actions — your prompt')
    unmount()
    render(
      <Turn turn={makeTurn([{ kind: 'text', id: 'a', text: 'hi' }])}
        isLastTurn={false} toolDetailMode="auto" thinkingCollapsed onAnswer={noop} />,
    )
    expect(screen.getByRole('button', { name: /^Message actions/ })).toHaveAccessibleName('Message actions — reply')
  })

  it('pins the trigger to the turn root\'s top-right corner, with no layout wrapper', () => {
    const { container } = render(
      <Turn turn={makeTurn([{ kind: 'text', id: 'a', text: 'hi' }])}
        isLastTurn={false} toolDetailMode="auto" thinkingCollapsed onAnswer={noop} />,
    )
    const root = container.firstElementChild as HTMLElement
    const trigger = screen.getByRole('button', { name: /^Message actions/ })
    expect(root).toHaveClass('relative')
    expect(trigger.parentElement).toBe(root)
    expect(trigger).toHaveClass('absolute', 'right-0', 'top-0', 'opacity-0', 'pointer-coarse:opacity-100')
  })

  it('reveals only on its OWN turn\'s hover/focus — a nested sub-agent turn does not leak', () => {
    // Tailwind's `group-hover/<name>` matches ANY hovered ancestor group, which an
    // outer turn always is for a nested one. The reveal is a direct-child selector
    // instead, so each trigger must be its own turn root's child and carry no
    // ancestor-group variant. (jsdom cannot evaluate :hover, so this asserts the
    // structure the selector depends on.)
    const task: Block = {
      kind: 'tool', id: 'task1', toolId: 'task1', name: 'Task', toolKind: 'unknown',
      input: { subagent_type: 'Explore', description: 'x' }, result: { ok: true, payload: { content: 'done' } },
    }
    render(
      <Turn turn={makeTurn([{ kind: 'text', id: 'a', text: 'outer' }, task])}
        isLastTurn={false} toolDetailMode="all-open" thinkingCollapsed onAnswer={noop}
        subAgents={{ task1: { openBlockId: null, turns: [
          { id: 'sa1', role: 'assistant', blocks: [{ kind: 'text', id: 'sa1:t', text: 'inner' }] },
        ] } }}
      />,
    )
    const triggers = screen.getAllByRole('button', { name: /^Message actions/, hidden: true })
    expect(triggers).toHaveLength(2)
    for (const t of triggers) {
      expect(t.parentElement).toHaveClass('relative', 'px-4', 'py-2')
      expect(t).toHaveClass('[:hover>&]:opacity-100', '[:focus-within>&]:opacity-100')
      expect(t.className).not.toMatch(/group-(hover|focus)/)
    }
    expect(triggers[0].parentElement).not.toBe(triggers[1].parentElement)
  })
})
