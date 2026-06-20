import { useEffect, useRef } from 'react'
import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'

type PermBlock = Extract<Block, { kind: 'permission' }>

interface Props {
  block: PermBlock
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
}

function keyArgSummary(block: PermBlock): string {
  const { toolKind, input } = block
  if (toolKind === 'bash' && typeof input.command === 'string') return input.command.slice(0, 60)
  if ((toolKind === 'edit' || toolKind === 'write' || toolKind === 'read') && typeof input.file_path === 'string') return input.file_path
  return ''
}

export function PermissionCard({ block, onDecision }: Props) {
  const primaryRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    primaryRef.current?.focus()
  }, [])

  const summary = keyArgSummary(block)
  const toolLabel = summary ? `${block.tool}: ${summary}` : block.tool

  return (
    <div
      role="group"
      aria-label={`Permission request: ${toolLabel}`}
      className="rounded border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/30 p-3 my-1"
    >
      <div className="mb-2 text-sm font-medium text-gray-900 dark:text-gray-100">
        {block.plan ? 'Plan step approval' : 'Permission request'}
      </div>
      <div className="mb-3 text-xs text-gray-700 dark:text-gray-300 font-mono truncate">
        {toolLabel}
      </div>

      <div className="flex flex-col sm:flex-row gap-2">
        {block.plan ? (
          <>
            <button
              ref={primaryRef}
              type="button"
              onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Approve & run ${toolLabel}`}
              className={cn(
                'min-h-[48px] rounded px-4 py-2 text-sm font-semibold',
                'bg-blue-600 text-white hover:bg-blue-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Approve & run
            </button>
            <button
              type="button"
              onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Keep planning ${toolLabel}`}
              className={cn(
                'min-h-[48px] rounded px-4 py-2 text-sm',
                'bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600',
                'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Keep planning
            </button>
          </>
        ) : (
          <>
            <button
              ref={primaryRef}
              type="button"
              onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Allow ${toolLabel}`}
              className={cn(
                'min-h-[48px] rounded px-4 py-2 text-sm font-medium',
                'bg-green-600 text-white hover:bg-green-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Yes
            </button>
            <button
              type="button"
              onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Deny ${toolLabel}`}
              className={cn(
                'min-h-[48px] rounded px-4 py-2 text-sm',
                'bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600',
                'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              No
            </button>
            <button
              type="button"
              onClick={() => onDecision(block.permId, 'always')}
              aria-label={`Always allow ${toolLabel}`}
              className={cn(
                'min-h-[48px] rounded px-4 py-2 text-sm font-semibold',
                'bg-blue-600 text-white hover:bg-blue-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Yes, always
            </button>
          </>
        )}
      </div>
    </div>
  )
}
