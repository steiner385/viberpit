import { useState } from 'react'
import type { Block } from '@/session/reducer'
import type { RulePreview, RuleScope } from '@/api/types'
import { AlwaysAllowDialog } from './AlwaysAllowDialog'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from '@/features/sessions/useCardKeyboard'
import { toolLabel, toolDetail } from '@/session/toolSummary'

type PermBlock = Extract<Block, { kind: 'permission' }>
type Decision = 'allow' | 'deny' | 'always'

interface Props {
  block: PermBlock
  onDecision: (permId: string, decision: Decision) => void
  /** Standing-rule wiring. All three present → "Always allow…" opens the chooser; otherwise
   *  (sub-agent page, assistant drawer) it falls back to the direct session-wide decision. */
  hasRepo?: boolean
  onPreviewRule?: (permId: string, pattern: string, scope: RuleScope) => Promise<RulePreview>
  onGrantRule?: (permId: string, pattern: string, scope: RuleScope, confirm: boolean) => Promise<unknown>
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

export function PermissionCard({ block, onDecision, hasRepo, onPreviewRule, onGrantRule }: Props) {
  const [chooserOpen, setChooserOpen] = useState(false)
  const label = toolLabel(block.tool, block.toolKind, block.input)
  // The whole request, not the 60-char gist: a command the operator cannot read
  // is a command they approve blind.
  const detail = toolDetail(block.toolKind, block.input)
  // Destructive app-action tools (spec §4.4) never offer "Always allow", in
  // every mode — the server marks this on the request rather than the client
  // inferring it from toolKind, since a MUTATION app_action IS grantable.
  const canAlwaysAllow = block.alwaysAllowed !== false && !block.plan
  // Server-proposed standing rules → the chooser. A grantable card with none is
  // engine-managed (Codex): its session-wide accept stays a direct decision.
  const hasSuggestions = canAlwaysAllow && (block.ruleSuggestions?.length ?? 0) > 0
  const rules = hasSuggestions && onPreviewRule && onGrantRule && hasRepo !== undefined
    ? { hasRepo, onPreviewRule, onGrantRule }
    : null

  // Ordered actions matching the rendered buttons; digit/arrow indices map here.
  const actions: Decision[] = canAlwaysAllow ? ['allow', 'deny', 'always'] : ['allow', 'deny']

  const activate = (d: Decision) => {
    if (d === 'always' && rules) setChooserOpen(true)
    else onDecision(block.permId, d)
  }

  const kb = useCardKeyboard({
    count: actions.length,
    onActivate: (i) => activate(actions[i]),
  })

  // `A` opens the chooser from anywhere inside the card (not from a text field).
  const onCardKeyDown = (e: React.KeyboardEvent) => {
    if (!rules || e.metaKey || e.ctrlKey || e.altKey) return
    const tag = (e.target as HTMLElement).tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA') return
    if (e.key === 'a' || e.key === 'A') {
      e.preventDefault(); e.stopPropagation()
      setChooserOpen(true)
    }
  }

  return (
    <>
    <div
      role="group"
      aria-label={`Permission request: ${label}`}
      className="rounded border border-warning bg-warning-wash p-3 my-1"
      onKeyDown={onCardKeyDown}
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
              Allow once
            </button>
            <button type="button" {...kb.itemProps(1)} onClick={() => onDecision(block.permId, 'deny')}
              aria-label={`Deny ${label}`} className={secondaryBtn}>
              Deny
            </button>
            {canAlwaysAllow && (
              <button type="button" {...kb.itemProps(2)} onClick={() => activate('always')}
                aria-label={`Always allow ${label}`} className={cn(tertiaryBtn, 'sm:ml-auto')}
                title={hasSuggestions ? 'Choose a standing rule' : 'Grant this for the rest of the session'}>
                {hasSuggestions ? 'Always allow…' : 'Always allow (this session)'}
              </button>
            )}
          </>
        )}
      </div>
    </div>
    {rules && (
      // Outside the card div: a portaled dialog's key events bubble through the React tree,
      // and must not reach the card's digit/A shortcuts.
      <AlwaysAllowDialog
        open={chooserOpen}
        block={block}
        hasRepo={rules.hasRepo}
        onPreview={(pattern, scope) => rules.onPreviewRule(block.permId, pattern, scope)}
        onGrant={(pattern, scope, confirm) => rules.onGrantRule(block.permId, pattern, scope, confirm)}
        onClose={() => setChooserOpen(false)}
      />
    )}
    </>
  )
}
