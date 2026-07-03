import { useState, useId } from 'react'
import { ChevronDown, ChevronRight, FileEdit, FileText, Terminal, List, Search, Globe, Bot, Plug, Wrench } from 'lucide-react'
import type { Block, SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import type { ToolKind } from '@/session/events'
import { ToolResult } from './ToolResult'
import { StructuredView } from './StructuredView'
import { sizeHint } from '@/lib/toolContent'
import { cn } from '@/lib/utils'
import { Turn } from './Turn'
import { modelName } from '@/session/modelName'

type ToolBlock = Extract<Block, { kind: 'tool' }>

interface Props {
  block: ToolBlock
  /** Compact single-line row ('line') or full bordered card ('card'). Default 'card'. */
  density?: 'line' | 'card'
  /** Auto-expand the result/detail panel (use for the most recent tool call). Default false. */
  defaultResultOpen?: boolean
  /** Global detail override: 'all-open'/'all-closed' force every panel; 'auto' uses defaultResultOpen. */
  toolDetailMode?: ToolDetailMode
  /** Persisted default for whether thinking blocks start collapsed (threaded to nested sub-agent turns). */
  thinkingCollapsed?: boolean
  /** When this is a Task tool, the spawned sub-agent's own thread (rendered nested). */
  subThread?: SubThread
  /** The session's current model — used to badge a subagent running on a different model. */
  sessionModel?: string | null
}

function kindIcon(kind: ToolKind) {
  switch (kind) {
    case 'edit':   return <FileEdit size={14} />
    case 'write':  return <FileEdit size={14} />
    case 'read':   return <FileText size={14} />
    case 'bash':   return <Terminal size={14} />
    case 'todo':   return <List size={14} />
    case 'search': return <Search size={14} />
    case 'web':    return <Globe size={14} />
    case 'task':   return <Bot size={14} />
    case 'mcp':    return <Plug size={14} />
    default:       return <Wrench size={14} />
  }
}

function keyArg(kind: ToolKind, input: Record<string, unknown>): string {
  if (kind === 'bash' && typeof input.command === 'string') {
    return input.command.slice(0, 60)
  }
  if ((kind === 'edit' || kind === 'write' || kind === 'read') && typeof input.file_path === 'string') {
    return input.file_path
  }
  return ''
}

export function ToolCallCard({ block, density = 'card', defaultResultOpen = false, toolDetailMode, thinkingCollapsed = true, subThread, sessionModel }: Props) {
  const [argsOpen, setArgsOpen] = useState(false)
  // The global toggle force-overrides the per-row default; 'auto' (or no mode)
  // falls back to defaultResultOpen so only the live tail's latest tool opens.
  const modeResultOpen =
    toolDetailMode === 'all-open' ? true
    : toolDetailMode === 'all-closed' ? false
    : defaultResultOpen
  const [resultOpen, setResultOpen] = useState(modeResultOpen)
  // Re-sync to the mode default when the global toggle flips (during-render reset),
  // mirroring ToolGroup's old pattern so expand/collapse-all stays uniform.
  const [prevMode, setPrevMode] = useState(toolDetailMode)
  if (prevMode !== toolDetailMode) {
    setPrevMode(toolDetailMode)
    setResultOpen(modeResultOpen)
  }
  const [subOpen, setSubOpen] = useState(false)
  const argsRegionId = useId()
  const resultRegionId = useId()
  const detailRegionId = useId()
  const subRegionId = useId()

  const summary = keyArg(block.toolKind, block.input)
  // Completed tools dim to grey (Claude-Code style); a running tool stays prominent.
  const complete = block.result !== undefined

  if (density === 'line') {
    const ok = block.result?.ok ?? true
    const hint = block.result ? sizeHint(block.toolKind, block.input, block.result.payload) : ''
    return (
      <div className="text-sm">
        <button
          type="button"
          aria-label="Toggle tool details"
          aria-expanded={resultOpen}
          aria-controls={detailRegionId}
          onClick={() => setResultOpen((v) => !v)}
          className={cn(
            'flex w-full items-center gap-2 px-3 py-1 text-left leading-tight',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'hover:bg-gray-50 dark:hover:bg-gray-900/40 rounded',
          )}
        >
          <span className={cn('shrink-0', complete ? 'text-gray-400 dark:text-gray-500' : 'text-gray-600 dark:text-gray-300')}>{kindIcon(block.toolKind)}</span>
          <span className={cn('shrink-0 font-medium', complete ? 'text-gray-500 dark:text-gray-400' : 'text-gray-900 dark:text-gray-100')}>{block.name}</span>
          {summary && (
            <span className="truncate text-xs text-gray-500 dark:text-gray-400">{summary}</span>
          )}
          {block.result && (
            <span className={cn('shrink-0 font-bold', ok ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400')}>
              {ok ? '✓' : '✕'}
            </span>
          )}
          {hint && <span className="ml-auto shrink-0 text-xs opacity-60">{hint}</span>}
          <span className={cn('shrink-0 text-gray-400 dark:text-gray-500', hint ? 'ml-1' : 'ml-auto')}>
            {resultOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </span>
        </button>

        <div
          id={detailRegionId}
          hidden={!resultOpen}
          className="border-l-2 border-gray-200 dark:border-gray-700 ml-4 px-3 py-2"
        >
          {resultOpen && (
            <div className="mb-2">
              <StructuredView data={block.input} />
            </div>
          )}
          {resultOpen && block.name === 'Task' && subThread && subThread.turns.length > 0 && (
            <div className="mb-2 rounded bg-indigo-50/30 dark:bg-indigo-950/10 border-l-2 border-indigo-300 dark:border-indigo-700 pl-2">
              <div className="px-1 py-1 text-xs font-medium text-indigo-700 dark:text-indigo-300">
                Sub-agent activity
                <span className="ml-1 opacity-60">{subThread.turns.length} turn{subThread.turns.length === 1 ? '' : 's'}</span>
              </div>
              {subThread.turns.map((t) => (
                <Turn
                  key={t.id}
                  turn={t}
                  isLastTurn={false}
                  toolDetailMode={toolDetailMode ?? 'auto'}
                  thinkingCollapsed={thinkingCollapsed}
                  onAnswer={() => {}}
                />
              ))}
            </div>
          )}
          {block.result && (
            <ToolResult kind={block.toolKind} ok={block.result.ok} input={block.input} payload={block.result.payload} name={block.name} />
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="my-1 rounded border border-gray-200 dark:border-gray-700 text-sm">
      {/* Header row */}
      <div className="flex items-center gap-2 px-3 py-1">
        <span className="text-gray-500 dark:text-gray-400">{kindIcon(block.toolKind)}</span>
        <span className="font-medium text-gray-900 dark:text-gray-100">{block.name}</span>
        {summary && (
          <span className="truncate text-gray-500 dark:text-gray-400 text-xs">{summary}</span>
        )}
        {/* Args toggle */}
        <button
          type="button"
          aria-label="Toggle args"
          aria-expanded={argsOpen}
          aria-controls={argsRegionId}
          onClick={() => setArgsOpen((v) => !v)}
          className={cn(
            'ml-auto flex items-center gap-1 text-gray-500 dark:text-gray-400',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px] px-2',
          )}
        >
          {argsOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
      </div>

      {/* Args panel */}
      <div id={argsRegionId} hidden={!argsOpen} className="border-t border-gray-200 dark:border-gray-700 px-3 py-2">
        {argsOpen && <StructuredView data={block.input} />}
      </div>

      {/* Sub-agent thread (Task tool only) */}
      {block.name === 'Task' && subThread && subThread.turns.length > 0 && (
        <div className="border-t border-gray-200 dark:border-gray-700">
          <button
            type="button"
            aria-label="Sub-agent activity"
            aria-expanded={subOpen}
            aria-controls={subRegionId}
            onClick={() => setSubOpen((v) => !v)}
            className={cn(
              'flex w-full items-center gap-2 px-3 py-1 text-left min-h-[44px]',
              'text-indigo-700 dark:text-indigo-300',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            <span className="text-xs font-medium">Sub-agent activity</span>
            <span className="text-xs opacity-60">{subThread.turns.length} turn{subThread.turns.length === 1 ? '' : 's'}</span>
            {subThread.model && subThread.model !== sessionModel && (
              <span className="text-xs rounded bg-indigo-100 dark:bg-indigo-900/40 px-1.5 py-0.5 text-indigo-700 dark:text-indigo-300">
                {modelName(subThread.model)}
              </span>
            )}
            <span className="ml-auto shrink-0 text-gray-400 dark:text-gray-500">
              {subOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          </button>
          <div
            id={subRegionId}
            hidden={!subOpen}
            className="border-t border-gray-200 dark:border-gray-700 bg-indigo-50/30 dark:bg-indigo-950/10 pl-2"
          >
            {subThread.turns.map((t) => (
              <Turn
                key={t.id}
                turn={t}
                isLastTurn={false}
                toolDetailMode={toolDetailMode ?? 'auto'}
                thinkingCollapsed={thinkingCollapsed}
                onAnswer={() => {}}
              />
            ))}
          </div>
        </div>
      )}

      {/* Result section */}
      {block.result && (() => {
        const hint = sizeHint(block.toolKind, block.input, block.result.payload)
        const ok = block.result.ok
        return (
          <>
            {/* Result summary / toggle row */}
            <div className="border-t border-gray-200 dark:border-gray-700">
              <button
                type="button"
                aria-label={`Toggle result`}
                aria-expanded={resultOpen}
                aria-controls={resultRegionId}
                onClick={() => setResultOpen((v) => !v)}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-1 text-left',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                  'min-h-[44px]',
                  ok
                    ? 'text-green-700 dark:text-green-400'
                    : 'text-red-700 dark:text-red-400',
                )}
              >
                {/* Status glyph */}
                <span className="font-bold">{ok ? '✓' : '✕'}</span>
                {/* Tool name */}
                <span className="text-xs font-medium">{block.name}</span>
                {/* Key arg */}
                {summary && (
                  <span className="truncate text-xs opacity-75">· {summary}</span>
                )}
                {/* Size hint */}
                {hint && (
                  <span className="ml-auto shrink-0 text-xs opacity-60">{hint}</span>
                )}
                {/* Chevron */}
                <span className={cn('shrink-0 text-gray-400 dark:text-gray-500', hint ? 'ml-1' : 'ml-auto')}>
                  {resultOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </span>
              </button>
            </div>

            {/* Result panel */}
            <div id={resultRegionId} hidden={!resultOpen} className="border-t border-gray-200 dark:border-gray-700 px-3 py-2">
              <ToolResult kind={block.toolKind} ok={block.result.ok} input={block.input} payload={block.result.payload} name={block.name} />
            </div>
          </>
        )
      })()}
    </div>
  )
}
