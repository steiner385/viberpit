import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from './useCardKeyboard'
import { toolSummary } from '@/session/toolSummary'

type PermBlock = Extract<Block, { kind: 'permission' }>
type Decision = 'allow' | 'deny' | 'always'

interface Props {
  block: PermBlock
  /** The session permission mode (e.g. 'interactive', 'auto', 'plan'). */
  mode?: string
  onDecision: (permId: string, decision: Decision) => void
}

const primaryBtn = cn(
  'min-h-[48px] rounded px-4 py-2 text-sm font-semibold',
  'bg-blue-600 text-white hover:bg-blue-700',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)
const allowBtn = cn(
  'min-h-[48px] rounded px-4 py-2 text-sm font-medium',
  'bg-green-600 text-white hover:bg-green-700',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)
const secondaryBtn = cn(
  'min-h-[48px] rounded px-4 py-2 text-sm',
  'bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600',
  'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
)

export function PermissionCard({ block, mode, onDecision }: Props) {
  const summary = toolSummary(block.toolKind, block.input)
  const toolLabel = summary ? `${block.tool}: ${summary}` : block.tool
  const isInteractive = mode === 'interactive'
  // Destructive app-action tools (spec §4.4) never offer "Always allow", in
  // every mode — the server marks this on the request rather than the client
  // inferring it from toolKind, since a MUTATION app_action IS grantable.
  const canAlwaysAllow = block.alwaysAllowed !== false

  // Ordered actions matching the rendered buttons; digit/arrow indices map here.
  const actions: Decision[] = block.plan
    ? ['allow', 'deny']
    : isInteractive || !canAlwaysAllow
      ? ['allow', 'deny']
      : ['allow', 'deny', 'always']

  const kb = useCardKeyboard({
    count: actions.length,
    onActivate: (i) => onDecision(block.permId, actions[i]),
  })

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

      <div className="flex flex-col sm:flex-row gap-2" {...kb.containerProps}>
        {block.plan ? (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Approve & run ${toolLabel}`} className={primaryBtn}>
              Approve & run
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Keep planning ${toolLabel}`} className={secondaryBtn}>
              Keep planning
            </button>
          </>
        ) : (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Allow ${toolLabel}`} className={allowBtn}>
              Yes
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Deny ${toolLabel}`} className={secondaryBtn}>
              No
            </button>
            {!isInteractive && canAlwaysAllow && (
              <button type="button" {...kb.itemProps(2)} onClick={() => onDecision(block.permId, 'always')}
                aria-label={`Always allow ${toolLabel}`} className={primaryBtn}>
                Yes, always
              </button>
            )}
          </>
        )}
      </div>
    </div>
  )
}
