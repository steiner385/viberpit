import { useState, useId } from 'react'
import { ChevronDown, ChevronRight, FileEdit, FileText, Terminal, List, Search, Wrench } from 'lucide-react'
import type { Block } from '@/session/reducer'
import type { ToolKind } from '@/session/events'
import { ToolResult } from './ToolResult'
import { cn } from '@/lib/utils'

type ToolBlock = Extract<Block, { kind: 'tool' }>

interface Props {
  block: ToolBlock
  /** Compact single-line row ('line') or full bordered card ('card'). Default 'card'. */
  density?: 'line' | 'card'
  /** Auto-expand the result/detail panel (use for the most recent tool call). Default false. */
  defaultResultOpen?: boolean
}

function kindIcon(kind: ToolKind) {
  switch (kind) {
    case 'edit':   return <FileEdit size={14} />
    case 'write':  return <FileEdit size={14} />
    case 'read':   return <FileText size={14} />
    case 'bash':   return <Terminal size={14} />
    case 'todo':   return <List size={14} />
    case 'search': return <Search size={14} />
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

/** Derive a size hint string from the result payload (e.g. "42 lines", "+3 −2"). */
function sizeHint(kind: ToolKind, payload: Record<string, unknown>): string {
  if (kind === 'edit' || kind === 'write') {
    // Count added and removed lines from diff payload
    const oldLines =
      typeof payload.old_string === 'string' ? payload.old_string.split('\n').length : 0
    const newLines =
      typeof payload.new_string === 'string'
        ? payload.new_string.split('\n').length
        : typeof payload.content === 'string'
          ? payload.content.split('\n').length
          : 0
    if (oldLines === 0 && newLines > 0) return `+${newLines}`
    if (newLines === 0 && oldLines > 0) return `−${oldLines}`
    return `+${newLines} −${oldLines}`
  }

  // For bash and read: count lines in the text content
  let text: string | null = null
  if (typeof payload.content === 'string') {
    text = payload.content
  } else if (typeof payload.stdout === 'string' || typeof payload.stderr === 'string') {
    const parts: string[] = []
    if (typeof payload.stdout === 'string' && payload.stdout) parts.push(payload.stdout)
    if (typeof payload.stderr === 'string' && payload.stderr) parts.push(payload.stderr)
    text = parts.join('\n')
  }
  if (text !== null) {
    const count = text === '' ? 0 : text.split('\n').length
    return `${count} ${count === 1 ? 'line' : 'lines'}`
  }

  return ''
}

export function ToolCallCard({ block, density = 'card', defaultResultOpen = false }: Props) {
  const [argsOpen, setArgsOpen] = useState(false)
  const [resultOpen, setResultOpen] = useState(defaultResultOpen)
  const argsRegionId = useId()
  const resultRegionId = useId()
  const detailRegionId = useId()

  const summary = keyArg(block.toolKind, block.input)

  if (density === 'line') {
    const ok = block.result?.ok ?? true
    const hint = block.result ? sizeHint(block.toolKind, block.result.payload) : ''
    return (
      <div className="text-sm">
        <button
          type="button"
          aria-label="Toggle tool details"
          aria-expanded={resultOpen}
          aria-controls={detailRegionId}
          onClick={() => setResultOpen((v) => !v)}
          className={cn(
            'flex w-full items-center gap-2 px-3 py-0.5 text-left',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px] hover:bg-gray-50 dark:hover:bg-gray-900/40 rounded',
          )}
        >
          <span className="shrink-0 text-gray-500 dark:text-gray-400">{kindIcon(block.toolKind)}</span>
          <span className="shrink-0 font-medium text-gray-900 dark:text-gray-100">{block.name}</span>
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
          <pre className="mb-2 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
            {JSON.stringify(block.input, null, 2)}
          </pre>
          {block.result && (
            <ToolResult kind={block.toolKind} ok={block.result.ok} payload={block.result.payload} />
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
        <pre className="overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
          {JSON.stringify(block.input, null, 2)}
        </pre>
      </div>

      {/* Result section */}
      {block.result && (() => {
        const hint = sizeHint(block.toolKind, block.result.payload)
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
              <ToolResult kind={block.toolKind} ok={block.result.ok} payload={block.result.payload} />
            </div>
          </>
        )
      })()}
    </div>
  )
}
