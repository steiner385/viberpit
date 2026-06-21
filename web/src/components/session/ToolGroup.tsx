import { useId, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ToolBlock, ToolDetailMode } from '@/session/groupBlocks'
import { ToolCallCard } from './ToolCallCard'

interface ToolGroupProps {
  tools: ToolBlock[]
  /** True when this group is the newest activity (last group of the last turn). */
  isLiveTail: boolean
  toolDetailMode: ToolDetailMode
  /** Minimum completed tools before an auto-fold summary appears. Default 4. */
  threshold?: number
}

export function ToolGroup({ tools, isLiveTail, toolDetailMode, threshold = 4 }: ToolGroupProps) {
  const regionId = useId()
  const [userExpanded, setUserExpanded] = useState(false)
  // Reset the per-group override when the global mode changes so the global
  // expand/collapse-all toggle produces a uniform state (during-render reset).
  const [prevMode, setPrevMode] = useState(toolDetailMode)
  if (prevMode !== toolDetailMode) {
    setPrevMode(toolDetailMode)
    setUserExpanded(false)
  }

  const allComplete = tools.every((t) => t.result !== undefined)
  const autoFold = allComplete && !isLiveTail && tools.length >= threshold

  let folded: boolean
  if (toolDetailMode === 'all-open') folded = false
  else if (toolDetailMode === 'all-closed') folded = !userExpanded
  else folded = autoFold && !userExpanded

  // Show the fold/collapse header whenever folding applies. Never in all-open.
  const showHeader = toolDetailMode !== 'all-open' && (toolDetailMode === 'all-closed' || autoFold)

  const anyFailed = tools.some((t) => t.result && !t.result.ok)
  const glyph = anyFailed ? '✕' : '✓'
  const names = [...new Set(tools.map((t) => t.name))]
  const nameList = names.slice(0, 3).join(', ') + (names.length > 3 ? '…' : '')
  const label = `${tools.length} tool ${tools.length === 1 ? 'call' : 'calls'} — ${nameList}`

  return (
    <div className="my-1">
      {showHeader && (
        <button
          type="button"
          aria-label="Toggle tool group"
          aria-expanded={!folded}
          aria-controls={regionId}
          onClick={() => setUserExpanded((v) => !v)}
          className={cn(
            'flex w-full items-center gap-2 px-3 py-0.5 text-left text-sm',
            'min-h-[44px] rounded hover:bg-gray-50 dark:hover:bg-gray-900/40',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            anyFailed ? 'text-red-700 dark:text-red-400' : 'text-gray-600 dark:text-gray-300',
          )}
        >
          <span className="shrink-0 font-bold">{glyph}</span>
          <span className="truncate">{label}</span>
          <span className="ml-auto shrink-0 text-gray-400 dark:text-gray-500">
            {folded ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </span>
        </button>
      )}

      <div id={regionId} hidden={folded}>
        {tools.map((t, i) => (
          <ToolCallCard
            key={t.id}
            block={t}
            density="line"
            defaultResultOpen={isLiveTail && i === tools.length - 1}
          />
        ))}
      </div>
    </div>
  )
}
