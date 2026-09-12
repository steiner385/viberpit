import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from '@/features/sessions/useCardKeyboard'
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
  'min-h-target rounded px-4 py-2 text-sm font-semibold',
  'bg-accent text-accent-ink hover:bg-accent/90',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const allowBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm font-medium',
  'bg-success-strong text-on-strong hover:bg-success-strong/90',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const secondaryBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm',
  'bg-surface border border-edge-strong',
  'text-ink hover:bg-surface-raised',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
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
      className="rounded border border-warning bg-warning-wash p-3 my-1"
    >
      <div className="mb-2 text-sm font-medium text-ink">
        {block.plan ? 'Plan step approval' : 'Permission request'}
      </div>
      <div className="mb-3 text-xs text-ink font-mono truncate">
        {toolLabel}
      </div>

      {block.items && block.items.length > 0 && (
        <ul className="mb-3 list-disc pl-5 text-xs text-ink space-y-0.5">
          {block.items.map((item, i) => (
            <li key={i} className="truncate">{item}</li>
          ))}
        </ul>
      )}

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
