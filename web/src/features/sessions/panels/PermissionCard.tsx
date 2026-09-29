import type { Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from '@/features/sessions/useCardKeyboard'
import { toolLabel, toolDetail } from '@/session/toolSummary'

type PermBlock = Extract<Block, { kind: 'permission' }>
type Decision = 'allow' | 'deny' | 'always'

interface Props {
  block: PermBlock
  /** The session permission mode (e.g. 'interactive', 'auto', 'plan'). */
  mode?: string
  onDecision: (permId: string, decision: Decision) => void
}

// Weighting is the message (2026-09-29 critique, P0): the ONE-TIME grant is the
// filled primary, the refusal is the outlined secondary, and the STANDING grant
// ("Always allow") is a quiet text-weight tertiary — it used to wear the heaviest
// button on the card, nudging the highest-stakes tap toward the broadest grant.
const primaryBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm font-semibold',
  'bg-accent text-accent-ink hover:bg-accent/90',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const allowBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm font-semibold',
  'bg-success-strong text-on-strong hover:bg-success-strong/90',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const secondaryBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm',
  'bg-surface border border-edge-strong',
  'text-ink hover:bg-surface-raised',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const tertiaryBtn = cn(
  'min-h-target rounded px-3 py-2 text-sm',
  'text-ink-muted underline-offset-4 hover:underline hover:text-ink',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)

export function PermissionCard({ block, mode, onDecision }: Props) {
  const label = toolLabel(block.tool, block.toolKind, block.input)
  // The whole request, not the 60-char gist: a command the operator cannot read
  // is a command they approve blind.
  const detail = toolDetail(block.toolKind, block.input)
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
      aria-label={`Permission request: ${label}`}
      className="rounded border border-warning bg-warning-wash p-3 my-1"
    >
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="font-medium text-ink">
          {block.plan ? 'Plan step approval' : 'Permission request'}
        </span>
        <span className="min-w-0 break-all font-mono text-xs text-ink-muted">{block.tool}</span>
      </div>

      {detail && (
        <pre
          data-testid="permission-detail"
          // Focusable so a keyboard user can scroll a long request; the label
          // says what the region is, not just that it is a block of monospace.
          tabIndex={0}
          aria-label="Full request"
          className={cn(
            'mb-3 max-h-48 overflow-y-auto overscroll-contain rounded border border-warning/40 bg-surface',
            'px-2.5 py-2 font-mono text-xs leading-relaxed text-ink',
            'whitespace-pre-wrap break-all',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          )}
        >
          {detail}
        </pre>
      )}

      {block.items && block.items.length > 0 && (
        <ul className="mb-3 list-disc pl-5 text-xs text-ink space-y-0.5">
          {block.items.map((item, i) => (
            <li key={i} className="break-words">{item}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-col sm:flex-row sm:items-center gap-2" {...kb.containerProps}>
        {block.plan ? (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Approve & run ${label}`} className={primaryBtn}>
              Approve & run
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Keep planning ${label}`} className={secondaryBtn}>
              Keep planning
            </button>
          </>
        ) : (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Allow ${label}`} className={allowBtn}>
              Allow
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Deny ${label}`} className={secondaryBtn}>
              Deny
            </button>
            {!isInteractive && canAlwaysAllow && (
              <button type="button" {...kb.itemProps(2)} onClick={() => onDecision(block.permId, 'always')}
                aria-label={`Always allow ${label}`} className={cn(tertiaryBtn, 'sm:ml-auto')}
                title="Grant this for the rest of the session">
                Always allow
              </button>
            )}
          </>
        )}
      </div>
    </div>
  )
}
