// SPIKE harness entry — mounts VirtualTranscriptView with synthetic data and
// controls to stress dynamic heights, streaming growth, and stick-to-bottom.
// Served via /spike.html in `vite dev`. Not part of the production app.
import { StrictMode, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { Turn as TurnType } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { VirtualTranscriptView } from '@/components/session/VirtualTranscriptView'
import { makeTranscript, makeAssistantTurn } from './spikeData'
import '../index.css'

const noop = () => {}
const INITIAL = 500

function Harness() {
  const [turns, setTurns] = useState<TurnType[]>(() => makeTranscript(INITIAL))
  const [toolDetailMode] = useState<ToolDetailMode>('auto')
  const [streaming, setStreaming] = useState(false)
  const [streamingBlockId, setStreamingBlockId] = useState<string | null>(null)
  const [renderedCount, setRenderedCount] = useState(0)
  const nextN = useRef(INITIAL)

  // Live readout of how many rows are actually in the DOM (virtualization proof).
  useEffect(() => {
    const id = setInterval(() => {
      setRenderedCount(document.querySelectorAll('[data-index]').length)
    }, 250)
    return () => clearInterval(id)
  }, [])

  // Streaming simulation: append words to the last assistant turn's open text
  // block; periodically start a new assistant turn.
  useEffect(() => {
    if (!streaming) {
      setStreamingBlockId(null)
      return
    }
    let ticks = 0
    const id = setInterval(() => {
      ticks++
      setTurns((prev) => {
        const next = [...prev]
        let last = next[next.length - 1]
        // Every ~20 ticks, open a fresh assistant turn to grow the list.
        if (ticks % 20 === 0 || last.role === 'user') {
          last = { id: `t-${nextN.current}`, role: 'assistant', blocks: [{ kind: 'text', id: `t-${nextN.current}-s`, text: 'Streaming' }] }
          nextN.current++
          next.push(last)
        } else {
          const blocks = [...last.blocks]
          const bi = blocks.length - 1
          const b = blocks[bi]
          if (b.kind === 'text') blocks[bi] = { ...b, text: b.text + ' word' }
          next[next.length - 1] = { ...last, blocks }
        }
        const lt = next[next.length - 1]
        const openText = [...lt.blocks].reverse().find((b) => b.kind === 'text')
        setStreamingBlockId(openText ? openText.id : null)
        return next
      })
    }, 120)
    return () => clearInterval(id)
  }, [streaming])

  return (
    <div className="flex h-screen flex-col bg-white text-gray-900 dark:bg-gray-950 dark:text-gray-100">
      <div className="flex items-center gap-3 border-b border-gray-200 px-4 py-2 text-sm dark:border-gray-800">
        <strong>Virtualization spike</strong>
        <span data-testid="total">turns: {turns.length}</span>
        <span data-testid="rendered" className="rounded bg-blue-100 px-2 py-0.5 dark:bg-blue-900/40">
          in DOM: {renderedCount}
        </span>
        <button className="rounded border px-2 py-0.5" onClick={() => { setTurns((p) => [...p, makeAssistantTurn(nextN.current++)]) }}>
          + turn
        </button>
        <button className="rounded border px-2 py-0.5" onClick={() => setStreaming((s) => !s)} data-testid="stream-toggle">
          {streaming ? '⏸ stop stream' : '▶ stream'}
        </button>
        <button className="rounded border px-2 py-0.5" onClick={() => setTurns(makeTranscript(2000))}>
          load 2000
        </button>
      </div>
      <VirtualTranscriptView
        turns={turns}
        permissionMode="interactive"
        toolDetailMode={toolDetailMode}
        streamingBlockId={streamingBlockId}
        onDecision={noop}
        onAnswer={noop}
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
)
