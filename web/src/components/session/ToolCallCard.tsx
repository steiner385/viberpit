import { useState, useId } from 'react'
import { ChevronDown, ChevronRight, FileEdit, FileText, Terminal, List, Search, Wrench } from 'lucide-react'
import type { Block } from '@/session/reducer'
import type { ToolKind } from '@/session/events'
import { ToolResult } from './ToolResult'
import { cn } from '@/lib/utils'

type ToolBlock = Extract<Block, { kind: 'tool' }>

interface Props { block: ToolBlock }

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

export function ToolCallCard({ block }: Props) {
  const [open, setOpen] = useState(false)
  const regionId = useId()

  const summary = keyArg(block.toolKind, block.input)

  return (
    <div className="my-1 rounded border border-gray-200 dark:border-gray-700 text-sm">
      <div className="flex items-center gap-2 px-3 py-1">
        <span className="text-gray-500 dark:text-gray-400">{kindIcon(block.toolKind)}</span>
        <span className="font-medium text-gray-900 dark:text-gray-100">{block.name}</span>
        {summary && (
          <span className="truncate text-gray-500 dark:text-gray-400 text-xs">{summary}</span>
        )}
        <button
          type="button"
          aria-expanded={open}
          aria-controls={regionId}
          onClick={() => setOpen((v) => !v)}
          className={cn(
            'ml-auto flex items-center gap-1 text-gray-500 dark:text-gray-400',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px] px-2',
          )}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
      </div>

      <div id={regionId} hidden={!open} className="border-t border-gray-200 dark:border-gray-700 px-3 py-2">
        <pre className="overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
          {JSON.stringify(block.input, null, 2)}
        </pre>
      </div>

      {block.result && (
        <div className="border-t border-gray-200 dark:border-gray-700 px-3 py-2">
          <ToolResult kind={block.toolKind} ok={block.result.ok} payload={block.result.payload} />
        </div>
      )}
    </div>
  )
}
